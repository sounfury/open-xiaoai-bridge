---
name: windows-control
description: |
  通过 AgentDock 控制用户的 Windows 电脑（WIN-M2OK7MS7L4J）：音量、静音、媒体播放、启动或关闭
  Spotify / Steam / 微信 / QQ / 游戏等程序、锁屏、关屏、睡眠、可取消的关机倒计时。触发词包括"电脑"、
  "Windows"、"音量"、"静音"、"锁屏"、"关屏"、"睡眠"、"关机"、"打开/关闭某个软件或游戏"。
---

## 调用方式

- 只能使用 AgentDock 的 `exec_command` 工具，参数 `cmd`。AgentDock 在 Windows 上**直接用 PowerShell 执行** `cmd`，所以下面的命令原样传入即可，不要再套 `powershell -Command`。
- 命令在用户当前登录的桌面会话中执行，权限为完整管理员。
- `cmd` 是 PowerShell，不要写 cmd 语法（`start ""`、`%APPDATA%`、`tasklist | findstr`）。`Get-Process` 找不到进程时退出码为 1，这表示"没在运行"，不是命令出错。
- 命令超过约 5 秒会被转到后台，返回 `status: running` 且没有输出。启动、关闭这类要等结果的命令，调用时加参数 `yield_time_ms: 15000`。

## 必须拿到结果（硬性规定）

- `exit_code: 0` 只说明命令执行完了，不说明目的达到；`explorer.exe` 不管程序起没起来都返回 0。
- 凡是改变电脑状态的操作（启动、关闭程序，关机等），都在**同一条命令末尾读回状态**，并按读回的结果回答用户。下面的模板已经带上读回。
- 返回 `status: running`、输出为空或者没有 `RESULT` 行，都表示结果未知：如实说"命令发出去了，还没确认"，不要说已经办好。
- 读回显示失败时，告诉用户失败和原因（例如"游戏启动后马上崩溃了"），最多换正确写法再试一次，不要反复启动。
- 命令被拦截或报错时按提示改正；提示说需要用户本人操作的，就告诉用户，**不要换写法绕过**。电脑离线时如实说明。

## 启动程序

普通 exe（游戏、Steam、微信、QQ、Spotify 等）用下面的模板，只替换第一行的完整路径：

```
$exe='完整路径.exe'; $n=[IO.Path]::GetFileNameWithoutExtension($exe); $t=Get-Date; $r=Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine="`"$exe`""; CurrentDirectory=(Split-Path $exe)}; Start-Sleep 3; $p=@(Get-Process -Name $n -EA SilentlyContinue); if ($r.ReturnValue -ne 0) {"RESULT start_failed code=$($r.ReturnValue)"} elseif ($p.Count) {"RESULT running count=$($p.Count) title=$(($p.MainWindowTitle | ? {$_}) -join '|')"} else {"RESULT exited"; Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=$t} -EA SilentlyContinue | ? {$_.Message -match [regex]::Escape("$n.exe")} | select -First 1 -ExpandProperty Message}
```

- 它以 exe 所在文件夹为工作目录启动。不要用 `explorer.exe "xx.exe"` 启动普通 exe：那样工作目录是 `C:\Windows\system32`，游戏等按相对路径读数据的程序会直接崩溃。
- 不要用 `Start-Process` / `start`：AgentDock 会在命令结束时把它启动的程序一起关掉，拦截钩子也会拒绝。
- 结果：`RESULT running` 才算打开了；`RESULT exited` 表示启动后退出了，后面若有崩溃记录就简短说明；`RESULT start_failed code=9` 一般是路径不对。
- 商店应用（`Get-StartApps` 里的 AppID）用 `explorer.exe "shell:AppsFolder\<AppID>"` 启动，同一条命令里接 `Start-Sleep 3; Get-Process 进程名 -EA SilentlyContinue | Select Id,MainWindowTitle` 读回。
- 不在下表、记忆里也没有路径的程序：先用 `Get-StartApps | Where-Object Name -match '关键词'` 或在用户说的文件夹里查找，找不到就告诉用户，不要猜路径。
- 已在运行时再次启动只会把窗口调出来，先查是否在运行：`Get-Process -Name 进程名 -ErrorAction SilentlyContinue`。

## 常用应用

| 应用 | 路径 | 关闭（末尾读回） | 进程名 |
|---|---|---|---|
| Spotify | `$env:APPDATA\Spotify\Spotify.exe` | `Stop-Process -Name Spotify; Start-Sleep 1; @(Get-Process Spotify -EA SilentlyContinue).Count` | `Spotify` |
| Steam | `E:\Steam\steam.exe` | `Start-Process 'E:\Steam\steam.exe' -ArgumentList '-shutdown'; Start-Sleep 4; @(Get-Process steam -EA SilentlyContinue).Count` | `steam` |
| 微信 | `E:\application\normal\Weixin\Weixin.exe` | `Stop-Process -Name Weixin; Start-Sleep 1; @(Get-Process Weixin -EA SilentlyContinue).Count` | `Weixin` |
| QQ | `E:\application\normal\qq\QQ.exe` | `Stop-Process -Name QQ; Start-Sleep 1; @(Get-Process QQ -EA SilentlyContinue).Count` | `QQ` |

- Spotify 启动模板里的路径写 `"$env:APPDATA\Spotify\Spotify.exe"`（双引号）。Spotify 的播放、搜索和歌单用官方 Spotify 工具；只有找不到播放设备时才用这里启动一次，再用 Spotify 设备工具确认，设备通常 5～10 秒内出现，最多再查一两次，不要反复重启客户端。
- 关闭后读回的进程数为 0 才算关掉了。Steam 用 `-shutdown` 正常退出，不直接结束进程。关闭微信、QQ 前提醒用户未发送的消息可能丢失。

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

- 音量键每按一下约 2%，命令里的 `1..5` 表示按 5 下。用户说"大一点 / 小一点"默认 10%；说"调大很多"可以按 10 下（约 20%）；要求具体百分比时按"目标差值 ÷ 2"换算次数，并说明是估算。音量和媒体键没有可靠的读回方式，回答时说"按了音量键"这类实际做的事。
- 这台电脑没有启用休眠，睡眠命令进入的就是睡眠（现代待机）。
- 关机、重启、睡眠前告诉用户将要执行的动作和倒计时。关机必须用 `shutdown.exe` 倒计时，用户没说时间默认 60 秒；它报错时把错误告诉用户，**不要改用 `Win32Shutdown`、`Stop-Computer` 等立即关机的写法**。

## 禁止

- 不删除、移动、改写用户文件，不安装或卸载软件，不改系统设置、注册表、防火墙和网络。用户的请求超出本 skill 的常用操作时，先说明要执行什么再做。
- 每次调用前都会经过危险命令规则检查（递归 / 强制删除、格式化、编码命令、强制结束进程等）。被拦截时不要换写法重试，直接告诉用户需要他本人在电脑上操作。
