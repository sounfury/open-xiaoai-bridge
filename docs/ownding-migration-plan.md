# 迁移方案：把 OwnDing/open-xiaoai 的能力移植进本项目

> 状态：阶段 1、2 已完成并在真机验收；阶段 3 + 4 代码完成、离线验收通过、已部署，待真机验收；新增阶段 8（调教 Hermes）。最后更新：2026-10-18。
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

### 阶段 8：调教 Hermes（米家、Spotify、Windows）

这一阶段不属于 OwnDing 移植，放在这里是为了和前面的阶段统一排期。米家和 Spotify 只改 Oracle 端：`~/hermes/profile` 的 SOUL.md 和 config.yaml、`home_tools/server.py`、`~/home-tools/mijia/service`。Windows 控制要在 bridge 加接口。

#### 用户故事（来自旧方案，需求保留）

| 编号 | 场景 | 期望 | 怎么实现 |
|---|---|---|---|
| S2 | "你好缪斯，我今晚想洗澡" | 先说"稍等"；按我平常的入睡时间安排热水器提前加热，播报"已安排 xx 点开始加热" | 入睡时间从 Hermes 记忆里取；用 Hermes cron 定时打开（见 8.1）；"稍等"在阶段 5 做 |
| S4 | "你好缪斯，打开热水器 / 电热毯" | 直接执行，同时安排兜底：没收到"关 / 洗完了"先提醒，超时自动关 | 提醒和超时自动关用 Hermes cron；`mijia-service` 现有的 3 小时硬上限保留作兜底；热水器要先接入 |
| S5 | "你好缪斯，播放 Spotify / 打开 xx 软件" | 在 Windows 上执行 | 改走 bridge 的白名单接口，不走 AgentDock（理由见阶段 6） |
| S6 | 不在家，在聊天里发"把空调关了" | 直接控制米家，和 Windows 开没开机无关 | 米家服务本来就在 Oracle 上；要确认文字入口（Hermes 网页）也能调用 `home_tools` |
| S7 | 长对话之后再让它控制米家 | 仍能正确调用 | 设备说明在 SOUL.md 和工具描述里，习惯在 Hermes 记忆里，两者每轮都会放进上下文，不依赖对话历史 |

#### 现状（来自 2026-10-17 的 `state.db` 对话记录，共 218 条消息）

模型是 `deepseek-v4.1-flash`，`reasoning_effort: none`，`max_turns: 6`。

- **Spotify**：
  - `spotify_play` 只接受搜索词，内部直接播搜索结果的第一条。模型已经搜到正确的歌，想按 URI 播，参数被忽略，于是"蘑菇乐队"放成了蘑菇兔子乐队，"我的摇滚"放成了别人的歌单。`spotify_ctl.py` 本身支持 `--uri`，只是没暴露。
  - 看不到个人歌单和收藏。
  - 播放接口返回成功、电脑上却没切歌时，模型照样说"放起来了"。
- **步数**：每次调用工具前都要先 `tool_describe`。"搜索 → 播放 → 核对"要 5～7 步，有 3 次撞到 `max_turns` 上限。
- **Windows**：没有任何控制通道（`terminal`、`computer_use` 已禁用）。
- **米家**：
  - 开关灯、读状态都正常。
  - 设备说明在 SOUL.md、`devices.md`、工具描述里各写了一份，已经不一致：热水器只在 `devices.md` 里有，而且标注"账号里暂未发现"。
  - 没有口语对照，也没有我的个人习惯。
  - 回报啰嗦。
  - Hermes 里没有米家 skill（旧方案的 `mijia-control` skill 在旧 box 上）。
- **记忆**：已开启，存在 `data/memories/USER.md`（关于我）和 `MEMORY.md`（其他），每轮都放进上下文。它已经自己记下"喜欢蘑菇帝国""「我的摇滚」是我的歌单"。但它也把 Spotify 工具的缺陷记了进去（"spotify_play 常选错，点歌前先搜索核对，必要时向用户确认"），工具修好后这条会让它多搜一遍、多问一句。

#### 原则

- **工具厚、提示词薄**：flash 模型多步推理不可靠，"搜索 → 选最匹配的 → 播放 → 核对"放进工具里做。
- **Hermes 已有的能力不重造**：习惯用它的记忆，定时用它的 cron。
- **规则写在代码里**：设备白名单、加热兜底、Windows 动作白名单都在代码里实现，模型绕不过去。
- **个人习惯交给 Hermes 的记忆**：我平时怎么说它就怎么记（"记住我一般十一点半睡""空调我喜欢 26 度"），记忆里没有时它问一次再记下，不手写进配置。SOUL.md 只写设备怎么用、家规，以及什么时候该去用记忆。
- **设备说明写在 SOUL.md 和工具描述里**：这是系统配置，不是习惯，每轮都在上下文里。

