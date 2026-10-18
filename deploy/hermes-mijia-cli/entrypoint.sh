#!/bin/sh
set -eu
mkdir -p /run/sshd
# 目录请求在后台完成，不阻塞 SSH；认证只在专用容器里使用。
su -s /bin/sh agent -c '/usr/local/bin/python /opt/mijia/warm_catalog.py' > /home/agent/workspace/warmup.log 2>&1 &
exec /usr/sbin/sshd -D -e
