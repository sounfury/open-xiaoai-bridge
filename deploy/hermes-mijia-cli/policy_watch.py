"""米家加热兜底观察器；设备控制复用上游库，不实现米家协议。"""
import argparse
import fcntl
import json
import logging
import time
from pathlib import Path

from mijiaAPI import mijiaAPI, mijiaDevice

log = logging.getLogger("mijia-policy")


def save_state(path: Path, state: dict) -> None:
    """原子落盘，重启不重新计时。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def check_heater(device, rule: dict, previous: dict | None, now: float, poll_s: int,
                 observe_only: bool = False) -> dict | None:
    """只有真实读取到布尔开关状态才更新计时，离线和异常由上层保留原记录。"""
    prop = rule["prop"]
    value = device.get(prop)
    if type(value) is not bool:
        raise ValueError("开关属性没有返回布尔值")
    if not value:
        return None
    if previous is None:
        # 给轮询与云端延迟留余量，关闭时间不超过配置上限。
        minutes = int(rule.get("max_minutes", 180))
        seconds = min(minutes, 180) * 60
        previous = {"deadline": now + max(1, seconds - max(60, poll_s * 2)),
                    "first_seen_on": now}
        log.info("发现加热设备开启 did=%s，兜底截止时间=%s", rule["did"],
                 time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(previous["deadline"])))
    if now >= previous["deadline"] and not observe_only:
        device.set(prop, False)
        if device.get(prop) is not False:
            raise RuntimeError("关闭后读回未确认，保留原截止时间并重试")
        log.warning("加热设备超时已关闭 did=%s", rule["did"])
        return None
    return previous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    lock_file = args.state.with_suffix(".lock").open("a")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    devices = {}
    errors = {}
    api = None
    while True:
        policy = json.loads(args.policy.read_text())
        poll_s = max(10, int(policy.get("poll_seconds", 30)))
        for rule in policy.get("heaters", []):
            did = str(rule["did"])
            key = did + ":" + rule["prop"]
            try:
                if api is None:
                    api = mijiaAPI(auth_data_path=args.auth)
                if did not in devices:
                    devices[did] = mijiaDevice(api, did=did, sleep_time=0)
                next_record = check_heater(devices[did], rule, state.get(key), time.time(),
                                           poll_s, args.observe_only)
                if next_record is None:
                    state.pop(key, None)
                else:
                    state[key] = next_record
                if not args.observe_only:
                    save_state(args.state, state)
                errors.pop(key, None)
            except Exception as exc:
                # 离线时不能清除截止时间；重新上线后若已到点，下一轮立即关闭。
                now = time.time()
                if now - errors.get(key, 0) >= 600:
                    log.warning("加热设备不可核实 did=%s error=%s；保留截止时间", did,
                                type(exc).__name__)
                    errors[key] = now
                api = None
                devices.clear()
        if args.once:
            break
        time.sleep(poll_s)


if __name__ == "__main__":
    main()
