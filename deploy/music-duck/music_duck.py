#!/usr/bin/env python3
"""music_duck: 小爱对话期间压低 Spotify，结束后恢复（部署在 Oracle，和 spotify_ctl.py 共用凭据）。

bridge 启动时用一条 SSH 长连接运行本脚本（每次新开 SSH 要 2～3 秒，太慢）：

    ssh oracle python3 -u ~/home-tools/spotify/music_duck.py serve

stdin 一行一条命令，stdout 每条命令回一行 JSON：
    duck [音量]   正在播放时把音量降到 min(当前, 音量)（默认 15）；设备不支持调音量时改为暂停
    restore       恢复：音量还是我们压下去的值才改回去（期间你自己调过就不动）；我们暂停的才继续播
    status        当前状态
    ping
安全措施：
- stdin 关闭（bridge 退出 / 网络断开，SSH 随之断开）→ 先恢复再退出
- 压低超过 --max-duck-s（默认 300 秒）还没收到 restore → 自动恢复

凭据：~/.config/spotify-ctl/auth.json（SPOTIFY_CTL_DIR 可改目录）。access token 只放内存，
不碰 Hermes 容器以 root 写的 token.json；Spotify 换发 refresh_token 时原子写回 auth.json，
否则 Hermes 那边的 spotify_ctl 会失效。
"""
import argparse
import base64
import json
import os
import sys
import threading
import time

import requests

CFG_DIR = os.path.expanduser(os.environ.get("SPOTIFY_CTL_DIR", "~/.config/spotify-ctl"))
AUTH = os.path.join(CFG_DIR, "auth.json")
API = "https://api.spotify.com/v1"


class Spotify:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.expires = 0.0
        self.lock = threading.Lock()

    def _refresh(self):
        with open(AUTH) as f:
            auth = json.load(f)
        basic = base64.b64encode(f"{auth['client_id']}:{auth['client_secret']}".encode()).decode()
        r = self.session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": auth["refresh_token"]},
            headers={"Authorization": f"Basic {basic}"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"token refresh failed: {r.status_code} {r.text[:200]}")
        j = r.json()
        new_refresh = j.get("refresh_token")
        if new_refresh and new_refresh != auth["refresh_token"]:
            auth["refresh_token"] = new_refresh
            tmp = AUTH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(auth, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, AUTH)
        self.token = j["access_token"]
        self.expires = time.time() + int(j.get("expires_in", 3600)) - 120

    def call(self, method, path, retry=True, **kw):
        with self.lock:
            if not self.token or time.time() > self.expires:
                self._refresh()
            token = self.token
        r = self.session.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, timeout=10, **kw)
        if r.status_code == 401 and retry:
            with self.lock:
                self._refresh()
            return self.call(method, path, retry=False, **kw)
        return r

    def player(self):
        r = self.call("GET", "/me/player")
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"GET /me/player: {r.status_code} {r.text[:200]}")
        return r.json()


