# 迁移方案：把 OwnDing/open-xiaoai 的能力移植进本项目

> 状态：阶段 1 已完成并在真机验证，下一步阶段 3 + 4。最后更新：2026-10-18。
> 参考项目：[OwnDing/open-xiaoai](https://github.com/OwnDing/open-xiaoai)（基于 commit `7ecea6f`，MIT）。

## 1. 背景与目标

现在的问题：

1. **打断不了**：播报时 bridge 会关麦防回声，自定义唤醒词听不到；只能喊"小爱同学"，而且会直接退出对话。
2. **回复播完后有 1.5～2 秒听不到**：要依次经过提示音、重启录音、等环境静音，才开始真正听；这段时间说的话会被丢掉。
3. **唤醒率低**：唤醒检测要等 VAD 判定开始说话才收音频，唤醒词开头被吃掉；所有唤醒词共用一个阈值；`max_active_paths` 写死为 8。

OwnDing 在 OH2P 上实测解决了这三个问题。我们**不换成他的整套方案**（他依赖小智服务端 + Home Assistant），只把需要的能力移植进本项目。

### 不变的部分

- 后端：Oracle 上的 Hermes（profile `muse-home`），走 OpenAI 兼容接口 `https://hermes.oci.sounfury.top/v1`。
- 米家、Spotify：继续用 Hermes 的 MCP `home_tools`。
- 不装 Home Assistant，不用小智服务端，不用 Docker。bridge 继续在 Windows 本机（192.168.1.210）直接运行。

### 目标架构

```
OH2P（OwnDing 客户端：麦克风 + 播放回采，二进制音频帧）
  ⇄ open-xiaoai-bridge（Windows 本机，:4399）
      回声消除 → 唤醒检测 / VAD → 本地 SenseVoice 识别
      → Hermes（流式）→ 分句 → 小爱原生 TTS 分段播放
      /api/play/text（只监听 127.0.0.1:9092）
          ▲ cloudflared 隧道（复用 AgentDock 的隧道，新增一个域名）
Oracle：Hermes muse-home ─ home_tools（米家 / Spotify / speaker_announce）
```

## 2. 要什么、不要什么

### 要

| 能力 | 他的代码 |
|---|---|
| 播报中打断：客户端发回播放回采 + 子带回声消除 | `packages/client-rust/src/services/audio/echo_ref.rs`，`examples/xiaozhi/xiaozhi/services/audio/aec.py` |
| 唤醒调优：每个唤醒词单独阈值、`max_active_paths` 可调、两路错开检测 | `examples/xiaozhi/xiaozhi/services/audio/kws/` |
| 轮次衔接：回答完马上接着听、允许抢话 | 桥接的会话逻辑 |
| 退出词规则："关灯，拜拜"先执行再退出，识别错的写法也认 | `config.py` 的 `wakeup.exit_words` 及判断逻辑 |
| 小爱原生音色分段播报 | `examples/xiaozhi/xiaozhi/services/native_tts.py` |
| 链路健壮性：心跳、发送卡住检测、二进制音频帧、实时音频丢旧帧 | `packages/client-rust/src/services/connect/` |
| Hermes 接入的体验细节：搜索时先说"我查一下"、去掉开头英文 | `deploy/hermes/xiaozhi-provider/hermes.py` 的对应部分 |

### 不要

| 内容 | 原因 |
|---|---|
| 小智服务端（xiaozhi-esp32-server）及其补丁 | 本项目已有本地 ASR，直接调 Hermes |
| Home Assistant 全套（`home_rules`、`home_history`、场景口令直接执行、`xiaoqi_announce`） | 用米家接口 + `home_tools` |
| 他的 Hermes profile（SOUL.md、"小七"人设） | 用自己的 `muse-home` |
| 工具校验（tool guard），至少第一期不要 | 按 HA 工具名写死；先观察是否有"没调工具就说做了" |
| Docker / PowerShell 部署脚本、Sherpa 服务端 TTS、电脑语音终端、bench、宣传片等 | 和需求无关 |

## 3. 客户端：直接用还是合并

### 代码差异（共同基点：上游 `idootop/open-xiaoai@30f5992`）

| 仓库 | 对 `packages/client-rust` 的改动 |
|---|---|
| coderzc（本项目 `native/` 当前依赖） | 只有 `src/bin/client.rs` 里 15 行：读 `OPEN_XIAOAI_TOKEN`，连接时带 `Authorization: Bearer` 头。其余是脚本和下载地址 |
| OwnDing | 13 个文件、+1313 行：`echo_ref`、二进制帧 `OXS1`、发送队列和心跳、连接超时和重连、录音健康日志、播放写入超时 |

### 协议兼容性

- OwnDing 的 `Stream::decode` 同时认新的二进制帧和旧的 JSON 帧，但**旧解码只认 JSON**。新客户端发的录音帧，旧服务端解不出来；反过来也一样（服务端用新 crate 后，播放帧也是二进制）。
- 所以**音箱客户端和本项目 `native/` 依赖的 crate 必须一起升级**，不能只换一边。

### 决定

1. **`native/` 的 Rust 依赖：直接用 OwnDing 的 crate，固定到 commit `7ecea6f`。**
   coderzc 那 15 行鉴权只在客户端二进制里生效；服务端的鉴权校验写在本项目自己的 `native/src/server.rs`，和 crate 无关。服务端用他的 crate 什么都不会丢。
2. **音箱客户端二进制：先直接用他的 release（`client-2026.10.09` 或更新）。**
   现在音箱启动命令是 `client ws://192.168.1.210:4399`，没用 `OPEN_XIAOAI_TOKEN`，缺那 15 行没有影响。
3. **以后需要客户端鉴权时再合并**：在自己的 fork（基于 OwnDing）里补上 coderzc 的鉴权提交 `355bed5`（只改 `client.rs` 的 `connect`），用 GitHub Actions 编译 aarch64 客户端，`native/` 改指向这个 fork。

### 不照搬的部分

他服务端的 `audio_watchdog` 会**每 5 秒重发一次 `start_recording`**，用来从断流中恢复。本项目在阶段 2 之前，播报时还要主动 `stop_recording` 防回声，看门狗会在播报中途把麦克风重新打开。所以阶段 1 不引入看门狗；阶段 2 改成不关麦后，再引入并让它尊重"暂停录音"状态。

## 4. 分阶段计划

### 阶段 0：准备和回退

- 音箱：备份 `/data/open-xiaoai/client` 到 `client.upstream`。
- 本项目：在迁移分支上做，每个阶段打 tag。当前运行中的 bridge 不动，新的 native 扩展先编译成 wheel，切换时再安装。
- 回退：音箱放回原客户端 → 安装旧 wheel（或切回上一个 tag 重新编译）→ 重启 bridge。

### 阶段 1：协议和客户端（风险最高，先做）

1. `native/Cargo.toml`：`open-xiaoai` 依赖改为 OwnDing 仓库，固定 `rev = "7ecea6f…"`。
2. `native/src/server.rs`：
   - 录音流的 metadata（`layout`、`seq` 等）一起传给 Python。
   - 新增 `set_echo_ref(json)`：设置后，所有 `start_recording` 请求带上 `echo_ref` 字段。
   - 新连接接管旧连接（他的 `take_over`）、握手超时。
3. `native/src/lib.rs`：Python 侧的 `start_recording()` 也带上 `echo_ref`（否则阶段 2 之前每次恢复录音都会退回单声道）。
4. `core/xiaoai.py`：
   - 收到 `layout == "mic_ref"` 的双声道帧时，阶段 1 先只取麦克风声道送进 `GlobalStream`，下游不受影响。
   - 调试开关：把原始双声道录音存成 wav，用来确认回采声道。
5. 验证：
   1. 音箱换客户端，`echo_ref` 不开：连接、录音、唤醒、播放、`run_shell` 都正常。
   2. 打开 `echo_ref`：播一段 TTS 时录音，确认 ROM 1.62.2 上第 4 声道（索引 3）是回采，并检查电平和对齐。

`echo_ref` 参数（来自他的 OH2P 配置）：`pcm=hw:0,3`（共享名 `Capture`），4 声道，48000Hz（必须是线上 16000Hz 的 3 倍），`mic_channel=0`，`ref_channel=3`，`mic_shift`/`ref_shift` 以他 `config.py` 的值为准。

### 阶段 2：回声消除和播报中打断

1. 新增 `core/services/audio/aec.py`：移植他的子带回声消除，包括"近端人声强度"判断，防止音箱自己念到唤醒词时误触发。
2. `core/xiaoai.py`：双声道帧先过回声消除，干净的单声道再进 `GlobalStream`。
3. `core/external_conversation.py`：
   - 播报期间不关麦（去掉 `_play_reply_and_resume` 里的 `stop_recording`）。
   - 播报时唤醒检测继续运行；命中后用自己持有的 `playback_token` 停 TTS，再 `stop_device_audio()`，直接进入下一轮，不退出对话。
   - "你好缪斯，明天天气怎么样"一口气说完时，唤醒词后面的话不丢。
4. 引入录音看门狗（见第 3 节）。
5. `config.py` 新增 `barge_in` 配置段；同步更新 `AGENTS.md` 里"TTS 时 stop_recording 防回声"的描述。

### 阶段 3：唤醒率

1. 唤醒检测加约 300ms 预录缓冲。
2. `keywords.py`：每个唤醒词单独阈值（`keyword_thresholds`）。
3. `sherpa.py`：`max_active_paths` 可配置，支持 `stream_offsets_ms` 两路错开检测。
4. 唤醒词只留缪斯那一组；`audio_input.gain` 从 4.0 下调后实测确定。
5. 调参：录 20 遍以上算命中率，放 20 分钟电视声确认没有误唤醒。

### 阶段 4：轮次衔接和退出词

1. 去掉 `_wait_for_silence`；开麦后只丢开头约 150ms（有回声消除后可以不要）。
2. 提示音可配置；监听灯后台执行，不阻塞开始听。
3. 允许抢话：VAD 恢复时不清掉已经开始的语音。
4. 移植他的退出词规则，替换简单的 `exit_keywords` 匹配。

### 阶段 5：Hermes 流式回复和分段播报

1. `core/openai.py`：`stream: True`，解析 SSE，识别 `hermes.tool.progress` 事件。
2. 按句切分：第一句出来就播；后续句子提前用 `mibrain text_to_speech save=1` 生成，再依次 `miplayer` 播放；打断时能停掉。
3. 移植：搜索时先念"我查一下"、去掉开头英文。
4. 工具校验先不做；上线后观察 Hermes 会话记录。注意本项目本地保存的历史也只有文字，存在"模型照抄以前的'灯关了'"的风险。

### 阶段 6：主动播报（走 bridge 的 `/api/play/text`）

现状：对话回复是 bridge 拉取 Hermes 回复后在进程内播放；**主动播报没有通路**——Hermes 在 Oracle 上，碰不到只监听 `127.0.0.1:9092` 的 bridge。以前小爱管家能播，是 Grok 通过本机的 AgentDock 调用的。

1. bridge 设置 `API_SERVER_TOKEN`，监听地址保持 `127.0.0.1`。
2. 复用本机 AgentDock 的 cloudflared 命名隧道（`agent.sounfury.me`），新增一个域名指向 `http://127.0.0.1:9092`，再加 Cloudflare Access 服务令牌。需先确认 AgentDock 是否允许另加域名。
3. Oracle 的 `home_tools` 新增 `speaker_announce(text, urgent=False)`：调用上面的地址；勿扰时段在工具里判断；调不通时改走 mijiaAPI 云端播报，指定 OH2P（did `2181631827`）。
4. bridge 的 `/api/play/text` 加排队：正在对话或播报时先等这一轮结束。
5. SOUL.md 写明"提醒和定时任务到点后用 `speaker_announce`"；实测定时任务能调到 `home_tools`（现在 `platform_toolsets.cron` 只有 `[memory]`）。

不采用的做法：让 Hermes 直接连 AgentDock 的 MCP。AgentDock 能操控整台电脑，等于给语音助手和无人值守的定时任务开了终端权限。

### 阶段 7：路由和清理

1. `before_wakeup`：命中"你好缪斯"（本地 KWS 或小爱 ASR 以"缪斯"开头）返回 `"openai"`，不再走 muse webhook。
2. 启动脚本：`OPENAI_ENABLE=1`，不设 `MUSE_ENABLE`。
3. muse、OpenClaw、QwenPaw 代码先保留但不启用。
4. 更新 README 和 AGENTS.md。

## 5. 执行顺序

| 顺序 | 内容 | 原因 |
|---|---|---|
| 1 | 阶段 1 | 最大的未知数，不通的话打断功能要重新设计 |
| 2 | 阶段 3 + 4 | 只改服务端，风险低，见效快 |
| 3 | 阶段 2 | 依赖阶段 1 |
| 4 | 阶段 5 | 改动独立 |
| 5 | 阶段 6 | 可与 2～4 并行，主要在 Oracle 端 |
| 6 | 阶段 7 | 收尾 |

## 6. 待确认

1. AgentDock 的 cloudflared 隧道能否另加一个域名。
2. 勿扰时段：跟随音箱自己的勿扰设置，还是写死时间段。
3. 回采声道在 ROM 1.62.2 上是否为索引 3（阶段 1 实测）。

## 7. 进度记录

- 2026-10-17：完成两边 `client-rust` 对比，确定第 3 节的决定。开始阶段 1。
- 2026-10-18：**阶段 1 完成**，分支 `feat/ownding-migration`。

### 阶段 1 改动

| 文件 | 改动 |
|---|---|
| `native/Cargo.toml`、`Cargo.lock` | `open-xiaoai` 依赖改为 OwnDing `7ecea6f` |
| `native/src/server.rs` | `set_echo_ref` / `recording_config()`；新连接接管旧连接（握手 10s 超时）；快速重连不重复播"已连接"；先开麦再播欢迎语；录音帧带 metadata 时交给 Python 的 `on_input_packet` |
| `native/src/lib.rs` | Python 的 `start_recording()` 带上 `echo_ref`；导出 `set_echo_ref` |
| `native/src/python.rs` | `has_fn()` |
| `core/xiaoai.py` | `on_input_packet`：`mic_ref` 双声道先只取麦克风声道（乘 `echo_mic_gain`）；`barge_in.echo_reference` / 环境变量 `BARGE_IN_ECHO_REFERENCE` 开关；`ECHO_REF_DUMP` 调试录音 |
| `config.py` | 新增 `barge_in` 段，`echo_reference` 默认 `False` |
| `tests/test_echo_ref_input.py` | 双声道拆分、调试录音、开关的单元测试 |

编译：Windows 上 `audiopus_sys` 需要 cmake，PATH 里没有，用 VS Build Tools 自带的：`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin`。`maturin build --release --out dist-migration` 生成 wheel，再 `uv pip install --no-deps --reinstall` 装进 `.venv`（要先停 bridge，否则 `.pyd` 被占用）。

### 真机验证结果（OH2P，ROM 1.62.2，`client-2026.10.09`）

1. **单声道**：连接、录音（采集 16000Hz、发送约 15984Hz、无丢包无 xrun）、阻塞 TTS（`run_shell`）、PCM 播放（`start_play` + 二进制 `play` 帧）都正常。
2. **回采**：打开 `echo_ref` 后客户端改录 `Capture`（4 声道 48kHz S32），按双声道发送。
   - 第 4 声道（索引 3）确认是播放回采：不播放时是数字 0；小爱原生 TTS 和 PCM（aplay）播放都能回采到。
   - 播放期间麦克风与回采相关系数约 -0.73，延迟约 1ms，基本采样对齐。
   - 麦克风声道（`mic_shift=14`）播放时峰值约 4000，没有削波；安静时底噪峰值约 60～120。
3. 验证后已恢复为单声道运行（`echo_reference: False`），等阶段 2 接入回声消除再打开。

注意：打开回采时麦克风声道会乘 64 倍，播放期间会严重削波；阶段 2 之前不要在正式运行时打开。

### 回退方法

1. 音箱：`cd /data/open-xiaoai && cp client.upstream client`，然后重启客户端（或重启音箱）。新客户端另存为 `client.ownding`。
2. 本机：停掉计划任务 `\OpenXiaoAI-Bridge` 及其 python 进程，把 `D:\ai\_ref\rollback\` 里的 `open_xiaoai_server` 和 `open_xiaoai_bridge-1.0.0.dist-info` 拷回 `.venv\Lib\site-packages\`，再启动计划任务。
3. 两边必须一起回退：新旧客户端的音频帧格式不兼容。

- 2026-10-18：清理小爱管家 webhook（Grok Bot）链路："你好缪斯"固定路由到 `"openai"`（Hermes），删除 webhook 客户端 / 单次处理器 / 追问模块和 mock 脚本。
  已知问题：进入连续对话后会重新听一句，"你好缪斯，开灯"里的"开灯"不会带过去，留到阶段 3 + 4 一起处理。
