@echo off
set SSH_ASKPASS=%USERPROFILE%\.xiaoai\askpass-win.cmd
set SSH_ASKPASS_REQUIRE=force
set DISPLAY=1
ssh -F NUL -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ConnectTimeout=8 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.1.73 "ps | grep open-xiaoai/client | grep -v grep | awk '{print $1}' | xargs -r kill -9; /data/open-xiaoai/client ws://192.168.1.210:4399 > /data/open-xiaoai/client.log 2>&1 & sleep 2; ps | grep open-xiaoai/client | grep -v grep; echo ---; cat /data/open-xiaoai/client.log"