#### 8.1 米家：执行方式不变，补提示词和定时

**为什么不改回 skill + CLI**：

- Hermes 里要调 CLI，就得打开 `terminal` 工具集。这等于让语音助手和无人值守的定时任务拿到 Oracle 的 shell。
- MCP 只暴露几个固定动作，家规在代码里检查，模型绕不过去。

**为什么不把知识放进 skill**：

- Hermes 的 skill 要模型先调用一次才会加载，每次控制都多一步，延迟更高。
- 设备说明不到 1KB，直接写在 SOUL.md 和工具描述里，每轮都在上下文里，S7 自然满足。

结论：执行继续用现有的 `home_tools`，不新增封装。要改的是三件事：提示词、习惯交给记忆、定时交给 cron。

1. **重新核对设备**：用 mijiaAPI 列出账号里的全部设备，确认热水器和台灯是哪个设备、有哪些属性。然后更新 `devices.md` 和工具里的 `DEVICES`，SOUL.md 只写怎么用，不再重复抄一份清单。
2. **习惯交给 Hermes 记忆（USER.md）**，SOUL.md 里只写怎么用：
   - 需要习惯的操作先看记忆。例如开空调没说温度就用记忆里的偏好；安排热水器用记忆里的入睡时间和加热时长；只说"开空调"时按季节选模式。
   - 记忆里没有时只问一次，例如"你一般几点睡？"，回答后记下来，下次不再问。
   - 我说"以后……""我一般……""我喜欢……"时更新记忆；同一件事只留最新的一条。
   - 只把关于我的稳定偏好记进 USER.md。工具缺陷、系统故障不进记忆（8.2 做完后删掉现有那条 Spotify 缺陷）。
   - 记忆容量有限，只存精简的结论。
   - 验收：新会话里说一次"记住我工作日十一点半睡"，再说"我今晚想洗澡"，应该直接按十一点半安排，不再问。

   **记忆维护**：不做定时清理，因为它分不清哪条是错的，正确的记忆也会一起删掉。改成控制写入、可以纠正、定期核对。

   现状：`USER.md` 里"「我的摇滚」（创建者 Xing Wu）是用户本人的歌单"**是错的**。我说的是"放我的摇滚歌单"，我自己的歌单叫 `rock`。它看不到我的歌单，就把搜到的同名公共歌单当成了我的，而且没问我就记了下来。这是"把推断当事实记下来"的实例。

   1. **控制写入**（写进 SOUL.md）：
      - 只在我明确说"记住 / 以后 / 我一般 / 我喜欢"，或者同一偏好出现过两次以上时才写。
      - 不从一次性的请求里推断偏好。
      - 语音识别出来的人名、歌名、艺人名，只记工具确认过的结果。例如实际放出来、我也没纠正的是"蘑菇帝国"，就记"蘑菇帝国"，不记我说错的"蘑菇乐队"。
      - 推断出来的事（"这个歌单应该是你的"）要先问我一句，确认后再记。
      - 工具缺陷、系统故障不进记忆。
   2. **可以用语音纠正**：
      - "你记得我什么"：把记忆按条念出来。
      - "你记错了，……"：替换那一条。
      - "忘掉……"：删掉那一条。
      - 记忆文件在 `~/hermes/data/memories/`，也可以直接打开改。
   3. **每周核对一次**：用 Hermes cron 建一个周期任务：
      - 合并重复条目。
      - 找出像推断、像工具笔记、和其他条目矛盾的条目。
      - 只列出来发到 Hermes 网页聊天里等我确认，自己不删。
   4. **一次性清理**：
      - 删掉 `USER.md` 里「我的摇滚」那条。
      - 8.2 做完后删掉 `MEMORY.md` 里两条 Spotify 工具笔记。
      - 不手动写"摇滚歌单是 rock"：有了 `spotify_my_playlists` 以后，它能直接在我的歌单里找到 `rock`。
      - 评测加一条："放我的摇滚歌单"应该放我的 `rock`，不是搜到的公共歌单。
   5. **评测**：加一条用例。用识别错的艺人名点歌并播放成功后，检查记忆里只出现正确的名字。
