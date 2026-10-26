"""判断一句识别结果"像没说完"或"只有语气词"，用于连续对话的断句。

- looks_unfinished("你觉得今天会很。") → True：停顿后多等一会，用户可能还在想
- is_filler_only("呃.") → True：不发给后端，接着听

规则故意偏保守：误判"没说完"会多等一段停顿，误判"说完了"会把半句话发出去。
"""

from __future__ import annotations

import re
import unicodedata

# 只有这些（可重复）时算没说话
FILLERS = ("呃", "额", "嗯", "恩", "唔", "呣", "哦", "噢", "诶", "欸", "哎", "那个", "这个", "就是", "嗯哼", "um", "uh", "hmm")

# 句尾是这些词时，多半话没说完
_UNFINISHED_WORDS = (
    # 连词、转折
    "然后", "还有", "而且", "但是", "不过", "因为", "所以", "如果", "要是", "或者", "还是", "以及", "比如", "就是", "那个", "这个",
    # 动词 / 助动词后面通常还要接东西
    "可以", "能不能", "能否", "帮我", "给我", "告诉我", "我想", "我要", "想要", "需要", "应该", "觉得", "知道",
)
# 句尾是这些单字时，多半话没说完（"今天会很"、"帮我把"、"讲个"）
# "凌晨12点的"、"让它变得"这类修饰语后面也通常还有内容。
_UNFINISHED_CHARS = set("很狠会在和跟与把被给让对从往向比是想要能太最更都也还就才又再没这那哪一个些第几呃额嗯你我他她它的得地")

_TRAILING = re.compile(r"[\s\W_]+$", re.UNICODE)


def _core(text: str) -> str:
    """去掉首尾标点和空白。"""
    if not text:
        return ""
    text = text.strip()
    text = _TRAILING.sub("", text)
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P")).strip()


def is_filler_only(text: str) -> bool:
    core = _core(text).lower().replace(" ", "")
    if not core:
        return True
    for filler in sorted(FILLERS, key=len, reverse=True):
        core = core.replace(filler, "")
    return core == ""


def _latin(text: str) -> str:
    return "".join(ch for ch in text.lower() if "a" <= ch <= "z")


def is_wake_echo(text: str, wake_words) -> bool:
    """只剩唤醒词（或它的一截）的识别结果："缪斯。"、"斯。"、"He miss."（唤醒词被识别成英文）。

    打断 / 唤醒后从唤醒词结尾接着听时，开头补进来的音频带着它；回答里说到"缪斯"时也可能漏进来。
    """
    core = _core(text).replace(" ", "")
    if not core:
        return False
    import difflib

    words = [w for w in (wake_words or ()) if isinstance(w, str) and w.strip()]
    compact = ["".join(ch for ch in w if not ch.isspace() and not unicodedata.category(ch).startswith("P")) for w in words]
    # 唤醒词本身或它的后半截（"好缪斯" / "缪斯" / "斯"）
    if any(c and c.lower().endswith(core.lower()) for c in compact):
        return True
    # 识别成了英文：全是字母、不超过 3 个词，和英文唤醒词读音相近
    latin = _latin(text)
    if latin and len(latin) == len(core) and len(text.split()) <= 3:
        targets = [_latin(w) for w in words if _latin(w) and _latin(w) == "".join(w.lower().split())]
        return any(difflib.SequenceMatcher(None, latin, t).ratio() >= 0.6 for t in targets)
    return False


def looks_unfinished(text: str) -> bool:
    core = _core(text)
    if not core or is_filler_only(core):
        return False
    if any(core.endswith(word) for word in _UNFINISHED_WORDS):
        return True
    return core[-1] in _UNFINISHED_CHARS
