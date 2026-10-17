"""HTTP API 回归测试（/api/play/text 主动播报、鉴权），无需音箱。

    python -m pytest tests/test_api_server.py
"""

import asyncio
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())


class _ConfigStub:
    def get_app_config(self, path=None, default=None):
        return default


# DoubaoTTS 在类定义时就调用 ConfigManager.instance()；真实实例会联网请求 OTA
# 并把 DEVICE_ID 写回 ./config.py，所以导入期间用桩替换。
with mock.patch("core.utils.config.ConfigManager.instance", return_value=_ConfigStub()):
    api_module = importlib.import_module("core.services.api_server")
ref = importlib.import_module("core.ref")

TEST_TOKEN = "unit-test-api-token"


class FakeSpeaker:
    def __init__(self):
        self.calls = []

    async def play(self, text=None, url=None, blocking=True, timeout=None, **kwargs):
        self.calls.append(("play", text or url, blocking))
        return True

    async def play_server_file(self, file_path, blocking=True, sample_rate=24000):
        self.calls.append(("play_file", os.path.exists(file_path), sample_rate))
        return True

    async def get_playing(self, sync=False):
        return "idle"

    async def stop_device_audio(self):
        self.calls.append(("stop_device_audio",))

    async def wake_up(self, awake=True, silent=True):
        self.calls.append(("wake_up", awake, silent))
        return True


class APIServerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.speaker = FakeSpeaker()
        self.xiaoai = types.SimpleNamespace(stop_conversation=mock.Mock())
        saved = dict(ref.GLOBAL_STATES)
        ref.GLOBAL_STATES.update(speaker=self.speaker, xiaoai=self.xiaoai)
        self.addCleanup(lambda: (ref.GLOBAL_STATES.clear(), ref.GLOBAL_STATES.update(saved)))

    async def make_client(self, token: str = "") -> TestClient:
        with (
            mock.patch.object(api_module.ConfigManager, "instance", return_value=_ConfigStub()),
            mock.patch.dict(os.environ, {"API_SERVER_TOKEN": token}),
        ):
            server = api_module.APIServer(host="127.0.0.1", port=0)
        client = TestClient(TestServer(server.app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def settle(self):
        # 非阻塞播放通过 create_task 在后台执行
        for _ in range(3):
            await asyncio.sleep(0)


class PlayTextAPITest(APIServerTestBase):
    async def test_play_text_background(self):
        client = await self.make_client()
        resp = await client.post("/api/play/text", json={"text": "好的，已安排"})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["success"])
        self.assertNotIn("request_id", body)
        await self.settle()
        self.assertEqual(self.speaker.calls, [("play", "好的，已安排", False)])

    async def test_play_text_blocking_with_request_id(self):
        client = await self.make_client()
        resp = await client.post(
            "/api/play/text",
            json={"text": "好的", "blocking": True, "timeout": 5000, "request_id": "req-123"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"success": True, "request_id": "req-123"})
        self.assertEqual(self.speaker.calls, [("play", "好的", True)])

    async def test_request_id_is_logged_and_non_string_accepted(self):
        client = await self.make_client()
        with self.assertLogs("xiaozhi", level="INFO") as logs:
            resp = await client.post("/api/play/text", json={"text": "好", "request_id": 42})
        self.assertEqual((await resp.json())["request_id"], "42")
        self.assertTrue(any("request_id=42" in line for line in logs.output))

    async def test_missing_text_and_invalid_json(self):
        client = await self.make_client()
        resp = await client.post("/api/play/text", json={"request_id": "x"})
        self.assertEqual(resp.status, 400)
        resp = await client.post(
            "/api/play/text", data="{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.speaker.calls, [])


class OtherEndpointsTest(APIServerTestBase):
    async def test_play_url_interrupt_status_health_without_auth(self):
        client = await self.make_client()

        resp = await client.post("/api/play/url", json={"url": "http://127.0.0.1/a.mp3", "blocking": True})
        self.assertEqual((resp.status, (await resp.json())["success"]), (200, True))

        resp = await client.post("/api/interrupt")
        self.assertEqual(resp.status, 200)
        self.xiaoai.stop_conversation.assert_called_once()

        resp = await client.get("/api/status")
        self.assertEqual((await resp.json())["data"]["status"], "idle")

        resp = await client.get("/api/health")
        self.assertTrue((await resp.json())["data"]["speaker_ready"])

        self.assertEqual(
            self.speaker.calls,
            [("play", "http://127.0.0.1/a.mp3", True), ("stop_device_audio",)],
        )

    async def test_play_file_upload(self):
        client = await self.make_client()
        form = FormData()
        form.add_field("file", b"ID3fake-mp3", filename="a.mp3", content_type="audio/mpeg")
        resp = await client.post("/api/play/file?blocking=true", data=form)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["size"], len(b"ID3fake-mp3"))
        self.assertEqual(self.speaker.calls, [("play_file", True, 24000)])


class APITokenAuthTest(APIServerTestBase):
    async def test_requires_bearer_token_when_configured(self):
        client = await self.make_client(token=TEST_TOKEN)

        resp = await client.post("/api/play/text", json={"text": "你好"})
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(await resp.json(), {"success": False, "error": "Unauthorized"})

        for header in ("Bearer wrong-token", f"Basic {TEST_TOKEN}", TEST_TOKEN, "Bearer"):
            with self.subTest(header=header):
                resp = await client.post(
                    "/api/play/text", json={"text": "你好"}, headers={"Authorization": header}
                )
                self.assertEqual(resp.status, 401)

        await self.settle()
        self.assertEqual(self.speaker.calls, [])

        resp = await client.post(
            "/api/play/text",
            json={"text": "你好", "request_id": "r1"},
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["request_id"], "r1")

    async def test_scheme_is_case_insensitive(self):
        client = await self.make_client(token=TEST_TOKEN)
        resp = await client.post(
            "/api/interrupt", headers={"Authorization": f"bearer {TEST_TOKEN}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_health_stays_open_and_other_endpoints_are_protected(self):
        client = await self.make_client(token=TEST_TOKEN)
        resp = await client.get("/api/health")
        self.assertEqual(resp.status, 200)

        for method, path in (
            ("GET", "/api/status"),
            ("POST", "/api/interrupt"),
            ("POST", "/api/play/url"),
            ("POST", "/api/play/file"),
            ("POST", "/api/wakeup"),
            ("GET", "/api/tts/doubao_voices"),
        ):
            with self.subTest(path=path):
                resp = await client.request(method, path)
                self.assertEqual(resp.status, 401)

        resp = await client.get("/api/status", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        self.assertEqual(resp.status, 200)

    async def test_token_not_logged_on_unauthorized(self):
        client = await self.make_client(token=TEST_TOKEN)
        with self.assertLogs("xiaozhi", level="WARNING") as logs:
            await client.post("/api/interrupt", headers={"Authorization": "Bearer guess"})
        output = "\n".join(logs.output)
        self.assertNotIn(TEST_TOKEN, output)
        self.assertNotIn("guess", output)

    async def test_blank_token_env_keeps_no_auth_behaviour(self):
        client = await self.make_client(token="   ")
        resp = await client.post("/api/play/text", json={"text": "你好"})
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
