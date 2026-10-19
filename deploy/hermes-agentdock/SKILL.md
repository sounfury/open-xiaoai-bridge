---
name: windows-control
description: |
  通过 AgentDock 控制用户的 Windows 电脑（WIN-M2OK7MS7L4J）：音量、静音、媒体播放、启动或关闭
  Spotify / Steam / 微信 / QQ 等程序、锁屏、关屏、睡眠、可取消的关机倒计时。触发词包括"电脑"、
  "Windows"、"音量"、"静音"、"锁屏"、"关屏"、"睡眠"、"关机"、"打开/关闭某个软件"。
---

## 调用方式

- 只能使用 AgentDock 的 `exec_command` 工具，参数 `cmd`。AgentDock 在 Windows 上**直接用 PowerShell 执行** `cmd`，所以下面的命令原样传入即可，不要再套 `powershell -Command`。
- 命令在用户当前登录的桌面会话中执行，权限为完整管理员。
- 每次调用前都会经过危险命令规则检查（递归 / 强制删除、格式化、编码命令、强制结束进程等）。被拦截时**不要换写法重试**，直接告诉用户需要他本人在电脑上操作。
- 不删除、移动、改写用户文件，不安装或卸载软件，不改系统设置、注册表、防火墙和网络。用户的请求超出本 skill 的常用操作时，先说明要执行什么再做。
- Spotify 的播放、搜索和歌单用官方 Spotify 工具；只有 Spotify 客户端没打开、找不到播放设备时，才用这里启动它。
- **启动程序必须用 `explorer.exe "完整路径"`，不能用 `Start-Process` 或 cmd 的 `start`。** AgentDock 会在每条命令结束时结束它派生的整棵进程树，`Start-Process` 打开的程序会跟着被关掉（表现为"闪退"），所以这类命令会被拦截钩子直接拒绝。经 `explorer.exe` 启动的程序不属于这棵树，命令结束后仍会运行。`explorer.exe` 本身的退出码不代表成功与否，要用后面的查询确认。
- `cmd` 是 PowerShell，不要写 cmd 语法（`start ""`、`%APPDATA%`、`tasklist | findstr`）。`Get-Process` 找不到进程时退出码为 1，这表示"没在运行"，不是命令出错。

## 常用应用

| 应用 | 启动 | 关闭 | 进程名 |
|---|---|---|---|
| Spotify | `explorer.exe "$env:APPDATA\Spotify\Spotify.exe"` | `Stop-Process -Name Spotify` | `Spotify` |
| Steam | `explorer.exe 'E:\Steam\steam.exe'` | `Start-Process 'E:\Steam\steam.exe' -ArgumentList '-shutdown'` | `steam` |
| 微信 | `explorer.exe 'E:\application\normal\Weixin\Weixin.exe'` | `Stop-Process -Name Weixin` | `Weixin` |
| QQ | `explorer.exe 'E:\application\normal\qq\QQ.exe'` | `Stop-Process -Name QQ` | `QQ` |

- 查是否在运行：`Get-Process -Name 进程名 -ErrorAction SilentlyContinue`。已在运行时再次启动只会把窗口调出来。
- 启动 Spotify 后，用一条命令等待并确认，例如 `explorer.exe "$env:APPDATA\Spotify\Spotify.exe"; Start-Sleep 3; @(Get-Process Spotify -EA SilentlyContinue).Count`，再用 Spotify 设备工具查询；设备通常在 5～10 秒内出现，最多再查一两次，不要反复重启客户端。命令总时长保持在 5 秒以内，超过后 AgentDock 会把命令转入后台。
- Steam 用 `-shutdown` 正常退出，不直接结束进程。关闭微信、QQ 前提醒用户未发送的消息可能丢失。
- 不在表里的程序：先用 `Get-StartApps | Where-Object Name -match '关键词'` 查找，再用 `explorer.exe "shell:AppsFolder\<AppID>"` 启动；找不到就告诉用户，不要猜路径。

## 系统操作

| 操作 | 命令 |
|---|---|
| 音量加 10% | `$w = New-Object -ComObject WScript.Shell; 1..5 \| ForEach-Object { $w.SendKeys([char]175) }` |
| 音量减 10% | 同上，`[char]175` 换成 `[char]174` |
| 静音切换 | `(New-Object -ComObject WScript.Shell).SendKeys([char]173)` |
| 播放 / 暂停 | `(New-Object -ComObject WScript.Shell).SendKeys([char]179)` |
| 下一首 / 上一首 | 同上，分别用 `[char]176`、`[char]177` |
| 锁屏 | `rundll32.exe 'user32.dll,LockWorkStation'` |
| 关屏 | `Add-Type -Name U -Namespace W -MemberDefinition '[DllImport("user32.dll")] public static extern bool PostMessage(int h, int m, int w, int l);'; [void][W.U]::PostMessage(0xFFFF, 0x0112, 0xF170, 2)` |
| 睡眠 | `Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)` |
| 关机倒计时 | `shutdown.exe /s /t 秒数`；取消用 `shutdown.exe /a` |

- 音量键每按一下约 2%，命令里的 `1..5` 表示按 5 下。用户说"大一点 / 小一点"默认 10%；说"调大很多"可以按 10 下（约 20%）；要求具体百分比时按"目标差值 ÷ 2"换算次数，并说明是估算。
- 这台电脑没有启用休眠，睡眠命令进入的就是睡眠（现代待机）。
- 关机、重启、睡眠前告诉用户将要执行的动作和倒计时。关机必须用倒计时，用户没说时间默认 60 秒，不立即关机。
