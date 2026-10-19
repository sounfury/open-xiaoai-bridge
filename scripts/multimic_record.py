"""从音箱录原始 4 声道（3 个麦克风 + 播放回采），存成 16 kHz S16 的 4 声道 wav，给 multimic_eval.py 用。

    .venv/Scripts/python.exe scripts/multimic_record.py far_5m.wav --seconds 120

在音箱上直接跑 arecord（Capture 是 dsnoop，和 bridge 的录音、原生小爱共享，不影响它们），
由 ALSA plug 转成 16 kHz S16（数值 ≈ 原始 S32 >> 16），经 ssh 的 stdout 传回来。
登录方式：SSH 密钥，或 %USERPROFILE%\\.xiaoai\\askpass-win.cmd（和 run-speaker-client.cmd 相同）。
"""

import argparse
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

SR = 16000
CHANNELS = 4


def ssh_command(host: str, remote: str) -> tuple[list[str], dict]:
    env = dict(os.environ)
    askpass = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".xiaoai" / "askpass-win.cmd"
    if askpass.exists():
        env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="1")
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null" if os.name != "nt" else "UserKnownHostsFile=NUL",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
        host,
        remote,
    ]
    return cmd, env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", help="输出 wav")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--host", default="root@192.168.1.73")
    parser.add_argument("--pcm", default="Capture")
    args = parser.parse_args()

    remote = (
        f"arecord --quiet -t raw -D {args.pcm} -f S16_LE -r {SR} -c {CHANNELS} -d {args.seconds}"
    )
    cmd, env = ssh_command(args.host, remote)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    frame_bytes = 2 * CHANNELS
    received = [0]

    def progress():
        while proc.poll() is None:
            time.sleep(5)
            print(f"  已录 {received[0] / frame_bytes / SR:.0f}s / {args.seconds}s", file=sys.stderr, flush=True)

    threading.Thread(target=progress, daemon=True).start()
    with wave.open(args.out, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        pending = b""
        while True:
            chunk = proc.stdout.read(SR * frame_bytes // 10)
            if not chunk:
                break
            pending += chunk
            usable = len(pending) // frame_bytes * frame_bytes
            wav.writeframes(pending[:usable])
            received[0] += usable
            pending = pending[usable:]
    proc.wait()
    print(f"已保存 {args.out}：{received[0] / frame_bytes / SR:.1f}s，4 声道 16 kHz", file=sys.stderr)
    return 0 if received[0] else 1


if __name__ == "__main__":
    sys.exit(main())
