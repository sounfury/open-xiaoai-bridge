"""分段播放：回复一段一段到达，第一段到了就开始播，后面的边生成边接上。

- XiaoAINativeSegmentPlayer：小爱原生 TTS。用 `ubus call mibrain text_to_speech save=1` 把每段提前
  生成成音频文件，再依次 `miplayer -f` 播放（移植自 OwnDing/open-xiaoai 的 NativeXiaomiTTS）。
  播放第 n 段时第 n+1、n+2 段已经在生成，段间几乎没有等待。
- CallbackSegmentPlayer：其他 TTS（豆包 / OpenAI 兼容等），按段依次调用播放函数，不预生成。

用法：start() → add(text) ... → finish()（等全部播完）；打断时 abort()。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.utils.logger import logger

_MODULE = "SegmentTTS"


class SegmentPlayer:
    """分段播放的公共接口。"""

    def __init__(
        self,
        on_play_start: Callable[[], None] | None = None,
        on_idle: Callable[[], None] | None = None,
    ):
        # on_play_start：每段开始播时调用；on_idle：一段播完、后面还没有东西可播时调用
        # （例如"稍等"念完了，后端还在查）
        self._on_play_start = on_play_start
        self._on_idle = on_idle
        self.added = 0  # 已加入的段数
        self.played = 0  # 已播完的段数
        self.error: str | None = None

    async def start(self):
        raise NotImplementedError

    async def add(self, text: str):
        raise NotImplementedError

    async def finish(self) -> bool:
        """不再有新段，等全部播完；被 abort() 时返回 False。"""
        raise NotImplementedError

    async def abort(self):
        raise NotImplementedError

    def _notify_play_start(self):
        if self._on_play_start:
            try:
                self._on_play_start()
            except Exception as exc:
                logger.debug(f"on_play_start failed: {exc}", module=_MODULE)

    def _notify_idle(self):
        if self._on_idle:
            try:
                self._on_idle()
            except Exception as exc:
                logger.debug(f"on_idle failed: {exc}", module=_MODULE)


class CallbackSegmentPlayer(SegmentPlayer):
    """按段依次 await play_fn(text)。"""

    def __init__(self, play_fn: Callable[[str], Awaitable[None]], on_play_start=None, on_idle=None):
        super().__init__(on_play_start, on_idle)
        self._play_fn = play_fn
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._aborted = False

    async def start(self):
        self._queue = asyncio.Queue()
        self._aborted = False
        self._worker = asyncio.create_task(self._run())

    async def add(self, text: str):
        if self._queue is None or self._aborted:
            return
        self.added += 1
        await self._queue.put(text)

    async def finish(self) -> bool:
        if self._queue is None:
            return False
        await self._queue.put(None)
        await asyncio.gather(self._worker, return_exceptions=True)
        return not self._aborted

    async def abort(self):
        self._aborted = True
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    async def _run(self):
        while True:
            text = await self._queue.get()
            if text is None:
                return
            self._notify_play_start()
            try:
                await self._play_fn(text)
                self.played += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                logger.error(f"Segment playback failed: {type(exc).__name__}: {exc}", module=_MODULE)
            if self._queue.empty():
                self._notify_idle()


@dataclass
class NativeTTSSettings:
    prefetch_segments: int = 2  # 最多提前生成几段
    generate_timeout_ms: int = 8000
    play_min_timeout_ms: int = 15000
    play_timeout_grace_ms: int = 8000
    play_timeout_ms: int = 10 * 60 * 1000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_config(cls, config) -> "NativeTTSSettings":
        values = {}
        if isinstance(config, dict):
            for name in cls.__dataclass_fields__:
                if name in config:
                    try:
                        values[name] = int(config[name])
                    except (TypeError, ValueError):
                        pass
        s = cls(**values)
        s.prefetch_segments = max(1, s.prefetch_segments)
        s.generate_timeout_ms = max(1000, s.generate_timeout_ms)
        s.play_min_timeout_ms = max(1000, s.play_min_timeout_ms)
        s.play_timeout_ms = max(s.play_min_timeout_ms, s.play_timeout_ms)
        s.max_total_bytes = max(s.max_file_bytes, s.max_total_bytes)
        return s


class XiaoAINativeSegmentPlayer(SegmentPlayer):
    """小爱原生 TTS 的分段流水线：生成（mibrain save=1）和播放（miplayer）并行。"""

    _FILE_PREFIX = "/tmp/open-xiaoai-bridge-tts-"
    # 小米云端 TTS 遇到它认为敏感的内容会拒读，返回一段固定大小的"内含敏感信息"音频
    _REFUSAL_BYTES = 5328
    # 少于这么多个读音单位时，正常句子也可能正好是这个大小
    _REFUSAL_MIN_UNITS = 9
    _SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")

    def __init__(self, speaker, settings: NativeTTSSettings | None = None, on_play_start=None, on_idle=None):
        super().__init__(on_play_start, on_idle)
        self.speaker = speaker
        self.settings = settings or NativeTTSSettings()
        self._token = 0
        self._aborted = False
        self._segment_queue: asyncio.Queue | None = None
        self._audio_queue: asyncio.Queue | None = None
        self._generation_task: asyncio.Task | None = None
        self._playback_task: asyncio.Task | None = None
        self._done: asyncio.Event | None = None
        self._files: dict[str, int] = {}  # 已生成未删除的文件 → 字节数
        self._index = 0
        self._session = uuid.uuid4().hex[:8]
        self._music_checked = False
        self._watchdog = True  # 设备的 busybox 不支持 timeout -t 时关掉
        self._generating = False  # 生成线程正在合成某一段

    async def start(self):
        await self._sh(f"rm -f {self._FILE_PREFIX}*.mp3", timeout=2000, check=False)
        self._token += 1
        token = self._token
        self._aborted = False
        self._segment_queue = asyncio.Queue()
        self._audio_queue = asyncio.Queue(maxsize=self.settings.prefetch_segments)
        self._done = asyncio.Event()
        self._generation_task = asyncio.create_task(self._generation_worker(token))
        self._playback_task = asyncio.create_task(self._playback_worker(token))

    async def add(self, text: str):
        if self._segment_queue is None or self._aborted:
            return
        text = str(text or "").strip()
        if not text:
            return
        self.added += 1
        await self._segment_queue.put(text)

    async def finish(self) -> bool:
        if self._segment_queue is None:
            return False
        token = self._token
        await self._segment_queue.put(None)
        await self._done.wait()
        return token == self._token and not self._aborted

    async def abort(self):
        if self._segment_queue is None or self._aborted:
            return
        self._aborted = True
        self._token += 1
        await self._kill_miplayer()
        tasks = [t for t in (self._generation_task, self._playback_task) if t]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._cleanup_files()
        if self._done:
            self._done.set()
        logger.debug("Native TTS aborted", module=_MODULE)

    # ---- workers ----

    async def _generation_worker(self, token: int):
        while token == self._token:
            text = await self._segment_queue.get()
            if text is None:
                await self._audio_queue.put(None)
                return
            self._generating = True
            try:
                path = await self._generate(text, token)
                if self._is_refusal(text, path):
                    await self._delete(path)
                    await self._queue_sentences(text, token)
                else:
                    await self._audio_queue.put((path, text))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                logger.error(f"Native TTS generate failed: {exc}", module=_MODULE)
                await self._audio_queue.put(None)
                return
            finally:
                self._generating = False

    async def _playback_worker(self, token: int):
        failed = False
        try:
            while token == self._token:
                item = await self._audio_queue.get()
                if item is None:
                    break
                path, text = item
                if not self._music_checked:
                    await self._pause_music()
                self._notify_play_start()
                loop = asyncio.get_running_loop()
                started = loop.time()
                try:
                    await self._play_file(path)
                    self.played += 1
                    logger.debug(
                        f"Played segment: chars={len(text)} elapsed={loop.time() - started:.2f}s",
                        module=_MODULE,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed = True
                    self.error = str(exc)
                    logger.error(f"Native TTS playback failed: {exc}", module=_MODULE)
                    await self._kill_miplayer()
                finally:
                    await self._delete(path)
                if failed:
                    break
                if self._audio_queue.empty() and self._segment_queue.empty() and not self._generating:
                    self._notify_idle()
        finally:
            if token == self._token:
                if failed and self._generation_task and not self._generation_task.done():
                    self._generation_task.cancel()
                    await asyncio.gather(self._generation_task, return_exceptions=True)
                await self._cleanup_files()
                self._done.set()

    # ---- device commands ----

    async def _sh(self, command: str, timeout: int, check: bool = True):
        result = await self.speaker.run_shell(command, timeout=timeout)
        if result is None:
            if check:
                raise RuntimeError(f"run_shell returned nothing: {command[:60]}")
            return None
        if check and result.exit_code != 0:
            raise RuntimeError((result.stderr or "").strip() or f"exit code {result.exit_code}: {command[:60]}")
        return result

    async def _generate(self, text: str, token: int) -> str:
        if token != self._token:
            raise asyncio.CancelledError
        payload = json.dumps({"text": text, "save": 1}, ensure_ascii=False, separators=(",", ":"))
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await self._sh(
            f"ubus call mibrain text_to_speech {shlex.quote(payload)}",
            timeout=self.settings.generate_timeout_ms,
        )
        try:
            response = json.loads(result.stdout)
            info = response.get("info")
            if isinstance(info, str):
                info = json.loads(info)
            generated = info.get("path") if isinstance(info, dict) else None
        except Exception as exc:
            raise RuntimeError(f"无法解析 mibrain 响应: {result.stdout!r}") from exc
        if not generated or not str(generated).startswith("/"):
            raise RuntimeError(f"mibrain 未返回音频路径: {result.stdout!r}")

        self._index += 1
        path = f"{self._FILE_PREFIX}{self._session}-{self._index}.mp3"
        moved = await self._sh(
            f"mv {shlex.quote(generated)} {shlex.quote(path)} && wc -c < {shlex.quote(path)}",
            timeout=2000,
        )
        try:
            size = int(moved.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("无法读取 TTS 音频大小") from exc
        self._files[path] = size
        if size > self.settings.max_file_bytes or sum(self._files.values()) > self.settings.max_total_bytes:
            await self._delete(path)
            raise RuntimeError(f"TTS 临时音频超过限制: {size} bytes")
        elapsed = loop.time() - started
        audio_s = size * 8 / 32000
        logger.debug(
            f"Generated segment: chars={len(text)} bytes={size} elapsed={elapsed:.2f}s audio≈{audio_s:.1f}s",
            module=_MODULE,
        )
        return path

    async def _play_file(self, path: str):
        size = self._files.get(path, 0)
        estimated_ms = math.ceil(size * 8 / 32000 * 1000)
        watchdog_ms = min(
            self.settings.play_timeout_ms,
            max(self.settings.play_min_timeout_ms, estimated_ms + self.settings.play_timeout_grace_ms),
        )
        seconds = max(1, math.ceil(watchdog_ms / 1000))
        if self._watchdog:
            result = await self._sh(
                f"busybox timeout -t {seconds} -s KILL miplayer -f {shlex.quote(path)}",
                timeout=watchdog_ms + 3000,
                check=False,
            )
            stderr = (result.stderr if result else "") or ""
            if result is not None and result.exit_code != 0 and (
                "applet not found" in stderr or "invalid option" in stderr or "Usage" in stderr
            ):
                # 这台设备的 busybox timeout 不认 -t：改成不带看门狗直接播
                logger.warning("busybox timeout -t unsupported, playing without watchdog", module=_MODULE)
                self._watchdog = False
            elif result is None or result.exit_code != 0:
                raise RuntimeError(stderr.strip() or f"miplayer exit code {getattr(result, 'exit_code', '?')}")
            else:
                return
        await self._sh(f"miplayer -f {shlex.quote(path)}", timeout=watchdog_ms + 3000)

    async def _kill_miplayer(self):
        try:
            await self._sh("killall miplayer 2>/dev/null; true", timeout=2000, check=False)
        except Exception:
            pass

    async def _pause_music(self):
        """有音乐在放就先暂停（和 tts_play.sh 一样，播完不自动恢复）。"""
        self._music_checked = True
        try:
            result = await self._sh("mphelper mute_stat", timeout=2000, check=False)
            if result is not None and result.stdout.strip() == "1":
                await self._sh("mphelper pause", timeout=2000, check=False)
        except Exception:
            pass

    @staticmethod
    def _spoken_units(text: str) -> int:
        """汉字 + 英文单词 / 数字：每个大约 0.2 秒。"""
        return len(re.findall(r"[\u4e00-\u9fff]", text)) + len(re.findall(r"[A-Za-z0-9]+", text))

    def _is_refusal(self, text: str, path: str) -> bool:
        units = self._spoken_units(text)
        if units < self._REFUSAL_MIN_UNITS:
            return False
        size = self._files.get(path, 0)
        if size == self._REFUSAL_BYTES:
            return True
        return size * 8 / 32000 < units * 0.1  # 远短于正常朗读

    async def _queue_sentences(self, text: str, token: int):
        """整段被拒读：逐句重新生成，只跳过仍被拒的句子。"""
        sentences = [p.strip() for p in self._SENTENCE_END.split(text) if p.strip()]
        if len(sentences) <= 1:
            logger.warning(f"Xiaomi TTS refused, skipped: {text[:40]}", module=_MODULE)
            return
        logger.warning(f"Xiaomi TTS refused a segment, retrying {len(sentences)} sentences", module=_MODULE)
        for sentence in sentences:
            path = await self._generate(sentence, token)
            if self._is_refusal(sentence, path):
                await self._delete(path)
                logger.warning(f"Xiaomi TTS refused, skipped: {sentence[:40]}", module=_MODULE)
                continue
            await self._audio_queue.put((path, sentence))

    async def _delete(self, path: str):
        self._files.pop(path, None)
        try:
            await self._sh(f"rm -f {shlex.quote(path)}", timeout=2000, check=False)
        except Exception:
            pass

    async def _cleanup_files(self):
        for path in list(self._files):
            await self._delete(path)
