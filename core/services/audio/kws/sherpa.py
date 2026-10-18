"""本地关键词识别：加载 Sherpa KWS 模型并处理音箱音频帧。

模型按固定长度的块（约 320 ms）处理音频，唤醒词落在块里的位置会影响能不能认出来。
stream_offsets_ms 可以同时跑几路检测、起点互相错开，漏掉的不是同一批，任一路认出即唤醒
（移植自 OwnDing/open-xiaoai）。
"""

import numpy as np

from core.utils.onnx_runtime import prepare_onnxruntime

prepare_onnxruntime()
import sherpa_onnx

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path

SAMPLE_RATE = 16000


def normalize_offsets(offsets_ms) -> list[int]:
    """[0, 160] 这类毫秒列表 → 去重排序后的采样数；空 / 非法时只跑一路。"""
    values: set[int] = set()
    for ms in offsets_ms or (0,):
        try:
            values.add(max(0, int(ms)))
        except (TypeError, ValueError):
            continue
    return [ms * SAMPLE_RATE // 1000 for ms in sorted(values or {0})]


class SherpaKeywordSpotter:
    """一个关键词模型 + 若干路错开的检测流。只在一个线程里用。"""

    def __init__(
        self,
        keywords_file: str,
        *,
        max_active_paths: int = 8,
        keywords_score: float = 2.0,
        keywords_threshold: float = 0.2,
        stream_offsets_ms=(0,),
        num_threads: int = 1,
    ):
        self.keyword_spotter = sherpa_onnx.KeywordSpotter(
            provider="cpu",
            num_threads=num_threads,
            max_active_paths=int(max_active_paths),
            keywords_score=float(keywords_score),
            keywords_threshold=float(keywords_threshold),
            num_trailing_blanks=0,
            keywords_file=keywords_file,
            tokens=get_model_file_path("tokens.txt"),
            encoder=get_model_file_path("encoder.onnx"),
            decoder=get_model_file_path("decoder.onnx"),
            joiner=get_model_file_path("joiner.onnx"),
        )
        self.offsets = normalize_offsets(stream_offsets_ms)
        self.streams = []
        self.skip: list[int] = []
        self.reset()

    def reset(self):
        """丢掉所有检测流的特征和解码状态（暂停过的音频不连续，旧状态不能接着用）。"""
        self.streams = [self.keyword_spotter.create_stream() for _ in self.offsets]
        self.skip = list(self.offsets)  # 每路还要丢掉多少采样才能保持错开

    def kws(self, frames: bytes) -> str | None:
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        for i, stream in enumerate(self.streams):
            skip = min(self.skip[i], len(samples))
            self.skip[i] -= skip
            if skip < len(samples):
                stream.accept_waveform(SAMPLE_RATE, samples[skip:])
        while True:
            ready = [s for s in self.streams if self.keyword_spotter.is_ready(s)]
            if not ready:
                return None
            self.keyword_spotter.decode_streams(ready)
            for stream in ready:
                result = self.keyword_spotter.get_result(stream)
                if result:
                    # 一次说话只唤醒一次，不是每路各一次
                    for s in self.streams:
                        self.keyword_spotter.reset_stream(s)
                    return result.lower()


def spotter_settings(config_getter=None) -> dict:
    """从 config 的 kws 段读取 SherpaKeywordSpotter 参数。"""
    getter = config_getter or ConfigManager.instance().get_app_config
    return {
        "max_active_paths": int(getter("kws.max_active_paths", 8) or 8),
        "keywords_score": float(getter("kws.keywords_score", 2.0)),
        "keywords_threshold": float(getter("kws.keywords_threshold", 0.2)),
        "stream_offsets_ms": getter("kws.stream_offsets_ms", [0]) or [0],
    }


class _SherpaOnnx:
    """线上用的全局实例：start() 按 config 和 core/models/keywords.txt 建模型。"""

    spotter: SherpaKeywordSpotter | None = None

    def start(self):
        self.spotter = SherpaKeywordSpotter(
            get_model_file_path("keywords.txt"), **spotter_settings()
        )

    def reset(self):
        """Reset the streams to discard any partial recognition state."""
        if self.spotter is not None:
            self.spotter.reset()

    def kws(self, frames):
        return self.spotter.kws(frames) if self.spotter is not None else None


SherpaOnnx = _SherpaOnnx()
