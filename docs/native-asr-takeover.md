# 原生 ASR 接管结论

设备：OH2P，`root@192.168.1.73`。模式：`openai.input_mode = "xiaoai_asr"`。缪斯唤醒仍用本地 KWS，小爱只负责识别。

## 现象

1. 第一次唤醒能识别，之后再唤醒，应答语说完灯就灭，约 20 秒后说“再见”。
2. 识别恢复后，说“播放华语 / 二次元歌单”时，聆听灯先随音量变化，随后突然静止；小爱自己说“没有找到歌单”，过一会儿缪斯又播放。

## 原因

固件连续对话打开时（`/data/mipns/dialog_continuous` 为 `on`），`Dialog.Finish` 被忽略，识别状态停在 `transmitend`。这时 `pnshelper event_notify {"src":1,"event":0}` 仍返回成功，但固件立刻关掉音频流，日志是 `local stream inflowed when transmitend, stop it`。聆听灯 41 也会马上关掉。

`src:3,event:7/8` 只是禁用或启用唤醒，不能退出 `transmitend`。`src:3,event:6` 只结束当前采集，单独使用也不能保证回到 `idle`。

小爱和缪斯抢答是另一件事。最终识别结果到达后约 150 毫秒，云端仍下发 `SpeechSynthesizer.Speak`，由 `mediaplayer` 播放“没有找到歌单”。原来的抑制只执行 `killall miplayer` 和 `mphelper pause`，而且 350 毫秒内的后续指令被跳过。第一次停播发生在语音文件还不存在的时候，随后的 `Speak` 就能播完。

识别结束后灯变静止，是 bridge 切到思考灯，不是聆听灯坏了。说话过程中灯随音量变化，由原生识别自己的灯效负责。

## 现在的做法

- 接管期间用 `src:3,event:18` 临时关闭固件连续对话，退出时恢复原来的设置。备份在音箱 `/tmp/open-xiaoai-bridge-native-asr.previous`。
- 每轮仍用 `event:6` 结束上一轮采集，再静默开麦。开麦后等 350 毫秒，用识别事件和聆听灯 41 确认真正开始。没开始时只重试一次：`mibrain text_to_speech save=1` 只合成不播放，借完整的 `Dialog.Finish` 清掉旧状态，并删除生成的临时文件。
- 进入原生聆听前先结束 bridge 的播报灯刷新，避免盖住原生聆听灯。bridge 不另开音量 FIFO。
- 收到 `SpeechSynthesizer` 或 `AudioPlayer` 时不再受 350 毫秒间隔限制，立刻执行 `mediaplayer player_play_operation action=stop`，并删除这一轮的 `/tmp/tts/tts_<dialog_id>_*.mp3`。约 0.45 秒后再停一次，赶上晚到的 `Speak`。

## 验收

连续唤醒几次，确认都能继续听。播放歌单时，小爱不应再插话“没有找到”。说话时聆听灯应随音量变化；识别结束后变成静止的思考灯是预期行为。
