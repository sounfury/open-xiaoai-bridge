"""连续对话的灯效：会话只设置状态，后台任务按最新状态切灯，不阻塞语音流程。

状态和默认灯效（OH2P 固件 /bin/wakeup.sh 里的编号）：
    listening  9   等你说话（noangle）
    hearing    41  听到你在说（asrstart）
    thinking   2   等后端回复（think）
    speaking   3   播报中，可以喊唤醒词打断（speek）
    off        —   全灭

- 灯效命令走设备 RPC（每次约 100～200ms），全部在后台执行；状态切换太快时只执行最后一个。
- 一次命令里先关旧灯效再开新灯效；两个状态灯效相同时不发命令。
- 播报时设备播放器会提前熄灯，speaking 状态每 0.5 秒检查一次，被关掉了就重新打开。
- 随音量起伏（conversation.light_level）：level_states 里的状态让 vis 改读麦克风，并由
  core/services/light_level.py 把麦克风音量送过去；离开时切回播放音源（播报灯跟着 TTS 动）。
  听到你在说（41）和播报（3）的白光宽度都跟着音量变；等你说话（9，黄光）不受影响。
"""

import asyncio

from core.services.light_level import MIC_SOURCE_OFF, MIC_SOURCE_ON, MicLevelFeeder
from core.utils.logger import logger

LOG_MODULE = "Light"

DEFAULT_EFFECTS = {
    # 和原生小爱一样：等你说、你在说都是短白灯 41（有音量通道时随你的音量变宽）
    "listening": 41,
    "hearing": 41,
    "thinking": 2,
    "speaking": 3,
}
STATES = ("off", *DEFAULT_EFFECTS)
# 设备会自己熄掉的灯效，需要定时补亮
_REFRESH_STATES = frozenset({"speaking"})
REFRESH_INTERVAL_S = 0.5
_COMMAND_TIMEOUT_MS = 2000


def _valid_effect(value) -> int | None:
    return value if type(value) is int and 1 <= value <= 255 else None


def effects_from_config(light: dict | None, *, native_listening: bool = False) -> dict[str, int | None]:
    """把 <backend>.listening_light 配置转成 {状态: 灯效}；enabled=False 时全部为 None。

    native_listening：用小爱原生识别时，设备自己会亮聆听灯，listening / hearing 不再另外开灯。
    """
    if not isinstance(light, dict) or not light.get("enabled", False):
        return {state: None for state in DEFAULT_EFFECTS}
    keys = {
        "listening": "effect_id",
        "hearing": "hearing_effect_id",
        "thinking": "thinking_effect_id",
        "speaking": "speaking_effect_id",
    }
    effects = {
        state: _valid_effect(light.get(key, DEFAULT_EFFECTS[state]))
        for state, key in keys.items()
    }
    if native_listening:
        effects["listening"] = effects["hearing"] = None
    return effects


def _show(effect: int) -> str:
    return f"ubus -t 1 call led show '{{\"L\":{effect}}}'"


def _shut(effect: int) -> str:
    return f"ubus -t 1 call led shut '{{\"L\":{effect}}}'"


