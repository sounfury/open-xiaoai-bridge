"""音箱定时提醒：Hermes cron 的一次性 no_agent 任务 + 一个只负责播报的小脚本。

- 时间由这里解析成带时区的时间点，不让模型去拼 cron 写法。
- 每条提醒一个脚本 `scripts/reminder-<id>.py`：到点 POST 米家服务 /speak，然后删除自己。
- 任务名统一以「提醒：」开头，列出 / 取消只看这类任务，不碰其他 cron 任务。
- 不另建调度器：持久化、重启恢复、到点触发都由 Hermes cron 负责（每分钟检查一次，可能晚一分钟以内）。
"""
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
SCRIPTS = HERMES_HOME / "scripts"
JOBS = HERMES_HOME / "cron" / "jobs.json"
PREFIX = "提醒："
MAX_DAYS = 366

SCRIPT_TEMPLATE = '''# 音箱提醒 {rid}：由 home_tools reminder_create 生成，Hermes cron（no_agent）到点运行一次后删除自己
import json, os, sys, urllib.request
BODY = json.loads({body})
failed = None
try:
    req = urllib.request.Request("{mijia}/speak", json.dumps(BODY).encode(), {{"Content-Type": "application/json"}})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    if not r.get("ok"):
        failed = r.get("error") or r
except Exception as e:
    failed = f"{{type(e).__name__}}: {{e}}"
finally:
    try:
        os.remove(__file__)
    except OSError:
        pass
if failed:
    print(f"提醒播报失败（{{BODY['text']}}）：{{failed}}")
    sys.exit(1)
'''


def _now() -> datetime:
    return datetime.now(TZ)


def _human(dt: datetime) -> str:
    today = _now().date()
    day = {0: "今天", 1: "明天", 2: "后天"}.get((dt.date() - today).days, dt.strftime("%m月%d日"))
    return f"{day} {dt.strftime('%H:%M')}"


def parse_when(in_minutes, at) -> datetime:
    """in_minutes：多少分钟后；at：'HH:MM'（今天，已过则明天）、'YYYY-MM-DD HH:MM' 或 ISO 时间。"""
    now = _now()
    if in_minutes not in (None, ""):
        minutes = float(in_minutes)
        if minutes < 1:
            raise ValueError("提醒时间至少要在一分钟以后")
        when = now + timedelta(minutes=minutes)
    elif at:
        s = str(at).strip().replace("：", ":")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
        if m:
            when = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            if when <= now:
                when += timedelta(days=1)
        else:
            try:
                when = datetime.fromisoformat(s.replace("/", "-"))
            except ValueError:
                raise ValueError(f"看不懂的时间：{at}。请用 HH:MM、YYYY-MM-DD HH:MM，或给 in_minutes")
            when = when.replace(tzinfo=TZ) if when.tzinfo is None else when.astimezone(TZ)
            if when <= now:
                raise ValueError(f"{_human(when)} 已经过去了")
    else:
        raise ValueError("需要 in_minutes 或 at 其中一个")
    if when - now > timedelta(days=MAX_DAYS):
        raise ValueError("提醒时间太远了，一年以内的才用音箱提醒")
    return when


def _hermes(*args) -> subprocess.CompletedProcess:
    # MCP 进程的环境被 Hermes 过滤过（没有 HERMES_HOME），这里显式补上，否则任务会写到 ~/.hermes
    env = {**os.environ, "HERMES_HOME": str(HERMES_HOME), "TZ": "Asia/Shanghai"}
    return subprocess.run([HERMES_BIN, "cron", *args], capture_output=True, text=True, timeout=60,
                          env=env, cwd="/tmp")


def _clean(out: str) -> str:
    return "\n".join(l for l in out.splitlines() if "out of sync" not in l).strip()


