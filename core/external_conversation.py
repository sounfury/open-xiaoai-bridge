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
  - TTS playback is blocking (awaited), so the next listening round
    only starts after the response has finished playing.
"""

import asyncio
import os

import open_xiaoai_server

from core.ref import get_speaker, get_vad
from core.services.listening_light import ListeningLight
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
        self._light_token: object | None = None
        self._light_refresh_task: asyncio.Task | None = None

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
            await self._turn_off_light()

    def stop(self):
        """Exit conversation mode and clean up."""
        if not self.active:
            return
        self.active = False
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        self._release_light_soon()
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

    async def _turn_on_light(self):
        """开启本轮监听灯。"""
        await self._set_light_phase("listening")

    async def _set_light_phase(self, phase: str):
        """切换监听、思考和回答灯效，释放上一阶段的灯效凭据。"""
        await self._turn_off_light()
        light = self._cfg("listening_light", {}) or {}
        if not isinstance(light, dict):
            return
        if not light.get("enabled", False):
            return
        if phase == "listening" and self.uses_xiaoai_asr():
            return
        if phase != "listening" and not self.active:
            return
        key = "effect_id" if phase == "listening" else f"{phase}_effect_id"
        effect_id = light.get(key, 9 if phase == "listening" else None)
        self._light_token = await ListeningLight.turn_on(
            get_speaker(), effect_id
        )
        if phase == "speaking" and self._light_token is not None:
            self._light_refresh_task = asyncio.create_task(
                self._refresh_speaking_light(self._light_token)
            )

    async def _refresh_speaking_light(self, token: object):
        """播报期间维持回答灯，设备自行熄灯后最迟半秒恢复。"""
        while self.active and self._light_token is token:
            await asyncio.sleep(0.5)
            if not self.active or self._light_token is not token:
                return
            if not await ListeningLight.refresh(token):
                return

    async def _turn_off_light(self):
        """释放本次监听持有的灯效，允许正常结束和中断路径重复清理。"""
        token, self._light_token = self._light_token, None
        refresh_task, self._light_refresh_task = self._light_refresh_task, None
        if refresh_task is not None:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        await ListeningLight.turn_off(token)

    def _release_light_soon(self):
        """同步路径（如 stop()，可能在其他线程调用）立即提交熄灯，不等待采集协程收尾。"""
        token, self._light_token = self._light_token, None
        refresh_task, self._light_refresh_task = self._light_refresh_task, None
        if refresh_task is not None and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(refresh_task.cancel)
        if token is not None and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(ListeningLight.turn_off(token), self._loop)

    # ---- conversation loop ----

    async def _conversation_loop(self):
        """Run VAD -> ASR -> backend -> TTS turns until exit."""

        # Mute mic → play notify → unmute.
        # _play_notify() blocks for ~740ms (the beep duration), during which
        # the mic is off and before_wakeup TTS echo naturally fades.
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        await self._stop_recording()
        logger.debug("Recording stopped", module=self.LOG_MODULE)
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
            "interrupted" - stop() was called while the light was turning on
            "error"    - unrecoverable error
        """
        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error"

        # 1. Start listening for speech (recording is already active)
        try:
            await self._turn_on_light()
            if not self.active:
                return "interrupted"
            speech_bytes = await self._wait_for_speech(vad)
        finally:
            # 录音结束后释放监听灯，后续阶段按需切换灯效。
            await self._turn_off_light()
        if speech_bytes is None:
            return "timeout"

        try:
            await self._set_light_phase("thinking")
            logger.debug(
                f"Got speech buffer: {len(speech_bytes)} bytes",
                module=self.LOG_MODULE,
            )

            # 2. ASR: convert speech to text
            from core.services.audio.asr import ASRService

            text = ASRService.asr(speech_bytes, sample_rate=16000)
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
        finally:
            await self._turn_off_light()

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

        try:
            await self._set_light_phase("thinking")
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
        finally:
            await self._turn_off_light()

    # ---- VAD integration ----

    async def _wait_for_speech(self, vad) -> bytes | None:
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
            vad.resume("speech")
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
        try:
            await open_xiaoai_server.stop_recording()
            logger.debug("Recording stopped", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"stop_recording error: {exc}", module=self.LOG_MODULE)

    async def _start_recording(self):
        """Restart the remote arecord process to resume mic input."""
        try:
            await open_xiaoai_server.start_recording()
            logger.debug("Recording started", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"start_recording error: {exc}", module=self.LOG_MODULE)

    # ---- TTS ----

    async def _play_reply_and_resume(self, text: str, vad=None):
        """关麦播报正常或失败回答，切换回答灯，播放结束后恢复下一轮监听。"""
        await self._stop_recording()
        try:
            await self._set_light_phase("speaking")
            await self._play_tts(text)
        finally:
            await self._turn_off_light()
            if self.active:
                await self._play_notify()
                await self._start_recording()
        if self.active and vad is not None:
            await self._wait_for_silence(vad)
        logger.debug("Ready for next conversation round", module=self.LOG_MODULE)

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
