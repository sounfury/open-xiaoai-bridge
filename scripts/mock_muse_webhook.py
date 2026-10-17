#!/usr/bin/env python3
"""本地 mock 小爱管家 webhook：打印 bridge 发来的 payload，用于手动联调。

用法:
    python scripts/mock_muse_webhook.py                     # 监听 http://127.0.0.1:18080/webhook
    MUSE_WEBHOOK_URL=http://127.0.0.1:18080/webhook MUSE_ENABLE=1 uv run main.py

可选:
    --require-token          校验 Authorization: Bearer $MUSE_WEBHOOK_TOKEN，不匹配返回 401
    --status 500             固定返回的状态码（测试失败提示）
    --fail-first 2           前 N 次返回 500，之后返回 --status（测试重试）
    --delay 3                每次响应前等待的秒数（测试超时）
    --reply-to http://127.0.0.1:9092/api/play/text
                             模拟小爱管家经 AgentDock 回推播报（PRD 7.2），
                             bridge 设置了 API_SERVER_TOKEN 时会自动带上
    --reply-text "收到：{text}"
"""

import argparse
import asyncio
import hmac
import json
import os
from datetime import datetime

import aiohttp
from aiohttp import web


def _log(message: str):
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {message}", flush=True)


def build_app(args) -> web.Application:
    state = {"count": 0}
    expected_token = os.environ.get("MUSE_WEBHOOK_TOKEN", "").strip()
    if args.require_token and not expected_token:
        raise SystemExit("--require-token 需要设置环境变量 MUSE_WEBHOOK_TOKEN")

    async def push_reply(payload: dict):
        await asyncio.sleep(args.reply_delay)
        body = {
            "text": args.reply_text.format(text=payload.get("text", "")),
            "request_id": payload.get("request_id"),
        }
        headers = {}
        api_token = os.environ.get("API_SERVER_TOKEN", "").strip()
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(args.reply_to, json=body, headers=headers) as resp:
                    _log(f"↩️  回推 {args.reply_to} → HTTP {resp.status}: {await resp.text()}")
        except Exception as exc:
            _log(f"↩️  回推失败: {type(exc).__name__}: {exc}")

    async def handle(request: web.Request) -> web.Response:
        state["count"] += 1
        raw = await request.text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None

        auth = request.headers.get("Authorization")
        _log(
            f"📥 #{state['count']} {request.method} {request.path} "
            f"Authorization: {'present' if auth else 'absent'}"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else raw, flush=True)

        if args.require_token and not hmac.compare_digest(
            (auth or "").encode(), f"Bearer {expected_token}".encode()
        ):
            _log("   → 401 (token mismatch)")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        if args.delay:
            await asyncio.sleep(args.delay)

        status = 500 if state["count"] <= args.fail_first else args.status
        _log(f"   → {status}")
        if 200 <= status < 300 and args.reply_to and isinstance(payload, dict):
            asyncio.create_task(push_reply(payload))
        return web.json_response({"ok": 200 <= status < 300}, status=status)

    app = web.Application()
    app.router.add_post(args.path, handle)
    return app


def main():
    parser = argparse.ArgumentParser(description="Mock 小爱管家 webhook")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--path", default="/webhook")
    parser.add_argument("--require-token", action="store_true")
    parser.add_argument("--status", type=int, default=200)
    parser.add_argument("--fail-first", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--reply-to", default="")
    parser.add_argument("--reply-text", default="收到：{text}")
    parser.add_argument("--reply-delay", type=float, default=1.0)
    args = parser.parse_args()

    print(f"Mock webhook: http://{args.host}:{args.port}{args.path}", flush=True)
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
