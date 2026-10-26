"""音箱关键词唤醒服务：准备唤醒词和模型，监听音频并派发唤醒事件。"""

import asyncio
import os
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor

from core.ref import get_app, get_xiaoai, get_xiaozhi, set_kws
from core.services.audio.kws.detector import FRAME_SAMPLES, KeywordDetector
from core.services.audio.kws.sherpa import SherpaOnnx, spotter_settings
from core.services.audio.stream import MyAudio
from core.services.diagnostics import diagnostics
from core.services.audio.vad.silero import create_silero
from core.services.protocols.typing import AudioConfig, DeviceState
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.utils.turn_timer import TurnTimer, lag_ms
from core.wakeup_session import EventManager


# 多路检测时，第一次命中后这么久内其它路的命中算同一次（秒）
MULTI_HIT_WINDOW_S = 2.0


class _KWS:
    def __init__(self):
        set_kws(self)
        self.config_manager = ConfigManager.instance()
        self.vad_threshold = 0.10
        self.min_silence_ms = 480
        self.preroll_ms = 300
        self.vad_gate = True

        # 设置帧大小为 512 以兼容 Silero VAD
        self.frame_size = FRAME_SAMPLES
        self.sample_rate = 16000
        self.paused = False
        self.detector: KeywordDetector | None = None
        # 多麦唤醒（core/services/audio/multimic_input.py）：路名 → 检测器，和 ch0 并行，任一路命中即唤醒
        self.multimic = None
        self.extra_detectors: dict[str, KeywordDetector] = {}
        # 多麦各路和 ch0 并行解码（onnxruntime 推理时释放 GIL）；每帧等各路都跑完再读下一帧
        self._pool: ThreadPoolExecutor | None = None
        self._last_hit = 0.0
        self.hit_counts: dict[str, int] = {}
        self._barge_in_handler = None
        self._barge_in_reset = False
        # 上一帧是否被跳过（暂停 / 小智对话中）：恢复处理时音频不连续，要清状态
        self._skipped = True

        self.apply_runtime_config()
        self.config_manager.add_reload_listener(self._on_config_reload)

    def apply_runtime_config(self):
        """同步最新 KWS 相关配置。"""
        vad_config = self.config_manager.get_app_config("vad", {})
        self.vad_threshold = vad_config.get("threshold", 0.10)
        kws_config = self.config_manager.get_app_config("kws", {}) or {}
        self.min_silence_ms = kws_config.get("min_silence_duration", 480)
        self.preroll_ms = kws_config.get("preroll_ms", 300)
        self.vad_gate = bool(kws_config.get("vad_gate", True))
        if self.detector is not None:
            self.detector.configure(**self._detector_settings())
        for detector in self.extra_detectors.values():
            detector.configure(**self._detector_settings())

    def _detector_settings(self) -> dict:
        return {
            "vad_threshold": self.vad_threshold,
            "min_silence_ms": self.min_silence_ms,
            "preroll_ms": self.preroll_ms,
            "vad_gate": self.vad_gate,
        }

    def start(self):
        # 必须先完成词表和模型初始化，再启动线程并报告成功；不能在后台静默失败。
        from core.services.audio.kws.keywords import main as generate_keywords

        if generate_keywords() != 0:
            raise RuntimeError("KWS 唤醒词生成失败，请检查模型词表和配置")
        SherpaOnnx.start()
        # 独立的 Silero：VAD 线程也在用全局那个，两路音频交替送进同一个实例会互相污染状态
        self.detector = KeywordDetector(SherpaOnnx, create_silero(), **self._detector_settings())
        self._start_multimic()
        self.audio = MyAudio.create()
        self.stream = self.audio.open(
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
        )

        # 启动 KWS 服务
        self.paused = False
        # 播报中打断：设置后即使 paused 也继续检测，命中交给它而不是 EventManager.wakeup
        self._barge_in_handler = None
        self._barge_in_reset = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()
        settings = spotter_settings(self.config_manager.get_app_config)
        logger.kws_event(
            "关键词唤醒服务启动",
            f"关键词:[score:{settings['keywords_score']}, threshold:{settings['keywords_threshold']}, "
            f"max_active_paths:{settings['max_active_paths']}, offsets:{settings['stream_offsets_ms']}ms], "
            f"VAD:[gate:{self.vad_gate}, threshold:{self.vad_threshold}, "
            f"min_silence:{self.min_silence_ms}ms, preroll:{self.preroll_ms}ms]",
        )

    def _start_multimic(self):
        from core.services.audio.multimic_input import get_multimic

        try:
            self.multimic = get_multimic(self.config_manager.get_app_config("multimic", {}) or {})
        except Exception as exc:
            logger.error(f"[KWS] 多麦唤醒输入启动失败，只用 ch0: {type(exc).__name__}: {exc}")
            self.multimic = None
        if self.multimic is None:
            return
        # 每路独立的唤醒模型和 Silero，在线程池里和 ch0 并行跑
        self.extra_detectors = {
            name: KeywordDetector(SherpaOnnx.new_spotter(), create_silero(), **self._detector_settings())
            for name in self.multimic.names
        }
        self._pool = ThreadPoolExecutor(max_workers=len(self.extra_detectors), thread_name_prefix="kws-mic")

    def _reset_detectors(self):
        diagnostics.count("kws.resets")
        self.detector.reset()
        for detector in self.extra_detectors.values():
            detector.reset()
        if self.multimic is not None:
            self.multimic.discard()

    def get_file_path(self, file_name: str):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "../../../models", file_name)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def arm_barge_in(self, handler):
        """播报期间检测唤醒词，用来打断播报。

        handler(text, position) 在 KWS 线程里调用；position 是唤醒词大约结束时
        GlobalStream 的绝对采样位置。命中后保持武装，由调用方 disarm。
        """
        self._barge_in_reset = True
        self._barge_in_handler = handler

    def disarm_barge_in(self):
        self._barge_in_handler = None
        self._barge_in_reset = True

    def _dispatch(self, text: str):
        """命中唤醒词：播报中打断时交给打断处理，否则按普通唤醒派发。"""
        # 唤醒词大约在已读到的位置结束（GlobalStream 绝对采样位置）
        position = self.stream.position()
        diagnostics.event("kws.hit", position=position, queued_ms=round(self.stream.unread_samples() / 16, 1))
        handler = getattr(self, "_barge_in_handler", None)
        # 耗时从这里算起；KWS 落后 = 已收到但还没过检测的音频
        TurnTimer.begin(
            "打断" if handler is not None else "唤醒词",
            f"KWS 落后 {lag_ms(self.stream.unread_samples())}ms",
        )
        if handler is not None:
            try:
                handler(text, position)
            except Exception as exc:
                logger.error(f"[KWS] Barge-in handler failed: {type(exc).__name__}: {exc}")
            return
        self.on_message(text, position)

    def on_message(self, text: str, position: int | None = None):
        """普通唤醒；position 让对话从唤醒词后面接着听（"你好缪斯，开灯"）。"""
        loop = get_app().loop if get_app() else get_xiaoai().async_loop
        logger.debug(f"[KWS] Dispatch wakeup event: {text}")
        # 派发前立即占住唤醒入口，主循环暂时繁忙时也不能继续积压唤醒请求。
        self.pause()
        future = asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws", position=position, detected_at=time.monotonic()),
            loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except CancelledError:
                pass
            except Exception as exc:
                logger.error(f"[KWS] Wakeup dispatch failed: {type(exc).__name__}: {exc}")

        future.add_done_callback(_log_result)

    # ===== 私有方法 =====

    @staticmethod
    def _process_chunks(detector: KeywordDetector, chunks: list[bytes], name: str) -> str | None:
        """同一路的几帧按顺序处理，返回第一个命中。"""
        with diagnostics.measure(f"kws.{name}", audio_s=sum(map(len, chunks)) / 32000, warn_s=0.2):
            for chunk in chunks:
                result = detector.process(chunk)
                if result:
                    return result
        return None

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def _detection_loop(self):
        self.stream.start_stream()
        while True:
            # 实时唤醒不补做数秒前的检测；丢旧帧后重置模型，避免拼接出旧唤醒词。
            dropped = self.stream.trim_input(self.sample_rate, self.sample_rate // 3)
            if dropped:
                diagnostics.count("kws.dropped_samples", dropped)
                diagnostics.event("kws.trim", dropped_ms=round(dropped * 1000 / self.sample_rate))
                logger.warning(f"[KWS] 主麦音频积压，丢弃 {dropped * 1000 // self.sample_rate}ms，恢复实时检测")
                self._reset_detectors()
                self._skipped = True
            # 读取缓冲区音频数据（ch0 单声道 + 多麦各路，每路只取一帧）
            frames = self.stream.read(self.frame_size)
            mono_ready = len(frames) == self.frame_size * 2
            extra = self.multimic.pull() if self.multimic is not None else []
            if not mono_ready and not extra:
                time.sleep(0.01)
                continue

            # 武装/解除打断检测时清掉上一段的识别状态，避免旧音频拼进新的检测
            if self._barge_in_reset:
                self._barge_in_reset = False
                self._reset_detectors()
                extra = []

            # 在说话和监听状态时，暂停 KWS（播报中打断检测除外）
            xiaozhi = get_xiaozhi()
            if (self.paused and self._barge_in_handler is None) or (
                xiaozhi and xiaozhi.device_state in [DeviceState.LISTENING, DeviceState.SPEAKING]
            ):
                self._skipped = True
                time.sleep(0.01)
                continue

            if self._skipped:
                # 中间有音频没处理，旧的识别状态接不上
                self._skipped = False
                self._reset_detectors()
                extra = []

            # 多麦各路先交给线程池（同一路的几帧在一个任务里按顺序处理），再在本线程跑 ch0
            chunks: dict[str, list[bytes]] = {}
            for name, chunk in extra:
                if name in self.extra_detectors:
                    chunks.setdefault(name, []).append(chunk)
            futures = [
                (name, self._pool.submit(self._process_chunks, self.extra_detectors[name], items, name))
                for name, items in chunks.items()
            ]
            hits: list[tuple[str, str]] = []
            if mono_ready:
                result = self._process_chunks(self.detector, [frames], "ch0")
                if result:
                    hits.append(("ch0", result))
            for name, future in futures:
                try:
                    with diagnostics.measure(f"kws.wait_{name}", warn_s=0.2):
                        result = future.result()
                except Exception as exc:
                    logger.error(f"[KWS] {name} 检测失败: {type(exc).__name__}: {exc}")
                    continue
                if result:
                    hits.append((name, result))
            if not hits:
                continue
            if self.stream.unread_samples() > self.sample_rate:
                # 本帧推理时已落后太多，命中也已过时；下一轮清缓存再检测。
                diagnostics.count("kws.stale_hits")
                continue
            now = time.monotonic()
            for name, _ in hits:
                self.hit_counts[name] = self.hit_counts.get(name, 0) + 1
            names = ", ".join(name for name, _ in hits)
            # 同一句话会被几路先后认出来（多麦链路比 ch0 晚约 80 ms）：第一路命中就唤醒，
            # 其余路照常跑，窗口内再命中只记日志（看哪几路认出了同一次）
            if self.extra_detectors and now - self._last_hit < MULTI_HIT_WINDOW_S:
                logger.info(f"[KWS] 同一次唤醒也被 {names} 认出")
                continue
            self._last_hit = now
            source, result = hits[0]
            if self.extra_detectors:
                logger.info(f"[KWS] 命中来源: {names}（累计 {self.hit_counts}）")
            logger.wakeup(result, module="KWS")
            self._dispatch(result)


KWS = _KWS()
