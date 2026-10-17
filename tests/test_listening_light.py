"""监听灯效回归测试：无需音箱硬件，用假 speaker 记录 ubus 命令。"""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# 开发机上可能没有设备侧依赖，缺失时用最小桩替代，仅为让模块可导入。
try:
    import open_xiaoai_server  # noqa: F401
except ImportError:
    _oxs = types.ModuleType("open_xiaoai_server")
    _oxs.decode_audio = lambda *a, **k: None
    sys.modules["open_xiaoai_server"] = _oxs

try:
    import core.utils.config  # noqa: F401
except ImportError:
    _cfg_mod = types.ModuleType("core.utils.config")
    _cfg_mod.ConfigManager = type(
        "ConfigManager", (), {"instance": staticmethod(lambda: None)}
    )
    sys.modules["core.utils.config"] = _cfg_mod

import core.external_conversation as ext  # noqa: E402
from core import ref  # noqa: E402
from core.muse_followup import MuseFollowupCapture  # noqa: E402
from core.services.listening_light import ListeningLightController  # noqa: E402

SHOW = "ubus -t 1 call led show '{\"L\":9}'"
SHUT = "ubus -t 1 call led shut '{\"L\":9}'"


class FakeConfig:
    def __init__(self, values: dict):
        self.values = values

    def get_app_config(self, key, default=None):
        return self.values.get(key, default)


def make_speaker(exit_code: int = 0):
    speaker = MagicMock()
    speaker.run_shell = AsyncMock(return_value=SimpleNamespace(exit_code=exit_code))
    speaker.wake_up = AsyncMock()
    speaker.play = AsyncMock()
    speaker.stop_device_audio = AsyncMock()
    return speaker


def commands(speaker) -> list[str]:
    return [c.args[0] for c in speaker.run_shell.await_args_list]


class OpenClawLike(ext.ExternalConversationController):
    CONFIG_PREFIX = "openclaw"
    MANAGER = SimpleNamespace(_rule_prompt="")


@pytest.fixture
def env(monkeypatch):
    """隔离全局状态：独立的灯效控制器、配置、speaker、设备 RPC。"""
    config = FakeConfig({
        "openclaw.listening_light": {"enabled": True, "effect_id": 9},
        "openclaw.input_mode": "local_asr",
        "muse.listening_light": {"enabled": True, "effect_id": 9},
        "muse.input_mode": "local_asr",
        "wakeup.timeout": 1,
    })
    monkeypatch.setattr(ext, "ConfigManager", SimpleNamespace(instance=lambda: config))
    monkeypatch.setattr(ext, "ListeningLight", ListeningLightController())
    monkeypatch.setattr(ext, "open_xiaoai_server", SimpleNamespace(
        stop_recording=AsyncMock(),
        start_recording=AsyncMock(),
        stop_tts_playback=MagicMock(),
        begin_playback_session=MagicMock(return_value=1),
    ))
    monkeypatch.setattr(ext, "_NOTIFY_PCM", None)
    monkeypatch.setattr(ext, "_SEND_PCM", None)
    speaker = make_speaker()
    monkeypatch.setitem(ref.GLOBAL_STATES, "speaker", speaker)
    monkeypatch.setitem(ref.GLOBAL_STATES, "vad", MagicMock())
    return SimpleNamespace(config=config, speaker=speaker)


async def drain(n: int = 10):
    for _ in range(n):
        await asyncio.sleep(0)


# ---- ListeningLightController ----

def test_lease_turn_on_and_off():
    async def main():
        light = ListeningLightController()
        speaker = make_speaker()
        token = await light.turn_on(speaker, 9)
        assert token is not None
        await light.turn_off(token)
        await light.turn_off(token)  # 重复释放无副作用
        assert commands(speaker) == [SHOW, SHUT]

    asyncio.run(main())


def test_stale_token_does_not_shut_new_owner():
    async def main():
        light = ListeningLightController()
        speaker = make_speaker()
        old = await light.turn_on(speaker, 9)
        new = await light.turn_on(speaker, 9)
        await light.turn_off(old)
        assert commands(speaker) == [SHOW, SHOW]
        await light.turn_off(new)
        assert commands(speaker) == [SHOW, SHOW, SHUT]

    asyncio.run(main())


@pytest.mark.parametrize("speaker_ok, effect_id", [(False, 9), (True, 0), (True, 256), (True, "9"), (True, True)])
def test_invalid_input_is_noop(speaker_ok, effect_id):
    async def main():
        light = ListeningLightController()
        speaker = make_speaker()
        token = await light.turn_on(speaker if speaker_ok else None, effect_id)
        assert token is None
        assert commands(speaker) == []
        await light.turn_off(None)

    asyncio.run(main())