class Ducker:
    def __init__(self, spotify: Spotify, max_duck_s: float):
        self.sp = spotify
        self.max_duck_s = max_duck_s
        self.state = None  # {"mode": "volume"|"pause", "device": id, "previous": n, "level": n, "track": uri, "at": t}
        self.lock = threading.Lock()

    def duck(self, level: int) -> dict:
        with self.lock:
            if self.state is not None:
                self.state["at"] = time.time()
                return {"ok": True, "ducked": True, "already": True}
            p = self.sp.player()
            if not p or not p.get("is_playing"):
                return {"ok": True, "ducked": False, "reason": "not_playing"}
            device = p.get("device") or {}
            track = (p.get("item") or {}).get("uri")
            vol = device.get("volume_percent")
            if device.get("supports_volume", True) and vol is not None:
                target = min(int(vol), level)
                if target == vol:
                    return {"ok": True, "ducked": False, "reason": "already_quiet", "volume": vol}
                r = self.sp.call("PUT", f"/me/player/volume?volume_percent={target}&device_id={device.get('id')}")
                if r.status_code not in (200, 202, 204):
                    return {"ok": False, "error": f"volume {r.status_code} {r.text[:200]}"}
                self.state = {"mode": "volume", "device": device.get("id"), "previous": vol, "level": target,
                              "track": track, "at": time.time(), "name": device.get("name")}
                return {"ok": True, "ducked": True, "mode": "volume", "device": device.get("name"),
                        "previous": vol, "volume": target}
            r = self.sp.call("PUT", f"/me/player/pause?device_id={device.get('id')}")
            if r.status_code not in (200, 202, 204):
                return {"ok": False, "error": f"pause {r.status_code} {r.text[:200]}"}
            self.state = {"mode": "pause", "device": device.get("id"), "track": track, "at": time.time(),
                          "name": device.get("name")}
            return {"ok": True, "ducked": True, "mode": "pause", "device": device.get("name")}

    def restore(self, reason: str = "restore") -> dict:
        with self.lock:
            state, self.state = self.state, None
            if state is None:
                return {"ok": True, "restored": False, "reason": "not_ducked"}
            p = self.sp.player()
            device = (p or {}).get("device") or {}
            if state["mode"] == "volume":
                if device.get("id") != state["device"]:
                    return {"ok": True, "restored": False, "reason": "device_changed", "why": reason}
                # /me/player 的音量要 1～1.5 秒才更新（实测）：刚压低就恢复时读到的可能还是旧值。
                # 只有读到第三个值才算对话期间自己调过音量（尊重现在的值）
                if device.get("volume_percent") not in (state["level"], state["previous"]):
                    return {"ok": True, "restored": False, "reason": "volume_changed",
                            "volume": device.get("volume_percent"), "why": reason}
                r = self.sp.call("PUT", f"/me/player/volume?volume_percent={state['previous']}&device_id={state['device']}")
                ok = r.status_code in (200, 202, 204)
                return {"ok": ok, "restored": ok, "mode": "volume", "volume": state["previous"], "why": reason,
                        **({} if ok else {"error": f"{r.status_code} {r.text[:200]}"})}
            # 我们暂停的：还停着、还是同一首才继续
            if p and not p.get("is_playing") and (p.get("item") or {}).get("uri") == state["track"]:
                r = self.sp.call("PUT", f"/me/player/play?device_id={state['device']}")
                ok = r.status_code in (200, 202, 204)
                return {"ok": ok, "restored": ok, "mode": "pause", "why": reason}
            return {"ok": True, "restored": False, "reason": "playback_changed", "why": reason}

    def watchdog(self):
        while True:
            time.sleep(5)
            st = self.state
            if st is not None and time.time() - st["at"] > self.max_duck_s:
                emit(self.safe(lambda: self.restore("timeout")))

    def safe(self, fn):
        try:
            return fn()
        except Exception as exc:  # 网络错误等：回报，不退出
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_out_lock = threading.Lock()


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def serve(args):
    sp = Spotify()
    ducker = Ducker(sp, args.max_duck_s)
    threading.Thread(target=ducker.watchdog, daemon=True).start()
    # 预热：刷新 token、建好到 Spotify 的连接，第一次 duck 不用等
    warm = ducker.safe(lambda: (sp.player(), {"ok": True})[1])
    emit({"ready": True, **warm})
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        t = time.perf_counter()
        if cmd == "duck":
            level = int(rest[0]) if rest and rest[0].isdigit() else args.level
            out = ducker.safe(lambda: ducker.duck(level))
        elif cmd == "restore":
            out = ducker.safe(lambda: ducker.restore())
        elif cmd == "status":
            out = {"ok": True, "state": ducker.state}
        elif cmd == "ping":
            out = {"ok": True, "pong": True}
        else:
            out = {"ok": False, "error": f"unknown command {cmd}"}
        out["cmd"] = cmd
        out["ms"] = round((time.perf_counter() - t) * 1000)
        emit(out)
    # stdin 关闭：bridge 走了，别让音乐一直小声
    emit({**ducker.safe(lambda: ducker.restore("eof")), "cmd": "eof"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--level", type=int, default=15, help="duck 时的音量上限（%%）")
    s.add_argument("--max-duck-s", type=float, default=300)
    args = ap.parse_args()
    if args.cmd == "serve":
        serve(args)


if __name__ == "__main__":
    main()
