# Muse 唤醒故障诊断

诊断模块：`core/services/diagnostics.py`。随 bridge 启动，日志仍写入 `logs/bridge-autostart.log`，模块标识为 `[Diagnostics]`。

2026-10-24 首次加入诊断时只增加观测，保留原行为以复现故障。2026-10-26 已根据现场证据修复 VAD 首次启动无目标运行及每帧复制全部历史的问题；诊断继续保留。KWS 丢帧策略、看门狗、模型参数和超时设置未因该修复改变。没有录制额外音频，也不在诊断日志中写入识别文本或 shell 命令正文。

## 2026-10-26 已复现的问题

- 当天 19:27 自启后，VAD 一直 `paused=false, target=null`，首次成功 Muse 对话前累计人声音频持续增长。
- 20:00：历史 162.528 秒，`vad.handle_speech.avg_ms=30.79`；21:00：465.472 秒、88.34 ms；22:45：768.8 秒，容器 195.58 MiB、平均约 143 ms。每帧音频只有 32 ms，模型推理本身仍约 0.6 ms。
- 22:44:52 已命中唤醒，但停播 shell 在约 5.1 秒后被会话超时取消。
- 22:45:13 唤醒成功，`vad.reset.max_discarded_speech_s=768.8`。同一进程及同一 `capture_id` 中，22:46:25 的下一窗口输入回调平均耗时降到 3.66 ms，VAD 已暂停且历史为零；此次恢复并非重启服务造成。
- 现场片段保存在 `logs/muse-incident-20261026.log`，恢复后的人工线程栈在 `logs/muse-stack-20261026-2246.txt`；故障时自动线程栈在原日志及现场片段中。
- 修复：VAD 初始化后待命且不订阅音频；`resume()` 只接受明确目标；仅开口阶段保留预录，真正派发时才转换 bytes。等待停顿的完整音频继续由会话层 / codec 接收。

## 日志频率

- 正常状态每 60 秒输出一条 JSON 汇总。
- 发现异常时输出完整快照，同类异常最多每 15 秒一条；统计每 60 秒重置，所以异常快照与下一条周期汇总的计数可能重叠，不能直接相加。
- 主循环探针延迟、实时处理或停播调用超过 2 秒，或诊断线程自身调度间隔超过 2 秒时，最多每 60 秒记录一次 Python 线程栈（最多 8 层，不含局部变量和源代码正文）。
- 音频线程只更新计数与耗时；独立诊断线程负责序列化和写日志。每个事件循环最多有一个未完成探针，避免故障期间继续堆积任务。
- 通用 RPC 的未完成耗时仍记录，但长 TTS 播放本身不触发线程栈告警。

## 重点字段

