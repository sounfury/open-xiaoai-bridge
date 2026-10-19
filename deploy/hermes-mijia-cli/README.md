# Hermes 米家 CLI 接入

米家控制使用上游 [mijiaAPI](https://github.com/Do1e/mijia-api) CLI（5.0.0）和 [mijia-api skill](https://github.com/Do1e/mijia-api/blob/2cc61f2e4ae21d9e3736e450c968bbe53c87ebd1/skills/SKILL.md)，直接装在 Hermes 容器的持久化目录里，Agent 通过本地 `terminal` 调用。Spotify 继续使用 Hermes 官方插件。

## 部署位置（容器内 `/opt/data` = 宿主 `/home/ubuntu/hermes/data`）

| 路径 | 内容 |
|---|---|
| `/opt/data/tools/mijia/venv` | 上游 `mijiaAPI==5.0.0` 独立虚拟环境 |
| `/opt/data/.local/bin/mijiaAPI` | 入口（`mijia_cli.py`），已在 hermes 用户 PATH 中 |
| `/opt/data/tools/mijia/mijia_house_policy.py`、`house-policy.json` | 禁用设备 / 场景检查（老家树莓派） |
| `/opt/data/tools/mijia/warm_catalog.py` | 刷新设备目录到 `/opt/data/workspace/mijia/devices.txt`，失败保留旧快照 |
| `/opt/data/.config/mijia-api/auth.json` | 米家认证（复制自原登录，0600） |
| `skills/mijia-api/SKILL.md` | 源配置与运行配置各一份 |

Hermes 配置：

```yaml
terminal:
  backend: local
  cwd: /opt/data/workspace/mijia
  timeout: 120
```

`api_server`、`cli`、`cron` 工具集包含 `terminal` 和 `skills`。Agent cron 和原生 `no_agent` 脚本都在 Hermes 容器运行，可直接调用 `/opt/data/.local/bin/mijiaAPI`。

## 注意

- 家规检查只针对标准 CLI 调用；Agent 有本地 terminal，可以读到 Hermes 容器里的其他凭据，这是有意接受的取舍。
- 虚拟环境基于 Hermes 镜像自带的 Python；镜像升级改变 Python 版本后，需要重建 venv：
  `python3 -m venv --clear /opt/data/tools/mijia/venv && /opt/data/tools/mijia/venv/bin/pip install "mijiaAPI==5.0.0"`
- 不再有启动预热；目录缓存缺失或过旧时由 skill 指引 Agent 刷新。
- 加热超时观察器已撤销。防忘关：电热毯由米家定时开关；热水器通电时由 Hermes 同时建一次性定时关闭任务（cron）。

## 回退

切换前备份：`/home/ubuntu/hermes/backups/mijia-cli-20261018-100032/files.tar.gz`（旧 `home_tools` MCP 配置与源文件）。旧 `mijia-service` 及其认证仍在宿主机 `~/home-tools/mijia/`。
