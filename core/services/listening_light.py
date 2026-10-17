"""监听灯效控制：通过音箱现有 RPC 调用 ubus，并隔离各次采集的灯效归属。"""

import asyncio

from core.utils.logger import logger


class ListeningLightController:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owners: dict[int, object] = {}
        self._leases: dict[object, tuple[int, object]] = {}

    async def turn_on(self, speaker, effect_id: int) -> object | None:
        """开启指定灯效，返回仅供本次采集关闭灯效的凭据。"""
        if not speaker or type(effect_id) is not int or not 1 <= effect_id <= 255:
            return None
        async with self._lock:
            token = object()
            previous = self._owners.get(effect_id)
            self._leases.pop(previous, None)
            self._owners[effect_id] = token
            self._leases[token] = (effect_id, speaker)
            try:
                success = await self._apply(speaker, effect_id, True)
            except asyncio.CancelledError:
                self._owners.pop(effect_id, None)
                self._leases.pop(token, None)
                await self._apply(speaker, effect_id, False)
                raise
            if not success:
                self._owners.pop(effect_id, None)
                self._leases.pop(token, None)
                # 命令超时仍可能已在设备执行，定向关闭以免留下常亮灯。
                await self._apply(speaker, effect_id, False)
                return None
            return token

    async def turn_off(self, token: object | None) -> None:
        """只关闭当前凭据持有的灯效，旧采集的清理不会影响新采集。"""
        async with self._lock:
            lease = self._leases.pop(token, None)
            if lease is None:
                return
            effect_id, speaker = lease
            if self._owners.get(effect_id) is not token:
                return
            self._owners.pop(effect_id, None)
            await self._apply(speaker, effect_id, False)

    async def refresh(self, token: object) -> bool:
        """维持有效凭据的灯效，防止设备播放器提前熄灯；凭据失效时不再操作。"""
        async with self._lock:
            lease = self._leases.get(token)
            if lease is None:
                return False
            effect_id, speaker = lease
            if self._owners.get(effect_id) is not token:
                return False
            await self._apply(speaker, effect_id, True, refresh=True)
            return True

    # ===== 私有方法 =====

    async def _apply(self, speaker, effect_id: int, enabled: bool, *, refresh: bool = False) -> bool:
        method = "show" if enabled else "shut"
        try:
            command = f'ubus -t 1 call led {method} \'{{"L":{effect_id}}}\''
            if refresh:
                # 灯效仍有效时不重启动画，只有被设备提前关闭后才重新开启。
                command = (
                    f'ubus -t 1 call led status | grep -q \'current id {effect_id}"\' '
                    f'|| {command}'
                )
            result = await speaker.run_shell(
                command,
                timeout=2000,
            )
            if result is not None and result.exit_code == 0:
                log = logger.info if enabled and not refresh else logger.debug
                log(f"监听灯效 {effect_id}: {method}", module="ListeningLight")
                return True
        except Exception as exc:
            logger.warning(f"监听灯效控制失败: {exc}", module="ListeningLight")
            return False
        logger.warning(f"监听灯效 {effect_id} {method} 失败，继续语音流程", module="ListeningLight")
        return False


ListeningLight = ListeningLightController()