3. **口语对照**：
   - 亮一点 / 暗一点按 ±20%；暖一点 / 白一点调色温。
   - "我要睡了"→ 晚安场景；"我出门了 / 我回来了"→ 离家 / 回家场景。
   - "屋里几度 / 湿度多少"→ 温湿度计；"门关了吗"→ 门窗传感器。
   - "好冷 / 好热"不直接操作，问一句要不要开空调。
   - 一句话里有多个操作时，在同一轮里一起调用。
   - 回报只说一句话。
4. **定时和兜底（S2、S4）用 Hermes cron**：
   - cron 支持一次性和周期任务，任务存在 `jobs.json`，重启不丢；可以查看、修改、删除。到点会开一个新会话，由 AI 执行创建任务时写好的指令。
   - S2 "我今晚想洗澡"：从记忆里取入睡时间、睡前多久洗、热水器要热多久，算出开启时间，建一个一次性任务"打开热水器"，播报"已安排 xx 点开始加热"。这里的时间由模型自己算，评测里重点检查；如果常算错，再加一个只负责算时间的工具。
   - S4 加热类设备打开后，再建两个一次性任务：
     - 到点提醒：还开着就播报提醒一次。
     - 超时：还开着就关掉。
     - 我说"洗完了 / 关掉"时，关设备，同时删掉这两个任务。
   - 任务里的指令要写完整：新会话看不到当前对话，要写明设备名和具体动作。这条写进 SOUL.md。
   - 提醒要播报到音箱，需要一个播报工具。阶段 6 之前用米家云让 OH2P（did `2181631827`）播报，之后改走 bridge 的 `speaker_announce`。
   - `mijia-service` 现有的"加热类最多开 3 小时"硬上限保留，作为 cron 没执行时的兜底。只补一点：服务启动时发现加热类开着，就重新计时。
   - 要先确认两件事：
     - cron 任务里确实能调用 `home_tools`。现在 `platform_toolsets.cron` 只列了 `memory`；全局 MCP 应该会自动带上，但 Hermes 有过一个 bug：单个任务另外指定工具集时，MCP 工具会全部丢失（issue #23997），要看 0.22.2 是否已修。
     - 语音入口（`api_server`）能用 `cronjob` 建任务：已在 `platform_toolsets.api_server` 里。
5. 新增 `mijia_status()`：一次读出全屋状态，查询类问题一步就能答。

#### 8.2 Spotify：补 MCP 工具

1. `spotify_play` 增加 `uri` 参数：
   - 有 URI 直接播。
   - 没有时在工具内部搜索，按歌名和艺人的匹配度挑选。
   - 播放后核对实际在播的内容，不一致返回 `ok: false`。
2. 新增 `spotify_my_playlists(query="")`，并支持播放"我喜欢的歌曲"。可能需要重新授权，补上 `playlist-read-private`、`user-library-read` 权限。
3. 把 `spotify_ctl.py` 已有的 `queue`、`shuffle` 暴露成工具。
4. 电脑上的 Spotify 没打开时，先调用 8.3 的接口打开它，再重试。

#### 8.3 Windows 控制（S5）

- **动作白名单（已确认）**：
  - 系统音量：设置、加减、静音。
  - 媒体键：播放 / 暂停、上一首、下一首。
  - 打开或关闭指定程序：先只有 Spotify，其余按需加。
  - 锁屏、关显示器、睡眠。
  - 关机：60 秒倒计时，可以说"取消关机"撤回。
- **通道**：bridge 本来就跑在这台 Windows 上，在它的 API server 加 `/api/pc/*`，每个接口对应一条 Windows 自带命令（锁屏 `rundll32 user32.dll,LockWorkStation`，关机 `shutdown /s /t 60`，取消 `shutdown /a`，等等）。外网访问复用 AgentDock 已有的 cloudflared 隧道，和阶段 6 是同一条域名和同一个 Access 令牌。`home_tools` 新增 `pc_*` 工具。
- **不让 Hermes 直连 AgentDock**：AgentDock 没有按工具或按令牌的权限控制。它的令牌就是全权限，执行命令和改文件的工具一直开着，审批也只针对 Coding Agent。直连等于 Oracle 上存着一个能完全控制这台电脑的令牌，网页内容里的注入指令也可能被执行。
- **不做**：执行任意命令、读写文件、任意键盘鼠标操作（音量键、媒体键由接口内部按固定的键）、浏览网页。

#### 8.4 模型和运行参数

