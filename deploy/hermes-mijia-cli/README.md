# Hermes 米家 CLI 接入

米家控制使用上游 [mijiaAPI](https://github.com/Do1e/mijia-api) CLI 和 [mijia-api skill](https://github.com/Do1e/mijia-api/blob/2cc61f2e4ae21d9e3736e450c968bbe53c87ebd1/skills/SKILL.md)。这里的文件只提供执行环境、目录预热与已有家规，不实现设备功能或维护普通设备白名单。

2026-10-18 已部署到 Oracle。Spotify 继续使用 Hermes 官方插件。

## 运行关系

```text
Hermes：加载 mijia-api skill
  └─ 原生 terminal SSH backend
       └─ 127.0.0.1:2223 → 专用容器 hermes-mijia-cli
            ├─ 上游 mijiaAPI 5.0.0：列表、规格、属性、动作、场景、播报
            ├─ /home/agent/workspace：目录快照、组合脚本
            └─ 独立米家认证文件

宿主机 mijia-heater-watch.service
  └─ 上游 Python 库 → 已登记加热设备：轮询、持久化截止时间、到点关闭
```

SSH 用户 `agent` 为容器内普通用户，入口仅绑定宿主机回环地址。容器没有 Docker socket、Hermes 数据目录或宿主机目录挂载；米家认证仅放在它自己的持久化 home。Hermes 使用的 SSH 私钥留在 Hermes profile，不同步到执行容器。

CLI 使用米家认证是其正常工作条件；这里隔离的是 Hermes 其他账号和宿主机权限，不声称脚本无法接触本容器的米家认证。该执行环境用于用户授权的个人自动化，不能视为运行恶意代码的安全边界。

## 文件职责

| 文件 | 职责 |
|---|---|
| `Dockerfile`、`compose.yaml`、`sshd_config`、`entrypoint.sh` | 固定上游版本、专用执行用户、回环 SSH、持久化工作区、启动预热 |
| `warm_catalog.py` | 调用上游列表命令，成功后原子更新目录，失败保留旧快照 |
| `mijia_cli.py`、`mijia_house_policy.py` | 很薄的启动校验；设备协议、命令解析及功能仍来自上游。检查已明确禁用的设备与场景，不列普通设备白名单 |
| `policy_watch.py`、`mijia-heater-watch.service` | 独立加热观察器；重启保留已观察到的开启截止时间，手动关闭后清除 |

家规校验加载只读 `/etc/mijia/house-policy.json`，对标准 CLI 调用进行启动检查和发送前检查；它不是对容器内任意 Python、网络程序的强制访问控制。升级上游时需核对 `_request` 和 `run_scene` 两个接入点。此处维护的是用户家规，新增普通设备或 CLI 功能无需修改这些文件。

## 当前部署

- 宿主目录：`/home/ubuntu/home-tools/mijia/cli-sandbox`。
- Hermes 源配置：`/home/ubuntu/hermes/profile`；运行数据：`/home/ubuntu/hermes/data`。
- skill：源配置和运行配置的 `skills/mijia-api/`，含上游来源、固定版本、许可证及部署说明；CLI 源码没有分叉。
- 认证：宿主目录 `home/.config/mijia-api/auth.json`，从原有登录安全复制；没有重新扫码。旧服务保留独立原认证用于回退。
- SSH：Hermes profile 内 `keys/mijia-sandbox-ed25519`；宿主目录 `authorized_keys` 和 `hostkeys/`。私钥和认证不进入仓库或构建上下文。
- 家规：宿主目录 `policy.json`；加热状态：`policy-state/state.json`，宿主账户拥有该目录。
- 旧 `home_tools` 已从源配置和运行配置的 `mcp_servers` 移除；旧源文件与 `mijia-service.service` 保留供回退，当前 Agent 不再调用它们。

Hermes 关键配置如下；SSH 私钥应归运行用户所有并设置 0600：

```yaml
terminal:
  backend: ssh
  ssh_host: 127.0.0.1
  ssh_port: 2223
  ssh_user: agent
  ssh_key: /opt/data/keys/mijia-sandbox-ed25519
  cwd: '~'
  timeout: 120
  credential_files: []
  env_passthrough: []
```

`api_server`、`cli`、`cron` 工具集加入 `terminal` 和 `skills`，从 `agent.disabled_toolsets` 移除 `terminal`。`file`、`code_execution` 等仍保持禁用；组合脚本可以通过 terminal 在专用工作区编写和运行。普通 Agent cron 可使用此 CLI，原生 `no_agent` 不会自动进入 SSH 后端，其通用执行入口尚未接通。

SOUL.md 只保留加载 skill、查真实设备、使用记忆和家规的方法，不再抄设备表。设备角色与叫法走 Hermes 原生记忆，变化中的在线状态从 CLI 查询。

## 目录刷新和设备规格

容器启动在后台预热，工作区持久化；启动时云端不可用不会清空原目录。手动刷新：

```sh
python /opt/mijia/warm_catalog.py && cat /home/agent/workspace/devices.updated.json /home/agent/workspace/devices.txt
```

也可以直接使用上游命令：

```sh
mijiaAPI -l --list_homes --list_scenes
mijiaAPI --get_device_info '从设备列表取得的实际 spec_type'
mijiaAPI get --did '已核实的 did' --prop_name '规格中实际的 name'
```

同名属性可能带服务编号后缀，应先查规格，不能猜 `on` 或模式枚举。用户说新增、移除、改名或匹配失败时，重新查询；下线不等于删除，同名设备需要用房间和 did 区分。原生 CLI 部分失败仍可能返回退出码零，必须检查文字结果，关键写入需要状态读回。

## 家规与加热观察器

`policy.json` 仅保存明确规则，结构如下，不是普通设备目录：

```json
{
  "blocked_devices": [{"did": "被明确禁用的设备 ID", "names": ["名称或指代"]}],
  "blocked_scenes": ["被明确禁用的场景"],
  "poll_seconds": 30,
  "heaters": [{"did": "已确认接加热设备的插座 ID", "prop": "已核实开关属性", "max_minutes": 180}]
}
```

当前已确认：原电热毯插座 `2028616332` 改接热水器，米家名称也改为“热水器”，开关属性经上游规格和只读查询核对为 `on-2`。这项用户事实已写入原生记忆，观察器登记仍绑定同一个 did。

观察器每 30 秒检查一次，首次观察到开启时记录截止时间并为轮询留一分钟余量，最长配置为三小时。截止时间按设备与属性原子保存，服务重启不重新计时；读不到状态时保留原截止时间，超时关闭失败会重试，上线后已到期则关闭。网络或设备离线期间无法保证实际关断。

观察器不替代用户安排的洗澡提醒，也不会从米家插座型号自动推断所接电器。更换负载时，应同时更新原生记忆和已登记用途。新增加热设备尚未登记时，不能声称它已经有独立兜底。

## 验收记录与运行命令

已完成实际 CLI 列表、设备改名、热水器开关只读查询、跨容器重启、Hermes 后端调用与聊天入口验收。离线时钟模拟覆盖截止时间恢复、到点关闭、手动关闭和只观察；离线策略模拟覆盖禁用设备 / 场景和新普通设备放行。未执行真实加热、开关或播报验收，未实际等待三小时，未创建定时任务。

```sh
cd /home/ubuntu/home-tools/mijia/cli-sandbox
sudo docker compose -f compose.yaml ps
sudo docker compose -f compose.yaml up -d --build
systemctl status mijia-heater-watch.service
journalctl -u mijia-heater-watch.service --no-pager -n 30
```

升级 CLI 由操作者修改 `MIJIA_VERSION` / 镜像标签并重建，更新上游 skill，核对家规接入点后执行只读验收。Agent 不自行覆盖 CLI 安装；不需要为上游每个新功能新增本地工具。

## 回退

本次切换前的受保护备份：`/home/ubuntu/hermes/backups/mijia-cli-20261018-100032/files.tar.gz`。用途变更前的原生记忆备份：`/opt/data/backups/mijia-memory-20261018-100702`。

回退时只恢复旧的 `config.yaml`、`SOUL.md`、MCP 配置与必要源文件，同步 profile 和运行数据后重启 Hermes；原来的米家服务仍可用。保留本次新增的对话、Spotify 授权、当前原生记忆和加热观察器状态。不要整体覆盖 profile 或恢复旧记忆，否则会丢失用户刚确认的热水器用途；旧 MCP 的电热毯固定名称也需要按当前用途处理。

确认旧链路工作后才停止专用 CLI 容器。已登记加热设备仍可能开启时，先核对其状态与接替的兜底，再决定是否停止观察器。
