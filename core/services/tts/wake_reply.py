"""唤醒 / 打断时的应答语（"我在" / "在" / "哎"……随机一句），和原生小爱一样。

小爱原生 TTS 时：第一次用到某句时用 `ubus call mibrain text_to_speech save=1` 在音箱上生成，
存成 /tmp/open-xiaoai-bridge-reply-<hash>.mp3 一直复用（音箱重启后 /tmp 清空，用时发现没了就重新生成），
用 miplayer 播：起播快，播放音量会喂给 vis，播报灯跟着声音动。
对话开始时 prepare() 在后台把所有应答语提前生成好，第一次唤醒就不用等。

其他 TTS（豆包等）时调用方传入的 fallback(text) 负责播。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import shlex
from typing import Awaitable, Callable

from core.utils.logger import logger

_MODULE = "WakeReply"
_PREFIX = "/tmp/open-xiaoai-bridge-reply-"
DEFAULT_PHRASES = ("我在", "在", "哎", "在呢", "我在听")


def phrases_from_config(cfg) -> list[str]:
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return []
    phrases = cfg.get("phrases", DEFAULT_PHRASES)
    if isinstance(phrases, str):
        phrases = [phrases]
    return [p.strip() for p in phrases or [] if isinstance(p, str) and p.strip()]


class WakeReplyPlayer:
    """单例：缓存在音箱上的应答语文件跟着 bridge 进程走。"""

    _instance: "WakeReplyPlayer | None" = None

    def __init__(self):
        self._paths: dict[str, str] = {}  # 文本 → 音箱上的文件
        self._lock = asyncio.Lock()
        self._last: str | None = None

    @classmethod
    def instance(cls) -> "WakeReplyPlayer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def pick(self, phrases: list[str]) -> str | None:
        """随机一句，尽量不和上一句重复。"""
        if not phrases:
            return None
        choices = [p for p in phrases if p != self._last] or phrases
        self._last = random.choice(choices)
        return self._last

    async def prepare(self, speaker, phrases: list[str]):
        """后台把应答语都生成好（已有的跳过）。"""
        for text in phrases:
            try:
                await self._ensure(speaker, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Prepare reply '{text}' failed: {exc}", module=_MODULE)

    async def play_native(self, speaker, text: str, timeout_ms: int = 5000) -> bool:
        """用小爱原生 TTS 播一句（阻塞到播完）。失败返回 False。"""
        try:
            path = await self._ensure(speaker, text)
            result = await speaker.run_shell(
                f"[ -f {shlex.quote(path)} ] || exit 3; miplayer -f {shlex.quote(path)}",
                timeout=timeout_ms,
            )
            if result is not None and result.exit_code == 3:
                # 文件被清掉了（音箱重启等）：重新生成再播一次
                self._paths.pop(text, None)
                path = await self._ensure(speaker, text)
                result = await speaker.run_shell(f"miplayer -f {shlex.quote(path)}", timeout=timeout_ms + 3000)
            return result is not None and result.exit_code == 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Reply '{text}' failed: {exc}", module=_MODULE)
            return False

    async def _ensure(self, speaker, text: str) -> str:
        async with self._lock:
            path = self._paths.get(text)
            if path:
                return path
            digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
            path = f"{_PREFIX}{digest}.mp3"
            check = await speaker.run_shell(f"[ -s {path} ]", timeout=2000)
            if check is None or check.exit_code != 0:
                payload = json.dumps({"text": text, "save": 1}, ensure_ascii=False, separators=(",", ":"))
                result = await speaker.run_shell(
                    f"ubus call mibrain text_to_speech {shlex.quote(payload)}", timeout=8000
                )
                if result is None or result.exit_code != 0:
                    raise RuntimeError(f"mibrain text_to_speech failed: {getattr(result, 'stderr', '')}")
                response = json.loads(result.stdout)
                info = response.get("info")
                if isinstance(info, str):
                    info = json.loads(info)
                generated = info.get("path") if isinstance(info, dict) else None
                if not generated or not str(generated).startswith("/"):
                    raise RuntimeError(f"mibrain 未返回音频路径: {result.stdout!r}")
                moved = await speaker.run_shell(f"mv {shlex.quote(generated)} {path}", timeout=2000)
                if moved is None or moved.exit_code != 0:
                    raise RuntimeError("无法保存应答语音频")
                logger.debug(f"Generated reply '{text}' → {path}", module=_MODULE)
            self._paths[text] = path
            return path


async def play_reply(
    speaker,
    phrases: list[str],
    native: bool,
    fallback: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    """随机播一句应答语，返回播的文本；没配置 / 失败返回 None。"""
    player = WakeReplyPlayer.instance()
    text = player.pick(phrases)
    if not text:
        return None
    if native and speaker is not None:
        return text if await player.play_native(speaker, text) else None
    if fallback is None:
        return None
    try:
        await fallback(text)
        return text
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"Reply '{text}' failed: {exc}", module=_MODULE)
        return None
