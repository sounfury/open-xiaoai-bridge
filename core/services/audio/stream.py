import uuid
from typing import Any, Callable, ClassVar, Optional


# 全局流保留最近这么多秒的输入，用于打断后把唤醒词之后已经说出的话补给 VAD
HISTORY_SECONDS = 3
_BYTES_PER_SAMPLE = 2  # 16 kHz 单声道 int16


class __GlobalStream:
    def __init__(self):
        self.readers = {}
        self.on_output_data = None
        # 累计输入的采样数（16 kHz），用作绝对位置
        self.samples = 0
        self._history = bytearray()
        self._history_limit = HISTORY_SECONDS * 16000 * _BYTES_PER_SAMPLE

    def register_reader(self, reader):
        if reader.id not in self.readers:
            self.readers[reader.id] = reader

    def unregister_reader(self, reader) -> None:
        if reader.id in self.readers:
            del self.readers[reader.id]

    def input(self, data: bytes) -> None:
        self._history.extend(data)
        overflow = len(self._history) - self._history_limit
        if overflow > 0:
            del self._history[:overflow]
        self.samples += len(data) // _BYTES_PER_SAMPLE
        for key in list(self.readers):
            reader = self.readers.get(key)
            if reader is not None:
                reader.input(data)

    def since(self, sample: int) -> bytes:
        """从绝对位置 sample 到现在的输入；超出保留范围的部分丢弃。"""
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

    def clear_input(self) -> None:
        """Drop all buffered input and reset the read cursor.

        Both must happen together: clearing input_bytes without resetting
        _read_offset leaves the cursor pointing past the (now shorter) buffer,
        which makes read() skip freshly-arrived audio until the buffer refills
        past the stale offset — corrupting every capture after the first.
        """
        self.input_bytes.clear()
        self._read_offset = 0

    def start_stream(self) -> None:
        if not self._is_active:
            self._is_active = True
            if self._is_input:
                GlobalStream.register_reader(self)

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
