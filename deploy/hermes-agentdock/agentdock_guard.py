#!/opt/hermes/.venv/bin/python
"""Hermes pre_tool_call 钩子：AgentDock exec_command 命中 Hermes 内置危险命令规则时拦截。

危险命令规则复用 Hermes 自带的 tools/approval_detection.py，这里不维护自己的规则表。
它防的是模型误操作，不是对任意 shell 的安全隔离。

另外拦截 Start-Process / start 启动程序：AgentDock 会在命令结束时结束它派生的进程树，
这样启动的程序会马上被关掉（Spotify "闪退"）。要求改用 windows-control 的启动模板。
以及立即关机的写法（Win32Shutdown / Stop-Computer / Restart-Computer）：关机只能用可取消的 shutdown.exe 倒计时。
"""
import json
import re
import sys

sys.path.insert(0, '/opt/hermes')
from tools.approval_detection import detect_dangerous_command

payload = json.load(sys.stdin)
args = payload.get('tool_input') or {}
command = args.get('cmd') or args.get('command') or ''
if not isinstance(command, str):
    command = json.dumps(command, ensure_ascii=False)

# PowerShell 里 start / saps 都是 Start-Process 的别名；Start-Sleep、Get-StartApps 不会命中
START_PROCESS = re.compile(r'(?i)(?:^|[\s;&|({])(?:start-process|start|saps)(?=\s|$)')
# steam.exe -shutdown 只把退出指令转给正在运行的 Steam，被结束也没关系
if START_PROCESS.search(command) and not re.search(r'(?i)-shutdown\b', command):
    print(json.dumps({
        'action': 'block',
        'message': '[AgentDockGuard] 不要用 Start-Process / start 启动程序：AgentDock 会在命令结束时'
                   '把它启动的程序一起关掉。先 skill_view windows-control，用里面的启动模板'
                   '（会设置工作目录并读回程序是否在运行）。',
    }, ensure_ascii=False))
    sys.exit(2)

IMMEDIATE_SHUTDOWN = re.compile(r'(?i)win32shutdown|stop-computer|restart-computer')
SHUTDOWN_NOW = re.compile(r'(?i)\bshutdown(?:\.exe)?\b(?=[^;\n|]*[/-][srpg]\b)(?![^;\n|]*[/-]t\s+[1-9])')
if IMMEDIATE_SHUTDOWN.search(command) or SHUTDOWN_NOW.search(command):
    print(json.dumps({
        'action': 'block',
        'message': '[AgentDockGuard] 关机 / 重启只能用可取消的倒计时：shutdown.exe /s /t 60（重启用 /r）。'
                   '它报错时把错误告诉用户，不要换成立即关机的写法。',
    }, ensure_ascii=False))
    sys.exit(2)

dangerous, _key, description = detect_dangerous_command(command)
if dangerous:
    print(json.dumps({
        'action': 'block',
        'message': f'[AgentDockGuard] 命令被危险命令规则拦截：{description}。'
                   '不要改写命令绕过；告诉用户这个操作需要他本人在电脑上完成。',
    }, ensure_ascii=False))
    sys.exit(2)
