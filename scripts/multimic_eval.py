"""多麦前端离线评估：同一段 4 声道录音，用和线上相同的唤醒检测（config.py 的 kws / vad 参数）
分别跑"只用 ch0（现状）""每一路麦克风""3 路求和""IVA 分离后的 3 路"，比较命中和误唤醒。

    .venv/Scripts/python.exe scripts/multimic_eval.py far_5m.wav --expect 15
    .venv/Scripts/python.exe scripts/multimic_eval.py far_5m.wav --asr        # 用 ASR 自动标注每段说了什么
    .venv/Scripts/python.exe scripts/multimic_eval.py tv_noise.wav            # 没说唤醒词：所有命中都是误唤醒

录音来自 scripts/multimic_record.py（16 kHz S16 4 声道：3 麦 + 回采，≈ 原始 S32 >> 16）。
线上链路相当于原始 S32 >> 8（客户端 >> 14 再乘 echo_mic_gain=64），所以这里默认乘 256 对齐电平。

--asr：用 VAD 在"3 路求和"上切出说话段，SenseVoice 逐段识别；文本里有缪斯（含同音字）的段
当作"说了唤醒词"。命中落在某段附近算命中，否则算误唤醒。
"""

import argparse
import os
import sys
import time
import wave
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("MUSE_ENABLE", "1")

import numpy as np  # noqa: E402

from core.services.audio.multimic import ChannelSum, OnlineAuxIVA, to_int16  # noqa: E402

SR = 16000
MUSE_MARKS = ("缪斯", "缪思", "缪丝", "谬斯", "谬思", "妙思", "妙斯", "木斯", "慕斯", "muse", "mus")
MERGE_S = 2.0  # 不同信号的命中相隔这么近算同一次


def load(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getsampwidth() == 2, "需要 16 kHz S16"
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, w.getnchannels())
    return data.astype(np.float64)


def build_signals(x: np.ndarray, gain: float, alpha: float, block: int = 1024) -> dict[str, np.ndarray]:
    mics = x[:, :3]
    if x.shape[1] > 3 and np.any(x[:, 3]):
        # 录音里有播放：和线上一样每路先用 ch3 回采消回声
        from core.services.audio.aec import SubbandEchoCanceller

        echo = [SubbandEchoCanceller() for _ in range(3)]
        mics = np.concatenate(
            [
                np.stack([echo[c].process(mics[i : i + block, c], x[i : i + block, 3]) for c in range(3)], axis=1)
                for i in range(0, len(mics), block)
            ]
        )
        print("录音里有播放，已按线上方式逐路回声消除")
    signals = {f"ch{c}": to_int16(mics[:, c], gain) for c in range(3)}
    signals["sum"] = to_int16(ChannelSum().process(mics)[:, 0], gain)
    iva = OnlineAuxIVA(3, alpha=alpha)
    started = time.perf_counter()
    out = np.concatenate([iva.process(mics[i : i + block]) for i in range(0, len(mics), block)])
    cost = time.perf_counter() - started
    print(f"IVA 用时 {cost:.1f}s（音频 {len(mics) / SR:.0f}s，{cost / (len(mics) / SR):.1%} 实时）")
    for k in range(3):
        signals[f"iva{k}"] = to_int16(out[:, k], gain)
    return signals


# ---- 唤醒检测（每个信号一个进程，和线上同一份 KeywordDetector）----


def _kws_worker(task):
    name, audio, overrides = task
    from kws_acceptance import Bench

    from core.utils.config import ConfigManager

    base = ConfigManager.instance().get_app_config

    def getter(path=None, default=None):
        return overrides[path] if path in overrides else base(path, default)

    bench = Bench(getter)
    hits = bench.run(audio)
    cpu = bench.cpu_s
    bench.close()
    return name, hits, cpu


def run_kws(signals: dict[str, np.ndarray], overrides: dict, jobs: int) -> dict[str, list]:
    tasks = [(name, audio, overrides) for name, audio in signals.items()]
    results = {}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for name, hits, cpu in pool.map(_kws_worker, tasks):
            results[name] = hits
            print(f"  {name}: {len(hits)} 次（KWS {cpu:.0f}s）", flush=True)
    return results


def union(hit_lists) -> list[float]:
    times = sorted(t for hits in hit_lists for t, _ in hits)
    merged: list[float] = []
    for t in times:
        if not merged or t - merged[-1] > MERGE_S:
            merged.append(t)
    return merged


# ---- ASR 标注 ----