| 字段 | 用途 |
| --- | --- |
| `uptime_s` / `window_s` | 诊断启动时长、本次统计窗口长度；用日志时间关联启动及首次唤醒 |
| `cpu_core_pct` | 本进程 CPU 时间 / 墙钟时间，100 表示占满一个逻辑核，可超过 100 |
| `vad.paused` / `target` | 是否暂停、是否有检测目标；`False` + `null` 会报 `vad_running_without_target` |
| `vad.speech_s` / `speech_storage_mb` | 累计人声音频秒数、保存该历史的容器内存（不是进程总内存） |
| `vad.queued_ms` / `kws.queued_ms` | 已送达但尚未处理的音频长度 |
| `vad.thread_alive` / `kws.thread_alive` | 检测线程是否仍存活 |
| `kws.speech_gate` | 主麦、多麦当前是否进入语音检测区间 |
| `kws.barge_in` / `hits` | 是否允许播报中打断、各路累计命中数 |
| `input.age_ms` / `max_gap_ms` | 进入 Python 音频回调之前的最近收包年龄、窗口内最大收包间隔 |
| `input.capture_id` / `seq` | 对齐音箱采集健康日志，并检查重连、换采集、跳号 |
| `input.relative_delay_ms` | 相对于同一采集内最低观测延迟的额外延迟估计；不是绝对网络延迟，也不能单独区分网络与回调调度；设备时钟跳变会影响它 |
| `delivered_age_ms` | 单声道输入处理入口时间距现在多久；`set_recording_wanted(True)` 也会刷新原有看门狗时间，优先结合 `input.age_ms` 判断 |
| `recording_wanted` | 会话层当前是否要求录音，避免把主动关麦当故障 |
| `multimic` | 接收年龄、连接次数、各路队列、多麦累计音频时长和 DSP 耗时 |
| `loops.main` / `loops.bridge` | 主业务循环、原生事件桥接循环的未应答探针年龄及窗口最大调度延迟；不代表 Rust Tokio 或网络 ping |
| `observer_gap_ms` | 诊断线程实际轮询间隔；正常约 1000 ms，明显增大提示 GIL、调度或主机休眠影响 |
| `active_ms` | 当前尚未返回的处理阶段；同名并发调用显示最长一个 |
| `timings` | 每阶段调用次数、平均/最大墙钟耗时、处理超过本批音频时长的次数、音频总时长；嵌套和并行阶段不能相加当 CPU 耗时 |
| `counts` | 主麦样本数、缺失包数、KWS 丢弃样本数/重置数/过时命中、多麦丢弃样本数等窗口计数 |
| `events` | 事件次数、最近一次的运行时刻及详情；`vad.reset.max_discarded_speech_s` 保留本窗口清空前的最大历史长度 |

主要计时名称：

- `input.packet`：含主麦 AEC、高通、分发的整个 Python 包回调。
- `input.aec` / `input.distribute`：主麦回声消除、全局流分发。
- `vad.inference` / `vad.handle_speech` / `vad.handle_silence` / `vad.frame`：模型、历史维护和回调、整帧。
- `kws.ch0` / `kws.iva0` 等：各路检测处理；`kws.wait_*` 是主检测线程等待多麦结果的时间。
- `multimic.dsp`：多麦回声消除、IVA 分离和输出缓冲；`multimic.restart_wait`：看门狗等待重启命令。
- `rpc.shell.*`：按有限命令类别统计的 RPC 往返；`stop_device.shell` / `stop_device.pcm` 分别定位停播的两步。

## 下次故障时

1. 记下大致时间和现象（完全没回应、很久后回应、应答后不听等）。若方便，先保留现场，不立刻重启；自动日志会继续记录。
2. 搜索故障前后的 `[Diagnostics]`、`[KWS]`、`[Wakeup]`、`[AppServer]`，从异常发生前一两分钟开始看。
3. 重点检验首次唤醒前 VAD 是否无目标运行、`speech_s` 是否增长、`vad.handle_speech.max_ms` 是否跟着增长；再对照首次对话的清理事件及恢复后的状态。
4. 如果 VAD 正常但 KWS 堆积，比较四路检测和等待耗时；如果输入回调慢，查看 AEC/分发与线程栈。
5. 收包间隔增大但电脑处理耗时低时，结合音箱 `/data/open-xiaoai/logs/audio-health.jsonl`（以及 `.1`、`.2` 轮转文件）的 `capture_id`、采集超时和发送队列丢包判断。两台设备时钟不完全同步，不直接比较绝对时间。
6. RPC 超时要同时看事件循环探针、`active_ms` 和停播两步；`/api/health` 只表示本地对象已初始化，`/api/status` 默认是缓存状态，不能证明远端链路健康。

诊断快照是跨线程近似值，不代表原子的业务状态。如果某个 C 扩展长期持有 GIL，Python 诊断线程也可能暂时无法运行；恢复后的 `observer_gap_ms` 能体现这段停顿，但恢复后抓到的栈未必仍是阻塞现场。遇到这种情况应在故障时额外用外部采样器 `py-spy dump --pid <实际运行 main.py 的 Python PID>` 抓栈。
