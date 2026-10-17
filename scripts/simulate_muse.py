#!/usr/bin/env python3
"""无音箱模拟一次"你好缪斯"：走 config.before_wakeup 路由 + MuseRequestHandler。

用法:
    # 终端 1：本地 mock webhook
    python scripts/mock_muse_webhook.py
    # 终端 2：模拟小爱原生 ASR 识别出"你好缪斯，我今晚想洗澡"
    python scripts/simulate_muse.py "你好缪斯，我今晚想洗澡" --url http://127.0.0.1:18080/webhook
    # 模拟 KWS 只听到唤醒词，追问内容由 --followup 代替麦克风
    python scripts/simulate_muse.py 你好缪斯 --source kws --followup "把台灯调暗一点" \
        --url http://127.0.0.1:18080/webhook

音箱动作（播报、拦截原生小爱）只打印，不需要 open_xiaoai_server 原生扩展。
config.py 直接读取（支持 CONFIG_PATH），不会实例化 ConfigManager。
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _log(message: str):
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {message}", flush=True)


class FakeSpeaker:
    async def play(self, text=None, url=None, **kwargs):
        _log(f"🔊 speaker.play(text={text!r})")
        return True

    async def abort_xiaoai(self):
        _log("🛑 speaker.abort_xiaoai()")
        return True

    async def wake_up(self, awake=True, silent=True):
        _log(f"speaker.wake_up(awake={awake})")
        return True


def config_getter(app_config: dict):
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


async def run(args) -> int:
    from core import muse
    from core.utils.config_loader import load_config_module

    config = load_config_module()
    muse._app_config = config_getter(config.APP_CONFIG)

    speaker = FakeSpeaker()
    route = await config.before_wakeup(speaker, args.text, args.source, None)
    _log(f"before_wakeup({args.text!r}, source={args.source!r}) → {route!r}")
    if route != muse.ROUTE:
        _log("未命中缪斯路由（MUSE_ENABLE 已由本脚本默认开启）")
        return 0

    handler = muse.MuseRequestHandler(speaker=speaker)

    async def fake_followup():
        _log(f"🎙️ 追问（模拟）: {args.followup!r}")
        return args.followup

    handler.capture_followup = fake_followup
    result = await handler.handle(args.text, args.source)
    _log(f"result: {result}")
    return 0 if result is None or result.delivered or result.skipped else 1


def main():
    parser = argparse.ArgumentParser(description="无音箱模拟小爱管家路由")
    parser.add_argument("text", nargs="?", default="你好缪斯，我今晚想洗澡")
    parser.add_argument("--source", choices=("xiaoai", "kws"), default="xiaoai")
    parser.add_argument("--url", help="覆盖 MUSE_WEBHOOK_URL")
    parser.add_argument("--followup", default="", help="只说唤醒词时模拟追问内容")
    args = parser.parse_args()

    os.environ.setdefault("MUSE_ENABLE", "1")
    if args.url:
        os.environ["MUSE_WEBHOOK_URL"] = args.url
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
