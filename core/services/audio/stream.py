import threading
import uuid
from typing import Any, Callable, ClassVar, Optional


# 全局流保留最近这么多秒的输入，用于打断后把唤醒词之后已经说出的话补给 VAD
HISTORY_SECONDS = 6
_BYTES_PER_SAMPLE = 2  # 16 kHz 单声道 int16


class __GlobalStream:
    def __init__(self):
        self.readers = {}
        self.on_output_data = None
        # 累计输入的采样数（16 kHz），用作绝对位置
        self.samples = 0
        self._history = bytearray()
        self._history_limit = HISTORY_SECONDS * 16000 * _BYTES_PER_SAMPLE
        # input() 在原生回调线程里跑；回放（since + 注册）要和它互斥，否则会漏掉或重复一包
        self._lock = threading.Lock()

    def register_reader(self, reader, since: int | None = None):
        """注册读者；带 since（绝对采样位置）时，先把从那里到现在的历史放进读者的缓冲区。"""
        with self._lock:
            if since is not None:
                history = self._since_locked(since)
                if history:
                    reader.input(history)
            if reader.id not in self.readers:
                self.readers[reader.id] = reader

    def unregister_reader(self, reader) -> None:
        if reader.id in self.readers:
            del self.readers[reader.id]

    def input(self, data: bytes) -> None:
        with self._lock:
            self._history.extend(data)
            overflow = len(self._history) - self._history_limit
            if overflow > 0:
                del self._history[:overflow]
            self.samples += len(data) // _BYTES_PER_SAMPLE
            readers = list(self.readers.values())
            for reader in readers:
                reader.input(data)

    def since(self, sample: int) -> bytes:
        """从绝对位置 sample 到现在的输入；超出保留范围的部分丢弃。"""
        with self._lock:
            return self._since_locked(sample)

    def between(self, start: int, end: int) -> bytes:
        """绝对位置 [start, end) 之间的输入（只取还在保留范围内的部分）。"""
        with self._lock:
            data = self._since_locked(start)
            begin = self.samples - len(data) // _BYTES_PER_SAMPLE  # data 实际起点
            keep = max(0, int(end) - begin)
            return data[: keep * _BYTES_PER_SAMPLE]

    def _since_locked(self, sample: int) -> bytes:
        count = max(0, self.samples - max(0, int(sample)))
        size = min(count * _BYTES_PER_SAMPLE, len(self._history))
        return bytes(self._history[len(self._history) - size :]) if size else b""

    def output(self, frames: bytes) -> None:
        if self.on_output_data:
            self.on_output_data(frames)


GlobalStream = __GlobalStream()


class MyStream:
    def __init__(
        self,
        rate: int,
        channels: int,
        format: int,
        input: bool = False,
        output: bool = False,
        frames_per_buffer: int = 1024,
        start: bool = True,
    ) -> None:
        self.id = uuid.uuid4()
        self._rate = rate
        self._channels = channels
        self._format = format
        self._frames_per_buffer = frames_per_buffer
        self._is_input = input
        self._is_output = output
        self._is_active = False

        self.input_bytes = bytearray()
        self._read_offset = 0

        if start:
            self.start_stream()

    def close(self) -> None:
        self.stop_stream()

    def is_active(self) -> bool:
        return self._is_active

    def unread_samples(self) -> int:
        """已收到但还没被 read() 取走的采样数。"""
        return max(0, len(self.input_bytes) - self._read_offset) // 2

    def position(self) -> int:
        """下一个要读的采样在 GlobalStream 里的绝对位置（只对已注册的输入流有意义）。"""
        with GlobalStream._lock:
            return GlobalStream.samples - self.unread_samples()

    def clear_input(self) -> None:
        """Drop all buffered input and reset the read cursor.

        Both must happen together: clearing input_bytes without resetting
        _read_offset leaves the cursor pointing past the (now shorter) buffer,
        which makes read() skip freshly-arrived audio until the buffer refills
        past the stale offset — corrupting every capture after the first.
        """
        self.input_bytes.clear()
        self._read_offset = 0

    def trim_input(self, max_samples: int, keep_samples: int) -> int:
        """积压超过上限时只保留最近的音频，返回丢弃的采样数。

        与原生输入回调互斥，游标和缓冲一起更新，供实时检测跳过过时音频。
        """
        with GlobalStream._lock:
            unread = self.unread_samples()
            if unread <= max_samples:
                return 0
            keep = max(0, min(keep_samples, unread))
            self._read_offset = len(self.input_bytes) - keep * _BYTES_PER_SAMPLE
            return unread - keep

    def start_stream(self, since: int | None = None) -> None:
        """开始接收输入；since（绝对采样位置）表示先回放从那里到现在的历史。"""
        if not self._is_active:
            self._is_active = True
            if self._is_input:
                GlobalStream.register_reader(self, since)

    def stop_stream(self) -> None:
        if self._is_active:
            self._is_active = False
            if self._is_input:
                GlobalStream.unregister_reader(self)
                self.clear_input()

    def write(self, frames: bytes) -> None:
        # 发送输出音频流到扬声器
        if not self._is_output or not self._is_active:
            return
        GlobalStream.output(frames)

    def input(self, data: bytes):
        # 收到麦克风输入音频流
        if not self._is_input or not self._is_active:
            return

        if len(data) > 0:
            self.input_bytes.extend(data)

    def read(self, num_frames=None, exception_on_overflow=False) -> bytes:
        if num_frames is None:
            data = bytes(self.input_bytes)
            self.input_bytes.clear()
            self._read_offset = 0
            return data

        bytes_needed = num_frames * 2
        if (
            not self._is_input
            or not self._is_active
            # 达不到预期长度时，返回空字节，等待下一次读取
            or len(self.input_bytes) - self._read_offset < bytes_needed
        ):
            return bytes([])

        # 偏移读：只取需要的部分，不删除剩余数据
        data = bytes(self.input_bytes[self._read_offset:self._read_offset + bytes_needed])
        self._read_offset += bytes_needed

        # 延迟回收：累积偏移超过阈值才批量清理已读数据
        if self._read_offset > 262144:
            del self.input_bytes[:self._read_offset]
            self._read_offset = 0

        return data


class MyAudio:
    """PyAudio 替代品，用于创建和管理音频流"""

    Stream: ClassVar[type] = MyStream

    @classmethod
    def create(cls):
        # 使用小爱音箱音频（通过 Rust 补丁）
        return MyAudio()

    @classmethod
    def get_input_device_index(cls, audio):
        return 0

    @classmethod
    def get_output_device_index(cls, audio):
        return 0

    def __init__(self) -> None:
        self._is_terminated = False

    def open(
        self,
        rate: int,
        channels: int,
        format: int,
        input: bool = False,
        output: bool = False,
        input_device_index: Optional[int] = None,
        output_device_index: Optional[int] = None,
        frames_per_buffer: int = 1024,
        start: bool = True,
        input_host_api_specific_stream_info: Optional[Any] = None,
        output_host_api_specific_stream_info: Optional[Any] = None,
        stream_callback: Optional[Callable] = None,
    ) -> MyStream:
        if self._is_terminated:
            raise RuntimeError("MyAudio instance has been terminated")

        return MyStream(
            rate=rate,
            channels=channels,
            format=format,
            input=input,
            output=output,
            frames_per_buffer=frames_per_buffer,
            start=start,
        )

    def terminate(self) -> None:
        if not self._is_terminated:
            self._is_terminated = True
