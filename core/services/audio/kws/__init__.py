"""音箱关键词唤醒服务：准备唤醒词和模型，监听音频并派发唤醒事件。"""

import asyncio
import os
import threading
import time

from core.ref import get_app, get_xiaoai, get_xiaozhi, set_kws
from core.services.audio.kws.detector import FRAME_SAMPLES, KeywordDetector
from core.services.audio.kws.sherpa import SherpaOnnx, spotter_settings
from core.services.audio.stream import MyAudio
from core.services.audio.vad.silero import create_silero
from core.services.protocols.typing import AudioConfig, DeviceState
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.wakeup_session import EventManager


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
        handler = getattr(self, "_barge_in_handler", None)
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
        future = asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws", position=position),
            loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(f"[KWS] Wakeup dispatch failed: {type(exc).__name__}: {exc}")

        future.add_done_callback(_log_result)

    # ===== 私有方法 =====

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def _detection_loop(self):
        self.stream.start_stream()
        while True:
            # 读取缓冲区音频数据
            frames = self.stream.read(self.frame_size)
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            # 武装/解除打断检测时清掉上一段的识别状态，避免旧音频拼进新的检测
            if self._barge_in_reset:
                self._barge_in_reset = False
                self.detector.reset()

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
                self.detector.reset()

            result = self.detector.process(frames)
            if result:
                logger.wakeup(result, module="KWS")
                self._dispatch(result)


KWS = _KWS()