def test_failed_show_issues_targeted_shut():
    async def main():
        light = ListeningLightController()
        speaker = make_speaker(exit_code=1)
        assert await light.turn_on(speaker, 9) is None
        assert commands(speaker) == [SHOW, SHUT]

    asyncio.run(main())


# ---- ExternalConversationController 开关与清理 ----

@pytest.mark.parametrize("key, value", [
    ("openclaw.listening_light", {"enabled": False, "effect_id": 9}),
    ("openclaw.listening_light", None),
    ("openclaw.listening_light", "on"),
    ("openclaw.listening_light", ["enabled"]),
    ("openclaw.input_mode", "xiaoai_asr"),
])
def test_gate_skips_light(env, key, value):
    env.config.values[key] = value

    async def main():
        conv = OpenClawLike()
        await conv._turn_on_light()
        assert conv._light_token is None

    asyncio.run(main())
    assert commands(env.speaker) == []


def test_gate_enabled_turns_on(env):
    async def main():
        conv = OpenClawLike()
        await conv._turn_on_light()
        assert conv._light_token is not None
        await conv._turn_off_light()

    asyncio.run(main())
    assert commands(env.speaker) == [SHOW, SHUT]


def test_stop_releases_light_immediately(env):
    async def main():
        conv = OpenClawLike()
        conv.active = True
        conv._loop = asyncio.get_running_loop()
        await conv._turn_on_light()
        conv.stop()
        assert conv._light_token is None
        await drain()
        assert commands(env.speaker) == [SHOW, SHUT]
        await conv._turn_off_light()  # 采集协程随后的清理不应重复熄灯

    asyncio.run(main())
    assert commands(env.speaker) == [SHOW, SHUT]


def test_timeout_turns_light_off(env):
    async def main():
        conv = OpenClawLike()
        conv.active = True
        conv._loop = asyncio.get_running_loop()
        conv._wait_for_speech = AsyncMock(return_value=None)
        assert await conv._run_one_turn_with_local_asr() == "timeout"
        assert conv._light_token is None

    asyncio.run(main())
    assert commands(env.speaker) == [SHOW, SHUT]


def test_interrupt_while_turning_on(env):
    conv_box = {}

    async def show_then_interrupt(cmd, timeout=None):
        if " show " in cmd:
            conv_box["conv"].stop()  # 亮灯 RPC 期间被打断
        return SimpleNamespace(exit_code=0)

    env.speaker.run_shell.side_effect = show_then_interrupt

    async def main():
        conv = OpenClawLike()
        conv_box["conv"] = conv
        conv.active = True
        conv._loop = asyncio.get_running_loop()
        conv._wait_for_speech = AsyncMock(return_value=b"x")
        assert await conv._run_one_turn_with_local_asr() == "interrupted"
        conv._wait_for_speech.assert_not_awaited()
        await drain()

    asyncio.run(main())
    assert commands(env.speaker) == [SHOW, SHUT]


def test_interrupted_loop_skips_goodbye(env):
    async def main():
        conv = OpenClawLike()
        conv.active = True
        conv._loop = asyncio.get_running_loop()
        conv._run_one_turn_with_local_asr = AsyncMock(return_value="interrupted")
        conv._call_after_wakeup = AsyncMock()
        conv._stop_xiaoai_native_listening = AsyncMock()
        await conv._conversation_loop()
        conv._run_one_turn_with_local_asr.assert_awaited_once()
        conv._call_after_wakeup.assert_not_awaited()
        conv._stop_xiaoai_native_listening.assert_not_awaited()

    asyncio.run(main())


# ---- Muse 追问 ----

def test_muse_stop_extinguishes_light_before_capture_returns(env):
    async def main():
        gate = asyncio.Event()
        muse = MuseFollowupCapture(timeout=1)

        async def fake_wait_for_speech(_vad):
            await gate.wait()
            return None

        muse._wait_for_speech = fake_wait_for_speech
        task = asyncio.create_task(muse.capture_once())
        await drain()
        assert commands(env.speaker) == [SHOW]

        muse.stop()
        await drain()
        assert not task.done()
        assert commands(env.speaker) == [SHOW, SHUT]

        gate.set()
        assert await task is None

    asyncio.run(main())
    assert commands(env.speaker) == [SHOW, SHUT]
