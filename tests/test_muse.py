"""小爱管家（"你好缪斯"）路由测试，无需音箱 / 原生扩展 / 模型。

    python -m pytest tests/test_muse*.py
    python -m unittest discover -s tests -p "test_muse*.py"
"""

import asyncio
import importlib
import importlib.util
import os
import socket
import sys
import time
import types
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Rust 扩展可能没有编译：用空模块占位（与其他测试一致）
sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

muse = importlib.import_module("core.muse")

TEST_TOKEN = "unit-test-token-not-a-secret"


def make_config_getter(app_config: dict):
    """模拟 ConfigManager.get_app_config 的嵌套路径查询。"""

    def get(path=None, default=None):
        if not path:
            return app_config
        value = app_config
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    return get


def load_repo_config():
    """以独立模块名加载仓库里的 config.py，不影响 sys.modules["config"]。"""
    spec = importlib.util.spec_from_file_location("muse_test_config", ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env(**values):
    """清掉 muse 相关环境变量后再设置，避免受本机环境影响。"""
    base = {"MUSE_ENABLE": "", "MUSE_WEBHOOK_URL": "", "MUSE_WEBHOOK_TOKEN": ""}
    base.update(values)
    return mock.patch.dict(os.environ, base)


class FakeSpeaker:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    async def play(self, text=None, **kwargs):
        self.events.append(("play", text))
        return True

    async def abort_xiaoai(self):
        self.events.append(("abort",))
        return True

    async def wake_up(self, awake=True, silent=True):
        self.events.append(("wake_up", awake))
        return True

    async def stop_device_audio(self):
        self.events.append(("stop_device_audio",))


class MockWebhook:
    """本地 aiohttp mock webhook（127.0.0.1 随机端口）。"""

    def __init__(self, statuses=None, delay=0.0, events=None):
        self.statuses = list(statuses or [])
        self.delay = delay
        self.events = events if events is not None else []
        self.requests = []
        self.server = None
        self.url = ""

    async def _handle(self, request):
        payload = await request.json()
        self.requests.append({"headers": dict(request.headers), "json": payload})
        self.events.append(("post", payload))
        if self.delay:
            await asyncio.sleep(self.delay)
        status = self.statuses.pop(0) if self.statuses else 200
        return web.json_response({"ok": 200 <= status < 300}, status=status)

    async def start(self):
        app = web.Application()
        app.router.add_post("/hook", self._handle)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        self.url = str(self.server.make_url("/hook"))
        return self

    async def close(self):
        if self.server:
            await self.server.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# 唤醒词文本解析
# ---------------------------------------------------------------------------


class MuseTextParsingTest(unittest.TestCase):
    WAKE_WORDS = ["你好缪斯"]

    def parse(self, text, mode="prefix", words=None):
        return muse.parse_muse_text(text, words or self.WAKE_WORDS, mode)

    def test_bare_wake_word(self):
        for text in ("你好缪斯", "你好缪斯。", "你好缪斯！", " 你好缪斯 ", "你好，缪斯", "你好缪斯啊"):
            with self.subTest(text=text):
                match = self.parse(text)
                self.assertIsNotNone(match)
                self.assertTrue(match.is_bare)
                self.assertEqual(match.content, "")
                self.assertEqual(match.wake_word, "你好缪斯")

    def test_wake_word_with_content(self):
        cases = {
            "你好缪斯，我今晚想洗澡": "我今晚想洗澡",
            "你好缪斯我今晚想洗澡": "我今晚想洗澡",
            "你好缪斯 我今晚想洗澡": "我今晚想洗澡",
            "你好 缪斯, 我今晚想洗澡": "我今晚想洗澡",
            "你好，缪斯！把台灯调暗一点": "把台灯调暗一点",
            "　你好缪斯：　打开热水器": "打开热水器",  # 全角空格 / 全角冒号
            "你好缪斯……播放 Spotify": "播放 Spotify",
            "你好缪斯、现在几点了？": "现在几点了？",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                match = self.parse(text)
                self.assertIsNotNone(match)
                self.assertFalse(match.is_bare)
                self.assertEqual(match.content, expected)

    def test_native_and_other_text_not_matched(self):
        for text in (
            "小爱同学",
            "小爱同学，开灯",
            "开灯",
            "你好小黑",
            "你好龙虾",
            "缪斯",
            "你好",
            "",
            "   ",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_prefix_mode_requires_leading_wake_word(self):
        self.assertIsNone(self.parse("我想说你好缪斯帮我开灯"))
        match = self.parse("我想说你好缪斯帮我开灯", mode="contains")
        self.assertEqual(match.content, "帮我开灯")

    def test_aliases(self):
        match = self.parse("你好缪思，开灯", words=["你好缪斯", "你好缪思"])
        self.assertEqual(match.wake_word, "你好缪思")
        self.assertEqual(match.content, "开灯")

    def test_match_muse_text_is_gated_by_muse_enable(self):
        settings = muse.MuseSettings()
        with env(MUSE_ENABLE=""):
            self.assertIsNone(muse.match_muse_text("你好缪斯，开灯", settings))
        with env(MUSE_ENABLE="1"):
            self.assertEqual(muse.match_muse_text("你好缪斯，开灯", settings).content, "开灯")
            self.assertIsNone(muse.match_muse_text("小爱同学开灯", settings))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class MuseSettingsTest(unittest.TestCase):
    def test_defaults_without_muse_section(self):
        with env():
            settings = muse.load_settings(make_config_getter({}))
        self.assertEqual(settings.url, "")
        self.assertEqual(settings.token, "")
        self.assertEqual(settings.source, "xiaoai-oh2p")
        self.assertEqual(settings.speaker, "desk")
        self.assertEqual(settings.wake_word, "你好缪斯")
        self.assertEqual(settings.wait_text, "稍等")
        self.assertEqual(settings.timeout, 10.0)
        self.assertEqual(settings.retries, 2)
        self.assertEqual(settings.auth_header, "Authorization")
        self.assertEqual(settings.auth_scheme, "Bearer")

    def test_env_overrides_config(self):
        getter = make_config_getter({"muse": {"url": "http://config.invalid/hook", "token": "cfg"}})
        with env(MUSE_WEBHOOK_URL="http://127.0.0.1:1/env", MUSE_WEBHOOK_TOKEN=TEST_TOKEN):
            settings = muse.load_settings(getter)
        self.assertEqual(settings.url, "http://127.0.0.1:1/env")
        self.assertEqual(settings.token, TEST_TOKEN)
        with env():
            settings = muse.load_settings(getter)
        self.assertEqual(settings.url, "http://config.invalid/hook")
        self.assertEqual(settings.token, "cfg")

    def test_prompts_can_be_disabled_and_invalid_values_fallback(self):
        getter = make_config_getter(
            {"muse": {"wait_text": "", "failure_text": "", "timeout": "abc", "retries": -3, "match_mode": "x"}}
        )
        with env():
            settings = muse.load_settings(getter)
        self.assertEqual(settings.wait_text, "")
        self.assertEqual(settings.failure_text, "")
        self.assertEqual(settings.timeout, 10.0)
        self.assertEqual(settings.retries, 0)
        self.assertEqual(settings.match_mode, "prefix")

    def test_repo_config_has_empty_webhook_secrets_and_muse_keyword(self):
        config = load_repo_config()
        muse_cfg = config.APP_CONFIG["muse"]
        self.assertEqual(muse_cfg["url"], "")
        self.assertEqual(muse_cfg["token"], "")
        self.assertIn("你好缪斯", config.APP_CONFIG["wakeup"]["keywords"])
        self.assertEqual(muse_cfg["wake_word"], "你好缪斯")

    def test_keyword_generation_enabled_for_muse(self):
        spec = importlib.util.spec_from_file_location(
            "kws_keywords_for_muse_test", ROOT / "core/services/audio/kws/keywords.py"
        )
        keywords = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(keywords)
        disabled = {
            "XIAOZHI_ENABLE": "",
            "OPENCLAW_ENABLE": "",
            "OPENCLAW_ENABLED": "",
            "OPENAI_ENABLE": "",
            "QWENPAW_ENABLE": "",
        }
        with mock.patch.dict(os.environ, {**disabled, "MUSE_ENABLE": ""}):
            self.assertFalse(keywords.should_generate_keywords()[0])
        with mock.patch.dict(os.environ, {**disabled, "MUSE_ENABLE": "1"}):
            self.assertEqual(keywords.should_generate_keywords(), (True, ""))

    def test_startup_entrypoints_prepare_keywords_for_muse(self):
        start_sh = (ROOT / "scripts/start.sh").read_text(encoding="utf8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf8")
        self.assertIn('[[ "$MUSE_ENABLE_VALUE" =~ ^(1|true|yes)$ ]]', start_sh)
        self.assertIn("${MUSE_ENABLE:-}", dockerfile)


# ---------------------------------------------------------------------------
# before_wakeup 路由（config.py）
# ---------------------------------------------------------------------------


class BeforeWakeupRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = load_repo_config()
        self.app_config = self.config.APP_CONFIG
        patcher = mock.patch.object(
            muse, "_app_config", side_effect=make_config_getter(self.app_config)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def route(self, text, source):
        speaker = FakeSpeaker()
        app = mock.AsyncMock()
        result = await self.config.before_wakeup(speaker, text, source, app)
        return result, speaker.events, app

    async def test_muse_kws_wake_word_routes_to_muse(self):
        with env(MUSE_ENABLE="1"):
            result, events, _ = await self.route("你好缪斯", "kws")
        self.assertEqual(result, "muse")
        self.assertEqual(events, [])  # "稍等" 由 handler 播放，不在 before_wakeup 里

    async def test_muse_xiaoai_text_routes_to_muse_and_suppresses_native(self):
        for text in ("你好缪斯，我今晚想洗澡", "你好缪斯我今晚想洗澡", "你好缪斯"):
            with self.subTest(text=text), env(MUSE_ENABLE="1"):
                result, events, _ = await self.route(text, "xiaoai")
                self.assertEqual(result, "muse")
                self.assertEqual(events, [("abort",)])

    async def test_native_suppress_modes(self):
        self.app_config["muse"]["native_suppress"] = "none"
        with env(MUSE_ENABLE="1"):
            result, events, _ = await self.route("你好缪斯，开灯", "xiaoai")
        self.assertEqual((result, events), ("muse", []))

        self.app_config["muse"]["native_suppress"] = "dialog"
        xiaoai = types.SimpleNamespace(
            last_recognize_dialog_id="dialog-9", _suppress_dialog=mock.AsyncMock()
        )
        with env(MUSE_ENABLE="1"), mock.patch.object(muse, "get_xiaoai", return_value=xiaoai):
            result, events, _ = await self.route("你好缪斯，开灯", "xiaoai")
        self.assertEqual((result, events), ("muse", []))
        xiaoai._suppress_dialog.assert_awaited_once_with("dialog-9", "小爱管家接管")

    async def test_native_and_existing_routes_unchanged(self):
        cases = [
            ("打开台灯", "xiaoai", None, []),
            ("小爱同学打开台灯", "xiaoai", None, []),
            ("今天天气怎么样", "xiaoai", None, []),
            ("召唤龙虾", "xiaoai", "openclaw", [("abort",)]),
            ("召唤小智", "xiaoai", "xiaozhi", [("abort",)]),
            ("你好龙虾", "kws", "openclaw", [("play", "龙虾来了")]),
            ("你好小黑", "kws", "openai", [("play", "小黑来了")]),
            ("你好小智", "kws", "xiaozhi", [("play", "小智来了")]),
            ("随便什么", "kws", None, []),
        ]
        for muse_enable in ("", "1"):
            for text, source, expected, expected_events in cases:
                with self.subTest(text=text, source=source, muse=muse_enable), env(MUSE_ENABLE=muse_enable):
                    result, events, _ = await self.route(text, source)
                    self.assertEqual(result, expected)
                    self.assertEqual(events, expected_events)

    async def test_muse_disabled_leaves_muse_text_to_previous_logic(self):
        with env(MUSE_ENABLE=""):
            self.assertEqual(await self.route("你好缪斯", "kws"), (None, [], mock.ANY))
            result, events, _ = await self.route("你好缪斯，我今晚想洗澡", "xiaoai")
        self.assertIsNone(result)
        self.assertEqual(events, [])


# ---------------------------------------------------------------------------
# EventManager 分发
# ---------------------------------------------------------------------------


def import_wakeup_session(app_config: dict):
    """参照 test_wakeup_keywords：用桩替换 ConfigManager 后导入 core.wakeup_session。"""

    class ConfigManagerStub:
        @classmethod
        def instance(cls):
            return cls()

        def get_app_config(self, path=None, default=None):
            return make_config_getter(app_config)(path, default)

    config_stub = types.SimpleNamespace(ConfigManager=ConfigManagerStub)
    saved = sys.modules.get("core.wakeup_session")
    sys.modules.pop("core.wakeup_session", None)
    try:
        with mock.patch.dict(sys.modules, {"core.utils.config": config_stub}):
            return importlib.import_module("core.wakeup_session")
    finally:
        if saved is None:
            sys.modules.pop("core.wakeup_session", None)
        else:
            sys.modules["core.wakeup_session"] = saved


BACKEND_STUBS = {
    "core.openclaw": types.SimpleNamespace(OpenClawManager=types.SimpleNamespace(_session_key=None)),
    "core.openai": types.SimpleNamespace(OpenAIManager=types.SimpleNamespace(_session_key=None)),
    "core.qwenpaw": types.SimpleNamespace(QwenPawManager=types.SimpleNamespace(_session_key=None)),
}


class EventManagerMuseDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.route = None
        self.seen = []

        async def before_wakeup(speaker, text, source, app):
            self.seen.append((text, source))
            return self.route

        self.module = import_wakeup_session({"wakeup": {"before_wakeup": before_wakeup}})
        self.manager = self.module.WakeupSessionManager()
        patcher = mock.patch.dict(sys.modules, BACKEND_STUBS)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_muse_route_dispatches_to_one_shot_handler(self):
        self.route = "muse"
        with (
            mock.patch.object(self.manager, "reset_all_sessions", new=mock.AsyncMock()) as reset,
            mock.patch.object(self.manager, "_start_muse_request", new=mock.AsyncMock()) as start,
        ):
            await self.manager.wakeup("你好缪斯，开灯", "xiaoai")
        reset.assert_awaited_once()
        start.assert_awaited_once_with("你好缪斯，开灯", "xiaoai")
        self.assertEqual(self.seen, [("你好缪斯，开灯", "xiaoai")])

    async def test_none_route_does_not_dispatch(self):
        self.route = None
        with (
            mock.patch.object(self.manager, "reset_all_sessions", new=mock.AsyncMock()) as reset,
            mock.patch.object(self.manager, "_start_muse_request", new=mock.AsyncMock()) as start,
        ):
            await self.manager.wakeup("开灯", "xiaoai")
        reset.assert_not_awaited()
        start.assert_not_awaited()

    async def test_start_muse_request_runs_handler_and_forwards_events(self):
        calls = []
        manager = self.manager

        class FakeHandler:
            def __init__(self):
                self.stopped = False

            async def handle(self, text, source):
                calls.append(("handle", text, source))
                # 追问期间：原生 ASR 结果与中断都会转给 handler
                self.capturing = True
                consumed = manager.consume_xiaoai_asr_result(
                    dialog_id="d1", text="开灯", is_final=True, is_vad_begin=None
                )
                calls.append(("consumed", consumed))
                manager._muse_handler.stop()
                self.capturing = False

            def is_active(self):
                return getattr(self, "capturing", False)

            def consume_xiaoai_recognize_result(self, **kwargs):
                calls.append(("asr", kwargs["text"]))
                return True

            def stop(self):
                calls.append(("stop",))

        with mock.patch.object(muse, "MuseRequestHandler", FakeHandler):
            await self.manager._start_muse_request("你好缪斯", "kws")

        self.assertEqual(
            calls,
            [("handle", "你好缪斯", "kws"), ("asr", "开灯"), ("consumed", True), ("stop",)],
        )
        self.assertIsNone(self.manager._muse_handler)


# ---------------------------------------------------------------------------
# Webhook 客户端（真实本地 HTTP 服务）
# ---------------------------------------------------------------------------


class MuseWebhookClientTest(unittest.IsolatedAsyncioTestCase):
    async def start_webhook(self, **kwargs):
        webhook = await MockWebhook(**kwargs).start()
        self.addAsyncCleanup(webhook.close)
        return webhook

    def client(self, url, **kwargs):
        kwargs.setdefault("retry_backoff", 0)
        return muse.MuseWebhookClient(url, **kwargs)

    async def test_payload_fields_per_prd_7_1(self):
        webhook = await self.start_webhook()
        result = await self.client(webhook.url).send("我今晚想洗澡", app_context="xiaoai")

        self.assertTrue(result.delivered)
        self.assertEqual((result.status, result.attempts), (200, 1))
        self.assertEqual(len(webhook.requests), 1)
        payload = webhook.requests[0]["json"]
        self.assertEqual(
            set(payload),
            {"request_id", "source", "speaker", "wake_word", "text", "app", "ts", "reply"},
        )
        self.assertEqual(uuid.UUID(payload["request_id"]).version, 4)
        self.assertEqual(payload["request_id"], result.request_id)
        self.assertEqual(payload["source"], "xiaoai-oh2p")
        self.assertEqual(payload["speaker"], "desk")
        self.assertEqual(payload["wake_word"], "你好缪斯")
        self.assertEqual(payload["text"], "我今晚想洗澡")
        self.assertEqual(payload["app"], "xiaoai")
        self.assertEqual(payload["reply"], {"mode": "push"})
        self.assertRegex(payload["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertIsNotNone(datetime.fromisoformat(payload["ts"]).tzinfo)
        self.assertTrue(webhook.requests[0]["headers"]["Content-Type"].startswith("application/json"))

    def test_ts_uses_local_offset_and_app_is_json_safe(self):
        client = self.client("")
        now = datetime(2026, 10, 16, 21, 30, tzinfo=timezone(timedelta(hours=8)))

        class MainAppLike:
            pass

        payload = client.build_payload("x", app_context=MainAppLike(), request_id="rid", now=now)
        self.assertEqual(payload["ts"], "2026-10-16T21:30:00+08:00")
        self.assertEqual(payload["app"], "MainAppLike")
        self.assertEqual(payload["request_id"], "rid")
        self.assertEqual(client.build_payload("x", app_context={"source": "kws"})["app"], {"source": "kws"})

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset is POSIX only")
    def test_default_ts_uses_local_timezone(self):
        try:
            with mock.patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
                time.tzset()
                ts = self.client("").build_payload("x")["ts"]
        finally:
            time.tzset()
        self.assertTrue(ts.endswith("+08:00"), ts)

    async def test_authorization_header_present_when_token_set(self):
        webhook = await self.start_webhook()
        await self.client(webhook.url, token=TEST_TOKEN).send("开灯")
        self.assertEqual(webhook.requests[0]["headers"].get("Authorization"), f"Bearer {TEST_TOKEN}")

    async def test_authorization_header_absent_when_token_empty(self):
        webhook = await self.start_webhook()
        await self.client(webhook.url, token="").send("开灯")
        self.assertNotIn("Authorization", webhook.requests[0]["headers"])

    async def test_custom_auth_header_and_raw_token(self):
        webhook = await self.start_webhook()
        await self.client(webhook.url, token=TEST_TOKEN, auth_header="X-Api-Key", auth_scheme="").send("开灯")
        headers = webhook.requests[0]["headers"]
        self.assertEqual(headers.get("X-Api-Key"), TEST_TOKEN)
        self.assertNotIn("Authorization", headers)

    async def test_retry_on_500_then_success(self):
        webhook = await self.start_webhook(statuses=[500, 200])
        result = await self.client(webhook.url).send("开灯")
        self.assertTrue(result.delivered)
        self.assertEqual(result.attempts, 2)
        ids = {request["json"]["request_id"] for request in webhook.requests}
        self.assertEqual(len(webhook.requests), 2)
        self.assertEqual(ids, {result.request_id})  # 重试复用同一个 request_id

    async def test_retry_on_429(self):
        webhook = await self.start_webhook(statuses=[429, 202])
        result = await self.client(webhook.url).send("开灯")
        self.assertTrue(result.delivered)
        self.assertEqual((result.status, result.attempts), (202, 2))

    async def test_gives_up_after_retries(self):
        webhook = await self.start_webhook(statuses=[503, 503, 503, 200])
        result = await self.client(webhook.url, retries=2).send("开灯")
        self.assertFalse(result.delivered)
        self.assertEqual((result.status, result.attempts), (503, 3))
        self.assertEqual(len(webhook.requests), 3)

    async def test_no_retry_on_400(self):
        webhook = await self.start_webhook(statuses=[400, 200])
        with self.assertLogs("xiaozhi", level="ERROR") as logs:
            result = await self.client(webhook.url, token=TEST_TOKEN).send("开灯")
        self.assertFalse(result.delivered)
        self.assertEqual((result.status, result.attempts), (400, 1))
        self.assertEqual(len(webhook.requests), 1)
        self.assertNotIn(TEST_TOKEN, "\n".join(logs.output))

    async def test_timeout_is_retried_then_reported(self):
        webhook = await self.start_webhook(delay=1.0)
        result = await self.client(webhook.url, timeout=0.2, retries=1).send("开灯")
        self.assertFalse(result.delivered)
        self.assertEqual(result.attempts, 2)
        self.assertIsNone(result.status)
        self.assertIn("timeout", result.error)

    async def test_connection_error_is_retried(self):
        url = f"http://127.0.0.1:{free_port()}/hook?token={TEST_TOKEN}"
        with self.assertLogs("xiaozhi", level="WARNING") as logs:
            result = await self.client(url, retries=1).send("开灯")
        self.assertFalse(result.delivered)
        self.assertEqual(result.attempts, 2)
        self.assertNotIn(TEST_TOKEN, result.error)
        self.assertNotIn(TEST_TOKEN, "\n".join(logs.output))

    async def test_empty_url_is_skipped(self):
        with self.assertLogs("xiaozhi", level="WARNING") as logs:
            result = await self.client("").send("开灯")
        self.assertTrue(result.skipped)
        self.assertFalse(result.delivered)
        self.assertEqual(result.attempts, 0)
        self.assertIn("MUSE_WEBHOOK_URL", "\n".join(logs.output))


# ---------------------------------------------------------------------------
# MuseRequestHandler（mock 音箱 + 本地 webhook）
# ---------------------------------------------------------------------------


class MuseRequestHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def make_handler(self, statuses=None, url=None, **settings_kwargs):
        self.events = []
        self.webhook = await MockWebhook(statuses=statuses, events=self.events).start()
        self.addAsyncCleanup(self.webhook.close)
        settings = muse.MuseSettings(
            url=self.webhook.url if url is None else url,
            token=TEST_TOKEN,
            retry_backoff=0,
            **settings_kwargs,
        )
        self.speaker = FakeSpeaker(self.events)
        return muse.MuseRequestHandler(settings=settings, speaker=self.speaker)

    def posted_texts(self):
        return [payload["text"] for kind, *rest in self.events if kind == "post" for payload in rest]

    async def test_plays_wait_text_before_webhook_post(self):
        handler = await self.make_handler()
        result = await handler.handle("你好缪斯，我今晚想洗澡", "xiaoai")

        self.assertTrue(result.delivered)
        self.assertEqual([event[0] for event in self.events], ["play", "post"])
        self.assertEqual(self.events[0], ("play", "稍等"))
        payload = self.events[1][1]
        self.assertEqual(payload["text"], "我今晚想洗澡")
        self.assertEqual(payload["app"], "xiaoai")
        self.assertEqual(self.webhook.requests[0]["headers"]["Authorization"], f"Bearer {TEST_TOKEN}")

    async def test_wait_text_is_configurable(self):
        handler = await self.make_handler(wait_text="好的，马上")
        await handler.handle("你好缪斯我今晚想洗澡", "kws")
        self.assertEqual(self.events[0], ("play", "好的，马上"))

    async def test_bare_wake_word_captures_one_followup(self):
        handler = await self.make_handler()

        async def capture():
            self.events.append(("capture",))
            return "我今晚想洗澡"

        with mock.patch.object(handler, "capture_followup", side_effect=capture):
            result = await handler.handle("你好缪斯", "kws")

        self.assertTrue(result.delivered)
        self.assertEqual([event[0] for event in self.events], ["capture", "play", "post"])
        self.assertEqual(self.posted_texts(), ["我今晚想洗澡"])
        self.assertEqual(self.events[2][1]["app"], "kws")

    async def test_followup_repeating_wake_word_is_stripped(self):
        handler = await self.make_handler()
        with mock.patch.object(handler, "capture_followup", new=mock.AsyncMock(return_value="你好缪斯，开灯")):
            await handler.handle("你好缪斯。", "kws")
        self.assertEqual(self.posted_texts(), ["开灯"])

    async def test_no_followup_exits_quietly(self):
        handler = await self.make_handler()
        for followup in (None, "", "你好缪斯"):
            with self.subTest(followup=followup):
                with mock.patch.object(handler, "capture_followup", new=mock.AsyncMock(return_value=followup)):
                    self.assertIsNone(await handler.handle("你好缪斯", "kws"))
        self.assertEqual(self.events, [])
        self.assertEqual(self.webhook.requests, [])

    async def test_no_followup_optional_prompt(self):
        handler = await self.make_handler(no_input_text="没听清")
        with mock.patch.object(handler, "capture_followup", new=mock.AsyncMock(return_value=None)):
            await handler.handle("你好缪斯", "kws")
        self.assertEqual(self.events, [("play", "没听清")])

    async def test_final_failure_plays_failure_text(self):
        handler = await self.make_handler(statuses=[500, 500, 500])
        result = await handler.handle("你好缪斯，开灯", "xiaoai")
        self.assertFalse(result.delivered)
        self.assertEqual(
            [event[0] for event in self.events], ["play", "post", "post", "post", "play"]
        )
        self.assertEqual(self.events[-1], ("play", "小爱管家暂时连不上"))

    async def test_failure_text_can_be_disabled(self):
        handler = await self.make_handler(statuses=[400], failure_text="")
        await handler.handle("你好缪斯，开灯", "xiaoai")
        self.assertEqual([event[0] for event in self.events], ["play", "post"])

    async def test_empty_url_skips_without_wait_text_or_exception(self):
        handler = await self.make_handler(url="")
        with self.assertLogs("xiaozhi", level="WARNING"):
            result = await handler.handle("你好缪斯，开灯", "xiaoai")
        self.assertTrue(result.skipped)
        self.assertEqual(self.events, [])

    async def test_empty_url_optional_hint(self):
        handler = await self.make_handler(url="", not_configured_text="小爱管家还没配置")
        await handler.handle("你好缪斯，开灯", "xiaoai")
        self.assertEqual(self.events, [("play", "小爱管家还没配置")])

    async def test_speaker_failure_does_not_block_post(self):
        handler = await self.make_handler()

        async def broken_play(**kwargs):
            raise RuntimeError("speaker offline")

        self.speaker.play = broken_play
        result = await handler.handle("你好缪斯，开灯", "xiaoai")
        self.assertTrue(result.delivered)
        self.assertEqual(self.posted_texts(), ["开灯"])

    async def test_handler_never_raises(self):
        handler = await self.make_handler()
        handler.client.send = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertLogs("xiaozhi", level="ERROR"):
            self.assertIsNone(await handler.handle("你好缪斯，开灯", "xiaoai"))

    async def test_without_wake_word_uses_text_as_is(self):
        handler = await self.make_handler()
        await handler.handle("把台灯调暗一点", "xiaoai")
        self.assertEqual(self.posted_texts(), ["把台灯调暗一点"])


# ---------------------------------------------------------------------------
# 追问采集（复用 ExternalConversationController 的输入链路）
# ---------------------------------------------------------------------------


class MuseFollowupCaptureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.followup = importlib.import_module("core.muse_followup")
        external = importlib.import_module("core.external_conversation")
        self.events = []
        self.speaker = FakeSpeaker(self.events)
        self.input_mode = "xiaoai_asr"
        config = types.SimpleNamespace(
            get_app_config=lambda path, default=None: (
                self.input_mode if path == "muse.input_mode" else default
            )
        )
        for patcher in (
            mock.patch.object(external.ConfigManager, "instance", return_value=config),
            mock.patch.object(external, "get_speaker", return_value=self.speaker),
            mock.patch.object(self.followup, "get_vad", return_value=None),
            mock.patch.object(muse, "get_kws", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def wait_for_asr_future(self, capture):
        for _ in range(200):
            if capture._xiaoai_asr_future:
                return
            await asyncio.sleep(0.01)
        self.fail("capture did not start waiting for XiaoAI ASR")

    async def test_xiaoai_asr_followup(self):
        capture = self.followup.MuseFollowupCapture(timeout=2)
        task = asyncio.create_task(capture.capture_once())
        await self.wait_for_asr_future(capture)

        # 部分结果被吞掉，最终结果作为追问文本
        self.assertTrue(
            capture.consume_xiaoai_recognize_result(
                dialog_id="d1", text="我今晚", is_final=False, is_vad_begin=None
            )
        )
        self.assertTrue(
            capture.consume_xiaoai_recognize_result(
                dialog_id="d1", text="我今晚想洗澡", is_final=True, is_vad_begin=None
            )
        )
        self.assertEqual(await task, "我今晚想洗澡")
        self.assertFalse(capture.is_active())
        self.assertIn(("wake_up", True), self.events)
        self.assertEqual(self.events[-1], ("wake_up", False))  # 结束后退出小爱原生监听

    async def test_xiaoai_asr_followup_timeout(self):
        capture = self.followup.MuseFollowupCapture(timeout=0.3)
        self.assertIsNone(await capture.capture_once())
        self.assertFalse(capture.is_active())

    async def test_handler_stop_cancels_followup_quietly(self):
        handler = muse.MuseRequestHandler(
            settings=muse.MuseSettings(followup_timeout=5), speaker=self.speaker
        )
        task = asyncio.create_task(handler.capture_followup())
        for _ in range(200):
            if handler._capture and handler._capture._xiaoai_asr_future:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(handler.is_active())
        handler.stop()  # 例如用户喊了"小爱同学"
        self.assertIsNone(await task)
        self.assertFalse(handler.is_active())
        self.assertNotIn(("wake_up", False), self.events)

    async def test_local_asr_without_vad_returns_none(self):
        self.input_mode = "local_asr"
        capture = self.followup.MuseFollowupCapture(timeout=1)
        with self.assertLogs("xiaozhi", level="WARNING"):
            self.assertIsNone(await capture.capture_once())


if __name__ == "__main__":
    unittest.main()
