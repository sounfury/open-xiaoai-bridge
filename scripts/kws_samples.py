"""用 edge-tts 合成唤醒词验收样本（不进项目依赖，单独用 uv 拉起）：

    uv run --no-project --with edge-tts python scripts/kws_samples.py [输出目录]

默认输出到 tests/data/kws_tts/（已 gitignore）：每条一个 mp3 + manifest.json。
已经存在的文件会跳过，可以反复运行补齐。语料见 scripts/kws_phrases.py。
"""

import asyncio
import json
import sys
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kws_phrases import RATES, phrases  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONCURRENCY = 6
RETRIES = 3


def rate_tag(rate: str) -> str:
    return "r0" if rate == "+0%" else ("f" if rate.startswith("+") else "s") + rate.strip("+-%")


async def synth(sem, text, voice, rate, path: Path) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return True
    async with sem:
        for attempt in range(RETRIES):
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
                return True
            except Exception as exc:  # 网络抖动：重试
                if attempt == RETRIES - 1:
                    print(f"FAIL {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                await asyncio.sleep(1 + attempt)
    return False


async def main(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    manifest, jobs = [], []
    for item in phrases():
        rates = RATES if item["kind"] == "wake" else ("+0%",)
        for voice in item["voices"]:
            for rate in rates:
                name = f"{item['id']}_{voice}_{rate_tag(rate)}.mp3"
                manifest.append({**{k: item[k] for k in ("id", "text", "kind", "expect")},
                                 "voice": voice, "rate": rate, "file": name})
                jobs.append(synth(sem, item["text"], voice, rate, out_dir / name))
    results = await asyncio.gather(*jobs)
    ok = [m for m, r in zip(manifest, results) if r]
    (out_dir / "manifest.json").write_text(json.dumps(ok, ensure_ascii=False, indent=1), encoding="utf8")
    print(f"{len(ok)}/{len(manifest)} samples in {out_dir}")
    return 0 if len(ok) == len(manifest) else 1


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "data" / "kws_tts"
    raise SystemExit(asyncio.run(main(target)))
