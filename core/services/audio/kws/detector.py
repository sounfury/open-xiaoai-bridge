"""唤醒词检测核心：VAD 门控 + 预录缓冲 + 关键词模型。

线上（core/services/audio/kws/__init__.py 的检测线程）和离线验收
（scripts/kws_acceptance.py）用的是同一份逻辑，调参结果可以直接搬到线上。

- vad_gate=True（默认）：只在 VAD 判定有人说话时才把音频送进关键词模型，省 CPU。
  VAD 判定开口时唤醒词往往已经说出一小段（"你"字偏轻），所以平时留着最近
  preroll_ms 还没送进模型的音频，开口时先补进去。
- vad_gate=False：每一帧都送进模型（OwnDing 的做法），不会被 VAD 吃掉开头，CPU 更高。
"""

import collections
import time

from core.utils.logger import logger

FRAME_SAMPLES = 512  # Silero VAD 要求 16 kHz 下每帧 512 采样（32 ms）
SAMPLE_RATE = 16000
FRAME_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE


class KeywordDetector:
    """逐帧处理 16 kHz 单声道 S16 音频（每帧 512 采样），命中返回唤醒词文本。

    只能在一个线程里用；spotter 需要有 kws(frames) / reset()，vad 需要有 vad(frames, rate) / reset()。
    """

    def __init__(
        self,
        spotter,
        vad,
        *,
        vad_threshold: float = 0.10,
        min_silence_ms: float = 480,
        preroll_ms: float = 300,
        vad_gate: bool = True,
    ):
        self.spotter = spotter
        self.vad = vad
        self.configure(
            vad_threshold=vad_threshold,
            min_silence_ms=min_silence_ms,
            preroll_ms=preroll_ms,
            vad_gate=vad_gate,
        )
        self.active = False
        self.silence_frames = 0
        self.started_at = time.monotonic()

    def configure(self, *, vad_threshold, min_silence_ms, preroll_ms, vad_gate):
        """更新运行时参数（配置热重载时调用，不清状态）。"""
        self.vad_threshold = float(vad_threshold)
        self.min_silence_frames = int(float(min_silence_ms) / FRAME_MS)
        self.vad_gate = bool(vad_gate)
        frames = max(0, round(float(preroll_ms) / FRAME_MS))
        old = list(getattr(self, "preroll", ()))
        # 只存"还没送进模型"的帧：送过的再送一遍可能让同一个唤醒词触发两次
        self.preroll = collections.deque(old[-frames:] if frames else (), maxlen=frames or 1)
        self.preroll_frames = frames

    def reset(self):
        """音频不连续（暂停过、开始/结束播报中打断检测）时清掉所有状态。"""
        self.active = False
        self.silence_frames = 0
        self.preroll.clear()
        self.spotter.reset()
        reset_vad = getattr(self.vad, "reset", None)
        if reset_vad:
            reset_vad()

    def process(self, frames: bytes) -> str | None:
        if not self.vad_gate:
            return self.spotter.kws(frames)

        speech_prob = self.vad.vad(frames, SAMPLE_RATE) or 0
        if speech_prob >= self.vad_threshold:
            if not self.active:
                self.active = True
                self.started_at = time.monotonic()
                logger.debug("检测到语音，开始 KWS 检测", module="KWS")
                result = self._feed_preroll()
                if result:
                    return result
            self.silence_frames = 0
            return self._feed(frames)

        if not self.active:
            self._keep(frames)
            return None

        self.silence_frames += 1
        if self.silence_frames <= self.min_silence_frames:
            # 说话中的短暂停顿也送进去（"你好，缪斯"）
            return self._feed(frames)

        logger.debug(
            f"检测到持续静音（{self.silence_frames * FRAME_MS:.0f}ms），暂停 KWS，"
            f"本次 KWS 监听时长 {(time.monotonic() - self.started_at) * 1000:.0f}ms",
            module="KWS",
        )
        self.active = False
        self.silence_frames = 0
        # 丢掉没说完的识别状态，免得上一句的残留和下一句拼起来
        self.spotter.reset()
        self._keep(frames)
        return None

    # ===== 私有方法 =====

    def _keep(self, frames: bytes):
        if self.preroll_frames:
            self.preroll.append(frames)

    def _feed_preroll(self) -> str | None:
        pending = list(self.preroll)
        self.preroll.clear()
        for frames in pending:
            result = self._feed(frames)
            if result:
                return result
        return None

    def _feed(self, frames: bytes) -> str | None:
        result = self.spotter.kws(frames)
        if result:
            # 唤醒后重新开始：下一次开口重新补预录
            self.active = False
            self.silence_frames = 0
            self.preroll.clear()
        return result
