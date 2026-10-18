"""OpenAI 兼容服务管理：连接 Hermes 等后端，收发对话消息并播放回复。

This module talks to any OpenAI-compatible Chat Completions endpoint,
including Hermes Agent API server mode.
"""

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

import aiohttp

from core.utils.base import get_env
from core.utils.config import ConfigManager
from core.utils.logger import logger


class OpenAIManager:
    """Manager for OpenAI-compatible chat backends."""

    XIAOAI_TTS_SPEAKER = "xiaoai"

    _initialized = False
    _reload_listener_registered = False
    _enabled = False
    _base_url = "http://127.0.0.1:8000/v1"
    _api_key = ""
    _model = "gpt-4o-mini"
    _session_key = "agent:default:open-xiaoai-bridge"
    # Optional header used to send session_key to the server (e.g. Hermes'
    # "X-Hermes-Session-Key" for long-term memory scoping). Standard OpenAI /
    # Ollama / LM Studio ignore the unknown header; set empty to disable.
    _session_header = "X-Hermes-Session-Key"
    _system_prompt = ""
    _temperature: float | None = None
    _max_tokens: int | None = None
    _timeout = 120
    _history_max_messages = 20
    # 连续对话用流式回复（SSE）：第一句出来就播，还能收到 Hermes 的工具进度事件
    _stream = True
    _extra_body: dict[str, Any] = {}
    _tts_provider: str | None = None
    _tts_speaker = None
    _session_tts_speakers: dict[str, str] = {}
    _tts_speed = 1.0
    _rule_prompt = ""
    _rule_prompt_for_skill = ""
    _sessions: dict[str, list[dict[str, str]]] = {}
    _response_events: dict[str, asyncio.Future] = {}
    _response_texts: dict[str, str] = {}
    _response_tts_speakers: dict[str, str | None] = {}
    last_error: str | None = None

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        logger.info("[OpenAI] Initializing from config...")
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """Refresh OpenAI settings from config.py."""
        config_manager = ConfigManager.instance()
        if not cls._reload_listener_registered:
            config_manager.add_reload_listener(
                lambda _old, _new: cls.reload_from_config()
            )
            cls._reload_listener_registered = True

        config = config_manager.get_app_config("openai", {})

        if enabled is not None:
            cls._enabled = enabled
        else:
            env_enabled = get_env("OPENAI_ENABLE")
            cls._enabled = (
                env_enabled.lower() in ("1", "true", "yes")
                if env_enabled is not None
                else False
            )

        # Env overrides (OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL) beat config.py
        # so secrets can live in start-bridge.bat / .hermes_api_key without editing the repo.
        cls._base_url = str(
            get_env("OPENAI_BASE_URL")
            or config.get("base_url", "http://127.0.0.1:8000/v1")
        ).rstrip("/")
        cls._api_key = str(
            get_env("OPENAI_API_KEY")
            or config.get("api_key", "")
            or ""
        )
        cls._model = str(
            get_env("OPENAI_MODEL")
            or config.get("model", "gpt-4o-mini")
        )
        cls._session_key = str(config.get("session_key", "agent:default:open-xiaoai-bridge"))
        cls._session_header = str(config.get("session_header", "X-Hermes-Session-Key") or "").strip()
        cls._system_prompt = str(config.get("system_prompt", "") or "")
        cls._timeout = int(config.get("response_timeout", 120))
        cls._history_max_messages = max(0, int(config.get("history_max_messages", 20)))
        cls._stream = bool(config.get("stream", True))
        cls._temperature = cls._optional_float(config.get("temperature"))
        cls._max_tokens = cls._optional_int(config.get("max_tokens"))
        cls._extra_body = config.get("extra_body", {})
        if not isinstance(cls._extra_body, dict):
            cls._extra_body = {}
        configured_provider = config.get("tts_provider")
        cls._tts_provider = (
            str(configured_provider).strip().lower()
            if configured_provider
            else None
        )
        cls._tts_speaker = config.get("tts_speaker", None)
        cls._session_tts_speakers = (
            {
                str(key): str(value)
                for key, value in config.get("session_tts_speakers", {}).items()
                if key and value
            }
            if isinstance(config.get("session_tts_speakers", {}), dict)
            else {}
        )
        cls._tts_speed = float(config.get("tts_speed", 1.0))
        cls._rule_prompt = str(config.get("rule_prompt", "") or "")
        cls._rule_prompt_for_skill = str(config.get("rule_prompt_for_skill", "") or "")

        if cls._enabled:
            logger.info(
                f"[OpenAI] Enabled, base_url={cls._base_url}, model={cls._model}"
            )

    @classmethod
    async def connect(cls) -> bool:
        """No-op connect hook for parity with WebSocket backends."""
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    async def close(cls):
        """Cancel pending response waiters."""
        for waiter in list(cls._response_events.values()):
            if not waiter.done():
                waiter.cancel()
        cls._response_events.clear()
        cls._response_texts.clear()
        cls._response_tts_speakers.clear()

    @classmethod
    def is_connected(cls) -> bool:
        return cls.is_enabled()

    @classmethod
    def is_enabled(cls) -> bool:
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    def supports_streaming(cls) -> bool:
        """连续对话是否走 stream_reply()。"""
        return cls.is_enabled() and cls._stream

    @classmethod
    async def stream_reply(cls, text: str) -> AsyncIterator[tuple[str, Any]]:
        """流式请求一轮对话，边收边产出事件：

        - ("text", str)：正文增量（delta.content；模型的 reasoning_content 不要）
        - ("tool", dict)：Hermes 的 `event: hermes.tool.progress`，
          {"tool", "emoji", "label", "toolCallId", "status": "running" | "completed"}

        结束后（包括调用方中途停止 / 被取消）把已经收到的回复记进历史。
        请求失败时抛异常（last_error 同步设置）。
        """
        if not cls._initialized:
            cls.initialize_from_config()
        if not cls._enabled:
            raise RuntimeError("OpenAI backend is disabled")

        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])
        payload = cls._build_payload(history, text, stream=True)
        module = f"OpenAI({session_key})"
        logger.user_speech(text, module=module)
        cls.last_error = None
        parts: list[str] = []
        completed = False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cls._timeout)
            ) as session:
                async with session.post(
                    cls._chat_completions_url(), json=payload, headers=cls._headers()
                ) as response:
                    content_type = response.headers.get("Content-Type", "unknown")
                    if response.status >= 400:
                        raise RuntimeError(
                            f"HTTP {response.status}: 后端服务请求失败 (Content-Type={content_type})"
                        )
                    if "text/event-stream" not in content_type:
                        # 服务端不支持流式，直接回了完整 JSON
                        try:
                            body = await response.json(content_type=None)
                        except ValueError as exc:
                            raise RuntimeError(
                                f"HTTP {response.status}: 后端返回了非 JSON 响应 (Content-Type={content_type})"
                            ) from exc
                        reply = cls._extract_response_text(body)
                        completed = True
                        if reply:
                            parts.append(reply)
                            yield ("text", reply)
                        return

                    event = None
                    async for raw in response.content:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            event = None  # 一个 SSE 事件结束
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event = line[6:].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            completed = True
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            logger.debug(f"[OpenAI] Skipping non-JSON SSE data: {data[:80]}")
                            continue
                        if event == "hermes.tool.progress":
                            if isinstance(obj, dict):
                                yield ("tool", obj)
                            continue
                        if isinstance(obj, dict) and obj.get("error"):
                            raise RuntimeError(f"后端返回错误: {obj['error']}")
                        delta = cls._extract_delta_text(obj)
                        if delta:
                            parts.append(delta)
                            yield ("text", delta)
                    else:
                        completed = True  # 连接正常结束但没有 [DONE]
        except asyncio.CancelledError:
            raise
        except GeneratorExit:
            raise
        except Exception as exc:
            cls.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[OpenAI] Streaming chat completion failed: {cls.last_error}")
            raise
        finally:
            reply = "".join(parts).strip()
            if reply:
                cls._append_history(history, text, reply)
                if completed:
                    logger.ai_response(reply, module=module)
                else:
                    logger.info(f"[OpenAI] Reply stopped early: {reply[:60]}")

    @classmethod
    def set_session_key(cls, session_key: str):
        logger.info(
            f"[OpenAI] Session key updated: {cls._session_key!r} -> {session_key!r}"
        )
        cls._session_key = session_key

    @classmethod
    def reset_session(cls, session_key: str | None = None):
        cls._sessions.pop(session_key or cls._session_key, None)

    @classmethod
    def get_tts_speaker_for_session_key(cls, session_key: str | None = None) -> str | None:
        target_session_key = session_key or cls._session_key
        return cls._session_tts_speakers.get(target_session_key) or cls._tts_speaker

    @classmethod
    async def send(cls, text: str, wait_response: bool = False) -> str | None:
        run_id = await cls._send_and_track(text)
        if run_id is None:
            return None
        if not wait_response:
            asyncio.create_task(cls._wait_response(run_id))
            return run_id
        try:
            return await cls._wait_response(run_id)
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    @classmethod
    async def send_and_play_reply(cls, text: str, wait_response: bool = False) -> str | None:
        run_id = await cls._send_and_track(text)
        if run_id is None:
            return None
        if not wait_response:
            asyncio.create_task(cls._wait_and_play_response(run_id))
            return run_id
        try:
            response_text = await cls._wait_response(run_id)
            if response_text:
                await cls._play_response_with_tts(
                    response_text,
                    tts_speaker=cls._response_tts_speakers.get(run_id),
                )
            return response_text
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    # ===== 私有方法 =====

    @classmethod
    def _optional_float(cls, value) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

    @classmethod
    def _optional_int(cls, value) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @classmethod
    def _resolve_tts_provider(cls, tts_speaker: str | None) -> str:
        """Resolve an explicit provider while preserving the legacy speaker rule."""
        from core.services.tts.router import TTSRouter

        return TTSRouter.resolve_provider(cls._tts_provider, tts_speaker)

    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        if not cls._initialized:
            cls.initialize_from_config()
        if not cls._enabled:
            logger.warning("[OpenAI] send called but backend is disabled")
            return None

        run_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        cls._response_events[run_id] = loop.create_future()
        cls._response_texts[run_id] = ""
        cls._response_tts_speakers[run_id] = cls.get_tts_speaker_for_session_key()
        logger.user_speech(text, module=f"OpenAI({cls._session_key})")
        asyncio.create_task(cls._run_chat_completion(run_id, text))
        return run_id

    @classmethod
    async def _wait_response(cls, run_id: str) -> str | None:
        event = cls._response_events.get(run_id)
        if not event:
            logger.warning(f"[OpenAI] No event found for run {run_id}")
            return None
        try:
            await asyncio.wait_for(event, timeout=cls._timeout)
            return cls._response_texts.pop(run_id, "") or None
        except asyncio.TimeoutError:
            logger.warning(f"[OpenAI] Timeout waiting for response (runId: {run_id})")
            return None
        finally:
            cls._response_events.pop(run_id, None)
            cls._response_texts.pop(run_id, None)

    @classmethod
    async def _run_chat_completion(cls, run_id: str, text: str):
        try:
            cls.last_error = None
            response_text = await cls._request_chat_completion(text)
            if response_text:
                cls._response_texts[run_id] = response_text
                logger.ai_response(response_text, module=f"OpenAI({cls._session_key})")
        except Exception as exc:
            cls.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[OpenAI] Chat completion failed: {cls.last_error}")
        finally:
            waiter = cls._response_events.get(run_id)
            if waiter and not waiter.done():
                waiter.get_loop().call_soon_threadsafe(waiter.set_result, None)

    @classmethod
    async def _request_chat_completion(cls, text: str) -> str | None:
        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])
        payload = cls._build_payload(history, text, stream=False)
        headers = cls._headers()

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=cls._timeout)
        ) as session:
            async with session.post(
                cls._chat_completions_url(),
                json=payload,
                headers=headers,
            ) as response:
                if response.status >= 400:
                    # 网关 502 等错误通常是 HTML，先检查状态，避免被 JSON 错误掩盖。
                    raise RuntimeError(
                        f"HTTP {response.status}: 后端服务请求失败 "
                        f"(Content-Type={response.headers.get('Content-Type', 'unknown')})"
                    )
                try:
                    body = await response.json(content_type=None)
                except ValueError as exc:
                    raise RuntimeError(
                        f"HTTP {response.status}: 后端返回了非 JSON 响应 "
                        f"(Content-Type={response.headers.get('Content-Type', 'unknown')})"
                    ) from exc

        response_text = cls._extract_response_text(body)
        if response_text:
            cls._append_history(history, text, response_text)
        return response_text

    @classmethod
    def _build_payload(cls, history: list[dict[str, str]], text: str, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": cls._model,
            "messages": cls._build_messages(history, text),
            "stream": stream,
            **cls._extra_body,
        }
        if cls._temperature is not None:
            payload["temperature"] = cls._temperature
        if cls._max_tokens is not None:
            payload["max_tokens"] = cls._max_tokens
        return payload

    @classmethod
    def _extract_delta_text(cls, chunk: Any) -> str:
        """从 chat.completion.chunk 里取正文增量（不取 reasoning_content）。"""
        if not isinstance(chunk, dict):
            return ""
        choices = chunk.get("choices")
        if not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return ""
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return ""

    @classmethod
    def _headers(cls) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if cls._api_key:
            headers["Authorization"] = f"Bearer {cls._api_key}"
        # Scope server-side long-term memory to this session (e.g. Hermes'
        # X-Hermes-Session-Key). chat/completions stays stateless — the
        # messages array is still the source of turn history — so this does
        # not duplicate context.
        if cls._session_header and cls._session_key:
            headers[cls._session_header] = cls._session_key
        return headers

    @classmethod
    def _chat_completions_url(cls) -> str:
        if cls._base_url.endswith("/chat/completions"):
            return cls._base_url
        return cls._base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def _build_messages(cls, history: list[dict[str, str]], text: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if cls._system_prompt:
            messages.append({"role": "system", "content": cls._system_prompt})
        messages.extend(history[-cls._history_max_messages :] if cls._history_max_messages else [])
        messages.append({"role": "user", "content": text})
        return messages

    @classmethod
    def _append_history(cls, history: list[dict[str, str]], text: str, response_text: str):
        if cls._history_max_messages <= 0:
            return
        history.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": response_text},
            ]
        )
        if len(history) > cls._history_max_messages:
            del history[: len(history) - cls._history_max_messages]

    @classmethod
    def _extract_response_text(cls, body: Any) -> str | None:
        if not isinstance(body, dict):
            return None
        choices = body.get("choices")
        if not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                return "".join(parts).strip() or None
        text = first.get("text")
        return text.strip() if isinstance(text, str) else None

    @classmethod
    async def _wait_and_play_response(cls, run_id: str):
        try:
            response_text = await cls._wait_response(run_id)
            if response_text:
                await cls._play_response_with_tts(
                    response_text,
                    tts_speaker=cls._response_tts_speakers.get(run_id),
                )
            else:
                logger.warning(f"[OpenAI] No response text received for run {run_id}")
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    @classmethod
    async def _play_response_with_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ):
        """Synthesize text and play it through the speaker."""
        from core.services.tts.router import TTSRouter

        resolved_tts_speaker = tts_speaker or cls.get_tts_speaker_for_session_key()
        await TTSRouter.play(
            text,
            configured_provider=cls._tts_provider,
            tts_speaker=resolved_tts_speaker,
            tts_speed=cls._tts_speed,
            playback_token=playback_token,
            log_prefix="OpenAI",
        )

    @classmethod
    async def _play_openai_compatible_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        provider: str = "openai",
    ):
        """Compatibility wrapper for the shared TTS router."""
        from core.services.tts.router import TTSRouter

        await TTSRouter.play(
            text,
            configured_provider=provider,
            tts_speaker=tts_speaker,
            tts_speed=cls._tts_speed,
            log_prefix="OpenAI",
        )
