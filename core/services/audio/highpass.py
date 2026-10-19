"""流式高通滤波：去掉 cutoff 以下的低频（空调 / 冰箱嗡声、桌面震动、脚步、音乐低音鼓）。

人声区分字音的信息主要在 300～4000 Hz，男声基频最低约 85 Hz，所以 100～150 Hz 以下去掉对识别没有影响。
原生小爱前端第一步就是高通（flexkws.json5 的 hpf 节点）；OwnDing 实测音箱底噪大部分在 150 Hz 以下。
2 阶 Butterworth（二阶节），跨包保留状态，每声道独立；CPU 可以忽略。
"""

import numpy as np
from scipy.signal import butter, sosfilt

SAMPLE_RATE = 16000


class HighPass:
    def __init__(self, cutoff_hz: float, channels: int = 1, order: int = 2, sample_rate: int = SAMPLE_RATE):
        self.cutoff_hz = float(cutoff_hz)
        self.channels = channels
        self.sos = butter(order, self.cutoff_hz, btype="highpass", fs=sample_rate, output="sos")
        # 从 0 开始（不按第一个采样预置），避免开头的直流偏置造成一下冲击
        self.zi = np.zeros((self.sos.shape[0], 2, channels)) if channels > 1 else np.zeros((self.sos.shape[0], 2))

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (n,) 或 (n, channels) 的 float；返回同形状 float64。"""
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return x
        y, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y

    def reset(self):
        self.zi[:] = 0


def make_highpass(cutoff_hz, channels: int = 1) -> HighPass | None:
    """cutoff_hz 为空 / 0 / 非法时返回 None（不滤波）。"""
    try:
        cutoff = float(cutoff_hz or 0)
    except (TypeError, ValueError):
        return None
    if cutoff <= 0 or cutoff >= SAMPLE_RATE / 2:
        return None
    return HighPass(cutoff, channels=channels)
