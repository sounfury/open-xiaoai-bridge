#!/opt/hermes/.venv/bin/python
"""Hermes pre_tool_call 钩子：AgentDock exec_command 命中 Hermes 内置危险命令规则时拦截。

规则复用 Hermes 自带的 tools/approval_detection.py，这里不维护自己的规则表。
它防的是模型误操作，不是对任意 shell 的安全隔离。
"""
import json
import sys

sys.path.insert(0, '/opt/hermes')
from tools.approval_detection import detect_dangerous_command

payload = json.load(sys.stdin)
args = payload.get('tool_input') or {}
command = args.get('cmd') or args.get('command') or ''
if not isinstance(command, str):
    command = json.dumps(command, ensure_ascii=False)

dangerous, _key, description = detect_dangerous_command(command)
if dangerous:
    print(json.dumps({
        'action': 'block',
        'message': f'[AgentDockGuard] 命令被危险命令规则拦截：{description}。'
                   '不要改写命令绕过；告诉用户这个操作需要他本人在电脑上完成。',
    }, ensure_ascii=False))
    sys.exit(2)
