"""小爱管家（"你好缪斯"）单次请求路由。

流程（PRD 6.1）：
  1. KWS 命中"你好缪斯"，或小爱原生 ASR 文本以"你好缪斯"开头
     → config.before_wakeup 返回 "muse" → EventManager 调用 MuseRequestHandler
  2. 文本里带了请求内容就直接用；只说了唤醒词则追问一句（local_asr / xiaoai_asr）
  3. 先播报"稍等"，再把文本 POST 到小爱管家 webhook（reply.mode = push）
  4. 回复由小爱管家经 AgentDock 在本机调用 /api/play/text 推送，bridge 不等待回复

本模块顶层只依赖标准库和 aiohttp，不导入 open_xiaoai_server，
因此 config.py / keywords.py / 测试可以直接导入。
"""

import asyncio
import json
import os
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp

from core.ref import get_kws, get_speaker, get_xiaoai
from core.utils.logger import logger

LOG_MODULE = "Muse"
ROUTE = "muse"

_TRUTHY = ("1", "true", "yes")
# 只剩这些语气词时视为"只说了唤醒词"
_FILLER_CHARS = frozenset("啊呀吧呢嗯哦喔哈嘛诶欸")
# 播报提示语的最长等待时间（秒）
_PLAY_TIMEOUT_SECONDS = 10

NATIVE_SUPPRESS_MODES = ("abort", "dialog", "none")


def is_muse_enabled() -> bool:
    """MUSE_ENABLE=1 时启用小爱管家路由（与其他后端一样只由环境变量控制）。"""
    return os.environ.get("MUSE_ENABLE", "").strip().lower() in _TRUTHY


# ---- 配置 ----


