import asyncio
import json
import os
import threading
import time
import wave

import numpy as np
import open_xiaoai_server

from core.ref import get_speaker, set_xiaoai
from core.services.audio.stream import GlobalStream
from core.services.speaker import SpeakerManager
from core.wakeup_session import EventManager
from core.xiaoai_conversation import XiaoAIConversationController
from core.utils.base import json_decode
from core.utils.config import ConfigManager
from core.utils.logger import logger

ASCII_BANNER = """
▄▖      ▖▖▘    ▄▖▄▖
▌▌▛▌█▌▛▌▚▘▌▀▌▛▌▌▌▐ 
▙▌▙▌▙▖▌▌▌▌▌█▌▙▌▛▌▟▖
  ▌                
                                                                                                                
"""

# 音箱录音带播放回采（OwnDing 客户端的 echo_ref）：一个麦克风 + 回采声道。
# OH2P 的 hw:0,3（共享名 Capture）4 声道，第 4 声道是播放回采。
# 采样率必须是线上 16 kHz 的 3 倍；shift 是 S32 → S16 的右移位数。
DEFAULT_ECHO_CAPTURE = {
    "pcm": "Capture",
    "channels": 4,
    "sample_rate": 48000,
    "mic_channel": 0,
    "ref_channel": 3,
    "mic_shift": 14,
    "ref_shift": 16,
}


