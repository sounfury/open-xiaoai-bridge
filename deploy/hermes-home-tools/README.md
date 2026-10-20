# Hermes 米家 MCP（home_tools）

日常米家控制走常驻服务 + 通用 MCP；统计、耗材等不常用功能继续用 [mijia-api skill 的 CLI](../hermes-mijia-cli/README.md)。

```text
Hermes ──stdio──▶ home_tools MCP（容器内）──HTTP──▶ mijia-service（宿主 127.0.0.1:9393）──▶ 米家云
```

设备、属性、场景都来自米家云的真实目录和 MIoT 规格，代码里没有设备表。新增 / 改名 / 移除设备不用改代码，也不用重启 Hermes：让缪斯调用 `mijia_refresh`，或者名字匹配不到时服务端自动刷新一次。

## 文件

| 仓库文件 | 部署位置 |
|---|---|
| `mijia_service.py` | `~/home-tools/mijia/service/server.py`（宿主，venv `~/home-tools/mijia/.venv`，上游 mijiaAPI 5.0.0） |
| `mijia-service.service` | `/etc/systemd/system/mijia-service.service`（副本 `~/home-tools/mijia-service.service`） |
| `mj` | `~/home-tools/mijia/service/mj`，调试用 curl 封装 |
| `home_tools_server.py` | `~/hermes/profile/mcp/home_tools/server.py` 与 `~/hermes/data/mcp/home_tools/server.py` |
| `home_tools_reminders.py` | 同目录 `reminders.py`（音箱定时提醒） |
| `house-policy.example.json` | 实际读 `~/hermes/data/tools/mijia/house-policy.json`，与 CLI 共用 |

认证与 CLI 共用 `~/hermes/data/.config/mijia-api/auth.json`。服务调用失败时，先检查 CLI 是否已经刷新过这个文件并重新加载，然后才自己刷新 token。

## 工具

| 工具 | 用途 |
|---|---|
| `mijia_get(devices?, room?, props?)` | 读状态；不传 devices 就查全屋，一次批量请求，带 `sampled_at` |
| `mijia_set(device, prop, value)` | 写属性；`on` 自动对应 `on-2` 等主开关；按规格校验类型、范围、枚举 |
| `mijia_action(device, action, params?)` | MIoT 动作 |
| `mijia_scene(name)` | 运行场景 |
| `mijia_speak(text, speaker?)` | 音箱朗读；默认 `default_speaker`（OH2P） |
| `mijia_devices(room?)` | 精简目录（平时不用先调） |
| `mijia_refresh()` | 重新拉目录，返回新增 / 移除 / 改名 / 上下线 |

| `reminder_create(text, in_minutes? \| at?)` | 音箱定时提醒；`at` 为 `HH:MM`（已过则明天）或 `YYYY-MM-DD HH:MM` |
| `reminder_list()` / `reminder_cancel(id? \| text?)` | 查看 / 取消还没到点的提醒 |

结果 `status`：`done`（云端确认）、`verified`（读回一致）、`accepted`（网关只确认收到，或场景 / 播报无法核实）、`failed`、`refused`、`dry_run`。

名字匹配：did → 名称完全一致 → 房间+名称 → 唯一子串。多台命中返回候选；匹配不到时返回名字相近的设备，并明确要求不能直接替用户换成候选。

## 音箱定时提醒

音箱本身没有定时朗读功能，也不另建调度器，到点执行交给 Hermes cron：

- `reminder_create` 把时间解析成 Asia/Shanghai 时间点，生成 `/opt/data/scripts/reminder-<id>.py`（POST 9393 `/speak`，运行后删除自己），再执行 `hermes cron create --no-agent --deliver local`，任务名以「提醒：」开头。
- 列出 / 取消只处理「提醒：」开头的任务，不碰其他 cron 任务；新建提醒时顺带清理一天前已经跑完的提醒任务。
- MCP 进程的环境变量被 Hermes 过滤过（没有 `HERMES_HOME`），调用 `hermes cron` 时要显式传 `HERMES_HOME=/opt/data`。
- cron 每 60 秒检查一次，实测到点后约 50 秒内播报。播报失败时脚本以退出码 1 结束，原因记在 `hermes cron list --all` / `hermes cron runs`。
- `HOME_TOOLS_DRY_RUN=1` 时照常建任务，到点只校验播报、不出声。
- 分流规则在 SOUL.md：带具体时间、要在家里听到的提醒用 `reminder_*`；“记一下 / 待办 / 要买 / 别忘了”或没有具体时间的用滴答清单。

## 家规

- `blocked_devices`：按 did 和名称拒绝。
- `blocked_scenes` 拒绝；另外，只要场景的动作会操作被禁止的设备，也一律拒绝（回家 / 离家 / 晚安都会开关老家树莓派插座）。CLI 的 `mijia_house_policy.py` 已加同样的检查。
- `default_speaker`：播报默认音箱。
- 可选 `homes`：只管理列出的家庭（名称或 id）；不写就是全部。
- 服务不做自动关闭或定时，热水器定时断电用 Hermes cron。

## 运维

```bash
sudo systemctl restart mijia-service          # 改服务代码后
cd ~/hermes && sudo docker compose restart hermes   # 改 MCP 工具定义后
~/home-tools/mijia/service/mj health
~/home-tools/mijia/service/mj get '{"devices":["厕所灯"]}'
~/home-tools/mijia/service/mj set '{"device":"厕所灯","prop":"on","value":true,"dry_run":true}'
```

MCP 进程设 `HOME_TOOLS_DRY_RUN=1` 时，所有写操作只校验不执行（评测用）。

改 SOUL.md 后，对音箱的固定会话执行 `echo y | sudo docker exec -i hermes hermes sessions repair-prompts <session_id> --apply`。

回退：备份在 `~/hermes/backups/home-tools-generic-20261020-*`，恢复其中的 `server.py`、`mj`、`mijia-service.service`、两份 MCP `server.py`、两份 SOUL.md、`house-policy.json` 和 `mijia_house_policy.py`，然后重启服务和 Hermes。