def _app_config(path: str, default=None):
    from core.utils.config import ConfigManager

    return ConfigManager.instance().get_app_config(path, default)


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_str(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


@dataclass
class MuseSettings:
    """小爱管家路由配置；字段含义见 config.py 的 APP_CONFIG["muse"]。"""

    url: str = ""
    token: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    timeout: float = 10.0
    retries: int = 2
    retry_backoff: float = 0.5
    source: str = "xiaoai-oh2p"
    speaker: str = "desk"
    wake_word: str = "你好缪斯"
    wake_word_aliases: list[str] = field(default_factory=list)
    match_mode: str = "prefix"
    native_suppress: str = "abort"
    followup_timeout: float = 8.0
    wait_text: str = "稍等"
    failure_text: str = "小爱管家暂时连不上"
    not_configured_text: str = ""
    no_input_text: str = ""

    @property
    def wake_words(self) -> list[str]:
        words = [self.wake_word, *self.wake_word_aliases]
        return [word for word in words if isinstance(word, str) and word.strip()]


def load_settings(config_getter=None) -> MuseSettings:
    """读取配置：环境变量 MUSE_WEBHOOK_URL / MUSE_WEBHOOK_TOKEN 优先，其次 APP_CONFIG["muse"]。"""
    getter = config_getter or _app_config
    cfg = getter("muse", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    defaults = MuseSettings()

    aliases = cfg.get("wake_word_aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]

    match_mode = _to_str(cfg.get("match_mode"), defaults.match_mode).lower()
    if match_mode not in ("prefix", "contains"):
        match_mode = defaults.match_mode
    native_suppress = _to_str(cfg.get("native_suppress"), defaults.native_suppress).lower()
    if native_suppress not in NATIVE_SUPPRESS_MODES:
        native_suppress = defaults.native_suppress

    def text_option(key: str) -> str:
        # 提示语允许显式设为 "" 关闭
        value = cfg.get(key, getattr(defaults, key))
        return _to_str(value)

    return MuseSettings(
        url=_to_str(os.environ.get("MUSE_WEBHOOK_URL")) or _to_str(cfg.get("url")),
        token=_to_str(os.environ.get("MUSE_WEBHOOK_TOKEN")) or _to_str(cfg.get("token")),
        auth_header=_to_str(cfg.get("auth_header"), defaults.auth_header) or defaults.auth_header,
        auth_scheme=_to_str(cfg.get("auth_scheme", defaults.auth_scheme)),
        timeout=max(0.1, _to_float(cfg.get("timeout"), defaults.timeout)),
        retries=max(0, _to_int(cfg.get("retries"), defaults.retries)),
        retry_backoff=max(0.0, _to_float(cfg.get("retry_backoff"), defaults.retry_backoff)),
        source=_to_str(cfg.get("source"), defaults.source) or defaults.source,
        speaker=_to_str(cfg.get("speaker"), defaults.speaker) or defaults.speaker,
        wake_word=_to_str(cfg.get("wake_word"), defaults.wake_word) or defaults.wake_word,
        wake_word_aliases=[str(alias) for alias in aliases if alias],
        match_mode=match_mode,
        native_suppress=native_suppress,
        followup_timeout=max(0.5, _to_float(cfg.get("followup_timeout"), defaults.followup_timeout)),
        wait_text=text_option("wait_text"),
        failure_text=text_option("failure_text"),
        not_configured_text=text_option("not_configured_text"),
        no_input_text=text_option("no_input_text"),
    )


# ---- 唤醒词文本解析 ----


def _is_separator(ch: str) -> bool:
    """空白和中英文标点（，。！？、…）都视为分隔符。"""
    return ch.isspace() or unicodedata.category(ch)[0] in ("P", "Z") or ch in "~～"


def _compact(text: str) -> tuple[str, list[int]]:
    """去掉分隔符并转小写，同时记录每个字符在原文中的下标。"""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if _is_separator(ch):
            continue
        chars.append(ch.lower())
        index.append(i)
    return "".join(chars), index


def _strip_leading_separators(text: str) -> str:
    i = 0
    while i < len(text) and _is_separator(text[i]):
        i += 1
    return text[i:].strip()


@dataclass(frozen=True)
class MuseMatch:
    wake_word: str  # 命中的唤醒词（可能是别名）
    content: str  # 去掉唤醒词和前导标点后的请求文本，只说了唤醒词时为空

    @property
    def is_bare(self) -> bool:
        return not self.content


def parse_muse_text(
    text: str,
    wake_words: list[str],
    match_mode: str = "prefix",
) -> MuseMatch | None:
    """判断文本是否以唤醒词开头（prefix）或包含唤醒词（contains）。

    唤醒词内部和前后的空格、标点会被忽略，例如：
        "你好缪斯，我今晚想洗澡" / "你好 缪斯 我今晚想洗澡" → content="我今晚想洗澡"
        "你好缪斯。" / "你好缪斯啊"                         → content=""（只说了唤醒词）
    """
    if not isinstance(text, str) or not text.strip():
        return None

    compact, index = _compact(text)
    best: tuple[int, int, str] | None = None
    for word in wake_words:
        word_compact, _ = _compact(word)
        if not word_compact:
            continue
        pos = compact.find(word_compact)
        if pos < 0 or (match_mode != "contains" and pos != 0):
            continue
        # 取最靠前的，位置相同取最长的
        if best is None or (pos, -len(word_compact)) < (best[0], -best[1]):
            best = (pos, len(word_compact), word)

    if best is None:
        return None

    pos, length, word = best
    end = index[pos + length - 1] + 1
    content = _strip_leading_separators(text[end:])
    if all(ch in _FILLER_CHARS or _is_separator(ch) for ch in content):
        content = ""
    return MuseMatch(wake_word=word, content=content)


def match_muse_text(text: str, settings: MuseSettings | None = None) -> MuseMatch | None:
    """MUSE_ENABLE 关闭时总是返回 None，保证其他路由行为不变。"""
    if not is_muse_enabled():
        return None
    settings = settings or load_settings()
    return parse_muse_text(text, settings.wake_words, settings.match_mode)


async def suppress_native_xiaoai(speaker, settings: MuseSettings | None = None) -> None:
    """拦截原生小爱对当前这句话的处理（source == "xiaoai" 时在 before_wakeup 中调用）。

    muse.native_suppress:
        "abort"  — speaker.abort_xiaoai()，与 召唤龙虾 / 让龙虾 示例一致（重启约 1-2 秒）
        "dialog" — 复用 XiaoAI 的 dialog 拦截机制，持续停止该 dialog 的原生回复
        "none"   — 不拦截
    """
    settings = settings or load_settings()
    mode = settings.native_suppress
    if mode == "none" or speaker is None:
        return
    try:
        if mode == "dialog":
            xiaoai = get_xiaoai()
            dialog_id = getattr(xiaoai, "last_recognize_dialog_id", "") if xiaoai else ""
            if dialog_id:
                await xiaoai._suppress_dialog(dialog_id, "小爱管家接管")
                return
            logger.debug("No native dialog_id recorded, fallback to abort_xiaoai", module=LOG_MODULE)
        await speaker.abort_xiaoai()
    except Exception as exc:
        logger.warning(f"拦截原生小爱失败: {type(exc).__name__}: {exc}", module=LOG_MODULE)


# ---- Webhook 客户端 ----


def _json_safe(value):
    """app 字段只透传可 JSON 序列化的值，其他对象（如 MainApp）只保留类型名。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return type(value).__name__


def _redact_url(url: str) -> str:
    """日志里只保留 scheme://host:port，webhook 路径和查询串里可能带密钥。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        return "<invalid url>"
    suffix = "/…" if parts.path not in ("", "/") or parts.query else ""
    return f"{parts.scheme}://{host}{suffix}"


@dataclass
class MuseDeliveryResult:
    request_id: str
    delivered: bool = False
    skipped: bool = False
    status: int | None = None
    attempts: int = 0
    error: str = ""


class MuseWebhookClient:
    """把请求文本 POST 到小爱管家 webhook，2xx 视为已投递。

    仅在网络错误 / 超时 / 5xx / 429 时重试（指数退避），其他 4xx 不重试。
    """

    def __init__(
        self,
        url: str = "",
        token: str = "",
        *,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        timeout: float = 10.0,
        retries: int = 2,
        retry_backoff: float = 0.5,
        source: str = "xiaoai-oh2p",
        speaker: str = "desk",
        wake_word: str = "你好缪斯",
    ):
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.auth_header = auth_header or "Authorization"
        self.auth_scheme = (auth_scheme or "").strip()
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.source = source
        self.speaker = speaker
        self.wake_word = wake_word

    @classmethod
    def from_settings(cls, settings: MuseSettings) -> "MuseWebhookClient":
        return cls(
            settings.url,
            settings.token,
            auth_header=settings.auth_header,
            auth_scheme=settings.auth_scheme,
            timeout=settings.timeout,
            retries=settings.retries,
            retry_backoff=settings.retry_backoff,
            source=settings.source,
            speaker=settings.speaker,
            wake_word=settings.wake_word,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.url)

    def build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers[self.auth_header] = (
                f"{self.auth_scheme} {self.token}" if self.auth_scheme else self.token
            )
        return headers

    def build_payload(
        self,
        text: str,
        app_context=None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """PRD 7.1 payload。ts 使用本机时区偏移（如 +08:00）。"""
        now = now or datetime.now()
        if now.tzinfo is None:
            now = now.astimezone()
        ts = now.isoformat(timespec="seconds")
        return {
            "request_id": request_id or str(uuid.uuid4()),
            "source": self.source,
            "speaker": self.speaker,
            "wake_word": self.wake_word,
            "text": text,
            "app": _json_safe(app_context),
            "ts": ts,
            "reply": {"mode": "push"},
        }

    def _scrub(self, message: str) -> str:
        if self.url:
            message = message.replace(self.url, _redact_url(self.url))
        if self.token:
            message = message.replace(self.token, "***")
        return message

    async def send(
        self,
        text: str,
        app_context=None,
        request_id: str | None = None,
    ) -> MuseDeliveryResult:
        """发送请求，不抛出异常（取消除外），结果见 MuseDeliveryResult。"""
        payload = self.build_payload(text, app_context=app_context, request_id=request_id)
        result = MuseDeliveryResult(request_id=payload["request_id"])

        if not self.url:
            logger.warning(
                "未配置小爱管家 webhook（MUSE_WEBHOOK_URL 或 config muse.url），跳过转发",
                module=LOG_MODULE,
            )
            result.skipped = True
            result.error = "webhook url not configured"
            return result

        target = _redact_url(self.url)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self.build_headers()
        max_attempts = 1 + self.retries

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(1, max_attempts + 1):
                    result.attempts = attempt
                    retryable = False
                    try:
                        async with session.post(self.url, data=body, headers=headers) as resp:
                            result.status = resp.status
                            if 200 <= resp.status < 300:
                                result.delivered = True
                                result.error = ""
                                logger.info(
                                    f"✅ 已投递到小爱管家 {target} "
                                    f"(request_id={result.request_id}, status={resp.status}, attempt={attempt})",
                                    module=LOG_MODULE,
                                )
                                return result
                            result.error = f"HTTP {resp.status}"
                            retryable = resp.status == 429 or resp.status >= 500
                    except (asyncio.TimeoutError, TimeoutError):
                        result.status = None
                        result.error = f"timeout after {self.timeout:g}s"
                        retryable = True
                    except aiohttp.InvalidURL:
                        result.status = None
                        result.error = "invalid webhook url"
                    except aiohttp.ClientError as exc:
                        result.status = None
                        result.error = self._scrub(f"{type(exc).__name__}: {exc}")
                        retryable = True

                    if not retryable or attempt >= max_attempts:
                        break
                    delay = self.retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        f"投递失败（{result.error}），{delay:.1f}s 后重试 "
                        f"({attempt}/{self.retries}, request_id={result.request_id})",
                        module=LOG_MODULE,
                    )
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result.error = self._scrub(f"{type(exc).__name__}: {exc}")

        logger.error(
            f"❌ 投递到小爱管家失败 {target} "
            f"(request_id={result.request_id}, attempts={result.attempts}, error={result.error})",
            module=LOG_MODULE,
        )
        return result


# ---- 单次请求处理 ----


class MuseRequestHandler:
    """EventManager 的 "muse" 路由：追问（可选）→ 播报"稍等" → webhook。"""

    def __init__(
        self,
        settings: MuseSettings | None = None,
        client: MuseWebhookClient | None = None,
        speaker=None,
    ):
        self.settings = settings or load_settings()
        self.client = client or MuseWebhookClient.from_settings(self.settings)
        self._speaker = speaker
        self._capture = None

    @property
    def speaker(self):
        return self._speaker or get_speaker()

    # ---- 追问期间供 EventManager 转发中断和原生 ASR 结果 ----

    def is_active(self) -> bool:
        return bool(self._capture and self._capture.is_active())

    def stop(self):
        """停止正在进行的追问（不会取消已发出的 webhook 请求）。"""
        if self._capture:
            self._capture.stop()

    def consume_xiaoai_recognize_result(self, **kwargs) -> bool:
        if not self._capture:
            return False
        return self._capture.consume_xiaoai_recognize_result(**kwargs)

    # ---- 主流程 ----

    async def handle(self, text: str, source: str) -> MuseDeliveryResult | None:
        """处理一次缪斯唤醒，永远不向外抛异常（任务被取消除外）。"""
        try:
            return await self._handle(text, source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"处理缪斯请求失败: {type(exc).__name__}: {exc}", module=LOG_MODULE)
            return None

    async def _handle(self, text: str, source: str) -> MuseDeliveryResult | None:
        request_text = self._extract_request_text(text)
        if not request_text:
            followup = await self.capture_followup()
            request_text = self._extract_request_text(followup) if followup else ""
            if not request_text:
                logger.info("未听到请求内容，退出", module=LOG_MODULE)
                await self._say(self.settings.no_input_text)
                return None

        if not self.client.is_configured:
            # 不播"稍等"：后面没有回复可等
            result = await self.client.send(request_text, app_context=source)
            await self._say(self.settings.not_configured_text)
            return result

        logger.info(f"📨 转发到小爱管家: {request_text}", module=LOG_MODULE)
        await self._say(self.settings.wait_text)
        result = await self.client.send(request_text, app_context=source)
        if not result.delivered:
            await self._say(self.settings.failure_text)
        return result

    def _extract_request_text(self, text: str | None) -> str:
        """去掉唤醒词；文本不含唤醒词时（追问内容）原样返回。"""
        if not text:
            return ""
        match = parse_muse_text(text, self.settings.wake_words, self.settings.match_mode)
        if match:
            return match.content
        return text.strip()

    async def capture_followup(self) -> str | None:
        """只说了唤醒词时，复用外部后端的输入链路再听一句。"""
        from core.muse_followup import MuseFollowupCapture

        kws = get_kws()
        if kws:
            kws.pause()
        self._capture = MuseFollowupCapture(timeout=self.settings.followup_timeout)
        try:
            return await self._capture.capture_once()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task and task.cancelling():
                raise
            # stop() 取消了内部 Future（例如用户喊了"小爱同学"）
            return None
        finally:
            self._capture = None
            if kws:
                kws.resume()

    async def _say(self, text: str):
        speaker = self.speaker
        if not text or not speaker:
            return
        try:
            await asyncio.wait_for(
                speaker.play(text=text, blocking=True, timeout=_PLAY_TIMEOUT_SECONDS * 1000),
                timeout=_PLAY_TIMEOUT_SECONDS + 1,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"播报失败（{text}）: {type(exc).__name__}: {exc}", module=LOG_MODULE)
