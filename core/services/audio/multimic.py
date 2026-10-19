"""多麦克风前端：把音箱的 3 路麦克风变成几路更干净的单声道，再分别送进唤醒检测。

音箱 `hw:0,3`（共享名 Capture）是 4 声道：ch0～ch2 三个麦克风，ch3 播放回采。
原生小爱（mipns-xiaomi，flexkws.json5）的做法：回声消除 → 去混响 → IVA 盲源分离成 3 路 →
每路各跑一个唤醒模型 → 挑分数最高的一路。这里实现同样思路里不依赖麦克风几何位置的部分：

- `OnlineAuxIVA`：在线辅助函数 IVA（Taniguchi 2014），逐帧更新分离矩阵，输出 3 路，
  每路按投影回参考麦克风的方式恢复电平，便于直接送 KWS / ASR。
- `ChannelSum`：3 路直接相加（零延迟波束，正对方向的声音同相叠加）。

所有类都是流式的：`process(block)` 输入 (n, M) int16/float，输出 (n', K) float，
内部按 STFT 帧对齐，输出比输入晚一帧（hop 个采样）。
"""

import numpy as np

SAMPLE_RATE = 16000


class StreamingSTFT:
    """sqrt-hann 窗、50% 重叠的流式 STFT / ISTFT（完全重构）。"""

    def __init__(self, channels: int, nfft: int = 512):
        self.nfft = nfft
        self.hop = nfft // 2
        self.window = np.sqrt(np.hanning(nfft + 1)[:-1]).astype(np.float64)
        self.channels = channels
        self.in_buf = np.zeros((0, channels))
        self.last = np.zeros((self.hop, channels))  # 上一帧的后半（作为下一帧的前半）
        self.out_tail = None  # 输出重叠相加的尾巴 (hop, K)

    def frames(self, block: np.ndarray):
        """喂入 (n, M) 采样，产出若干帧的频谱 (F, M)。"""
        self.in_buf = np.concatenate([self.in_buf, np.asarray(block, dtype=np.float64)])
        while len(self.in_buf) >= self.hop:
            new = self.in_buf[: self.hop]
            self.in_buf = self.in_buf[self.hop :]
            frame = np.concatenate([self.last, new]) * self.window[:, None]
            self.last = new
            yield np.fft.rfft(frame, axis=0)

    def synth(self, spec: np.ndarray) -> np.ndarray:
        """一帧频谱 (F, K) → hop 个输出采样 (hop, K)。"""
        frame = np.fft.irfft(spec, n=self.nfft, axis=0) * self.window[:, None]
        if self.out_tail is None:
            self.out_tail = np.zeros((self.hop, spec.shape[1]))
        out = self.out_tail + frame[: self.hop]
        self.out_tail = frame[self.hop :]
        return out


class OnlineAuxIVA:
    """在线 AuxIVA：M 路麦克风 → M 路分离结果（投影回 ref 麦克风的电平）。

    alpha：加权协方差的遗忘因子（每帧 16 ms，0.97 ≈ 0.5 s 时间常数）。
    分离结果的顺序不固定，唤醒检测应该每一路都跑。
    """

    def __init__(self, channels: int = 3, nfft: int = 512, alpha: float = 0.97, ref: int = 0, iters: int = 1):
        self.stft = StreamingSTFT(channels, nfft)
        self.M = channels
        self.F = nfft // 2 + 1
        self.alpha = float(alpha)
        self.ref = ref
        self.iters = max(1, int(iters))
        eye = np.eye(channels, dtype=np.complex128)
        self.W = np.tile(eye, (self.F, 1, 1))  # (F, K, M)
        self.V = np.tile(eye * 1e-3, (channels, self.F, 1, 1))  # (K, F, M, M)
        self.frame_count = 0

    def _update(self, X: np.ndarray) -> np.ndarray:
        """X: (F, M) → 投影回参考麦克风后的 Y: (F, K)。"""
        M = self.M
        XX = X[:, :, None] * X[:, None, :].conj()  # (F, M, M)
        Y = np.einsum("fkm,fm->fk", self.W, X)
        for _ in range(self.iters):
            r = np.sqrt(np.sum(np.abs(Y) ** 2, axis=0)) + 1e-6  # (K,)
            phi = 1.0 / r  # 拉普拉斯先验
            eye = np.eye(M, dtype=np.complex128)
            for k in range(M):
                self.V[k] = self.alpha * self.V[k] + (1 - self.alpha) * phi[k] * XX
                WV = self.W @ self.V[k]  # (F, K, M)
                # 加一点对角载荷，避免安静时矩阵奇异
                WV = WV + 1e-9 * eye
                w = np.linalg.solve(WV, np.broadcast_to(eye[:, k], (self.F, M))[..., None])[..., 0]  # (F, M)
                norm = np.sqrt(np.real(np.einsum("fm,fmn,fn->f", w.conj(), self.V[k], w))) + 1e-12
                self.W[:, k, :] = (w / norm[:, None]).conj()
            Y = np.einsum("fkm,fm->fk", self.W, X)
        # 投影回参考麦克风：Y_k * A[ref, k]，A = W^-1
        try:
            A = np.linalg.inv(self.W + 1e-9 * np.eye(M))
        except np.linalg.LinAlgError:
            A = np.tile(np.eye(M), (self.F, 1, 1))
        return Y * A[:, self.ref, :]

    def process(self, block: np.ndarray) -> np.ndarray:
        outs = []
        for X in self.stft.frames(block):
            Y = self._update(X)
            self.frame_count += 1
            outs.append(self.stft.synth(Y))
        if not outs:
            return np.zeros((0, self.M))
        return np.concatenate(outs)


class ChannelSum:
    """3 路直接平均（零延迟波束）。"""

    def process(self, block: np.ndarray) -> np.ndarray:
        return np.asarray(block, dtype=np.float64).mean(axis=1, keepdims=True)


def to_int16(x: np.ndarray, gain: float = 1.0) -> np.ndarray:
    return np.clip(np.round(np.asarray(x) * gain), -32768, 32767).astype(np.int16)
