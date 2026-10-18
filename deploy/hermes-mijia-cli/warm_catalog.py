"""调用上游 CLI 预热目录；失败时保留上次成功快照。"""
import json
import subprocess
import time
from pathlib import Path

workspace = Path.home() / "workspace"
workspace.mkdir(exist_ok=True)
result = subprocess.run(["mijiaAPI", "-l", "--list_homes", "--list_scenes"],
                        capture_output=True, text=True, timeout=120)
if result.returncode or "设备列表:" not in result.stdout or "家庭列表:" not in result.stdout:
    raise SystemExit("[MijiaWarmup] 目录刷新失败，保留旧缓存；请直接运行 mijiaAPI -l 核对认证。")
next_path = workspace / "devices.txt.tmp"
next_path.write_text(result.stdout)
next_path.replace(workspace / "devices.txt")
metadata = {"updated_at": time.time(), "source": "mijiaAPI -l --list_homes --list_scenes"}
(workspace / "devices.updated.json").write_text(json.dumps(metadata) + "\n")
