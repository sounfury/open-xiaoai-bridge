"""阶段 1 回归：新客户端录音帧（带 metadata / 双声道 mic_ref）进入 GlobalStream 的处理。

无需音箱和原生扩展：用桩替代 open_xiaoai_server 和 ConfigManager。
"""

import json
import sys
import types
import wave

import numpy as np
import pytest

_oxs = sys.modules.get("open_xiaoai_server")
if _oxs is None:
    try:
        import open_xiaoai_server as _oxs  # noqa: F401
    except ImportError:
        _oxs = types.ModuleType("open_xiaoai_server")
        _oxs.decode_audio = lambda *a, **k: None
        sys.modules["open_xiaoai_server"] = _oxs


class _FakeConfig:
    values: dict = {}

    def get_app_config(self, key, default=None):
        node = self.values
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def add_reload_listener(self, _listener):
        pass


_FAKE = _FakeConfig()

import core.utils.config as _config_mod  # noqa: E402

_config_mod.ConfigManager.instance = staticmethod(lambda: _FAKE)

from core.services.audio.stream import GlobalStream  # noqa: E402
from core.xiaoai import XiaoAI  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    chunks: list[bytes] = []
    monkeypatch.setattr(GlobalStream, "input", lambda data: chunks.append(data))
    monkeypatch.setattr(XiaoAI, "_input_gain_enabled", False)
    monkeypatch.setattr(XiaoAI, "_input_gain", 1.0)
    monkeypatch.setattr(XiaoAI, "_echo_dump", None)
    return chunks


def test_mono_packet_passes_through(captured):
    pcm = np.array([1, -2, 300, -32768], dtype="<i2").tobytes()
    XiaoAI.on_input_packet((pcm, json.dumps({"seq": 3, "capture_id": "x"})))
    assert captured == [pcm]


def test_packet_without_metadata_passes_through(captured):
    pcm = np.array([5, 6], dtype="<i2").tobytes()
    XiaoAI.on_input_packet((pcm, "null"))
    assert captured == [pcm]


def test_mic_ref_keeps_mic_channel_with_gain(captured, monkeypatch):
    monkeypatch.setattr(XiaoAI, "_echo_mic_gain", 64.0)
    # [mic, ref] 交错：mic 声道 10、-20、1000（1000*64 会削顶），ref 声道随意
    stereo = np.array([[10, 999], [-20, -999], [1000, 7]], dtype="<i2")
    XiaoAI.on_input_packet((stereo.tobytes(), json.dumps({"layout": "mic_ref"})))
    mono = np.frombuffer(captured[0], dtype="<i2")
    assert mono.tolist() == [640, -1280, 32767]


def test_mic_ref_dump_writes_stereo_wav(captured, monkeypatch, tmp_path):
    path = tmp_path / "dump.wav"
    monkeypatch.setattr(
        XiaoAI,
        "_echo_dump",
        {"path": str(path), "max_frames": 4, "chunks": [], "frames": 0},
    )
    written = []
    monkeypatch.setattr(
        "core.xiaoai.threading.Thread",
        lambda target, daemon: types.SimpleNamespace(start=lambda: written.append(target())),
    )
    stereo = np.arange(12, dtype="<i2").reshape(-1, 2)  # 6 帧，超过 max_frames
    XiaoAI.on_input_packet((stereo.tobytes(), json.dumps({"layout": "mic_ref"})))
    assert written and XiaoAI._echo_dump is None
    with wave.open(str(path), "rb") as f:
        assert (f.getnchannels(), f.getframerate(), f.getnframes()) == (2, 16000, 4)
        frames = np.frombuffer(f.readframes(4), dtype="<i2").reshape(-1, 2)
    assert frames.tolist() == stereo[:4].tolist()


def test_setup_echo_reference_sends_capture(monkeypatch):
    sent = []
    monkeypatch.setattr(_oxs, "set_echo_ref", lambda cfg: sent.append(cfg), raising=False)
    monkeypatch.setattr(
        _FAKE,
        "values",
        {"barge_in": {"echo_reference": True, "capture": {"ref_channel": 2}, "echo_mic_gain": 32}},
    )
    monkeypatch.delenv("ECHO_REF_DUMP", raising=False)
    monkeypatch.delenv("BARGE_IN_ECHO_REFERENCE", raising=False)
    XiaoAI._setup_echo_reference()
    capture = json.loads(sent[0])
    assert capture["pcm"] == "Capture" and capture["ref_channel"] == 2
    assert XiaoAI._echo_mic_gain == 32.0


def test_setup_echo_reference_disabled_by_default(monkeypatch):
    sent = []
    monkeypatch.setattr(_oxs, "set_echo_ref", lambda cfg: sent.append(cfg), raising=False)
    monkeypatch.setattr(_FAKE, "values", {})
    monkeypatch.delenv("BARGE_IN_ECHO_REFERENCE", raising=False)
    XiaoAI._setup_echo_reference()
    assert sent == []


@pytest.mark.parametrize("env,config_on,expected", [("1", False, 1), ("0", True, 0)])
def test_env_overrides_config(monkeypatch, env, config_on, expected):
    sent = []
    monkeypatch.setattr(_oxs, "set_echo_ref", lambda cfg: sent.append(cfg), raising=False)
    monkeypatch.setattr(_FAKE, "values", {"barge_in": {"echo_reference": config_on}})
    monkeypatch.setenv("BARGE_IN_ECHO_REFERENCE", env)
    monkeypatch.delenv("ECHO_REF_DUMP", raising=False)
    XiaoAI._setup_echo_reference()
    assert len(sent) == expected