1. 看 Hermes 能否把 `home_tools` 的工具定义直接放进上下文（不懒加载），省掉每次的 `tool_describe`。
2. `max_turns` 6 → 15（改 `~/hermes/profile/config.yaml`，重新跑 `install.sh` 同步到 `data/config.yaml`，再重启容器）。留足余量给重试和一句话里有多个操作的情况。平均步数仍在评测里监控：上限放宽不等于允许绕远路。
3. 用评测集对比 `deepseek-v4.1-flash` 和一个更强的模型，看正确率和首句延迟，再决定换不换，或者只在调用工具时用强模型。
4. SOUL.md：回复短、少反问；失败时只说一次原因和下一步。

#### 8.5 评测和验收

- S2～S7 每条写成评测用例，再加 30 句左右常用口令，包括识别错字的写法（如"播放我的摇滚歌达"）。
- 评测时：
  - 用单独的 `session_key`。
  - `home_tools` 设 `HOME_TOOLS_DRY_RUN=1`，不真正执行，也能模拟"设备离线""Spotify 没开"这类故障。
  - 用 `scripts/hermes_eval.py` 从 `state.db` 读出工具调用，统计正确率、步数、延迟和虚报次数。
- 先用现在的配置跑一遍作为基线。
- 目标：米家 ≥ 95%，Spotify ≥ 90%，Windows ≥ 95%，虚报 0 次。
- 最后真机把 S2～S7 各走一遍。

顺序：

1. 8.1 第 1、2 条：核对设备，改 SOUL.md 里使用记忆的规则；然后我用语音或网页把习惯告诉它一遍，顺便验收记忆。
2. 8.1 第 3～5 条和 8.2。
3. 8.4。
4. 8.3（等阶段 6 的隧道）。

S2 的"稍等"跟阶段 5 一起做。

## 5. 执行顺序

| 顺序 | 内容 | 原因 |
|---|---|---|
| 1 | 阶段 1 | 最大的未知数，不通的话打断功能要重新设计 |
| 2 | 阶段 2（已提前完成） | 依赖阶段 1 |
| 3 | 阶段 3 + 4 | 只改服务端，风险低，见效快 |
| 4 | 阶段 5 | 改动独立 |
| 5 | 阶段 6 | 可与 2～4 并行，主要在 Oracle 端 |
| 6 | 阶段 7 | 收尾 |
| 7 | 阶段 8 | 米家、Spotify、模型参数只改 Oracle 端，可以随时插队；Windows（8.3）依赖阶段 6 的隧道 |

## 6. 待确认

1. AgentDock 的 cloudflared 隧道能否另加一个域名。
2. 勿扰时段：跟随音箱自己的勿扰设置，还是写死时间段。
3. ~~回采声道在 ROM 1.62.2 上是否为索引 3~~：已确认（阶段 1）。
4. 阶段 8.1：热水器和台灯是米家里的哪个设备（现在设备清单里都没有）。
5. 阶段 8.2：Spotify 重新授权，补上读取个人歌单和收藏的权限。
6. 阶段 8.4：能否接受换更强的模型（费用、延迟），等评测结果再定。

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
- 2026-10-18：修复"你好缪斯"唤醒失效：新客户端 S32 `>> 8` 比旧客户端响约 256 倍，`audio_input.gain` 4.0 → 1.0。
- 2026-10-18：**阶段 2 完成并真机验收**（打断、补录、灯效）。

### 阶段 2 改动

| 文件 | 改动 |
|---|---|
| `core/services/audio/aec.py` | 移植 OwnDing 子带回声消除；加两道保护：前 16 帧只学习不相减、每个频点输出不超过麦克风（不加时每次开始播放前 0.5 秒会放大 15～30 dB） |
| `core/xiaoai.py` | `mic_ref` 帧先消回声再乘 `echo_mic_gain`；`echo_reference_live` / `echo_active` / `near_end_ratio_db`；录音看门狗（`set_recording_wanted`） |
| `core/services/audio/stream.py` | `GlobalStream` 保留最近 3 秒、绝对采样位置 `samples`、`since()`；`MyStream.unread_samples()` |
| `core/services/audio/vad/__init__.py` | `resume(target, preroll_from=)` 补录；回声残留期间用 `echo_vad_threshold` |
| `core/services/audio/kws/__init__.py` | `arm_barge_in` / `disarm_barge_in`：暂停时仍检测，命中交给打断处理并带上命中位置 |
| `core/external_conversation.py` | 回采在线时不关麦、可打断、打断后直接下一轮（不播提示音）、去掉补录里的唤醒词；灯效改用 `ConversationLight` |
| `core/services/conversation_light.py` | 状态驱动灯效，替代 `listening_light.py` |
| `config.py` | `barge_in.enabled` / `echo_reference` 打开；`speech_lookback_ms`、`echo_vad_threshold`、`near_end_min_db`、`echo_canceller`；`listening_light.hearing_effect_id` |

