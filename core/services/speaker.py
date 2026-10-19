import asyncio
import os
import re
from contextlib import asynccontextmanager
from typing import Literal

import open_xiaoai_server

from core.ref import get_xiaoai, set_speaker
from core.utils.base import json_decode, json_encode
from core.utils.logger import logger


class CommandResult:
    def __init__(self, stdout: str, stderr: str, exit_code: int):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class SpeakerManager:
    status: Literal["playing", "paused", "idle"] = "idle"

    def __init__(self):
        set_speaker(self)
        # 原生 ASR 接管只在 MainApp.loop 使用，防止旧会话恢复设置覆盖新会话。
        self._native_asr_lock = asyncio.Lock()

    async def get_playing(self, sync=False):
        """获取播放状态"""
        if sync:
            # 同步远端最新状态
            res = await self.run_shell("mphelper mute_stat")
            if "1" in res.stdout:
                self.status = "playing"
            elif "2" in res.stdout:
                self.status = "paused"
        return self.status

    async def set_playing(self, playing=True):
        """播放/暂停"""
        command = "mphelper play" if playing else "mphelper pause"
        res = await self.run_shell(command)
        return '"code": 0' in res.stdout

    async def play(
        self,
        text=None,
        url=None,
        buffer=None,
        server_file=None,
        blocking=True,
        timeout=10 * 60 * 1000,
    ):
        """
        播放文字、音频链接、音频流

        参数:
            text: 文字内容
            url: 音频链接
            buffer: 音频流
            server_file: 服务端本地音频文件路径
            timeout: 超时时长（毫秒），默认10分钟
            blocking: 是否阻塞运行
        """
        if server_file is not None:
            return await self.play_server_file(
                file_path=server_file,
                blocking=blocking,
            )

        if buffer is not None:
            return get_xiaoai().on_output_data(buffer)

        if blocking:
            command = (
                f"miplayer -f '{url}'"
                if url
                else f"/usr/sbin/tts_play.sh '{text.replace("'", "'\\''") or '你好'}'"
            )
            res = await self.run_shell(command, timeout=timeout)
            return res.exit_code == 0

        if url:
            data = json_encode({"url": url, "type": 1})
            command = f"ubus call mediaplayer player_play_url '{data}'"
        else:
            data = json_encode({"text": text or "你好", "save": 0})
            command = f"ubus call mibrain text_to_speech '{data}'"

        res = await self.run_shell(command, timeout=timeout)
        return '"code": 0' in res.stdout if res else False

    async def play_server_file(
        self,
        file_path: str,
        blocking: bool = True,
        sample_rate: int = 24000,
    ) -> bool:
        """播放服务端本地音频文件（解码为 PCM 后推流到音箱）"""
        if not file_path:
            raise ValueError("file_path is required")

        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)

        logger.info(
            f"[Speaker] Playing local file via Rust audio pipeline: {file_path}, "
            f"sample_rate={sample_rate}"
        )

        if blocking:
            await open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
            return True

        asyncio.create_task(
            open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
        )
        return True

    async def stop_device_audio(self) -> None:
        """
        停止设备上的全部播放链路。
        aplay 不立即重启，由 Rust 侧 ensure_player_ready() 在首次
        发送音频数据时按需启动，避免空 buffer 导致 underrun。
        """
        await self.run_shell(
            "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"
        )
        await open_xiaoai_server.stop_playing()

    async def wake_up(self, awake=True, silent=True):
        """
        （取消）唤醒小爱

        参数:
            awake: 是否唤醒
            silent: 是否静默唤醒
        """

        if awake:
            if silent:
                command = 'ubus call pnshelper event_notify \'{"src":1,"event":0}\''
            else:
                command = 'ubus call pnshelper event_notify \'{"src":0,"event":0}\''
        else:
            # event 6 结束当前采集，但不能保证清除 transmitend。
            # 原生 ASR 接管还必须关闭固件连续对话，否则 Dialog.Finish 会被忽略。
            command = 'ubus -t 2 call pnshelper event_notify \'{"src":3,"event":6}\''
        res = await self.run_shell(command)
        data = json_decode(res.stdout) if res else None
        ok = bool(res and res.exit_code == 0 and isinstance(data, dict) and data.get("code") == 0)
        if not ok:
            logger.warning(
                f"原生{'唤醒' if awake else '停止会话'}失败: "
                f"exit={getattr(res, 'exit_code', None)}, stdout={getattr(res, 'stdout', '')[:200]}",
                module="Speaker",
            )
        return ok

    @asynccontextmanager
    async def native_asr_session(self):
        """接管期间禁用固件连续对话；退出时恢复原设置，异常退出的备份供下次沿用。"""
        async with self._native_asr_lock:
            saved = "/tmp/open-xiaoai-bridge-native-asr.previous"
            setting = "/data/mipns/dialog_continuous"
            try:
                result = await self.run_shell(
                    f"test -f {saved} || cp -p {setting} {saved}; "
                    f"test -f {saved} || exit 1; "
                    "ubus -t 2 call pnshelper event_notify '{\"src\":3,\"event\":18}' >/dev/null; "
                    f"cat {setting}",
                    timeout=3000,
                )
                if result is None or result.exit_code != 0 or result.stdout.strip() != "off":
                    raise RuntimeError("无法关闭固件连续对话，原生 ASR 接管未启动")
                logger.info("原生 ASR 接管：暂时关闭固件连续对话", module="Speaker")
                yield
            finally:
                # 使用原文件内容恢复，也兼容 combin_on/off；用户在会话中改变设置则保留。
                result = await self.run_shell(
                    f"if [ -f {saved} ]; then "
                    f'if [ "$(cat {setting})" = off ]; then cat {saved} > {setting} || exit 1; fi; '
                    f"rm -f {saved}; fi; cat {setting}",
                    timeout=3000,
                )
                if result is not None and result.exit_code == 0:
                    logger.info(f"原生 ASR 接管结束：固件连续对话={result.stdout.strip()}", module="Speaker")
                else:
                    logger.warning("恢复固件连续对话失败，音箱保留备份供下次恢复", module="Speaker")

    async def native_asr_is_listening(self) -> bool:
        """检查固件是否仍显示聆听灯；结合识别事件判断，不能只信开麦命令返回值。"""
        result = await self.run_shell("ubus -t 1 call led status", timeout=1500)
        return bool(result and result.exit_code == 0 and re.search(r"current id 41\b", result.stdout))

    async def recover_native_asr(self) -> bool:
        """排空遗留 transmitend：仅合成不播放，让固件收到一次完整 Dialog.Finish。"""
        # 只有原生连续对话已关闭时才可调用；不重启 mico_aivs_lab，不影响事件桥接。
        result = await self.run_shell(
            "ubus -t 5 call mibrain text_to_speech '{\"text\":\"嗯\",\"save\":1}'",
            timeout=6000,
        )
        data = json_decode(result.stdout) if result else None
        if not isinstance(data, dict) or data.get("code") != 0:
            return False
        info = json_decode(data.get("info", ""))
        path = info.get("path", "") if isinstance(info, dict) else ""
        # 只清理此次生成的固件 TTS 文件。
        if re.fullmatch(r"/tmp/tts/[\w.-]+\.mp3", path):
            await self.run_shell(f"rm -f '{path}'", timeout=1500)
        await asyncio.sleep(0.15)
        return True

    async def native_asr_status(self, recent: bool = False) -> str:
        """原生识别诊断：麦克风标记、唤醒标记、当前灯效及最近的状态转换。"""
        command = (
            "printf 'native_continuous='; cat /data/mipns/dialog_continuous; echo; "
            "if [ -f /tmp/mipns/mute ]; then echo native_mic=off; else echo native_mic=on; fi; "
            "if [ -f /tmp/mipns/player_wakeup ]; then echo player_wakeup=1; else echo player_wakeup=0; fi; "
            "ubus -t 1 call led status"
        )
        if recent:
            command += (
                "; tail -n 120 /tmp/log/messages | grep -E "
                "'local mic on|local stream inflowed|transmitend|asrstart|asrend|stop session' || true"
            )
        res = await self.run_shell(command, timeout=2000)
        if res is None or res.exit_code != 0:
            return f"状态读取失败(exit={getattr(res, 'exit_code', None)})"
        return " ".join(res.stdout.split())[:1800]

    async def ask_xiaoai(self, text: str, silent=False):
        """
        把文字指令交给原来的小爱执行

        参数:
            text: 文字指令
            silent: 是否静默执行
        """

        data = {"nlp": 1, "nlp_text": text}
        if not silent:
            data["tts"] = 1

        command = f"ubus call mibrain ai_service '{json_encode(data)}'"
        res = await self.run_shell(command)
        return '"code": 0' in res.stdout

    async def abort_xiaoai(self):
        """
        中断原来小爱的运行

        注意：重启需要大约 1-2s 的时间，在此期间无法使用小爱音箱自带的 TTS 服务
        """
        # Stop current audio playback first, then restart xiaoai voice service
        res = await self.run_shell("/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1")
        return res.exit_code == 0

    async def get_boot(self):
        """获取启动分区"""
        res = await self.run_shell("echo $(fw_env -g boot_part)")
        return res.stdout.strip()

    async def set_boot(self, boot_part: Literal["boot0", "boot1"]):
        """设置启动分区"""
        command = f"fw_env -s boot_part {boot_part} >/dev/null 2>&1 && echo $(fw_env -g boot_part)"
        res = await self.run_shell(command)
        return boot_part in res.stdout

    async def get_device(self):
        """获取设备型号、序列号信息"""
        res = await self.run_shell("echo $(micocfg_model) $(micocfg_sn)")
        info = res.stdout.strip().split(" ")
        return {
            "model": info[0] if len(info) > 0 else "unknown",
            "sn": info[1] if len(info) > 1 else "unknown",
        }

    async def get_mic(self):
        """获取麦克风状态"""
        res = await self.run_shell("[ ! -f /tmp/mipns/mute ] && echo on || echo off")
        status = "off"
        if "on" in res.stdout:
            status = "on"
        return status

    async def set_mic(self, on=True):
        """打开/关闭麦克风"""
        if on:
            command = (
                'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":7}\' 2>&1'
            )
        else:
            command = (
                'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":8}\' 2>&1'
            )
        res = await self.run_shell(command)
        return '"code":0' in res.stdout

    async def run_shell(self, script: str, timeout=10000):
        """
        执行脚本

        参数:
            script: 脚本内容
            timeout: 超时时间（毫秒）
        """
        res = "unknown"
        try:
            res = await get_xiaoai().run_shell(script, timeout=timeout)
            data = json_decode(res)
            if data:
                return CommandResult(
                    data.get("stdout", ""),
                    data.get("stderr", ""),
                    data.get("exit_code", 0),
                )
        except Exception:
            return CommandResult("error", res, -1)