def label_segments(audio: np.ndarray) -> list[tuple[float, float, str, bool]]:
    from core.services.audio.asr.sherpa import SherpaASR
    from core.services.audio.vad.silero import create_silero

    vad = create_silero()
    frame = 512
    probs = np.array([vad.vad(audio[i : i + frame].tobytes(), SR) or 0 for i in range(0, len(audio) - frame, frame)])
    speech = probs > 0.5
    segs, start, silence = [], None, 0
    for i, s in enumerate(speech):
        if s:
            start = i if start is None else start
            silence = 0
        elif start is not None:
            silence += 1
            if silence > 15:  # 约 0.5 s 静音断句
                segs.append((start, i - silence + 1))
                start, silence = None, 0
    if start is not None:
        segs.append((start, len(speech)))
    out = []
    for a, b in segs:
        t0, t1 = a * frame / SR, b * frame / SR
        if t1 - t0 < 0.3:
            continue
        pcm = audio[max(0, int((t0 - 0.2) * SR)) : int((t1 + 0.2) * SR)]
        text = SherpaASR.asr(pcm.tobytes())
        low = text.lower().replace(" ", "")
        out.append((t0, t1, text, any(m in low for m in MUSE_MARKS)))
    return out


def score(times: list[float], segs) -> tuple[int, int]:
    """返回 (命中的唤醒词段数, 误唤醒次数)。"""
    wake = [(a, b) for a, b, _, is_wake in segs if is_wake]
    used = set()
    false = 0
    for t in times:
        idx = next((i for i, (a, b) in enumerate(wake) if a - 0.3 <= t <= b + 1.5), None)
        if idx is None:
            false += 1
        else:
            used.add(idx)
    return len(used), false


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wav")
    parser.add_argument("--gain", type=float, default=256.0, help="对齐线上电平（默认 256）")
    parser.add_argument("--alpha", type=float, default=0.97, help="IVA 遗忘因子")
    parser.add_argument("--expect", type=int, help="实际说了几遍唤醒词")
    parser.add_argument("--asr", action="store_true", help="ASR 自动标注说话段")
    parser.add_argument("--no-vad-gate", action="store_true")
    parser.add_argument("--threshold", type=float, help="覆盖 kws.keywords_threshold")
    parser.add_argument("--save", help="把各路信号存成 <前缀>_<名字>.wav，方便试听")
    parser.add_argument("-j", "--jobs", type=int, default=min(7, os.cpu_count() or 4))
    args = parser.parse_args()

    x = load(args.wav)
    print(f"{args.wav}: {len(x) / SR:.1f}s，{x.shape[1]} 声道")
    for c in range(x.shape[1]):
        print(f"  ch{c}: rms {np.sqrt(np.mean(x[:, c] ** 2)):.1f}  峰值 {np.max(np.abs(x[:, c])):.0f}")
    signals = build_signals(x, args.gain, args.alpha)
    if args.save:
        for name, audio in signals.items():
            with wave.open(f"{args.save}_{name}.wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(audio.tobytes())

    overrides = {}
    if args.no_vad_gate:
        overrides["kws.vad_gate"] = False
    if args.threshold is not None:
        overrides["kws.keywords_threshold"] = args.threshold
    print("唤醒检测：")
    results = run_kws(signals, overrides, args.jobs)

    combos = {
        "ch0（现状）": ["ch0"],
        "ch1": ["ch1"],
        "ch2": ["ch2"],
        "sum": ["sum"],
        "任一麦克风": ["ch0", "ch1", "ch2"],
        "iva 任一路": ["iva0", "iva1", "iva2"],
        "iva 任一路 + ch0": ["iva0", "iva1", "iva2", "ch0"],
        "iva 任一路 + sum": ["iva0", "iva1", "iva2", "sum"],
        "全部": list(signals),
    }
    segs = label_segments(signals["sum"]) if args.asr else None
    if segs is not None:
        wake_n = sum(1 for s in segs if s[3])
        print(f"\nASR 标注：{len(segs)} 段说话，其中 {wake_n} 段含唤醒词")
        for a, b, text, is_wake in segs:
            print(f"  {a:7.1f}-{b:6.1f}s {'★' if is_wake else ' '} {text}")
    print("\n结果：")
    for label, names in combos.items():
        times = union(results[n] for n in names)
        if segs is not None:
            got, false = score(times, segs)
            wake_n = sum(1 for s in segs if s[3])
            print(f"  {label:16s} 命中 {got}/{wake_n}  误唤醒 {false}")
        elif args.expect:
            print(f"  {label:16s} {len(times)} 次 / 说了 {args.expect} 遍")
        else:
            print(f"  {label:16s} {len(times)} 次")
    print("\n各路命中时间：")
    for name, hits in results.items():
        print(f"  {name}: " + ", ".join(f"{t:.1f}s {w}" for t, w in hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
