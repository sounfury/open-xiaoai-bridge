---
name: windows-control
description: |
  通过 AgentDock 控制用户的 Windows 电脑（WIN-M2OK7MS7L4J）：音量、静音、媒体播放、启动或关闭程序、
  锁屏、关屏、睡眠、可取消的关机倒计时。触发词包括"电脑"、"Windows"、"音量"、"静音"、"锁屏"、
  "关屏"、"睡眠"、"关机"、"打开/关闭某个软件"。
allowed-tools: mcp agentdock exec_command
---

## 调用方式

- 只能使用 AgentDock 的 `exec_command`，参数 `cmd`。命令在用户当前登录的桌面会话中执行，权限为完整管理员。需要 PowerShell 时写 `powershell -NoProfile -Command "..."`。
- 每次调用前都会经过危险命令规则检查（递归 / 强制删除、格式化、编码命令、强制结束进程等）。被拦截时**不要换写法重试**，直接告诉用户需要他本人在电脑上操作。
- 不删除、移动、改写用户文件，不安装或卸载软件，不改系统设置、注册表、防火墙和网络。用户的请求超出下面这些常用操作时，先说明要执行什么再做。
- Spotify 的播放、搜索和歌单用官方 Spotify 工具；只有 Spotify 客户端没打开、找不到播放设备时，才用这里启动它。

## 常用操作

| 操作 | 命令 |
|---|---|
| 音量加 / 减（每次约 2%） | `powershell -NoProfile -Command "$w=New-Object -ComObject WScript.Shell; 1..5 \| % { $w.SendKeys([char]175) }"`（减用 174） |
| 静音切换 | `powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"` |
| 播放 / 暂停、下一首、上一首 | 同上，分别用 `[char]179`、`[char]176`、`[char]177` |
| 启动程序 | `powershell -NoProfile -Command "Start-Process spotify:"`，或 `Start-Process '程序完整路径'` |
| 关闭程序 | `powershell -NoProfile -Command "Stop-Process -Name 进程名"`（不加 -Force，先确认进程名） |
| 锁屏 | `rundll32.exe user32.dll,LockWorkStation` |
| 关屏 | `powershell -NoProfile -Command "(Add-Type '[DllImport(\"user32.dll\")]public static extern int SendMessage(int h,int m,int w,int l);' -Name U -PassThru)::SendMessage(-1,0x0112,0xF170,2)"` |
| 睡眠 | `rundll32.exe powrprof.dll,SetSuspendState 0,1,0` |
| 关机倒计时 | `shutdown /s /t 秒数`，用户没说时间默认 60 秒；取消用 `shutdown /a` |

- 关机、重启、睡眠前告诉用户将要执行的动作和倒计时；关机必须使用倒计时，不立即关机。
- 程序路径不确定时，先用 `Get-Command`、`Get-StartApps` 或开始菜单快捷方式查找，不要猜。