def create(text: str, in_minutes=None, at=None, speaker=None, mijia="http://127.0.0.1:9393", dry_run=False) -> dict:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return {"ok": False, "error": "没有提醒内容"}
    if len(text) > 200:
        return {"ok": False, "error": "提醒内容太长，请精简到一两句话"}
    try:
        when = parse_when(in_minutes, at)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    rid = uuid.uuid4().hex[:8]
    script = SCRIPTS / f"reminder-{rid}.py"
    body = {"text": text, **({"speaker": speaker} if speaker else {}), **({"dry_run": True} if dry_run else {})}
    SCRIPTS.mkdir(parents=True, exist_ok=True)
    script.write_text(SCRIPT_TEMPLATE.format(rid=rid, body=repr(json.dumps(body, ensure_ascii=False)), mijia=mijia))
    _prune()
    name = PREFIX + (text if len(text) <= 40 else text[:39] + "…")
    r = _hermes("create", "--name", name, "--deliver", "local", "--no-agent", "--script", script.name,
                when.isoformat(timespec="seconds"))
    m = re.search(r"Created job:\s*(\w+)", r.stdout)
    if r.returncode != 0 or not m:
        script.unlink(missing_ok=True)
        return {"ok": False, "error": "创建定时任务失败：" + (_clean(r.stderr) or _clean(r.stdout))[:300]}
    return {"ok": True, "id": m.group(1), "text": text, "at": _human(when), "time": when.isoformat(timespec="minutes"),
            "note": "到点由音箱播报，可能晚一分钟以内" + ("；评测模式，到点不会出声" if dry_run else "")}


def _prune():
    """删掉一天前已经跑完的提醒任务和残留脚本，免得 cron 列表越积越多。"""
    try:
        jobs = json.loads(JOBS.read_text()).get("jobs", [])
    except Exception:
        return
    cutoff = _now() - timedelta(days=1)
    for j in jobs:
        if not str(j.get("name", "")).startswith(PREFIX) or j.get("state") != "completed":
            continue
        try:
            done = datetime.fromisoformat(j.get("last_run_at") or "").astimezone(TZ)
        except Exception:
            continue
        if done < cutoff:
            _hermes("remove", j["id"])
            if j.get("script"):
                (SCRIPTS / Path(j["script"]).name).unlink(missing_ok=True)


def _pending() -> list[dict]:
    try:
        jobs = json.loads(JOBS.read_text()).get("jobs", [])
    except Exception:
        return []
    out = []
    for j in jobs:
        if not str(j.get("name", "")).startswith(PREFIX) or j.get("state") == "completed" or not j.get("enabled", True):
            continue
        run_at = j.get("next_run_at") or (j.get("schedule") or {}).get("run_at")
        try:
            when = datetime.fromisoformat(run_at).astimezone(TZ)
        except Exception:
            continue
        if when < _now() - timedelta(minutes=5):
            continue
        out.append({"id": j["id"], "text": j["name"][len(PREFIX):], "at": _human(when), "_when": when,
                    "_script": j.get("script")})
    return sorted(out, key=lambda x: x["_when"])


def list_pending() -> dict:
    items = [{k: v for k, v in p.items() if not k.startswith("_")} for p in _pending()]
    return {"ok": True, "count": len(items), "reminders": items}


def cancel(id=None, text=None) -> dict:
    pend = _pending()
    if id:
        hits = [p for p in pend if p["id"] == str(id).strip()]
    elif text:
        key = re.sub(r"\s", "", str(text))
        hits = [p for p in pend if key in re.sub(r"\s", "", p["text"])]
    else:
        return {"ok": False, "error": "需要 id 或提醒内容"}
    if not hits:
        return {"ok": False, "error": "没有找到对应的待播提醒",
                "reminders": [{"id": p["id"], "text": p["text"], "at": p["at"]} for p in pend]}
    if len(hits) > 1:
        return {"ok": False, "error": "有多条提醒符合，请说清是哪一条",
                "candidates": [{"id": p["id"], "text": p["text"], "at": p["at"]} for p in hits]}
    p = hits[0]
    r = _hermes("remove", p["id"])
    if r.returncode != 0:
        return {"ok": False, "error": "取消失败：" + (_clean(r.stderr) or _clean(r.stdout))[:300]}
    if p.get("_script"):
        (SCRIPTS / Path(p["_script"]).name).unlink(missing_ok=True)
    return {"ok": True, "cancelled": {"id": p["id"], "text": p["text"], "at": p["at"]}}
