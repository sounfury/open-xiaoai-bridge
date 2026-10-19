"""对话期间压低音乐：进入连续对话时压低，退出时恢复（原生小爱唤醒时也会压低自己的音乐）。

音乐一直响，唤醒后的 VAD / ASR 会把歌词当成你在说话（日语歌被识别成韩文送给 Hermes，对话停不下来）。

- Spotify：bridge 启动时建一条 SSH 长连接，在 Oracle 上常驻 deploy/music-duck/music_duck.py，
  stdin 发 duck / restore，stdout 回 JSON。每次新开 SSH 要 2～3 秒，长连接下一条命令约 0.7 秒
  （Spotify Web API）。SSH 断开时脚本自己恢复音量；这边断了就后台重连。
- 音箱自己的播放器（mediaplayer）：正在播（status == 1）就暂停，退出时只继续由我们暂停的那次。

duck() / restore() 只排队，不阻塞对话流程；可在任意线程调用。配置见 APP_CONFIG["music_duck"]。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time

from core.utils.logger import logger

LOG_MODULE = "MusicDuck"
_MEDIAPLAYER_STATUS = "ubus -t 2 call mediaplayer player_get_play_status"
_MEDIAPLAYER_OP = "ubus -t 2 call mediaplayer player_play_operation '{{\"action\":\"{action}\"}}'"


def find_ssh(configured: str | None = None) -> str:
    """优先 Git 自带的 ssh：Windows 自带的 OpenSSH 要求 ~/.ssh/config 权限严格，
    这台机器上有别的用户组能读它，会报 Bad owner or permissions 直接退出。"""
    if configured:
        return configured
    for candidate in (
        r"C:\Program Files\Git\usr\bin\ssh.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\ssh.exe"),
    ):
        if os.name == "nt" and os.path.exists(candidate):
            return candidate
    return shutil.which("ssh") or "ssh"


class _SpotifyLink:
    """到 Oracle 上 music_duck.py 的 SSH 长连接。"""

    def __init__(self, host: str, command: str, volume: int, ssh_bin: str | None = None):
        self.ssh_bin = find_ssh(ssh_bin)
        self.host = host
        self.command = command
        self.volume = int(volume)
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._pending: list[str] = []  # 连接还没好时排队的命令

    def ensure(self):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if time.monotonic() - self._last_start < 10:
                return  # 重连限速
            self._last_start = time.monotonic()
            self.ready = False
            try:
                self.proc = subprocess.Popen(
                    [
                        self.ssh_bin, "-T",
                        "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=10",
                        "-o", "ServerAliveInterval=20",
                        "-o", "ServerAliveCountMax=3",
                        self.host,
                        self.command,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                logger.warning(f"Spotify 压低服务启动失败: {type(exc).__name__}: {exc}", module=LOG_MODULE)
                self.proc = None
                return
            proc = self.proc
            threading.Thread(target=self._read, args=(proc,), daemon=True, name="music-duck-read").start()
            threading.Thread(target=self._read_err, args=(proc,), daemon=True, name="music-duck-err").start()

    def send(self, line: str):
        self.ensure()
        with self._lock:
            if not self.ready or self.proc is None:
                # duck 和 restore 成对：只保留最后一条有意义的（restore 抵消还没发出的 duck）
                if line.startswith("restore") and self._pending and self._pending[-1].startswith("duck"):
                    self._pending.pop()
                else:
                    self._pending.append(line)
                return
            self._write(line)

    def _write(self, line: str):
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            logger.debug(f"Spotify 压低命令发送失败: {exc}", module=LOG_MODULE)

    def _read(self, proc: subprocess.Popen):
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                logger.debug(f"music_duck: {raw.strip()}", module=LOG_MODULE)
                continue
            if msg.get("ready"):
                with self._lock:
                    if self.proc is proc:
                        self.ready = True
                        pending, self._pending = self._pending, []
                        for line in pending:
                            self._write(line)
                logger.info(f"Spotify 压低服务已连接（{self.host}）", module=LOG_MODULE)
                continue
            self._log(msg)
        with self._lock:
            if self.proc is proc:
                self.ready = False
        logger.info("Spotify 压低服务连接断开，下次对话时重连", module=LOG_MODULE)

    def _read_err(self, proc: subprocess.Popen):
        for raw in proc.stderr:
            if raw.strip():
                # ssh 本身的错误（认证 / 权限 / 连不上）要看得见
                logger.warning(f"music_duck: {raw.strip()}", module=LOG_MODULE)

    @staticmethod
    def _log(msg: dict):
        cmd = msg.get("cmd")
        if not msg.get("ok", False):
            logger.warning(f"Spotify {cmd} 失败: {msg.get('error')}", module=LOG_MODULE)
        elif cmd == "duck" and msg.get("ducked") and not msg.get("already"):
            how = "暂停" if msg.get("mode") == "pause" else f"音量 {msg.get('previous')} → {msg.get('volume')}"
            logger.info(f"Spotify 已压低：{how}（{msg.get('device')}，{msg.get('ms')} ms）", module=LOG_MODULE)
        elif cmd in ("restore", "eof") and msg.get("restored"):
            how = "继续播放" if msg.get("mode") == "pause" else f"音量恢复 {msg.get('volume')}"
            logger.info(f"Spotify {how}（{msg.get('why')}，{msg.get('ms', 0)} ms）", module=LOG_MODULE)
        elif cmd in ("restore", "eof") and msg.get("reason") not in (None, "not_ducked"):
            logger.info(f"Spotify 未恢复：{msg.get('reason')}", module=LOG_MODULE)
        else:
            logger.debug(f"Spotify {cmd}: {msg}", module=LOG_MODULE)

    def close(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()  # 远端读到 EOF 会先恢复
            except Exception:
                pass


class MusicDucker:
    _instance: "MusicDucker | None" = None

    def __init__(self, cfg: dict):
        sp = cfg.get("spotify") or {}
        self.spotify = (
            _SpotifyLink(
                str(sp.get("ssh_host", "oracle")),
                str(sp.get("command", "python3 -u ~/home-tools/spotify/music_duck.py serve")),
                int(sp.get("volume", 15)),
                sp.get("ssh_bin") or None,
            )
            if sp.get("enabled", True)
            else None
        )
        self.speaker_enabled = bool((cfg.get("speaker") or {}).get("enabled", True))
        self._speaker_paused = False
        self._ducked = False

    @classmethod
    def instance(cls) -> "MusicDucker | None":
        """music_duck 未开启时返回 None。"""
        from core.utils.config import ConfigManager

        cfg = ConfigManager.instance().get_app_config("music_duck", {}) or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        if cls._instance is None:
            cls._instance = cls(cfg)
        return cls._instance

    def warm_up(self):
        """bridge 启动时先连上（第一次唤醒就不用等 SSH 握手）。"""
        if self.spotify is not None:
            threading.Thread(target=self.spotify.ensure, daemon=True, name="music-duck-warm").start()

    def duck(self, loop: asyncio.AbstractEventLoop | None = None):
        if self._ducked:
            return
        self._ducked = True
        if self.spotify is not None:
            self.spotify.send(f"duck {self.spotify.volume}")
        if self.speaker_enabled and loop is not None:
            asyncio.run_coroutine_threadsafe(self._pause_speaker(), loop)

    def restore(self, loop: asyncio.AbstractEventLoop | None = None):
        if not self._ducked:
            return
        self._ducked = False
        if self.spotify is not None:
            self.spotify.send("restore")
        if self.speaker_enabled and loop is not None:
            asyncio.run_coroutine_threadsafe(self._resume_speaker(), loop)

    # ---- 音箱自己的播放器 ----

    @staticmethod
    async def _speaker_status() -> int | None:
        from core.ref import get_speaker

        speaker = get_speaker()
        if speaker is None:
            return None
        res = await speaker.run_shell(_MEDIAPLAYER_STATUS, timeout=3000)
        try:
            info = json.loads(json.loads(res.stdout).get("info", "{}"))
            return int(info.get("status"))
        except Exception:
            return None

    async def _pause_speaker(self):
        from core.ref import get_speaker

        try:
            if await self._speaker_status() != 1:
                return
            await get_speaker().run_shell(_MEDIAPLAYER_OP.format(action="pause"), timeout=3000)
            self._speaker_paused = True
            logger.info("音箱播放器已暂停", module=LOG_MODULE)
        except Exception as exc:
            logger.debug(f"暂停音箱播放器失败: {exc}", module=LOG_MODULE)

    async def _resume_speaker(self):
        from core.ref import get_speaker

        if not self._speaker_paused:
            return
        self._speaker_paused = False
        try:
            # 对话里切了歌 / 自己停了（不是暂停状态）就不动
            if await self._speaker_status() != 2:
                return
            await get_speaker().run_shell(_MEDIAPLAYER_OP.format(action="play"), timeout=3000)
            logger.info("音箱播放器已继续", module=LOG_MODULE)
        except Exception as exc:
            logger.debug(f"继续音箱播放器失败: {exc}", module=LOG_MODULE)