验证：

- 阶段 1 的真机双声道录音离线回放：开始学习后每次播放开头即消 22～27 dB（软件启动后第一次播放约 6 dB），CPU 1～4%。
- 真机：音箱自己念 4 遍"你好缪斯 / 嗨缪斯 / hey muse"未自唤醒；用户实测打断、补录、灯效通过。

未做 / 留到后面：

- `near_end_min_db` 先不设门限，每次打断的近端 dB 写在日志里，积累数据后再定。
- 播完后的提示音和 `_wait_for_silence` 仍在（阶段 4）。
- `tests/test_echo_ref_input.py::test_mic_ref_keeps_mic_channel_with_gain` 仍按"直接乘 64"断言，需按回声消除的 1024 采样延迟改写。

- 2026-10-18：**阶段 3 + 4 代码完成**，离线验收后部署到本机 bridge，等待真机验收。

### 阶段 3 + 4 改动

| 文件 | 改动 |
|---|---|
| `core/services/audio/kws/detector.py`（新） | 唤醒检测核心：VAD 门控 + 300 ms 预录 + 关键词模型。线上和离线验收共用 |
| `core/services/audio/kws/sherpa.py` | `max_active_paths`、`stream_offsets_ms`（多路错开）可配 |
| `core/services/audio/kws/keywords.py` | `kws.keyword_thresholds` / `keyword_scores`：每个唤醒词单独阈值 / 加权 |
| `core/services/audio/kws/__init__.py`、`vad/silero.py` | KWS 用独立的 Silero（以前和 VAD 共用一个，状态互相污染）；普通唤醒也带上唤醒词结束位置 |
| `core/services/audio/stream.py` | `GlobalStream` 加锁、保留 6 秒；读者可以从历史位置开始回放（`since`） |
| `core/services/audio/vad/__init__.py` | `resume(replay_from=)` 回放历史；判定开口后从停下的位置接着读；回声阈值按采样位置判断；每轮开始清 Silero 状态 |
| `core/external_conversation.py` | 去掉 `_wait_for_silence`；第一轮从唤醒词后面接着听（"你好缪斯，开灯"）；播完从"播完 + 150 ms"回放（可以接话）；提示音可配；退出词改用 `core/exit_words.py` |
| `core/exit_words.py`（新） | 移植 OwnDing 退出词规则："拜拜"马上退出，"关灯，拜拜"先回答再退出 |
| `core/xiaoai.py` | `echo_active_at(sample)`；`AUDIO_INPUT_DUMP` 录下 KWS 实际收到的音频 |
| `config.py` | 唤醒词只留缪斯一组（加"嘿缪斯"，不加单独的"缪斯"）；`kws` 新参数；新增 `conversation` 段；退出词表 |
| `scripts/kws_acceptance.py`、`kws_samples.py`、`kws_phrases.py`（新） | 验收：`synth` 合成语音命中率 / 误唤醒，`dialog` 一口气说完 + 退出词，`wav` 真机录音 |

离线验收（edge-tts 合成，9 个中文语音 + 2 个英文语音，安静 / 小声 / 电视背景等 8 种条件）：

| 唤醒词 | 改动前·安静 | 改动后·安静 | 改动后·8 种条件 |
|---|---|---|---|
| 你好缪斯 | 约 78% | 97% | 91% |
| 哈喽缪斯 | 约 57% | 100% | 88% |
| 嗨缪斯 | 约 18% | 93% | 73% |
| 嘿缪斯 | — | 85% | 56% |
| hi / hey / hello muse | 0～33% | 80～93% | 30～47% |

误唤醒：电视长段 7 分钟 0 次；相近发音和日常语句 688 条中 1 次；句中提到"缪斯"128 条中 1 次。单独的"缪斯"放进 KWS 后，句中提到"缪斯"32 条中了 29 条，所以不做。

已知问题（未改）：

- 唤醒词尾音会混进请求（"司开灯"）：补录往回 350 ms 偏多，离线扫描 200 ms 最好，但这个值打断时也用，改动后要在真机复测打断。
- "拜拜"偶尔被识别成"白白"，不会退出。

- 2026-10-18：**阶段 3 + 4 提交**（分支 `feat/ownding-migration`）。同时提前做了阶段 7 的一部分：`config.py` 删掉上游的龙虾 / 小黑 / 小爪 / 小智演示路由和告别语，`before_wakeup` 只留缪斯 → `"openai"`，`after_wakeup` 的 `"openai"` 告别语从"小黑，再见"改为"再见"。