class ConversationLight:
    """一次连续对话持有的灯。只关闭自己开过的灯效，不影响设备其他灯。"""

    def __init__(
        self,
        speaker,
        effects: dict[str, int | None],
        loop: asyncio.AbstractEventLoop,
        level_feeder: MicLevelFeeder | None = None,
        level_states=("listening", "hearing"),
    ):
        self._speaker = speaker
        self._effects = dict(effects)
        self._loop = loop
        self._feeder = level_feeder
        self._level_states = frozenset(level_states or ())
        self._mic_on = False  # vis 当前读麦克风（我们切过去的）
        self._desired = "off"
        self._applied_effect: int | None = None
        self._changed = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._task: asyncio.Task | None = None

    @property
    def state(self) -> str:
        return self._desired

    def set(self, state: str):
        """切换状态，立即返回。可以在其他线程调用。"""
        if state not in STATES:
            raise ValueError(f"unknown light state: {state}")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._set(state)
        elif self._loop.is_running():
            self._loop.call_soon_threadsafe(self._set, state)

    async def settle(self, timeout: float = 2.0):
        """等当前灯效切换完成，之后可把灯的控制权交还原生 ASR。"""
        await asyncio.wait_for(self._wait_idle(), timeout)

    async def close(self, timeout: float = 3.0):
        """熄灯并结束后台任务（最多等 timeout 秒）。"""
        self._set("off")
        if self._task is not None:
            try:
                await asyncio.wait_for(self._wait_idle(), timeout)
            except asyncio.TimeoutError:
                logger.warning("熄灯超时", module=LOG_MODULE)
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ===== 私有方法 =====

    def _want_mic(self, state: str) -> bool:
        return self._feeder is not None and state in self._level_states

    def _settled(self) -> bool:
        return (
            self._effects.get(self._desired) == self._applied_effect
            and self._want_mic(self._desired) == self._mic_on
        )

    def _set(self, state: str):
        if self._closed:
            return
        self._desired = state
        if self._settled() and state not in _REFRESH_STATES:
            return
        self._idle.clear()
        self._changed.set()
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._worker())

    async def _wait_idle(self):
        while True:
            await self._idle.wait()
            if self._settled():
                return
            self._idle.clear()

    async def _worker(self):
        while True:
            if self._desired in _REFRESH_STATES and self._applied_effect is not None:
                try:
                    await asyncio.wait_for(self._changed.wait(), REFRESH_INTERVAL_S)
                except asyncio.TimeoutError:
                    await self._refresh(self._applied_effect)
                    continue
            else:
                self._idle.set()
                await self._changed.wait()
            self._changed.clear()
            state = self._desired
            target = self._effects.get(state)
            want_mic = self._want_mic(state)
            if want_mic and not self._mic_on and self._feeder is not None and not self._feeder.ready:
                # 第一次需要时让音箱连上音量通道：先把灯切好再连；连不上就整场对话不用，灯照常切
                if target != self._applied_effect:
                    await self._apply(target, self._mic_on)
                if not await self._feeder.ensure_ready(self._speaker):
                    self._feeder = None
                    want_mic = False
                if self._desired != state:
                    continue
            if target != self._applied_effect or want_mic != self._mic_on:
                await self._apply(target, want_mic)
            if self._settled():
                self._idle.set()

    async def _apply(self, target: int | None, want_mic: bool = False):
        previous = self._applied_effect
        commands = []
        if self._mic_on and not want_mic:
            commands.append(MIC_SOURCE_OFF)
        if previous is not None and previous != target:
            commands.append(_shut(previous))
        if target is not None and previous != target:
            commands.append(_show(target))
        if want_mic and not self._mic_on:
            commands.append(MIC_SOURCE_ON)
        # 先记下目标：即使命令失败，下次切换也会定向关掉它，不会留下常亮灯
        self._applied_effect = target
        if want_mic != self._mic_on:
            self._mic_on = want_mic
            if self._feeder is not None:
                self._feeder.set_active(want_mic)
        if commands:
            await self._run("; ".join(commands), f"{previous} → {target}{' +mic' if want_mic else ''}")

    async def _refresh(self, effect: int):
        command = (
            f"ubus -t 1 call led status | grep -q 'current id {effect}\"' || {_show(effect)}"
        )
        await self._run(command, f"refresh {effect}", quiet=True)

    async def _run(self, command: str, label: str, quiet: bool = False):
        if not self._speaker:
            return
        try:
            result = await self._speaker.run_shell(command, timeout=_COMMAND_TIMEOUT_MS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"灯效 {label} 失败: {exc}", module=LOG_MODULE)
            return
        if result is not None and getattr(result, "exit_code", 0) == 0:
            if quiet:
                logger.debug(f"灯效 {label}", module=LOG_MODULE)
            else:
                logger.info(f"灯效 {label}（状态 {self._desired}）", module=LOG_MODULE)
        elif not quiet:
            logger.warning(f"灯效 {label} 失败，继续语音流程", module=LOG_MODULE)
