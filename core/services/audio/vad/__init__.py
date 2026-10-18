import threading
import time

from core.ref import get_xiaoai, set_vad
from core.services.audio.stream import GlobalStream, MyAudio
from core.services.audio.vad.silero import Silero
from core.services.protocols.typing import AudioConfig
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.wakeup_session import EventManager


class _VAD:
    def __init__(self):
        set_vad(self)
        self.config_manager = ConfigManager.instance()

        # 参数设置
        self.sample_rate = 16000
        self.frame_size = 512
        self.threshold = 0.01
        self.min_speech_duration = 250
        self.min_silence_duration = 500
        # 调用方临时指定的"说完"静音时长（ms）；None = 用 min_silence_duration。
        # 连续对话用它设自己的断句时长，不影响小智流程
        self.silence_override_ms: int | None = None
        # 刚播放过声音时（回声消除后仍有残留），说话判定用这个更高的阈值；None = 不提高
        self.echo_threshold = None

        # 状态变量
        self.paused = True
        self.thread = None
        self.speech_count = 0
        self.silence_count = 0

        self.audio = None
        self.stream = None

        # 暂存的语音片段
        self.silence_frames = []  # 静音片段
        self.speech_frames = []  # 语音片段
        self.target = None  # 检测目标 speech/silence
        # 判定开口时读到的位置：接着等说完（resume("silence")）时从这里继续，
        # 回放历史时缓冲区里积压的后半句不会被清掉
        self._speech_end: int | None = None

        self.apply_runtime_config()
        self.config_manager.add_reload_listener(self._on_config_reload)

    def apply_runtime_config(self):
        """同步最新 VAD 配置。"""
        config = self.config_manager.get_app_config("vad", {})
        self.threshold = config.get("threshold", 0.01)
        self.min_speech_duration = config.get("min_speech_duration", 250)
        self.min_silence_duration = config.get("min_silence_duration", 500)
        echo = self.config_manager.get_app_config("barge_in.echo_vad_threshold", None)
        self.echo_threshold = None if echo is None else float(echo)

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def _reset_state(self):
        """重置状态"""
        self.speech_count = 0
        self.silence_count = 0
        self.speech_frames = []
        self.silence_frames = []

    def start(self):
        """启动VAD检测器"""
        logger.vad_event("语音活动检测服务启动", f"最小语音时长={self.min_speech_duration}ms, 最小静音时长={self.min_silence_duration}ms, 阈值={self.threshold}")
        
        self._initialize_audio_stream()

        # 启动检测线程
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()

    def pause(self):
        """暂停VAD检测"""
        self.paused = True
        self._reset_state()
        self.stream.stop_stream()

    def resume(
        self,
        target: str,
        preroll_from: int | None = None,
        replay_from: int | None = None,
    ):
        """恢复VAD检测

        replay_from: GlobalStream 的绝对采样位置。从这里到现在已经收到的音频重新过一遍检测
            （允许抢话：唤醒词刚说完、播报刚结束就开口，已经说出的部分不丢）。
        preroll_from: 只在 target="speech" 时有效。[preroll_from, replay_from 或现在) 之间的音频
            不做检测，作为"开口前的静音片段"放在语音开头（补上唤醒词末尾一点，识别更稳）。
        """
        speech_end, self._speech_end = self._speech_end, None
        if target == "speech":
            # 新的一轮监听从干净的 Silero 状态开始；"silence" 是同一句话的后半段，状态保留。
            # （OwnDing 实测：状态一直不清，运行几小时后清楚的一句话也会被判成静音。）
            Silero.reset()
        elif (
            replay_from is None
            and speech_end is not None
            and GlobalStream.samples - speech_end < 2 * self.sample_rate
        ):
            # 同一句话的后半段：从判定开口时读到的位置接着读，不丢积压的音频
            replay_from = speech_end
        self.paused = False
        self.target = target
        self._reset_state()
        self.stream.stop_stream()  # 清掉旧缓冲（stop 会清空输入并重置读指针）
        if preroll_from is not None and target == "speech":
            end = replay_from if replay_from is not None else GlobalStream.samples
            preroll = GlobalStream.between(preroll_from, end)
            self.silence_frames = list(preroll[-2 * self.sample_rate :])
        self.stream.start_stream(since=replay_from)

    def _speech_threshold(self, position: int | None = None) -> float:
        """回声残留期间提高说话判定阈值；position 是这一帧在 GlobalStream 里的起点。"""
        if self.echo_threshold is None:
            return self.threshold
        xiaoai = get_xiaoai()
        echo_active_at = getattr(xiaoai, "echo_active_at", None)
        if position is not None and echo_active_at:
            active = echo_active_at(position)
        else:
            echo_active = getattr(xiaoai, "echo_active", None)
            active = bool(echo_active and echo_active())
        return max(self.threshold, self.echo_threshold) if active else self.threshold

    def _handle_speech_frame(self, frames):
        """处理语音帧"""
        # speech_count/silence_count are measured in samples (int16 => 2 bytes
        # per sample) so they can be compared against duration_ms * rate / 1000.
        self.speech_count += len(frames) // 2
        self.silence_count = 0

        if self.target == "speech":
            if not self.speech_frames:
                # 加入静音片段（潜在的语音片段）
                self.speech_frames.extend(self.silence_frames)

        # 加入语音片段
        self.speech_frames.extend(frames)

        speech_bytes = bytes(self.speech_frames)

        if (
            self.target == "speech"
            and self.speech_count > self.min_speech_duration * self.sample_rate / 1000
        ):
            self._speech_end = self.stream.position()
            self.pause()
            EventManager.on_speech(speech_bytes)

    def _handle_silence_frame(self, frames):
        """处理静音帧"""
        # Count in samples, not bytes (int16 => 2 bytes/sample), to match the
        # min_silence_duration * rate / 1000 threshold below.
        self.silence_count += len(frames) // 2
        self.speech_count = 0

        if self.target == "speech":
            if not self.speech_frames:
                # 如果之前没有语音片段，则将当前帧加入静音片段
                self.silence_frames.extend(frames)
                # 确保静音片段长度不超过 1s
                self.silence_frames = self.silence_frames[
                    -1 * 1 * 2 * self.sample_rate :
                ]
            else:
                # 如果之前有语音片段，则将当前帧加入语音片段
                self.speech_frames.extend(frames)

        silence_ms = self.silence_override_ms or self.min_silence_duration
        if (
            self.target == "silence"
            and self.silence_count > silence_ms * self.sample_rate / 1000
        ):
            self.pause()
            EventManager.on_silence()

    def _initialize_audio_stream(self):
        """初始化独立的音频流"""
        try:
            # 创建 PyAudio 实例
            self.audio = MyAudio.create()
            # 创建输入流
            self.stream = self.audio.open(
                format=AudioConfig.FORMAT,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_size,
                start=True,
            )
            return True
        except Exception:
            return False

    def _close_audio_stream(self):
        """关闭音频流"""
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if self.audio:
                self.audio.terminate()
                self.audio = None

        except Exception:
            pass

    def _detection_loop(self):
        """VAD检测主循环"""
        while True:
            # 如果暂停或者音频流未初始化，则跳过
            if self.paused or not self.stream:
                time.sleep(0.1)
                continue

            # 读取缓冲区音频数据
            frames = self.stream.read(self.frame_size)
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            # 检测是否是语音（回放历史时按这一帧原来的位置判断是否还在回声残留期）
            position = self.stream.position() - self.frame_size
            speech_prob = Silero.vad(frames, self.sample_rate) or 0
            is_speech = speech_prob >= self._speech_threshold(position)
            if is_speech:
                self._handle_speech_frame(frames)
            else:
                self._handle_silence_frame(frames)

            # 回放历史时缓冲区里还有积压，先追上再休息
            if self.stream.unread_samples() < self.frame_size:
                time.sleep(0.01)

VAD = _VAD()
