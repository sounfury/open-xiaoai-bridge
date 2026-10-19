"""一轮对话的耗时打点：从唤醒词命中（或开始听）到把文字发给后端。

KWS 线程、VAD 线程和主循环都会打点，所以加锁。发给后端前 finish() 汇总成一行，
由对话控制器打 INFO 日志。没有进行中的计时时 mark() 直接忽略。
"""

import threading
import time

SAMPLE_RATE = 16000


class _TurnTimer:
    def __init__(self):
        self._lock = threading.Lock()
        self._marks: list[tuple[str, float, str]] = []
        # 最后一帧人声读完时的 GlobalStream 绝对采样位置，用来算"说完到发出"
        self.speech_end_pos: int | None = None

    def begin(self, name: str, note: str = ""):
        """从这里重新开始计时（唤醒词 / 打断命中）。"""
        with self._lock:
            self._marks = [(name, time.monotonic(), note)]
            self.speech_end_pos = None

    def ensure(self, name: str, note: str = ""):
        """没有进行中的计时就开始一段，否则只打点（每轮开始听时调用）。"""
        with self._lock:
            if not self._marks:
                self.speech_end_pos = None
            self._marks.append((name, time.monotonic(), note))

    def mark(self, name: str, note: str = ""):
        with self._lock:
            if self._marks:
                self._marks.append((name, time.monotonic(), note))

    def clear(self):
        with self._lock:
            self._marks = []
            self.speech_end_pos = None

    def finish(self, name: str, now_samples: int | None = None) -> str | None:
        """结束这一轮计时，返回汇总文字；没有进行中的计时返回 None。"""
        with self._lock:
            if not self._marks:
                return None
            marks = self._marks + [(name, time.monotonic(), "")]
            end_pos = self.speech_end_pos
            self._marks = []
            self.speech_end_pos = None

        parts = []
        prev = marks[0][1]
        for i, (step, at, note) in enumerate(marks):
            text = step if i == 0 else f"+{(at - prev) * 1000:.0f} {step}"
            if note:
                text += f"({note})"
            parts.append(text)
            prev = at
        summary = " → ".join(parts) + f" ｜ 共 {(marks[-1][1] - marks[0][1]) * 1000:.0f} ms"
        if end_pos is not None and now_samples is not None:
            summary += f"，说完到发出 {(now_samples - end_pos) * 1000 / SAMPLE_RATE:.0f} ms"
        return summary


def lag_ms(behind_samples: int) -> int:
    """处理位置落后于已收到音频多少 ms。"""
    return int(max(0, behind_samples) * 1000 / SAMPLE_RATE)


TurnTimer = _TurnTimer()
