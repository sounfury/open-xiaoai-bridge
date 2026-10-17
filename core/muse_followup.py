"""小爱管家追问：只说了"你好缪斯"时再听一句。

复用 ExternalConversationController 的输入链路（与 OpenClaw / OpenAI / QwenPaw 相同）：
  - muse.input_mode = "local_asr"  : 本地 VAD → ASR
  - muse.input_mode = "xiaoai_asr" : 静默唤醒小爱，接管原生 ASR 结果
只采集一轮，不对接任何对话后端。
"""

import asyncio

from core.external_conversation import ExternalConversationController
from core.ref import get_speaker, get_vad
from core.utils.logger import logger


class _NoBackend:
    """单次追问不对接后端，仅用于满足基类的 MANAGER 约定。"""


class MuseFollowupCapture(ExternalConversationController):
    CONFIG_PREFIX = "muse"
    BACKEND_NAME = "小爱管家"
    LOG_MODULE = "Muse"
    WAKEUP_SOURCE = "muse"
    MANAGER = _NoBackend

    def __init__(self, timeout: float = 8.0):
        super().__init__()
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    async def capture_once(self) -> str | None:
        """提示音 → 听一句 → 返回识别文本；超时或被中断返回 None。"""
        if self.active:
            return None
        self.active = True
        self._loop = asyncio.get_running_loop()
        logger.info("🎙️ 等待请求内容", module=self.LOG_MODULE)
        try:
            await self._turn_on_light()
            await self._stop_recording()
            prompt = self._cfg("followup_prompt", "")
            speaker = get_speaker()
            if prompt and speaker:
                try:
                    await speaker.play(text=str(prompt), blocking=True, timeout=10000)
                except Exception as exc:
                    logger.warning(f"追问提示播放失败，继续听取请求: {exc}", module=self.LOG_MODULE)
            await self._play_notify()
            if not self.active:
                return None
            await self._start_recording()

            if self.uses_xiaoai_asr():
                return await self._wait_for_xiaoai_asr_text()

            vad = get_vad()
            if not vad:
                logger.warning(
                    "VAD 不可用（AUDIO_INPUT_ENABLE=0？），无法采集请求内容",
                    module=self.LOG_MODULE,
                )
                return None
            speech_bytes = await self._wait_for_speech(vad)
            await self._turn_off_light()
            if not speech_bytes:
                return None

            from core.services.audio.asr import ASRService

            text = await asyncio.to_thread(ASRService.asr, speech_bytes, sample_rate=16000)
            return text.strip() if text else None
        finally:
            # 被 stop() 打断时（如用户喊了"小爱同学"）不要再去关小爱的监听
            ended_naturally = self.active
            self.stop()
            await self._turn_off_light()
            if ended_naturally and self.uses_xiaoai_asr():
                await self._stop_xiaoai_native_listening()

    def stop(self):
        """只清理追问状态，不涉及后端 TTS / after_wakeup。"""
        if not self.active:
            return
        self.active = False
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        # 被打断时立即熄灯，不等 capture_once 的 finally
        self._release_light_soon()
