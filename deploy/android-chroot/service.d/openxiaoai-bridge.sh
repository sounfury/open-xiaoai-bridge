#!/system/bin/sh
# KernelSU 开机脚本：复制到 /data/adb/service.d/（chmod 755）。
# 等系统启动完成、Wi-Fi 拿到 IP 后启动 bridge。
LOG=/data/local/ubuntu-tools/run/boot.log
mkdir -p /data/local/ubuntu-tools/run

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 5; done
i=0
while [ $i -lt 60 ] && ! ip -4 addr show wlan0 2>/dev/null | grep -q "inet "; do
  sleep 5; i=$((i + 1))
done
sleep 10
echo "$(date '+%F %T') boot: wlan0=$(ip -4 addr show wlan0 | awk '/inet /{print $2}')" >> "$LOG"
touch /data/local/ubuntu-tools/run/enabled
sh /data/local/ubuntu-tools/bridge-ctl.sh start >> "$LOG" 2>&1
