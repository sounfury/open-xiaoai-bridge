#!/usr/local/bin/python
"""加载家规后进入上游 CLI，功能和参数解析均由上游维护。"""
import sys
from mijia_house_policy import install

try:
    install()
except PermissionError as exc:
    sys.stderr.write(str(exc) + '\n')
    raise SystemExit(2)

from mijiaAPI.__main__ import cli
cli()
