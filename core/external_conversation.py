"""外部后端连续对话控制：协调录音、监听灯效、语音识别、请求与播报。

After a custom wake word triggers wakeup, this module drives either:
  - local VAD -> ASR -> backend -> TTS
  - XiaoAI native ASR -> backend -> TTS

The selected input path runs independently of the XiaoZhi session state
machine.

Key design decisions:
  - Uses per-session asyncio.Future objects so it never conflicts with the
    XiaoZhi wakeup session state machine.
  - Never calls abort_xiaoai() (which would break the FileMonitor).
  - TTS playback is awaited, so the next listening round only starts after
    the response has finished playing — unless the user barges in: with the
    speaker's echo reference live (barge_in.enabled), the microphone stays on
    while speaking, KWS keeps listening, and a wake word stops the reply and
    starts the next turn right away (speech after the wake word is kept).
  - Lights are state-driven (core/services/conversation_light.py) and never
    block the voice flow.
"""

import asyncio
import os

import open_xiaoai_server

from core.ref import get_kws, get_speaker, get_vad, get_xiaoai
from core.services.conversation_light import ConversationLight, effects_from_config
from core.utils.config import ConfigManager

_NOTIFY_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "tts_notify.mp3",
)

_SEND_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "send_notify.mp3",
)

def _load_notify_sound() -> bytes | None:
    """Decode tts_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_NOTIFY_SOUND_PATH):
        return None
    try:
        with open(_NOTIFY_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

def _load_send_sound() -> bytes | None:
    """Decode send_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_SEND_SOUND_PATH):
        return None
    try:
        with open(_SEND_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

_NOTIFY_PCM = _load_notify_sound()
_SEND_PCM = _load_send_sound()
from core.utils.logger import logger


class ExternalConversationController:
    """Manages multi-turn conversation for an external backend."""

    LOCAL_ASR_INPUT = "local_asr"
    XIAOAI_ASR_INPUT = "xiaoai_asr"
    XIAOAI_ASR_TIMEOUT = "__timeout__"
    CONFIG_PREFIX = "openclaw"
    BACKEND_NAME = "OpenClaw"
    LOG_MODULE = "OpenClaw Conv"
    WAKEUP_SOURCE = "openclaw"
    MANAGER = None

    def __init__(self):
        self.config = ConfigManager.instance()
        if self.MANAGER is None:
            raise RuntimeError("MANAGER must be set by subclass")
        self.backend = self.MANAGER
        self.active = False

        # Per-session asyncio.Future used to receive VAD events
        self._vad_future: asyncio.Future | None = None
        # Per-session asyncio.Future used to receive XiaoAI native ASR results
        self._xiaoai_asr_future: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Playback token for the current TTS session
        self._playback_token: int | None = None
        self._light: ConversationLight | None = None
        # 打断后下一轮从 GlobalStream 的这个位置开始补录（唤醒词之后已经说出的话）
        self._barge_in_from: int | None = None
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def exit_keywords(self) -> list[str]:
        return self._cfg("exit_keywords", ["退出", "停止", "再见"])

    @property
    def timeout(self) -> int:
        return int(self.config.get_app_config("wakeup.timeout", 20))

    @property
    def input_mode(self) -> str:
        mode = self._cfg("input_mode", self.LOCAL_ASR_INPUT)
        if not isinstance(mode, str):
            return self.LOCAL_ASR_INPUT
        normalized = mode.strip().lower()
        if normalized in {self.LOCAL_ASR_INPUT, self.XIAOAI_ASR_INPUT}:
            return normalized
        logger.warning(
            f"Unknown {self.CONFIG_PREFIX}.input_mode={mode!r}, fallback to {self.LOCAL_ASR_INPUT}",
            module=self.LOG_MODULE,
        )
        return self.LOCAL_ASR_INPUT

    def uses_xiaoai_asr(self) -> bool:
        return self.input_mode == self.XIAOAI_ASR_INPUT

    # ---- public API ----

    def is_active(self) -> bool:
        return self.active

    async def start(self):
        """Enter OpenClaw conversation mode."""
        if self.active:
            logger.warning(f"[{self.LOG_MODULE}] Already active, ignoring start()")
            return
        self.active = True
        self._loop = asyncio.get_running_loop()
        self._barge_in_from = None
        self._light = ConversationLight(
            get_speaker(),
            effects_from_config(
                self._cfg("listening_light", {}), native_listening=self.uses_xiaoai_asr()
            ),
            self._loop,
        )

        logger.info(f"🎙️ 进入 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

        try:
            await self._conversation_loop()
        except Exception as exc:
            import traceback
            logger.error(
                f"Conversation loop error: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                module=self.LOG_MODULE,
            )
        finally:
            self.stop()
            light, self._light = self._light, None
            if light is not None:
                await light.close()
            # 会话结束：无论是否在关麦期间退出，都允许看门狗恢复录音
            self._set_recording_wanted(True)

    def stop(self):
        """Exit conversation mode and clean up."""
        if not self.active:
            return
        self.active = False
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        kws = get_kws()
        if kws and hasattr(kws, "disarm_barge_in"):
            kws.disarm_barge_in()
        self._light_state("off")
        if self._playback_token is not None:
            open_xiaoai_server.stop_tts_playback(self._playback_token)
            self._playback_token = None
        if self.uses_xiaoai_asr():
            speaker = get_speaker()
            if speaker and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        speaker.wake_up(awake=False),
                        self._loop,
                    )
                except Exception as exc:
                    logger.debug(
                        f"Failed to stop XiaoAI native listening: {exc}",
                        module=self.LOG_MODULE,
                    )
        logger.info(f"👋 退出 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

    def consume_xiaoai_recognize_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Consume XiaoAI native ASR events while the backend is waiting."""
        if not (
            self.active
            and self.uses_xiaoai_asr()
            and self._xiaoai_asr_future
        ):
            return False

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not is_final:
            logger.debug(
                f"Ignoring partial XiaoAI ASR result: {normalized_text}",
                module=self.LOG_MODULE,
            )
            return True

        # Silent wakeup may emit an empty final marker before real speech starts.
        if not normalized_text and is_vad_begin is False:
            logger.debug("Ignoring XiaoAI wake marker for native ASR", module=self.LOG_MODULE)
            return True

        if normalized_text:
            logger.debug(f"XiaoAI native ASR recognized: {normalized_text}", module=self.LOG_MODULE)
            self._resolve_xiaoai_asr_future(normalized_text)
            return True

        logger.debug(
            "XiaoAI native ASR received empty final result",
            module=self.LOG_MODULE,
        )
        self._resolve_xiaoai_asr_future(self.XIAOAI_ASR_TIMEOUT)
        return True

    # ===== 私有方法 =====

    def _cfg(self, key: str, default=None):
        return self.config.get_app_config(f"{self.CONFIG_PREFIX}.{key}", default)

    def _light_state(self, state: str):
        """切换会话灯状态（立即返回，可在任意线程调用）。"""
        if self._light is not None:
            self._light.set(state)

    def _barge_cfg(self, key: str, default=None):
        return self.config.get_app_config(f"barge_in.{key}", default)

    def _barge_in_ready(self) -> bool:
        """打断已开启，且音箱正在发送回采（回声能消掉）。"""
        if not self._barge_cfg("enabled", False):
            return False
        xiaoai = get_xiaoai()
        live = getattr(xiaoai, "echo_reference_live", None)
        kws = get_kws()
        return bool(live and live() and kws and hasattr(kws, "arm_barge_in"))

    @staticmethod
    def _set_recording_wanted(wanted: bool):
        """告诉录音看门狗现在是否应该有录音（主动关麦时不要重新打开）。"""
        xiaoai = get_xiaoai()
        if xiaoai and hasattr(xiaoai, "set_recording_wanted"):
            xiaoai.set_recording_wanted(wanted)

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ---- conversation loop ----

    async def _conversation_loop(self):
        """Run VAD -> ASR -> backend -> TTS turns until exit."""

        # 没有回采时：关麦 → 提示音 → 开麦，避免录到 before_wakeup 的播报和提示音。
        # 有回采时麦克风一直开着，回声由回声消除去掉。
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        if self._barge_in_ready():
            await self._play_notify()
        else:
            await self._stop_recording()
            await self._play_notify()
            await self._start_recording()
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        while self.active:
            if self.uses_xiaoai_asr():
                result = await self._run_one_turn_with_xiaoai_asr()
            else:
                result = await self._run_one_turn_with_local_asr()
            if result in ("exit", "timeout"):
                if self.uses_xiaoai_asr():
                    await self._stop_xiaoai_native_listening()
                await self._call_after_wakeup()
                break
            elif result in ("error", "interrupted"):
                # 被 stop() 打断时静默退出，不播告别语、不再操作小爱监听
                break

    async def _run_one_turn_with_local_asr(self) -> str:
        """Execute a single conversation turn.

        Returns:
            "continue" - turn completed, loop to next
            "exit"     - user said an exit keyword
            "timeout"  - no speech detected within timeout
            "interrupted" - stop() was called before listening started
            "error"    - unrecoverable error
        """
        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error"

        # 1. Start listening for speech (recording is already active)
        self._light_state("listening")
        if not self.active:
            return "interrupted"
        barge_in_from, self._barge_in_from = self._barge_in_from, None
        speech_bytes = await self._wait_for_speech(vad, preroll_from=barge_in_from)
        if speech_bytes is None:
            return "timeout"

        self._light_state("thinking")
        logger.debug(
            f"Got speech buffer: {len(speech_bytes)} bytes",
            module=self.LOG_MODULE,
        )

        # 2. ASR: convert speech to text
        from core.services.audio.asr import ASRService

        text = ASRService.asr(speech_bytes, sample_rate=16000)
        if barge_in_from is not None:
            # 补录的开头带着唤醒词（"你好缪斯，明天天气怎么样"），去掉
            text = self._strip_wake_word(text)
        if not text or not any(character.isalnum() for character in text):
            logger.debug("ASR empty, retrying", module=self.LOG_MODULE)
            return "continue"

        # 3. Check exit keywords
        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                return "exit"

        # 4. Send to backend and wait for response
        full_text = text
        if self.backend._rule_prompt:
            full_text = text + "\n" + self.backend._rule_prompt
        # 先发出请求（不阻塞等回复），立即播"咻"给用户即时反馈，再等回复
        # 注意：直接用 _send_and_track 而非 send(wait_response=False)，
        # 后者会立即清掉 Future，导致后续 _wait_response 拿不到回复
        run_id = await self.backend._send_and_track(full_text)
        await self._play_send_sound()
        response = await self.backend._wait_response(run_id) if run_id else None
        if response is None:
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            await self._play_reply_and_resume(
                self._cfg("failure_text", "我听到了，但 AI 服务暂时没有返回回答，请稍后再试"), vad
            )
            return "continue"

        await self._play_reply_and_resume(str(response), vad)
        return "continue"

    async def _run_one_turn_with_xiaoai_asr(self) -> str:
        """Execute a single conversation turn using XiaoAI native ASR."""
        text = await self._wait_for_xiaoai_asr_text()
        if text is None:
            logger.debug("XiaoAI native ASR turn timed out", module=self.LOG_MODULE)
            return "timeout"

        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                return "exit"

        self._light_state("thinking")
        full_text = text
        if self.backend._rule_prompt:
            full_text = text + "\n" + self.backend._rule_prompt
        response = await self.backend.send(full_text, wait_response=True)
        if response is None:
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            await self._play_reply_and_resume(
                self._cfg("failure_text", "我听到了，但 AI 服务暂时没有返回回答，请稍后再试")
            )
            return "continue"

        await self._play_reply_and_resume(str(response))
        return "continue"

    @staticmethod
    def _strip_wake_word(text: str) -> str:
        """去掉句首的缪斯唤醒词；不含唤醒词时原样返回。"""
        if not text:
            return text
        try:
            from core.muse import load_settings, match_settings_text

            match = match_settings_text(text, load_settings())
        except Exception:
            return text
        return match.content if match else text

    # ---- VAD integration ----

    async def _wait_for_speech(self, vad, preroll_from: int | None = None) -> bytes | None:
        """Use VAD to detect speech and collect complete utterance.

        Follows the same two-step pattern as the XiaoZhi wakeup session:
          1. resume("speech") → wait for on_speech (voice detected)
          2. resume("silence") → keep recording → wait for on_silence (user stopped)

        The audio stream is tapped between step 1 and 2 to capture the
        full utterance that the VAD does not provide by itself.

        Returns:
            PCM bytes of captured speech, or None on timeout.
        """
        from core.wakeup_session import EventManager

        self._vad_future = self._loop.create_future()
        recording_frames: list[bytes] = []
        is_recording = False

        original_on_speech = EventManager.on_speech
        original_on_silence = EventManager.on_silence

        def _on_speech_hook(speech_buffer: bytes):
            """Voice detected — save initial buffer, start recording, wait for silence."""
            nonlocal is_recording
            recording_frames.append(speech_buffer)
            is_recording = True
            self._light_state("hearing")
            logger.debug(f"VAD speech detected, buffer size: {len(speech_buffer)}", module=self.LOG_MODULE)
            # Now wait for silence to know user stopped speaking
            vad.resume("silence")

        def _on_silence_hook():
            """Silence detected — stop recording and resolve."""
            nonlocal is_recording
            is_recording = False
            logger.debug("VAD detected silence, stop recording", module=self.LOG_MODULE)
            if self._vad_future and not self._vad_future.done():
                self._loop.call_soon_threadsafe(
                    self._vad_future.set_result, b"".join(recording_frames)
                )

        # Tap into VAD's audio stream to record frames while waiting for silence
        _orig_handle_speech = vad._handle_speech_frame
        _orig_handle_silence = vad._handle_silence_frame

        def _recording_speech_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
            _orig_handle_speech(frames)

        def _recording_silence_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
            _orig_handle_silence(frames)

        EventManager.on_speech = _on_speech_hook
        EventManager.on_silence = _on_silence_hook
        vad._handle_speech_frame = _recording_speech_frame
        vad._handle_silence_frame = _recording_silence_frame

        try:
            if preroll_from is None:
                vad.resume("speech")
            else:
                vad.resume("speech", preroll_from=preroll_from)
            result = await asyncio.wait_for(self._vad_future, timeout=self.timeout)
            return result

        except asyncio.TimeoutError:
            logger.debug("VAD timeout, no speech detected", module=self.LOG_MODULE)
            vad.pause()
            return None

        finally:
            EventManager.on_speech = original_on_speech
            EventManager.on_silence = original_on_silence
            vad._handle_speech_frame = _orig_handle_speech
            vad._handle_silence_frame = _orig_handle_silence
            self._vad_future = None

    def _cancel_vad_future(self):
        """Cancel any pending VAD future."""
        if self._vad_future and not self._vad_future.done():
            self._loop.call_soon_threadsafe(self._vad_future.cancel)
        self._vad_future = None

    async def _wait_for_xiaoai_asr_text(self) -> str | None:
        """Wake XiaoAI and wait for a final native ASR result."""
        speaker = get_speaker()
        if not speaker:
            logger.error("Speaker not available", module=self.LOG_MODULE)
            return None

        deadline = self._loop.time() + self.timeout
        while self.active:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None

            self._xiaoai_asr_future = self._loop.create_future()
            try:
                wait_seconds = max(0.1, remaining)
                logger.debug(
                    f"Triggering XiaoAI native ASR, waiting up to {wait_seconds:.1f}s",
                    module=self.LOG_MODULE,
                )
                await speaker.wake_up(awake=True, silent=True)
                result = await asyncio.wait_for(
                    self._xiaoai_asr_future,
                    timeout=wait_seconds,
                )
                if result == self.XIAOAI_ASR_TIMEOUT:
                    logger.debug(
                        "XiaoAI native ASR ended without speech (native timeout), retrying",
                        module=self.LOG_MODULE,
                    )
                    continue
                return result
            except asyncio.TimeoutError:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None
            finally:
                self._xiaoai_asr_future = None

        return None

    def _resolve_xiaoai_asr_future(self, text: str):
        """Resolve the pending XiaoAI ASR future on the owner loop."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.set_result, text)

    def _cancel_xiaoai_asr_future(self):
        """Cancel any pending XiaoAI ASR future."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.cancel)
        self._xiaoai_asr_future = None

    async def _stop_xiaoai_native_listening(self):
        """Exit XiaoAI native listening before playing the goodbye prompt."""
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.stop_device_audio()
            await speaker.wake_up(awake=False)
            await asyncio.sleep(0.15)
        except Exception as exc:
            logger.debug(
                f"Failed to stop XiaoAI native listening: {exc}",
                module=self.LOG_MODULE,
            )

    async def _wait_for_silence(self, vad):
        """Wait until the environment is silent before starting to listen.

        Uses VAD silence detection to confirm the speaker has stopped playing.
        If speech is detected (e.g. TTS tail), keeps retrying silence detection.
        """
        from core.wakeup_session import EventManager

        future = self._loop.create_future()
        original_on_silence = EventManager.on_silence
        original_on_speech = EventManager.on_speech

        def _on_silence():
            if not future.done():
                self._loop.call_soon_threadsafe(future.set_result, True)

        def _on_speech(_speech_buffer: bytes):
            # Still hearing sound — switch back to silence detection
            vad.resume("silence")

        EventManager.on_silence = _on_silence
        EventManager.on_speech = _on_speech
        try:
            vad.resume("silence")
            await asyncio.wait_for(future, timeout=1)
        except asyncio.TimeoutError:
            vad.pause()
        finally:
            EventManager.on_speech = original_on_speech
            EventManager.on_silence = original_on_silence

    # ---- Recording control (physical mute/unmute via remote arecord) ----

    async def _stop_recording(self):
        """Kill the remote arecord process so the mic doesn't pick up TTS."""
        self._set_recording_wanted(False)
        try:
            await open_xiaoai_server.stop_recording()
            logger.debug("Recording stopped", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"stop_recording error: {exc}", module=self.LOG_MODULE)

    async def _start_recording(self):
        """Restart the remote arecord process to resume mic input."""
        self._set_recording_wanted(True)
        try:
            await open_xiaoai_server.start_recording()
            logger.debug("Recording started", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"start_recording error: {exc}", module=self.LOG_MODULE)

    # ---- TTS ----

    async def _play_reply_and_resume(self, text: str, vad=None):
        """播报回答，结束后恢复下一轮监听。

        回采在线时不关麦，播报中喊唤醒词可以打断：直接进入下一轮，不播提示音。
        否则关麦播报，播完放提示音再开麦。
        """
        barge_in = self._barge_in_ready()
        interrupted = False
        if not barge_in:
            await self._stop_recording()
        try:
            self._light_state("speaking")
            if barge_in:
                interrupted = await self._play_tts_interruptible(text)
            else:
                await self._play_tts(text)
        finally:
            if self.active and not interrupted:
                self._light_state("listening")
                await self._play_notify()
                if not barge_in:
                    await self._start_recording()
        if interrupted or not self.active:
            return
        if vad is not None:
            await self._wait_for_silence(vad)
        logger.debug("Ready for next conversation round", module=self.LOG_MODULE)

    async def _play_tts_interruptible(self, text: str) -> bool:
        """边播报边检测唤醒词；被打断返回 True。"""
        kws = get_kws()
        hit = self._loop.create_future()

        def on_hit(keyword, position):  # KWS 线程
            self._loop.call_soon_threadsafe(self._on_barge_in_hit, hit, keyword, position)

        tts = asyncio.create_task(self._play_tts(text))
        kws.arm_barge_in(on_hit)
        try:
            await asyncio.wait({tts, hit}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            tts.cancel()
            raise
        finally:
            kws.disarm_barge_in()
        if not hit.done() or hit.cancelled():
            hit.cancel()
            return False

        keyword, position = hit.result()
        # 先切灯让用户马上看到打断生效，再停播报
        self._light_state("listening")
        token = self._playback_token
        if token is not None:
            open_xiaoai_server.stop_tts_playback(token)
        tts.cancel()
        await asyncio.gather(tts, return_exceptions=True)
        speaker = get_speaker()
        if speaker:
            self._background(speaker.stop_device_audio())
        lookback = int(self._barge_cfg("speech_lookback_ms", 350)) * 16
        self._barge_in_from = max(0, int(position) - lookback)
        logger.info(f"✋ 打断播报: {keyword}", module=self.LOG_MODULE)
        return True

    def _on_barge_in_hit(self, hit: asyncio.Future, keyword: str, position: int):
        """播报中 KWS 命中：确认像真人在喊（near_end_min_db）再打断。"""
        if hit.done():
            return
        xiaoai = get_xiaoai()
        ratio_fn = getattr(xiaoai, "near_end_ratio_db", None)
        ratio = ratio_fn() if ratio_fn else None
        min_db = self._barge_cfg("near_end_min_db", None)
        ratio_text = "-" if ratio is None else f"{ratio:.1f} dB"
        if min_db is not None and ratio is not None and ratio < float(min_db):
            logger.info(
                f"🙉 忽略疑似音箱自己的声音: {keyword}（近端 {ratio_text} < {min_db} dB）",
                module=self.LOG_MODULE,
            )
            return
        logger.info(f"Barge-in: {keyword}, near-end {ratio_text}", module=self.LOG_MODULE)
        hit.set_result((keyword, position))

    async def _play_tts(self, text: str):
        """Play text via Doubao TTS (blocks until playback finishes)."""
        self._playback_token = open_xiaoai_server.begin_playback_session()
        try:
            await self.backend._play_response_with_tts(
                text,
                tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                playback_token=self._playback_token,
            )
        except Exception as exc:
            logger.error(
                f"TTS playback error: {exc}",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text=text)
        finally:
            self._playback_token = None

    async def _play_notify(self):
        """Play the listening-ready notification sound via PCM buffer."""
        if not _NOTIFY_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_NOTIFY_PCM)
                # Wait for playback to finish: PCM is int16 at 24000Hz
                duration = len(_NOTIFY_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Notify sound error: {exc}", module=self.LOG_MODULE)

    async def _play_send_sound(self):
        """Play the send notification before waiting for backend response."""
        if not _SEND_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_SEND_PCM)
                # 等待播放完成：PCM 为 int16，24000Hz
                duration = len(_SEND_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Send sound error: {exc}", module=self.LOG_MODULE)

    async def _call_after_wakeup(self):
        """Call the user-defined after_wakeup hook.

        The hook is user-supplied config; guard it so a bug in the callback
        (e.g. an IndexError while parsing session_key) never tears down the
        conversation exit flow.
        """
        after_wakeup = self.config.get_app_config("wakeup.after_wakeup")
        if after_wakeup:
            speaker = get_speaker()
            if speaker:
                try:
                    await after_wakeup(
                        speaker,
                        source=self.WAKEUP_SOURCE,
                        session_key=self.backend._session_key,
                    )
                except Exception as exc:
                    import traceback
                    logger.error(
                        f"after_wakeup hook error: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        module=self.LOG_MODULE,
                    )
