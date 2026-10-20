"""米家 MCP（home_tools）：把常驻米家服务（127.0.0.1:9393）的通用操作暴露给 Hermes。

设备、属性、场景都来自米家云的真实目录和 MIoT 规格，这里不写任何设备表。
Spotify 用 Hermes 官方插件；不常用的米家功能（统计、耗材等）用 mijia-api skill 的 CLI。
音箱定时提醒（reminder_*）见 reminders.py：底层是 Hermes cron 的一次性 no_agent 任务。
HOME_TOOLS_DRY_RUN=1 时所有写操作只做校验，不真正执行；提醒照常建任务，到点播报只校验不出声（评测用）。
"""
import os
import sys
from typing import Any, Optional

import requests
from mcp.server.mcpserver import MCPServer

from . import reminders

MIJIA = os.environ.get("MIJIA_URL", "http://127.0.0.1:9393").rstrip("/")
DRY_RUN = os.environ.get("HOME_TOOLS_DRY_RUN") == "1"

server = MCPServer("home_tools", instructions=(
    "米家家居控制。直接用用户说的设备名调用，不要先列设备或查规格；"
    "返回 candidates 时按提示处理。只有工具结果确认的事才能说做了：status=verified/done 表示已确认，"
    "accepted 表示只确认收到指令，failed/refused 表示没做成。"))


def _call(path: str, body: Optional[dict] = None, write: bool = False, timeout: float = 25) -> dict:
    body = {k: v for k, v in (body or {}).items() if v is not None}
    if write and DRY_RUN:
        body["dry_run"] = True
    try:
        r = requests.post(MIJIA + path, json=body, timeout=timeout)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"米家服务不可用：{type(e).__name__}"}


@server.tool()
def mijia_get(devices: Optional[list[str]] = None, room: Optional[str] = None,
              props: Optional[list[str]] = None) -> dict[str, Any]:
    """读取米家设备的实时状态和传感器读数，一次可查多台。
    devices: 设备名列表（用户怎么叫就怎么传，例如 ["厕所灯"]、["温湿度计", "门窗传感器"]）；
    不传 devices 则查全屋（可用 room 限定房间），适合"屋里几度""门关了吗""家里什么开着"。
    props: 可选，只读这些属性（如 ["on", "temperature"]）；不传则返回每台设备的主要读数。
    返回值带采样时间 sampled_at；读不到的属性会单独列出。"""
    return _call("/get", {"devices": devices, "room": room, "props": props})


@server.tool()
def mijia_set(device: str, prop: str, value: Any) -> dict[str, Any]:
    """设置米家设备的一个属性，例如 厕所灯 on=true、厕所灯 brightness=50、空调 target-temperature=26、热水器 on=true。
    开关统一用 prop="on"（插座等会自动对应到主开关）。属性名不对时会返回该设备可写的属性列表，照着改一次即可。
    一句话多个动作时分别调用。红外设备（如部分灯）会提示改用对应场景。"""
    return _call("/set", {"device": device, "prop": prop, "value": value}, write=True)


@server.tool()
def mijia_action(device: str, action: str, params: Optional[list] = None) -> dict[str, Any]:
    """执行设备的 MIoT 动作（如音箱 play / pause / next，插座 toggle）。params 为动作参数列表。
    动作名不对时会返回该设备可用的动作列表。播报文字请用 mijia_speak。"""
    return _call("/action", {"device": device, "action": action, "params": params}, write=True)


@server.tool()
def mijia_scene(name: str) -> dict[str, Any]:
    """运行米家手动场景（例如 开灯、关灯、开空调）。场景名不对时返回可用场景。
    牵涉禁用设备的场景会被拒绝。"""
    return _call("/scene", {"name": name}, write=True)


@server.tool()
def mijia_speak(text: str, speaker: Optional[str] = None) -> dict[str, Any]:
    """让小爱音箱朗读一段文字（主动提醒、定时播报）。不指定 speaker 时用家规里的默认音箱。
    成功只代表音箱已接收指令，不能说已经播完。"""
    return _call("/speak", {"text": text, "speaker": speaker}, write=True)


@server.tool()
def mijia_devices(room: Optional[str] = None) -> dict[str, Any]:
    """列出米家设备（名称、房间、在线状态、主要属性）和场景。平时不需要先调用；
    用户问"家里有哪些设备 / 场景"或需要按类别找设备时再用。目录是缓存，看 updated_at。"""
    return _call("/devices", {"room": room})


@server.tool()
def mijia_refresh() -> dict[str, Any]:
    """从米家云重新读取设备和场景目录，返回与上次相比的变化（新增、移除、改名、上下线）。
    用户说"我加了 / 换了 / 删了设备""更新一下设备列表"时调用；刷新失败会保留旧目录。"""
    return _call("/refresh", timeout=40)


@server.tool()
def reminder_create(text: str, in_minutes: Optional[float] = None, at: Optional[str] = None) -> dict[str, Any]:
    """设一个到点由音箱说出来的提醒。用于带具体时间、到点要在家里听到的提醒，
    例如“二十分钟后提醒我关火”“十点提醒我吃药”。“记一下 / 待办 / 要买 / 别忘了”或没有具体时间的不用它，用滴答清单。
    text: 到点要播的一句话，直接对用户说，例如“该关火了”“该吃药了”。
    in_minutes: 多少分钟后（“半小时后”传 30）；at: 具体时间，24 小时制 "HH:MM"（今天，已过就是明天）
    或 "YYYY-MM-DD HH:MM"。二选一。“十点”这类没说上午下午的，取离现在最近的那个。
    返回的 at 是实际安排的时间，回复时照它说。"""
    return reminders.create(text, in_minutes=in_minutes, at=at, mijia=MIJIA, dry_run=DRY_RUN)


@server.tool()
def reminder_list() -> dict[str, Any]:
    """列出还没到点的音箱提醒（“我有哪些提醒”）。"""
    return reminders.list_pending()


@server.tool()
def reminder_cancel(id: Optional[str] = None, text: Optional[str] = None) -> dict[str, Any]:
    """取消一个还没到点的音箱提醒。用 id（reminder_create / reminder_list 返回的）或提醒内容里的关键词，
    例如“取消关火的提醒”传 text="关火"。多条符合时会返回候选。"""
    return reminders.cancel(id=id, text=text)


def main():
    print("home_tools: ready", file=sys.stderr)
    server.run("stdio")


if __name__ == "__main__":
    main()
