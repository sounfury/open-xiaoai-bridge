#!/bin/sh
ADDR=$(cat /data/open-xiaoai/server.txt)
kill -9 $(ps | grep "open-xiaoai/client" | grep -v grep | awk '{print $1}') 2>/dev/null || true
/data/open-xiaoai/client "$ADDR" > /data/open-xiaoai/client.log 2>&1 &
sleep 2
ps | grep "open-xiaoai/client" | grep -v grep
echo ---LOG---
cat /data/open-xiaoai/client.log
