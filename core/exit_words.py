"""连续对话的退出词判断（移植自 OwnDing/open-xiaoai 的 event.py）。

和以前"文本里包含退出词就退出"不同：

- "拜拜" / "好的，谢谢，再见" / "你好缪斯，拜拜" → 马上退出（now）
- "关灯，拜拜" / "不用了，帮我把灯关掉" → 先把请求发给后端、播完回答再退出（after_answer）
- "用英语怎么说再见" / "再见用英语怎么说" / "停止播放音乐" → 不是退出，照常发给后端

本模块只依赖标准库，config.py / 测试可以直接导入。
"""

import re

DEFAULT_EXIT_WORDS = ("拜拜", "再见", "退出", "退下", "没事了", "不用了", "结束对话", "bye", "goodbye")
# 只表示告别的词：放在句首时，后面跟着别的话也直接退出（后面的话多半是在跟别人说）
_GOODBYES = ("拜拜", "再见", "bye", "goodbye")
# 退出词前后常带、不改变意思的话
_EXIT_FILLERS = (
    "你好", "您好", "好的", "好吧", "好", "行", "嗯", "哦", "那", "那就",
    "谢谢", "谢了", "没事", "不用", "ok", "okay",
)
_PARTICLES = "吧啦了呀啊哈哦喔嘛呗"
_CLAUSE_BREAKS = re.compile(r"[，。！？、；：,.!?;:…]+")

NOW = "now"
AFTER_ANSWER = "after_answer"


def _plain(text) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _bye(plain: str) -> str:
    """识别器对 "bye bye" 的各种写法（"拜 by"、"拜e bye"、"Goode bye"）统一成"拜拜"。"""
    plain = re.sub(r"goode?(?=by)", "", plain)
    return re.sub(r"(?:拜e?|bye?){2,}", "拜拜", plain)


def _exit_list(words) -> list[str]:
    return sorted({_bye(w) for w in map(_plain, words) if w}, key=len, reverse=True)


def _strip_exit_tail(rest: str, exits: list[str]) -> tuple[str | None, str]:
    """从末尾去掉退出词、语气词和助词；返回最后说的退出词（没有为 None）和剩下的部分。"""
    found = None
    while rest:
        word = next((w for w in exits if rest.endswith(w)), None)
        if word:
            found = found or word
        else:
            word = next((w for w in _EXIT_FILLERS if rest.endswith(w)), None)
            if not word and rest[-1] in _PARTICLES:
                word = rest[-1]
            if not word:
                break
        rest = rest[: -len(word)]
    return found, rest


def _names(keywords) -> set[str]:
    """唤醒词及其后缀（"你好缪斯" → "好缪斯"、"缪斯"），句首出现时可以忽略。"""
    names = {k for k in map(_plain, keywords) if k}
    return names | {k[i:] for k in names for i in range(len(k) - 1)}


def _strip_leading(rest: str, names: set[str]) -> str:
    leading = sorted(names | set(_EXIT_FILLERS), key=len, reverse=True)
    while rest:
        word = next((w for w in leading if rest.startswith(w)), None)
        if not word:
            break
        rest = rest[len(word):]
    return rest


def match_exit_words(text, words=DEFAULT_EXIT_WORDS, keywords=()) -> str | None:
    """text 只是在说退出（"拜拜"、"好的，谢谢，再见"、"你好缪斯，拜拜"）时返回那个退出词，否则 None。

    唤醒词也可能被一起识别进来，有时还识别错（"你好谬思"），打断后还会带上唤醒词的尾音，
    所以唤醒词以外最多允许剩一个字；"关灯，拜拜"、"用英语怎么说再见"不算。
    """
    found, rest = _strip_exit_tail(_bye(_plain(text)), _exit_list(words))
    if not found:
        return None
    names = _names(keywords)
    rest = _strip_leading(rest, names)
    name_chars = set("".join(names))
    return found if sum(ch not in name_chars for ch in rest) <= 1 else None


def match_exit_intent(text, words=DEFAULT_EXIT_WORDS, keywords=()) -> tuple[str | None, str | None]:
    """text 要怎样结束对话：(NOW, 词)、(AFTER_ANSWER, 词) 或 (None, None)。

    NOW：只说了退出（match_exit_words），或者句首是告别、后面的话是对别人说的
        （"拜拜，15加12等于多少"）。
    AFTER_ANSWER：先回答、再退出（不再接着听）。句末告别（"关灯，拜拜"），唤醒词识别错得
        认不出（"你好傻次，拜拜"），或句首是别的退出词（"不用了，帮我把灯关掉"）。
    "用英语怎么说再见"、"再见用英语怎么说"不是退出。
    """
    if not words:
        return None, None
    word = match_exit_words(text, words, keywords)
    if word:
        return NOW, word
    names = _names(keywords)
    clauses = [c for c in _CLAUSE_BREAKS.split(str(text)) if _strip_leading(_plain(c), names)]
    if len(clauses) > 1:
        word = match_exit_words(clauses[0], words, keywords)
        if word:
            return (NOW if word in _exit_list(_GOODBYES) else AFTER_ANSWER), word
        word = match_exit_words(clauses[-1], words, keywords)
        if word:
            return AFTER_ANSWER, word
    found, rest = _strip_exit_tail(_bye(_plain(text)), _exit_list(words))
    # 识别错的唤醒词字数不变（"你好傻次"）
    longest = max((sum(not ch.isascii() for ch in _plain(k)) for k in keywords), default=0)
    if found and rest and len(rest) <= longest:
        return AFTER_ANSWER, found
    return None, None
