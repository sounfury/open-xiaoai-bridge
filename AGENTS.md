# AGENTS.md

> 小爱音箱与外部 AI 服务（小智 AI、OpenClaw）的桥接器。
> 接管音箱音频输入输出，实现与第三方 AI 的对话。

## 系统架构

详见 [README.md 系统架构](README.md#系统架构)（含 Mermaid 流程图和各模块工作流程）。

## 项目结构

```
open-xiaoai-bridge/
├── main.py                        # 入口：解析环境变量，启动 MainApp
├── config.py                      # 用户配置（唤醒词、路由钩子、TTS、OpenClaw 等）
├── core/
│   ├── app.py                     # MainApp 主控制器（单例，管理生命周期）
│   ├── xiaoai.py                  # XiaoAI 设备接入 / 事件桥接
│   ├── xiaoai_conversation.py     # 小爱连续对话策略
│   ├── xiaozhi.py                 # 小智 AI WebSocket 协议客户端
│   ├── openclaw.py                # OpenClaw 网关客户端（连接、消息、TTS 播放）
│   ├── external_conversation.py   # 外部后端连续对话基类（VAD → ASR → Agent → TTS，播报中打断）
│   ├── openclaw_conversation.py   # OpenClaw 连续对话循环（VAD → ASR → Agent → TTS）
│   ├── wakeup_session.py          # 小智唤醒会话状态机
│   ├── muse.py                    # 小爱管家（你好缪斯）唤醒词：集合、文本解析、KWS 子集、拦截原生小爱
│   ├── ref.py                     # 全局引用注册表（get/set 依赖注入）
│   ├── models/                    # 模型文件（KWS/VAD/ASR，.gitignore 排除）
│   ├── assets/sounds/             # 音效（tts_notify.mp3 等）
│   ├── services/
│   │   ├── speaker.py             # SpeakerManager 音箱硬件控制
│   │   ├── api_server.py          # HTTP REST API（aiohttp）
│   │   ├── conversation_light.py  # 连续对话灯效（状态驱动、后台切换）
│   │   ├── light_level.py         # 聆听灯随麦克风音量起伏（推噪声给音箱 vis）
│   │   ├── tts/router.py           # 统一 TTS provider 路由与播放
│   │   ├── tts/wake_reply.py       # 唤醒 / 打断后的应答语（小爱原生 TTS 生成后缓存在音箱 /tmp）
│   │   ├── tts/segmenter.py        # 流式回复切段 + 朗读前清理（Markdown / <think> / 开头英文）
│   │   ├── tts/segment_player.py   # 分段播放（小爱原生预生成流水线 / 按段回调）
│   │   ├── audio/
│   │   │   ├── stream.py          # GlobalStream 全局音频流（多路输入广播，保留最近 3 秒）
│   │   │   ├── aec.py             # 子带回声消除（移植自 OwnDing/open-xiaoai）
│   │   │   ├── codec.py           # 音频编解码
│   │   │   ├── vad/silero.py      # Silero VAD 语音活动检测（ONNX）
│   │   │   ├── kws/sherpa.py      # Sherpa KWS 关键词唤醒
│   │   │   └── asr/sherpa.py      # Sherpa ASR 离线语音识别（SenseVoice）
│   │   ├── tts/doubao.py          # 豆包 TTS 客户端（火山引擎）
│   │   ├── tts/openai.py           # OpenAI-compatible TTS 协议客户端
│   │   ├── tts/mlx_audio.py        # MLX-Audio TTS provider（复用 OpenAI 协议客户端）
│   │   └── protocols/
│   │       ├── websocket_protocol.py  # 小智 WebSocket 协议实现
│   │       └── typing.py              # 协议类型定义
│   └── utils/
│       ├── logger.py              # 彩色日志（XiaozhiLogger 单例）
│       ├── config.py              # ConfigManager（嵌套路径查询、热重载）
│       ├── config_loader.py       # config.py 动态导入
│       ├── base.py                # 基础工具
│       └── file.py                # 文件工具
├── native/                        # Rust PyO3 扩展（maturin 编译）
│   └── src/
│       ├── lib.rs                 # 模块入口：on_output_data, start_server, stop/start_recording, stop/start_playing
│       ├── server.rs              # WebSocket 音频服务器（TCP :4399）
│       ├── python.rs              # Python 回调注册中心（HashMap）
│       ├── macros.rs              # 辅助宏
│       └── tts/                   # TTS 音频处理（流式、PCM 直通、MP3 解码）
├── app/openclaw/                  # OpenClaw 设备身份存储（Ed25519 密钥）
├── skills/xiaoai-tts/             # Agent 工具：通过 HTTP API 控制小爱播放
└── tests/                         # 测试脚本
```

## 核心组件

### MainApp (core/app.py)

应用主控制器，单例模式，管理全部服务生命周期。

- `instance(enable_xiaozhi, enable_openclaw)` → 单例获取
- `run(enable_api_server)` → 启动各服务
- `set_device_state(state)` → 管理设备状态（IDLE / LISTENING / SPEAKING / CONNECTING）
- `send_text(text)` → 发送文本到小智
- `send_to_openclaw(text, wait_response)` → 发送消息到 OpenClaw（返回 run_id 或回复文本）
- `send_to_openclaw_and_play_reply(text, wait_response)` → 发送并 TTS 播放回复
- `schedule(callback)` → 主线程任务队列
- `shutdown()` → 优雅关闭

**边界约束**:
- `MainApp` 是业务主循环和设备状态的单一入口
- `device_state` 以 `MainApp` 为准，其他模块通过代理回写，不各自维护平行状态
- `MainApp.loop` 是业务协程的主调度循环

### XiaoAI (core/xiaoai.py)

小爱音箱交互接口，类级变量（classmethod 风格）。

- `init_xiaoai()` → 初始化原生服务，注册事件处理
- `on_event(event)` → 处理小爱事件（RecognizeResult / AudioPlayer）
- `on_input_data(data)` / `on_output_data(data)` → 麦克风 / 扬声器音频回调
- `on_input_packet((data, metadata))` → 新客户端录音帧；`layout == "mic_ref"`（[麦克风, 播放回采] 双声道）时先过回声消除，再按单声道送进 `GlobalStream`
- `echo_reference_live()` / `echo_active()` / `near_end_ratio_db()` → 回采是否在线、当前输入是否可能带播放残留、打断时近端人声强度
- `set_recording_wanted(wanted)` → 会话层主动关麦时置 False；录音看门狗只在"应该有录音"且断流 4 秒后重新请求录音
- `run_shell(script, timeout)` → 远端 shell 执行
- 内部维护独立 `async_loop`（后台线程），仅用于原生扩展回调和事件桥接

**边界约束**:
- 负责设备接入和事件桥接，不承载连续对话策略
- 连续对话状态放在 `xiaoai_conversation.py`
- `async_loop` 不应承载新的业务状态机

### XiaoZhi (core/xiaozhi.py)

小智 AI WebSocket 协议客户端，单例模式。

- `connect()` / `disconnect()` → 连接管理
- `send_audio(frames)` / `send_text(text)` → 发送音频 / 文本
- `send_start_listening(mode)` / `send_abort_speaking(reason)` → 协议命令
- 回调委托：`on_incoming_audio`, `on_incoming_json`, `on_network_error` 等

**边界约束**:
- 只负责协议收发，不负责唤醒策略和连续对话策略
- `session_id` 必须由服务端消息更新，不能长期使用空值发送控制消息

### OpenClawManager (core/openclaw.py)

OpenClaw 网关客户端，管理 WebSocket 连接、消息分发、自动重连、TTS 播放。

- `initialize_from_config(enabled)` → 从 config 初始化
- `connect()` → 建立连接（Ed25519 设备身份认证）
- `send(text, wait_response)` → 发送消息，返回 run_id 或回复文本，失败返回 None
- `send_and_play(text, wait_response)` → 发送并 TTS 播放回复
- `is_connected()` / `is_enabled()` → 状态查询

**内部机制**:
- Ed25519 设备身份认证（密钥存储在 `app/openclaw/identity/`）
- WebSocket ping/pong + tick 事件监控连接健康
- 指数退避重连（初始 1s，最大 60s）
- 请求 ID 映射 `_pending: dict[str, asyncio.Future]` 追踪响应
- TTS 播放：通过共享 TTS Router 选择 `xiaoai`、`doubao`、`openai` 或 `mlx_audio` provider；缺省时保持 `tts_speaker` 的旧选择逻辑
- Rust TTS 播放使用单一活动 `playback_token`：开始新的 Rust TTS 会使旧 token 失效；`stop_tts_playback(token)` 只应由持有该 token 的调用方定向停止自己的播放

**连接参数限制**:
- `client.id`: 必须是 OpenClaw 预定义常量
- `client.mode`: 必须是预定义常量
- `session_key`: 只从 config.py 读取

### OpenClawConversationController (core/openclaw_conversation.py)

OpenClaw 连续对话控制器。唤醒词触发后进入独立的 VAD → ASR → OpenClaw → TTS 循环。

- `start()` → 进入对话模式
- `stop()` → 退出对话
- `is_active()` → 状态查询

**对话循环** (`_run_one_turn`):
1. VAD 检测语音开始（`_wait_for_speech`）
2. 录制完整语音（VAD 帧 hook）
3. SherpaASR 离线识别
4. 退出关键词检测
5. 发送到 OpenClaw
6. TTS 播放回复（等待完成，或被唤醒词打断）
7. 恢复监听

**回声防护与播报中打断**（`barge_in` 配置，实现在 `core/external_conversation.py`）:
- 回采在线（`barge_in.enabled` 且 `XiaoAI.echo_reference_live()`）时：播报期间**不关麦**，回声由 `core/services/audio/aec.py` 消除；KWS 通过 `arm_barge_in(handler)` 在暂停状态下继续检测，命中后先切灯，再用自己持有的 `playback_token` 停 TTS；启用打断应答语时先等待 `stop_device_audio()` 完成，再随机说一句应答语（"我在"/"在"/"哎"…，`conversation.wake_reply.on_barge_in`）后进入下一轮；关闭应答语时后台停播并继续监听
- 打断命中位置（`GlobalStream` 绝对采样位置）往前 `speech_lookback_ms` 作为下一轮 `VAD.resume("speech", preroll_from=...)` 的预录，"你好缪斯，明天天气怎么样"后半句不丢；ASR 结果去掉句首唤醒词
- 回声残留期间（播放中及停播后 `echo_vad_hangover_ms`）VAD 用更高的 `echo_vad_threshold`
- 回采不在线时退回旧方式：`stop_recording` → kill 远端 arecord → 麦克风物理静音，TTS 和提示音都在关麦期间播放
- `VAD.resume()` 会自动 `_reset_state()` + `input_bytes.clear()`，清除旧的 `speech_frames` 和音频流缓冲（带 `preroll_from` 时再从 `GlobalStream` 历史补回）

**流式回复和分段播报**（后端 `supports_streaming()` 为真时，目前只有 OpenAI / Hermes）:
- `OpenAIManager.stream_reply(text)` 产出 `("text", delta)` / `("tool", hermes.tool.progress)`；只取 `delta.content`，不取 `reasoning_content`；结束或被取消时把已收到的回复记进历史
- `ReplyStream` 在后台读流（创建即发请求，和"咻"并行）；`_speak_stream` 用 `core/services/tts/segmenter.py` 切段，送进 `core/services/tts/segment_player.py`
- `tts_speaker == "xiaoai"`：`XiaoAINativeSegmentPlayer`（`mibrain text_to_speech save=1` 预生成 + `miplayer -f` 依次播放，临时文件 `/tmp/open-xiaoai-bridge-tts-*.mp3`）；其他 provider：`CallbackSegmentPlayer` 按段调 `_play_tts`
- 工具开始、还没有正文时念一次 `openai.tool_filler`（"稍等" / "我查一下"）
- 打断覆盖整个回复过程（包括后端还在思考）：取消 `_speak` 任务 → `player.abort()`（`killall miplayer`、删临时文件）+ `ReplyStream.close()`（断开 HTTP）
- 分段播放一段都没播成（`player.error` 且 `played == 0`）时，整段退回 `_play_tts`

**灯效**（`core/services/conversation_light.py`）:
- OpenAI / 缪斯会话调用 `_light_state(state)`，和原生小爱一致：提示音 / 回答 `speaking` 3（跟播放音量起伏，miplayer 自带）→ `listening` / `hearing` 41（短白灯，随你的音量变宽）→ `thinking` 2 → `speaking` 3 → `off`；不再用黄灯 9
- 聆听灯随音量（`core/services/light_level.py`，配置 `conversation.light_level`）：音箱 `vis` 从 `/tmp/mic_audio.fifo` 读 48 kHz PCM 算音量、UDP 发给 `ledd`；`ubus call mic_audio set_event '{"event":1,"value":1}'` 切到麦克风、`value:2` 切回播放。bridge 开 TCP 4400，音箱 `nc` 连进来写 FIFO；bridge 只送"同响度的噪声"，不送语音
  - `vis` 要求输入不能明显慢于实时，否则关掉重开 FIFO，`nc` 写入 EPIPE 被杀：bridge 用自己的时钟每 110 ms 发一帧 9600 字节（不跟麦克风包走），音箱端 `nc` 被杀自动重连
  - 不能比实时快太多，否则 FIFO 积压成延迟；当前保留小幅噪声作为静音底值；噪声 RMS 约 8000 时实测 `vis` 偶发输出 0，具体原因未确认
  - 宽度按"比跟踪到的底噪高多少 dB"算（`margin_db` / `span_db`），每轮 INFO 日志有一行"麦克风音量"
  - 设备时钟比 bridge 主机快约 12 秒，对照音箱 strace 和 bridge 日志时注意换算
- 应答语（`core/services/tts/wake_reply.py`，配置 `conversation.wake_reply`）：唤醒 / 打断后随机说一句代替"叮"；小爱原生 TTS 时首次用 `mibrain text_to_speech` 生成、存 `/tmp/open-xiaoai-bridge-reply-<hash>.mp3` 复用，`miplayer` 播。打断时必须先 `await stop_device_audio()` 再播（它会 `killall miplayer`）
- 超时：`wakeup.timeout` 秒没开口就退出（播 after_wakeup 告别语）；等待期结束时若已经开口，则继续等待至多 `conversation.max_utterance_s` 秒
- 后台任务按最新状态切灯，不阻塞语音流程；`set()` 可在 VAD/KWS 线程调用

**VAD 状态泄漏陷阱**:
- VAD 检测循环持续运行，`speech_frames` 会不断积累音频帧
- 如果 `resume()` 不调用 `_reset_state()`，旧帧（唤醒词回声、TTS 回声）会泄漏到下一轮检测，导致 ASR 识别出幽灵音频
- `pause()` 会调 `_reset_state()`，但 `resume()` 必须也调——两者都需要清理状态

**边界约束**:
- 使用独立 VAD Future，不与 WakeupSessionManager 冲突
- 播报被等待完成后才继续监听；唯一的提前结束方式是打断（或 `stop()`）
- 主动关麦（`_stop_recording`）必须同时 `set_recording_wanted(False)`，否则录音看门狗会在播报中重新打开麦克风；会话结束时恢复为 True
- 自己持有并管理当前 TTS 的 `playback_token`；停止 OpenClaw 对话时应调用 `stop_tts_playback(token)`，不要在外层直接无 token 全局停止 Rust TTS

### WakeupSessionManager (core/wakeup_session.py)

小智唤醒会话状态机，协调 KWS → VAD → 小智/OpenClaw 的唤醒流程。

- `wakeup(text, source)` → 处理唤醒（调用 `before_wakeup` 钩子，路由到 XiaoZhi 或 OpenClaw）
- `wait_next_step(timeout)` → 异步等待状态变化（带待决状态缓冲）
- `update_step(step, step_data)` → 更新步骤
- 事件回调：`on_interrupt()`, `on_wakeup()`, `on_tts_start()`, `on_tts_end()`, `on_speech()`, `on_silence()`
- `on_interrupt()` → 小爱唤醒时：cancel OpenClaw task、停止设备音频播放、恢复录音通道、stop XiaoAI conversation

**路由规则**（`before_wakeup` 返回值）:
- `"xiaozhi"` → 走小智流程
- `"openclaw"` → 走 OpenClaw 连续对话
- `"openai"` → 走 OpenAI 兼容服务（Hermes）连续对话；"你好缪斯"唤醒词（`MUSE_ENABLE=1`）也路由到这里
- `"qwenpaw"` → 走 QwenPaw 连续对话
- `None` → 不处理（用户自行处理）

**小爱管家（muse）边界约束**:
- `core/muse.py` 只负责唤醒词（匹配、KWS 子集、拦截原生小爱），不再有自己的请求链路；对话由 `"openai"` 路由负责
- `core/muse.py` 顶层不能导入 `open_xiaoai_server`（config.py / 测试会直接导入）

**边界约束**:
- 它是"小智唤醒会话状态机"，不是通用事件总线
- 只允许缓存 `on_speech` / `on_silence` 等外部探测信号
- 不要缓存 `on_wakeup` / `on_interrupt` 等控制步骤

### XiaoAIConversationController (core/xiaoai_conversation.py)

小爱自身的连续对话管理。

- `handle_text_command(text, speaker)` → 处理退出 / 连续对话关键词
- `handle_listening_timeout(speaker)` → 超时重试逻辑
- `handle_audio_player_instruction(header_name)` → 检测播放器指令退出
- `handle_playing_status(playing_status, speaker)` → TTS 完成后重新唤醒

**边界约束**:
- 小爱连续对话和小智唤醒 / 会话超时是两套独立机制
- 只有在"小爱连续对话确实激活"时才允许停止
- 小智超时退出时不应打印"小爱停止连续对话"日志

### SpeakerManager (core/services/speaker.py)

音箱硬件控制。

- `play(text, url, buffer, blocking, timeout)` → 播放文字 / URL / PCM 缓冲
- `stop_device_audio()` → 停止设备上的播放链路（阻塞 TTS / 非阻塞 TTS / PCM），并重启 PCM 播放通道
- `wake_up(awake, silent)` → 唤醒 / 休眠小爱
- `abort_xiaoai()` → 中断小爱当前操作
- `ask_xiaoai(text, silent)` → 让小爱执行指令
- `run_shell(command, timeout)` → RPC shell

**边界约束**:
- `stop_device_audio()` 只负责"停播放"，不负责恢复录音；`start_recording()` 属于会话层恢复逻辑，应由 `WakeupSessionManager` / `OpenClawConversationController` 等上层按场景决定

### APIServer (core/services/api_server.py)

HTTP REST API 服务器（aiohttp），端口可配（默认 9092），默认监听 `127.0.0.1`（`API_SERVER_HOST`）。
设置 `API_SERVER_TOKEN` 后除 `/api/health` 外都需要 `Authorization: Bearer <token>`。

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/play/text` | POST | 播放文本 |
| `/api/play/url` | POST | 播放 URL |
| `/api/play/file` | POST | 播放本地文件 |
| `/api/status` | GET | 获取设备状态 |
| `/api/wakeup` | POST | 唤醒设备 |
| `/api/interrupt` | POST | 中断播放 |
| `/api/health` | GET | 健康检查 |
| `/api/tts/doubao` | POST | Doubao TTS 合成 |
| `/api/tts/doubao_voices` | GET | 获取音色列表 |

### 音频处理链

| 模块 | 文件 | 职责 |
|------|------|------|
| GlobalStream | `audio/stream.py` | 多路输入广播（模拟 PyAudio API） |
| VAD | `audio/vad/silero.py` | Silero ONNX 语音活动检测 |
| KWS | `audio/kws/sherpa.py` | Sherpa ONNX 关键词唤醒（信心度 2.0，阈值 0.2） |
| ASR | `audio/asr/sherpa.py` | Sherpa SenseVoice 离线语音识别（懒加载，INT8 量化） |
| TTS | `tts/openai.py`, `tts/mlx_audio.py`, `tts/doubao.py` | OpenAI-compatible TTS、MLX-Audio 与豆包 TTS（一次性/本地音频播放） |

### Rust 原生扩展 (native/)

通过 maturin + PyO3 编译的 `open_xiaoai_server` Python 模块。

| 文件 | 职责 |
|------|------|
| `lib.rs` | 模块入口：`on_output_data`, `start_server`, `stop/start_recording`, `stop/start_playing`, `run_shell` |
| `server.rs` | TCP :4399 WebSocket 服务器，处理音频流和事件路由 |
| `python.rs` | Python 回调注册中心（`register_fn` / `call_fn`），跨语言调用 |
| `tts/` | TTS 音频处理：HTTP 流式请求、MP3 解码、PCM 直通 |

## 运行模式

### 模式 1: 仅小爱（默认）
```bash
uv run main.py
```
- 不启动 KWS/VAD 初始化
- `core/services/audio/kws/keywords.py` 在此模式下应直接退出成功

### 模式 2: 小智 AI
```bash
XIAOZHI_ENABLE=1 uv run main.py
```
- 启动 VAD + KWS，唤醒后连接小智 AI
- KWS 初始化失败应视为启动失败

### 模式 3: OpenClaw
```bash
OPENCLAW_ENABLE=1 uv run main.py
```
- 小爱指令拦截 → 转发到 OpenClaw → TTS 播放结果

### 模式 4: 小智 + OpenClaw（混合）
```bash
XIAOZHI_ENABLE=1 OPENCLAW_ENABLE=1 uv run main.py
```
- config.py `before_wakeup` 按唤醒词路由到小智或 OpenClaw 连续对话
- OpenClaw 连续对话：VAD → ASR → OpenClaw → TTS 循环
- 退出关键词：config `openclaw.exit_keywords`

### 启用 API Server
```bash
API_SERVER_ENABLE=1 uv run main.py
```

## 开发规范

### 代码风格
- 中文注释和文档字符串
- 英文 commit message
- 类型提示: `dict[str, asyncio.Future]`

### 异步编程
- 所有 I/O 使用 `async/await`
- 线程安全使用 `asyncio.run_coroutine_threadsafe()`
- `MainApp.loop` 是业务协程主循环
- `XiaoAI.async_loop` 仅用于原生扩展回调桥接，不挂新业务状态机

### 日志规范
- 所有日志必须带模块标识：通过 `module=` 参数或 `[Module]` 前缀
- 使用 `core.utils.logger.logger`，禁止裸 `print`
- 调试输出用 `DEBUG` 级别，不污染 `INFO`
- 消息体不要重复模块名（模块名已在日志前缀中）
- 唯一允许的裸输出：启动 ASCII banner

### 全局引用 (ref.py)
- `set_app/get_app`, `set_xiaozhi/get_xiaozhi`, `set_xiaoai/get_xiaoai`
- `set_vad/get_vad`, `set_kws/get_kws`, `set_speaker/get_speaker`
- `set_audio_codec/get_audio_codec`, `set_speech_frames/get_speech_frames`

### 本机运行注意
- bridge 在跑时不要用 `uv run` / `uv sync`：它会重装项目包，`open_xiaoai_server.pyd` 被占用删不掉，重装中断后 `open_xiaoai_server/__init__.py` 会丢，下次启动报 `no attribute 'register_fn'`。跑脚本直接用 `.venv/Scripts/python.exe`
- 已经弄坏时：从 `dist-migration/*.whl` 取回 `open_xiaoai_server/__init__.py`（或 `uv pip install --force-reinstall --no-deps` 这个 wheel，需先停 bridge）
- 重启：结束 `start-bridge.bat` 的 cmd 进程树，再 `schtasks /Run /TN \OpenXiaoAI-Bridge`；日志在 `logs/bridge-autostart.log`

### 兼容约束
- `CLI` 环境变量不再作为功能开关，不要引入依赖 `CLI` 的运行时分支
- `XIAOZHI_ENABLE=0` 时必须允许跳过 KWS 初始化
- `scripts/start.sh` 在仅小爱模式下不应检查 `core/models/` 下的模型文件

## 测试
不写任何单测，直接连接音箱做验收测试

## 音箱设备控制命令

小爱音箱（LX06 等）基于 OpenWrt + busybox，设备端命令和行为如下：

### 音频播放通道

音箱上有多条独立的音频播放通道，中断时需要分别处理：

| 通道 | 进程/服务 | 触发方式 | 中断方式 |
|------|-----------|---------|---------|
| PCM 直通 | `aplay` | `open_xiaoai_server.start_playing()` → WebSocket stream | `open_xiaoai_server.stop_playing()` |
| 阻塞 TTS | `tts_play.sh` → `miplayer -f <file>` | `speaker.play(blocking=True)` | `killall tts_play.sh miplayer` |
| 分段 TTS（流式回复） | `miplayer -f /tmp/open-xiaoai-bridge-tts-*.mp3` | `XiaoAINativeSegmentPlayer`（先 `mibrain text_to_speech save=1` 生成） | `player.abort()` / `killall miplayer` |
| 非阻塞 TTS | `mibrain_service` (内部播放) | `speaker.play(blocking=False)` → `ubus call mibrain text_to_speech` | `mphelper pause`（不一定可靠） |
| 媒体播放器 | `mediaplayer` (系统守护进程) | `ubus call mediaplayer player_play_url` | `mphelper pause` / `ubus call mediaplayer player_play_operation '{"action":"pause"}'` |

### tts_play.sh 工作流程

`/usr/sbin/tts_play.sh` 是设备上的阻塞 TTS 脚本，内部流程：
1. `mphelper pause` — 暂停当前播放
2. `ubus call mibrain text_to_speech '{"text":"...","save":1}'` — 生成音频文件到 `/tmp/tts/`
3. `miplayer -f <path>` — 播放音频文件（子进程）
4. `rm <path>` — 清理临时文件

**关键注意事项**：
- 杀掉 `tts_play.sh` **不会**自动杀掉子进程 `miplayer`，必须同时 `killall miplayer`
- `miplayer` 是一次性播放器（非守护进程），杀掉后不影响后续 TTS 调用
- busybox 的 `pkill` 无法匹配到 `miplayer`，必须用 `killall`

### 录音通道

| 操作 | 命令 | 说明 |
|------|------|------|
| 停止录音 | `open_xiaoai_server.stop_recording()` | 杀掉设备端 `arecord` 进程，麦克风静音 |
| 恢复录音 | `open_xiaoai_server.start_recording()` | 重启 `arecord`，音频数据恢复流入 `GlobalStream` |

**注意**：回采不在线时，对话中 TTS 播放会 `stop_recording` 防止回声（回采在线时不关麦）。如果在此期间触发中断（"小爱同学"），必须在中断处理中调用 `start_recording` 恢复录音，否则 KWS 将因无音频数据而永久失效。

### on_interrupt 中断处理要点

`on_interrupt()` 触发时（用户喊"小爱同学"），需要完成以下步骤：
1. Cancel OpenClaw asyncio task
2. 让 OpenClaw controller 自己停止当前 TTS（使用自己持有的 `playback_token`）
3. `SpeakerManager.stop_device_audio()` — 停止阻塞 TTS / 非阻塞 TTS / PCM，并重置 PCM 通道
4. `start_recording` — 恢复录音（KWS 依赖此通道）
5. `XiaoAI.stop_conversation()` — 停止连续对话

### 不可用的中断方式

以下方式在实践中验证**不可靠或有副作用**：
- `abort_xiaoai()`（重启 `mico_aivs_lab`）— 会导致小爱整体不可用，恢复需 1-2 秒
- `pkill miplayer` — busybox `pkill` 无法匹配 `miplayer` 进程名
- `ubus call mediaplayer player_play_operation '{"action":"pause"}'` — 对 `mibrain text_to_speech` 触发的播放无效

### 相关讨论

- [open-xiaoai#36](https://github.com/idootop/open-xiaoai/issues/36) — 小爱 TTS 打断方案讨论

## 参考资源

- 项目主页: https://github.com/coderzc/open-xiaoai-bridge
- 刷机教程: https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md
- Client 端补丁: https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