class XiaoAI:
    speaker = SpeakerManager()
    async_loop: asyncio.AbstractEventLoop = None
    config_manager = ConfigManager.instance()
    conversation = XiaoAIConversationController()
    _input_gain_enabled = False
    _input_gain = 1.0
    # 双声道录音里麦克风声道的电平比单声道录音低，乘这个倍数回到原来的电平
    _echo_mic_gain = 64.0
    _echo_dump: dict | None = None
    _async_loop_ready = threading.Event()
    _external_wakeup_keywords: set[str] = set()
    _muse_wakeup_keywords: set[str] = set()
    _suppressed_dialog_ids: set[str] = set()
    _suppressed_dialog_last_attempt: dict[str, float] = {}
    # dialog_id of the latest final command passed to before_wakeup (used by core/muse.py)
    last_recognize_dialog_id: str = ""
    _MAX_SUPPRESSED_DIALOGS = 100
    _SUPPRESS_RETRY_INTERVAL = 0.35

    @classmethod
    def refresh_runtime_config(cls, *_args):
        """从配置中心同步运行时参数。"""
        cls.conversation.apply_runtime_config(
            cls.config_manager.get_app_config("xiaoai", {})
        )
        gain = cls.config_manager.get_app_config("audio_input.gain", 1.0)
        try:
            gain = float(gain)
        except (TypeError, ValueError):
            gain = 1.0
        cls._input_gain = max(1.0, min(gain, 8.0))
        cls._input_gain_enabled = cls._input_gain > 1.0
        wakeup_keywords = cls.config_manager.get_app_config("wakeup.keywords", [])
        cls._external_wakeup_keywords = {
            cls._normalize_text(keyword)
            for keyword in wakeup_keywords
            if isinstance(keyword, str) and cls._normalize_text(keyword)
        }
        # 小爱管家的 KWS 唤醒词在生成 keywords.txt 时才合并（MUSE_ENABLE=1），这里同步一份，
        # 用缪斯自己的归一化（忽略全半角 / 大小写 / 标点）比较，例如原生 ASR 给出 "Hey, Muse."
        from core.muse import kws_wake_words, normalize_wake_text

        cls._muse_wakeup_keywords = {
            normalize_wake_text(keyword)
            for keyword in kws_wake_words(cls.config_manager.get_app_config)
            if normalize_wake_text(keyword)
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            return ""
        return "".join(text.strip().lower().split())

    @classmethod
    def _is_external_wakeup_text(cls, text: str) -> bool:
        normalized = cls._normalize_text(text)
        if not normalized:
            return False
        if normalized in cls._external_wakeup_keywords:
            return True
        if cls._muse_wakeup_keywords:
            from core.muse import normalize_wake_text

            return normalize_wake_text(text) in cls._muse_wakeup_keywords
        return False

    @classmethod
    async def _suppress_dialog(cls, dialog_id: str, reason: str):
        if not dialog_id:
            return

        # Prevent unbounded growth: if too many stale dialog_ids accumulated
        # (missed Dialog.Finish events), clear them all before adding new one
        if len(cls._suppressed_dialog_ids) >= cls._MAX_SUPPRESSED_DIALOGS:
            logger.debug(
                f"[XiaoAI] Clearing {len(cls._suppressed_dialog_ids)} stale suppressed dialog_ids"
            )
            cls._suppressed_dialog_ids.clear()
            cls._suppressed_dialog_last_attempt.clear()

        is_new_dialog = dialog_id not in cls._suppressed_dialog_ids
        cls._suppressed_dialog_ids.add(dialog_id)

        if is_new_dialog:
            logger.info(
                f"[XiaoAI] 🛑 停止小爱当前对话: {reason}"
            )

        now = time.monotonic()
        last_attempt = cls._suppressed_dialog_last_attempt.get(dialog_id, 0.0)
        if not is_new_dialog and (now - last_attempt) < cls._SUPPRESS_RETRY_INTERVAL:
            return

        cls._suppressed_dialog_last_attempt[dialog_id] = now
        try:
            await cls.speaker.run_shell(
                "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"
            )
            if is_new_dialog:
                await cls.speaker.wake_up(awake=False)
        except Exception as exc:
            logger.debug(
                f"[XiaoAI] Failed to pause suppressed dialog {dialog_id}: {exc}"
            )

    @classmethod
    def on_input_packet(cls, packet):
        """新客户端的录音帧：(PCM bytes, metadata JSON)。

        双声道 [麦克风, 播放回采]（layout=mic_ref）时，阶段 1 只取麦克风声道，
        回声消除在阶段 2 接入。
        """
        data, metadata = packet
        try:
            layout = json.loads(metadata).get("layout") if metadata else None
        except (TypeError, ValueError, AttributeError):
            layout = None
        if layout == "mic_ref":
            frames = np.frombuffer(data, dtype="<i2").reshape(-1, 2)
            if cls._echo_dump is not None:
                cls._append_echo_dump(frames)
            mic = frames[:, 0].astype(np.float32) * cls._echo_mic_gain
            data = np.clip(np.round(mic), -32768, 32767).astype(np.int16).tobytes()
        cls.on_input_data(data)

    @classmethod
    def _setup_echo_reference(cls):
        """barge_in.echo_reference 打开时，请音箱同时发回播放回采。"""
        barge_in = cls.config_manager.get_app_config("barge_in", {}) or {}
        enabled = bool(barge_in.get("echo_reference", False))
        override = os.environ.get("BARGE_IN_ECHO_REFERENCE", "").strip().lower()
        if override:
            enabled = override in ("1", "true", "yes", "on")
        if not enabled:
            return
        if not hasattr(open_xiaoai_server, "set_echo_ref"):
            logger.warning("[XiaoAI] 原生扩展不支持 echo_ref，请重新编译 native")
            return
        capture = {**DEFAULT_ECHO_CAPTURE, **(barge_in.get("capture") or {})}
        try:
            cls._echo_mic_gain = float(barge_in.get("echo_mic_gain", 64))
        except (TypeError, ValueError):
            cls._echo_mic_gain = 64.0
        open_xiaoai_server.set_echo_ref(json.dumps(capture))
        logger.info(f"[XiaoAI] 🔁 请音箱同时发送播放回采: {capture}")

        # 调试：ECHO_REF_DUMP=<文件.wav> 把开头若干秒的原始双声道录音存下来，用于确认回采声道
        dump_path = os.environ.get("ECHO_REF_DUMP", "").strip()
        if dump_path:
            try:
                seconds = float(os.environ.get("ECHO_REF_DUMP_SECONDS", "30"))
            except ValueError:
                seconds = 30.0
            cls._echo_dump = {
                "path": dump_path,
                "max_frames": int(seconds * 16000),
                "chunks": [],
                "frames": 0,
            }
            logger.info(f"[XiaoAI] 将保存 {seconds:.0f}s 原始双声道录音到 {dump_path}")

    @classmethod
    def _append_echo_dump(cls, frames):
        dump = cls._echo_dump
        dump["chunks"].append(frames.copy())
        dump["frames"] += len(frames)
        if dump["frames"] < dump["max_frames"]:
            return
        cls._echo_dump = None
        audio = np.concatenate(dump["chunks"])[: dump["max_frames"]]

        def _write():
            try:
                with wave.open(dump["path"], "wb") as f:
                    f.setnchannels(2)
                    f.setsampwidth(2)
                    f.setframerate(16000)
                    f.writeframes(audio.astype("<i2").tobytes())
                logger.info(f"[XiaoAI] 原始双声道录音已保存: {dump['path']}")
            except Exception as exc:
                logger.error(f"[XiaoAI] 保存双声道录音失败: {exc}")

        threading.Thread(target=_write, daemon=True).start()

    @classmethod
    def on_input_data(cls, data: bytes):
        audio_array = np.frombuffer(data, dtype=np.int16)
        if cls._input_gain_enabled and audio_array.size > 0:
            boosted = audio_array.astype(np.float32) * cls._input_gain
            audio_array = np.clip(boosted, -32768, 32767).astype(np.int16)
        GlobalStream.input(audio_array.tobytes())

    @classmethod
    def on_output_data(cls, data: bytes):
        async def on_output_data_async(data: bytes):
            return await open_xiaoai_server.on_output_data(data)

        asyncio.run_coroutine_threadsafe(
            on_output_data_async(data),
            cls.async_loop,
        )

    @classmethod
    async def run_shell(cls, script: str, timeout: float = 10 * 1000):
        return await open_xiaoai_server.run_shell(script, timeout)

    @classmethod
    async def on_event(cls, event: str):
        event_json = json_decode(event) or {}
        if not isinstance(event_json, dict):
            logger.debug("[XiaoAI] 忽略非字典事件负载")
            return

        event_data = event_json.get("data", {})
        event_type = event_json.get("event")

        if not event_json.get("event"):
            return

        # 记录所有事件用于调试监听退出
        logger.debug(f"[XiaoAI] 📡 收到事件: {event_type} | 数据: {event_data}")

        if event_type == "instruction":
            if not isinstance(event_data, dict):
                logger.debug(
                    f"[XiaoAI] 忽略非字典 instruction 数据: {event_data}"
                )
                return

            raw_line = event_data.get("NewLine")
            if not raw_line:
                return

            line = json_decode(raw_line) if isinstance(raw_line, str) else raw_line
            if not isinstance(line, dict):
                logger.debug(f"[XiaoAI] 忽略无法解析的指令行: {raw_line}")
                return

            header = line.get("header", {}) if isinstance(line.get("header"), dict) else {}
            dialog_id = header.get("dialog_id", "")
            namespace = header.get("namespace")
            header_name = header.get("name")

            if dialog_id and dialog_id in cls._suppressed_dialog_ids:
                if namespace in {"Nlp", "SpeechSynthesizer", "AudioPlayer"}:
                    await cls._suppress_dialog(
                        dialog_id,
                        f"{namespace}.{header_name}",
                    )
                    return
                if namespace == "Dialog" and header_name == "Finish":
                    cls._suppressed_dialog_ids.discard(dialog_id)
                    cls._suppressed_dialog_last_attempt.pop(dialog_id, None)
                    logger.debug(
                        f"[XiaoAI] Cleared suppressed dialog: {dialog_id}"
                    )
                    return

            if (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "SpeechRecognizer"
            ):
                if header_name == "RecognizeResult":
                    payload = line.get("payload", {})
                    if not isinstance(payload, dict):
                        logger.debug(
                            f"[XiaoAI] 忽略非字典 RecognizeResult payload: {payload}"
                        )
                        return

                    results = payload.get("results") or []
                    first_result = results[0] if results else {}
                    if isinstance(first_result, dict):
                        text = first_result.get("text") or ""
                    elif isinstance(first_result, str):
                        text = first_result
                    else:
                        text = ""
                    is_final = payload.get("is_final")
                    is_vad_begin = payload.get("is_vad_begin")

                    if EventManager.consume_xiaoai_asr_result(
                        dialog_id=dialog_id,
                        text=text,
                        is_final=is_final,
                        is_vad_begin=is_vad_begin,
                    ):
                        if dialog_id and text and is_final:
                            await cls._suppress_dialog(
                                dialog_id,
                                f"Bridge 接管原生 ASR",
                            )
                        return
                    
                    # 只有明确的 is_vad_begin=False 且没有文本时才触发唤醒
                    # 避免重复触发
                    if not text and is_vad_begin is False:
                        logger.wakeup("小爱同学", module="XiaoAI")
                        cls.conversation.reset_retries()
                        EventManager.on_interrupt()
                    elif text and is_final and cls._is_external_wakeup_text(text):
                        await cls._suppress_dialog(
                            dialog_id,
                            f"外部唤醒词接管: {text}",
                        )
                        await EventManager.wakeup(text, "kws")
                        return
                    elif text and is_final:
                        logger.info(f"[XiaoAI] 🔥 收到指令: {text}")
                        cls.last_recognize_dialog_id = dialog_id
                        cls.conversation.reset_retries()
                        await cls.conversation.handle_text_command(
                            text,
                            get_speaker(),
                        )
                        await EventManager.wakeup(text, "xiaoai")
                    elif is_final and not text:
                        logger.debug("[XiaoAI] 🛑 小爱监听超时自动退出")
                        await cls.conversation.handle_listening_timeout(
                            get_speaker()
                        )
            elif (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "AudioPlayer"
            ):
                cls.conversation.handle_audio_player_instruction(header_name)
        elif event_type == "playing":
            if not isinstance(event_data, str):
                logger.debug(
                    f"[XiaoAI] 忽略非字符串 playing 数据: {event_data}"
                )
                return

            playing_status = event_data.lower()
            
            get_speaker().status = playing_status
            await cls.conversation.handle_playing_status(
                playing_status,
                get_speaker(),
            )
        
        else:
            # 记录未处理的事件类型，可能包含监听退出信息
            logger.debug(f"[XiaoAI] ❓ 未处理的事件类型: {event_type} | 完整数据: {event_json}")

    @classmethod
    def __init_background_event_loop(cls):
        def run_event_loop():
            cls.async_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.async_loop)
            cls._async_loop_ready.set()
            cls.async_loop.run_forever()

        thread = threading.Thread(target=run_event_loop, daemon=True)
        thread.start()
        cls._async_loop_ready.wait(timeout=2)

    @classmethod
    def __on_event(cls, event: str):
        future = asyncio.run_coroutine_threadsafe(
            cls.on_event(event),
            cls.async_loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(
                    f"[XiaoAI] Event handler failed: {type(exc).__name__}: {exc}"
                )

        future.add_done_callback(_log_result)

    @classmethod
    async def init_xiaoai(cls):
        cls.refresh_runtime_config()
        cls.config_manager.add_reload_listener(cls.refresh_runtime_config)
        set_xiaoai(XiaoAI)
        GlobalStream.on_output_data = cls.on_output_data
        cls._setup_echo_reference()
        open_xiaoai_server.register_fn("on_input_data", cls.on_input_data)
        open_xiaoai_server.register_fn("on_input_packet", cls.on_input_packet)
        open_xiaoai_server.register_fn("on_event", cls.__on_event)
        cls.__init_background_event_loop()
        logger.info("[XiaoAI] 启动小爱音箱服务...")
        print(ASCII_BANNER)
        await open_xiaoai_server.start_server()

    @classmethod
    def stop_conversation(cls):
        cls.conversation.stop()
