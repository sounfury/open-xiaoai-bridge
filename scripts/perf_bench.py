"""平台性能基准：离线测线上音频链路处理每 32 ms 音频要多久（不连音箱）。

用 tests/data/kws_tts/.pcm/ 里的样本拼成连续人声（最坏情况：一直有人说话），按线上方式跑：

- 主麦：回声消除（SubbandEchoCanceller）+ 高通
- 多麦：3 麦各自回声消除 + 高通 + 在线 AuxIVA（每 1024 采样一块），和线上一样在独立线程里跑
- 唤醒：ch0 + iva0~2 共 4 个独立检测器（各自 4 条错位解码流），ch0 在当前线程，其余进线程池
- VAD：Silero 每帧一次
- 识别：SenseVoice 识别几段不同长度的音频

    .venv/bin/python scripts/perf_bench.py --seconds 60
    .venv/bin/python scripts/perf_bench.py --no-gate      # 每帧都送唤醒模型（vad_gate=False）

输出各阶段每帧耗时（平均 / p95 / p99 / 最大）和整条链路的实时率（处理耗时 / 音频时长）。
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUSE_ENABLE", "1")

import numpy as np  # noqa: E402

from core.utils.onnx_runtime import prepare_onnxruntime  # noqa: E402

prepare_onnxruntime()

from core.services.audio.aec import SubbandEchoCanceller  # noqa: E402
from core.services.audio.highpass import make_highpass  # noqa: E402
from core.services.audio.kws.detector import FRAME_SAMPLES, KeywordDetector  # noqa: E402
from core.services.audio.kws.sherpa import SherpaOnnx  # noqa: E402
from core.services.audio.multimic import OnlineAuxIVA  # noqa: E402
from core.services.audio.vad.silero import create_silero  # noqa: E402
from core.utils.config import ConfigManager  # noqa: E402

SR = 16000
BLOCK = 1024  # 多麦每块采样数（同 multimic_input.BLOCK）
PCM_DIR = ROOT / "tests" / "data" / "kws_tts" / ".pcm"


def load_speech(seconds: float) -> np.ndarray:
    """把缓存样本首尾相接（中间留 200 ms 空隙），凑够指定时长的 int16 单声道。"""
    files = sorted(PCM_DIR.glob("*.npy"))
    if not files:
        raise SystemExit(f"没有样本缓存：{PCM_DIR}（先在 PC 上跑一次 kws_acceptance.py synth）")
    gap = np.zeros(SR // 5, dtype=np.int16)
    parts, total = [], 0
    while total < seconds * SR:
        for f in files:
            audio = np.load(f).astype(np.int16).reshape(-1)
            parts += [audio, gap]
            total += len(audio) + len(gap)
            if total >= seconds * SR:
                break
    return np.concatenate(parts)[: int(seconds * SR)]


def stats(values_ms: list[float]) -> str:
    a = np.asarray(values_ms)
    return (f"平均 {a.mean():6.2f}  p95 {np.percentile(a, 95):6.2f}  p99 {np.percentile(a, 99):6.2f}"
            f"  最大 {a.max():6.2f} ms")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=60, help="测试音频时长")
    parser.add_argument("--no-gate", action="store_true", help="每帧都跑唤醒模型（不靠 VAD 门控）")
    parser.add_argument("--no-multimic", action="store_true", help="只跑主麦 ch0")
    parser.add_argument("--asr-runs", type=int, default=3)
    args = parser.parse_args()

    cfg = ConfigManager.instance().get_app_config
    speech = load_speech(args.seconds)
    n_frames = len(speech) // FRAME_SAMPLES
    rng = np.random.default_rng(0)
    print(f"音频 {len(speech) / SR:.1f}s，{n_frames} 帧；CPU 亲和：{sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else '-'}")

    # 构造输入：主麦 [麦克风, 回采]；多麦 3 麦（不同延迟 + 噪声）+ 回采
    ref = (rng.standard_normal(len(speech)) * 300).astype(np.float64)
    mic0 = speech.astype(np.float64) / 64 + np.roll(ref, 40) * 0.3
    mics = np.stack([np.roll(speech, d).astype(np.float64) / 256 + rng.standard_normal(len(speech)) * 2
                     for d in (0, 3, 7)], axis=1)
    multi = np.concatenate([mics, ref[:, None] / 256], axis=1)

    gate = not args.no_gate
    settings = {
        "vad_threshold": float(cfg("kws.vad_threshold", cfg("vad.threshold", 0.1)) or 0.1),
        "min_silence_ms": float(cfg("kws.min_silence_duration", 480)),
        "preroll_ms": float(cfg("kws.preroll_ms", 300)),
        "vad_gate": gate,
    }
    t0 = time.perf_counter()
    SherpaOnnx.start()
    names = ["ch0"] if args.no_multimic else ["ch0", "iva0", "iva1", "iva2"]
    detectors = {n: KeywordDetector(SherpaOnnx.new_spotter(), create_silero(), **settings) for n in names}
    vad = create_silero()
    print(f"加载 {len(detectors)} 个唤醒检测器：{time.perf_counter() - t0:.1f}s；vad_gate={gate}，"
          f"stream_offsets_ms={cfg('kws.stream_offsets_ms', [0])}，max_active_paths={cfg('kws.max_active_paths', 8)}")

    aec_main = SubbandEchoCanceller()
    hp_main = make_highpass(cfg("audio_input.highpass_hz", None))
    aec_multi = [SubbandEchoCanceller() for _ in range(3)]
    hp_multi = make_highpass(cfg("audio_input.highpass_hz", None), channels=3)
    iva = OnlineAuxIVA(3, alpha=float(cfg("multimic.iva_alpha", 0.97)))
    pool = ThreadPoolExecutor(max_workers=max(1, len(names) - 1), thread_name_prefix="kws-mic")

    timing: dict[str, list[float]] = {k: [] for k in ("aec_main", "multimic_dsp", "kws", "vad", "wait_multimic", "total")}
    multi_out = {n: bytearray() for n in names[1:]}
    multi_lock = threading.Lock()
    hits: dict[str, int] = {n: 0 for n in names}

    def multimic_worker():
        """线上 multimic-conn 线程：尽快处理每块，结果写进各路缓冲。"""
        for start in range(0, n_frames * FRAME_SAMPLES - BLOCK + 1, BLOCK):
            t = time.perf_counter()
            block = multi[start:start + BLOCK]
            m = np.stack([aec_multi[c].process(block[:, c], block[:, 3]) for c in range(3)], axis=1)
            if hp_multi is not None:
                m = hp_multi.process(m)
            y = iva.process(m)
            with multi_lock:
                for k in range(3):
                    pcm = np.clip(np.round(y[:, k] * 256), -32768, 32767).astype("<i2").tobytes()
                    multi_out[f"iva{k}"].extend(pcm)
            timing["multimic_dsp"].append((time.perf_counter() - t) * 1000)

    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    worker = None
    if not args.no_multimic:
        worker = threading.Thread(target=multimic_worker, daemon=True, name="multimic")
        worker.start()
    need = FRAME_SAMPLES * 2
    for i in range(n_frames):
        s = slice(i * FRAME_SAMPLES, (i + 1) * FRAME_SAMPLES)
        frame_start = time.perf_counter()

        t = time.perf_counter()
        out = aec_main.process(mic0[s], ref[s]) * 64
        if hp_main is not None:
            out = hp_main.process(out)
        frame = np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
        timing["aec_main"].append((time.perf_counter() - t) * 1000)

        chunks = {}
        if worker is not None:
            # 等多麦线程产出这一帧（它落后时这里会等，计入 wait_multimic）
            t = time.perf_counter()
            last_block_end = (n_frames * FRAME_SAMPLES) // BLOCK * BLOCK
            if (i + 1) * FRAME_SAMPLES <= last_block_end:
                while True:
                    with multi_lock:
                        if all(len(multi_out[n]) >= need for n in names[1:]):
                            for n in names[1:]:
                                chunks[n] = bytes(multi_out[n][:need])
                                del multi_out[n][:need]
                            break
                    time.sleep(0.0005)
            timing["wait_multimic"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        futures = [(n, pool.submit(detectors[n].process, c)) for n, c in chunks.items()]
        if detectors["ch0"].process(frame):
            hits["ch0"] += 1
        for n, f in futures:
            if f.result():
                hits[n] += 1
        timing["kws"].append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        vad.vad(frame, SR)
        timing["vad"].append((time.perf_counter() - t) * 1000)

        timing["total"].append((time.perf_counter() - frame_start) * 1000)

    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    audio_s = n_frames * FRAME_SAMPLES / SR
    frame_ms = FRAME_SAMPLES * 1000 / SR
    print(f"\n每帧（{frame_ms:.0f} ms 音频）耗时：")
    for k, v in timing.items():
        if v:
            print(f"  {k:13s} {stats(v)}")
    over = sum(1 for v in timing["total"] if v > frame_ms)
    print(f"\n整条链路：{audio_s:.1f}s 音频用了 {wall:.2f}s，实时率 {wall / audio_s:.3f}；"
          f"单帧超过 {frame_ms:.0f} ms 的 {over} 次（{over / n_frames:.1%}）")
    print(f"进程 CPU：{cpu:.1f}s，相当于 {cpu / audio_s * 100:.0f}% 个核（满负荷一直说话时）")
    print(f"命中次数（仅供参考，拼接样本里有唤醒词）：{hits}")

    # 识别
    from core.services.audio.asr import ASRService

    t = time.perf_counter()
    ASRService.asr(np.zeros(SR // 2, dtype=np.int16).tobytes())
    print(f"\nSenseVoice 首次加载 + 识别：{time.perf_counter() - t:.2f}s")
    for seconds in (2, 5, 10):
        clip = speech[: seconds * SR].tobytes()
        runs = []
        for _ in range(args.asr_runs):
            t = time.perf_counter()
            text = ASRService.asr(clip)
            runs.append(time.perf_counter() - t)
        print(f"  {seconds:2d}s 音频：平均 {np.mean(runs) * 1000:.0f} ms，最快 {min(runs) * 1000:.0f} ms；{text[:30]}")
    pool.shutdown()


if __name__ == "__main__":
    main()
