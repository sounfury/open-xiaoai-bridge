"""连续对话的"随音量起伏"灯效（和原生小爱聆听时一样）。

OH2P 固件的做法（2026-10-18 实测）：
- `vis` 从 /tmp/mic_audio.fifo（麦克风）或 /tmp/vis_audio.fifo（播放）读 48 kHz 16 位单声道音频，
  每 100 ms 读 9600 字节，算出音量（0～99），用 UDP 发给 127.0.0.1:7777 的 `ledd`。
- `ledd` 按当前灯效画：聆听灯（41）、播报灯（3）、音乐灯的白光宽度跟着音量变。
- 原生识别时 `mipns` 先 `ubus call mic_audio set_event '{"event":1,"value":1}'` 让 vis 改读麦克风，
  结束时 value=2 切回播放；播放音量由播放器自己写进 vis_audio.fifo（所以 miplayer 播报时灯本来就会动）。

我们的对话不走原生识别，没人往 mic_audio.fifo 写东西，聆听灯就是死的。这里补上：
- bridge 开一个 TCP 端口，让音箱 `nc <bridge> <port> > /tmp/mic_audio.fifo` 连进来（只在需要时启动一次，
  连接一直保持；bridge 退出后 nc 读到 EOF 自己结束）。
- 处于"听"的状态时，按麦克风每一包的音量生成同样响度的噪声（48 kHz）推过去：灯只需要响度，
  不把语音内容送出 bridge。
- 音量映射：按"比环境底噪高多少 dB"算（与麦克风增益无关）：高出 margin_db 开始变宽，
  高出 margin_db + span_db 满宽，线性映射到噪声 RMS [0, max_rms]。底噪持续跟踪：变安静时很快跟下来，
  变吵时每秒最多抬 0.5 dB（说话期间不会把自己当成底噪）。
- 实测（vis 输出 ≈ 噪声 RMS / 85）：噪声 RMS 1000（vis≈11）白光开始变宽，2000（vis≈23）接近全宽；
  8000 以上 vis 偶发溢出成 0（白光闪一下），所以 max_rms 默认 4000。
- vis 要求数据不能明显慢于实时，否则关掉重开 FIFO（nc 写入 EPIPE 被杀）；见 _FRAME_INTERVAL_S。
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import uuid

import numpy as np

from core.utils.logger import logger

LOG_MODULE = "LightLevel"
MIC_SOURCE_ON = "ubus -t 1 call mic_audio set_event '{\"event\":1,\"value\":1}'"
MIC_SOURCE_OFF = "ubus -t 1 call mic_audio set_event '{\"event\":1,\"value\":2}'"
_FIFO = "/tmp/mic_audio.fifo"
_PIDFILE = "/tmp/open-xiaoai-bridge-level.pid"  # 音箱上重连循环的 sh
_NC_PIDFILE = "/tmp/open-xiaoai-bridge-level-nc.pid"  # 当前的 nc
# vis 每 ~100 ms 读一帧 9600 字节（48 kHz）。实测：数据明显慢于实时（2/3）时它约 0.4 秒就关掉重开
# FIFO，nc 写入 EPIPE 退出；每 100/104/110 ms 一帧都稳定。比实时快又会积压成延迟（管道里的数据
# 按顺序读）。所以用自己的时钟每 110 ms 发一帧：略慢于 vis，积压会自己消化；不跟麦克风包走
# （WiFi 上一阵一阵地到）。内容是这段时间麦克风最大音量对应的噪声。
_FRAME_BYTES = 9600
_FRAME_INTERVAL_S = 0.110
_MAX_BUFFERED = _FRAME_BYTES * 2  # 发不出去时最多堆 2 帧，再多就丢：宁可少送也不要延迟
# 永远带一点点噪声（vis 算出来是 0），不发纯数字静音
_DITHER_RMS = 40.0


class MicLevelFeeder:
    """把麦克风音量送给音箱的 vis。单例，跟着 bridge 进程走。"""

    _instance: "MicLevelFeeder | None" = None

    def __init__(self, port: int, margin_db: float, span_db: float, max_rms: float, server_port: int = 4399):
        self.port = port
        self.margin_db = margin_db
        self.span_db = max(1.0, span_db)
        self.max_rms = max_rms
        self._noise_db: float | None = None  # 跟踪到的环境底噪
        self.server_port = server_port  # 音箱连 bridge 的端口，用来在音箱上查 bridge 的 IP
        self.id = f"light-level-{uuid.uuid4().hex[:8]}"  # GlobalStream 读者 id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self._registered = False
        self._active = False
        self._rng = np.random.default_rng()
        self._stats: list[float] = []  # 本段"听"期间每包的 dB（日志用）
        self._peak = 0.0  # 上次发帧之后麦克风的最大音量（0~1）
        self._sender: asyncio.Task | None = None
        self._connects = 0

    @classmethod
    def from_config(cls, cfg: dict | None) -> "MicLevelFeeder | None":
        """conversation.light_level 未开启时返回 None。"""
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        if cls._instance is None:
            try:
                cls._instance = cls(
                    port=int(cfg.get("port", 4400)),
                    margin_db=float(cfg.get("margin_db", 3)),
                    span_db=float(cfg.get("span_db", 18)),
                    max_rms=float(cfg.get("max_rms", 4000)),
                )
            except (TypeError, ValueError) as exc:
                logger.warning(f"Invalid conversation.light_level: {exc}", module=LOG_MODULE)
                return None
        else:
            inst = cls._instance
            inst.margin_db = float(cfg.get("margin_db", inst.margin_db))
            inst.span_db = max(1.0, float(cfg.get("span_db", inst.span_db)))
            inst.max_rms = float(cfg.get("max_rms", inst.max_rms))
        return cls._instance

    @property
    def ready(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ---- 连接 ----

    async def ensure_ready(self, speaker, timeout: float = 3.0) -> bool:
        """启动 TCP 服务，没连上时让音箱连过来。返回是否已连上。"""
        self._loop = asyncio.get_running_loop()
        if self._server is None:
            try:
                self._server = await asyncio.start_server(self._on_client, "0.0.0.0", self.port)
                logger.info(f"Level feed server listening on :{self.port}", module=LOG_MODULE)
            except OSError as exc:
                logger.warning(f"Level feed server failed on :{self.port}: {exc}", module=LOG_MODULE)
                return False
        if not self._registered:
            from core.services.audio.stream import GlobalStream

            GlobalStream.register_reader(self)
            self._registered = True
        if self._writer is not None and not self._writer.is_closing():
            return True
        self._connected.clear()
        command = (
            f"ip=$(netstat -tn 2>/dev/null | awk '$5 ~ /:{self.server_port}$/ && $6 == \"ESTABLISHED\" "
            f"{{split($5,a,\":\"); print a[1]; exit}}'); "
            f"[ -n \"$ip\" ] && [ -p {_FIFO} ] || exit 1; "
            f"kill $(cat {_PIDFILE} {_NC_PIDFILE} 2>/dev/null) 2>/dev/null; "
            # nc 被杀（vis 重开 FIFO 时写入 EPIPE，退出码 >128）就重连；bridge 关闭 / 连不上就结束
            f"start-stop-daemon -S -b -m -p {_PIDFILE} -x /bin/sh -- -c "
            f"\"while :; do nc $ip {self.port} > {_FIFO} & echo \\$! > {_NC_PIDFILE}; wait \\$!; "
            f"[ \\$? -gt 128 ] || break; sleep 0.1; done; rm -f {_NC_PIDFILE}\""
        )
        try:
            result = await speaker.run_shell(command, timeout=3000)
        except Exception as exc:
            logger.warning(f"Failed to start level feed on speaker: {exc}", module=LOG_MODULE)
            return False
        if result is None or getattr(result, "exit_code", 1) != 0:
            logger.warning(
                f"Speaker could not start level feed: {getattr(result, 'stderr', '') or 'no vis fifo / bridge ip'}",
                module=LOG_MODULE,
            )
            return False
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Speaker did not connect to level feed (firewall?)", module=LOG_MODULE)
            return False

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else ""
        try:
            private = ipaddress.ip_address(host).is_private
        except ValueError:
            private = False
        if not private:
            logger.warning(f"Rejected level feed client {host}", module=LOG_MODULE)
            writer.close()
            return
        old, self._writer = self._writer, writer
        if old is not None:
            old.close()
        self._connects += 1
        if self._connects == 1:
            logger.info(f"Speaker connected to level feed: {host}", module=LOG_MODULE)
        else:
            logger.debug(f"Speaker reconnected to level feed ({self._connects})", module=LOG_MODULE)
        self._connected.set()
        try:
            # nc 的标准输入是 /dev/null，连上就会半关闭（我们这边马上读到 EOF），
            # 但它仍然在收数据。EOF 不算断开，等连接真正断掉（写失败 / 对方重置）再清理。
            await reader.read()
            await writer.wait_closed()
        except Exception as exc:
            logger.debug(f"Level feed connection error: {exc}", module=LOG_MODULE)
        finally:
            if self._writer is writer:
                self._writer = None
                logger.debug("Speaker level feed disconnected", module=LOG_MODULE)
            writer.close()

    # ---- 推流 ----

    def set_active(self, active: bool):
        """进入 / 离开"听"的状态。可在事件循环线程调用。"""
        if active == self._active:
            return
        self._active = active
        if active:
            self._stats = []
            self._peak = 0.0
            if self._loop is not None and (self._sender is None or self._sender.done()):
                self._sender = self._loop.create_task(self._send_frames())
            return
        if self._sender is not None:
            self._sender.cancel()
            self._sender = None
        if self._stats:
            stats, self._stats = sorted(self._stats), []
            noise = self._noise_db or 0.0
            p90 = stats[int(len(stats) * 0.9)]
            logger.info(
                f"麦克风音量：底噪 {noise:.0f} dB，中位 {stats[len(stats) // 2]:.0f}，p90 {p90:.0f}，峰值 {stats[-1]:.0f}"
                f"（p90 高出底噪 {p90 - noise:.0f} dB，满宽需 {self.margin_db + self.span_db:.0f}）",
                module=LOG_MODULE,
            )

    def input(self, data: bytes):
        """GlobalStream 回调（原生扩展线程）：16 kHz 单声道一包 → 同样响度的噪声（按 48 kHz 送，样本数见 _OUTPUT_PER_SAMPLE）。"""
        if not data:
            return
        samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
        if samples.size == 0:
            return
        x = samples.astype(np.float32)
        rms = float(np.std(x))  # 去掉直流偏置
        db = 20 * math.log10(rms + 1.0)
        # 底噪一直跟踪（不只在"听"的时候）：变安静快速跟下，变吵每包（约 64 ms）只抬 0.03 dB
        if self._noise_db is None:
            self._noise_db = db
        elif db < self._noise_db:
            self._noise_db += 0.3 * (db - self._noise_db)
        else:
            self._noise_db += 0.03
        if not self._active:
            return
        self._stats.append(db)
        t = min(1.0, max(0.0, (db - self._noise_db - self.margin_db) / self.span_db))
        if t > self._peak:
            self._peak = t  # float 赋值是原子的，发帧协程读完清零

    async def _send_frames(self):
        """按自己的时钟每 _FRAME_INTERVAL_S 发一帧（绝对时间排期，不漂移）。"""
        loop = asyncio.get_running_loop()
        start = loop.time()
        n = 0
        try:
            while self._active:
                level, self._peak = self._peak, 0.0
                writer = self._writer
                if writer is not None and not writer.is_closing():
                    if writer.transport.get_write_buffer_size() <= _MAX_BUFFERED:
                        noise = self._rng.standard_normal(_FRAME_BYTES // 2) * max(_DITHER_RMS, level * self.max_rms)
                        try:
                            writer.write(np.clip(noise, -32768, 32767).astype(np.int16).tobytes())
                        except Exception:
                            pass
                n += 1
                delay = start + n * _FRAME_INTERVAL_S - loop.time()
                if delay < -_FRAME_INTERVAL_S:
                    # 落后太多（事件循环卡了）：重新对时，不补发
                    start, n, delay = loop.time(), 0, 0
                await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            pass
