"""本地家规校验；不实现或枚举 CLI 功能，所有命令仍交给上游。"""
import functools
import json
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).with_name('house-policy.json')


def has_blocked_did(value, blocked: set[str]) -> bool:
    if isinstance(value, dict):
        if str(value.get('did', '')) in blocked:
            return True
        return any(has_blocked_did(item, blocked) for item in value.values())
    if isinstance(value, list):
        return any(has_blocked_did(item, blocked) for item in value)
    return False


def install() -> None:
    """在上游实际发送设备请求 / 场景前检查原有禁用规则。"""
    from mijiaAPI import mijiaAPI
    policy = json.loads(POLICY_PATH.read_text())
    blocked = {str(item['did']) for item in policy.get('blocked_devices', [])}
    blocked_scenes = set(policy.get('blocked_scenes', []))
    names = {name for item in policy.get('blocked_devices', [])
             for name in item.get('names', [])}
    # 命名场景和直接 did 在 CLI 启动阶段即拒绝，避免无意义云端请求。
    for index, arg in enumerate(sys.argv[1:], 1):
        if arg in ('--did', '--dev_name') and index + 1 < len(sys.argv):
            target = sys.argv[index + 1]
            if target in blocked or target in names:
                raise PermissionError('[MijiaPolicy] 该设备已被用户禁止控制')
        if '--run_scene' in sys.argv and arg in blocked_scenes:
            raise PermissionError('[MijiaPolicy] 该场景已被用户禁止运行')
    if any(x in sys.argv for x in ('run', '--run')) and any(
            name in arg for arg in sys.argv for name in names):
        raise PermissionError('[MijiaPolicy] 禁止通过自然语言控制已禁用设备')
    original_request = mijiaAPI._request

    @functools.wraps(original_request)
    def guarded_request(self, *args, **kwargs):
        if has_blocked_did(list(args) + list(kwargs.values()), blocked):
            raise PermissionError('[MijiaPolicy] 请求包含被禁止的设备')
        return original_request(self, *args, **kwargs)

    original_scene = mijiaAPI.run_scene

    @functools.wraps(original_scene)
    def guarded_scene(self, scene_id, home_id, *args, **kwargs):
        scenes = self.get_scenes_list(home_id)
        for scene in scenes:
            sid = str(scene.get('scene_id') or scene.get('id'))
            if str(scene_id) == sid and scene.get('name') in blocked_scenes:
                raise PermissionError('[MijiaPolicy] 该场景已被用户禁止运行')
            # 场景名不在黑名单、但实际会操作禁用设备（如回家 / 离家 / 晚安会开关树莓派插座）也拒绝
            if str(scene_id) == sid and has_blocked_did(scene.get('scene_action'), blocked):
                raise PermissionError('[MijiaPolicy] 该场景会操作被禁止控制的设备')
        return original_scene(self, scene_id, home_id, *args, **kwargs)

    mijiaAPI._request = guarded_request
    mijiaAPI.run_scene = guarded_scene
