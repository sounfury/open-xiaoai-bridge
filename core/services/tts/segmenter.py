"""把流式回复切成适合朗读的段落。

- 第一段尽快出：遇到句号类标点就出；遇到逗号类标点、且已经够 first_min_chars 个字也出。
- 后面的段攒到 target_chars 个字左右，在标点处切开（长段合成一次，段间停顿少）。
- 流停住时（模型在调用工具、或者网络慢）由调用方调 on_stall()，把攒着的完整句子先放出去。
- 朗读前清理：<think> 块、Markdown 符号、链接、表情；开头整句的英文（模型的思考漏进正文）
  在后面出现中文时丢掉，整个回答都是英文时照常读。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 句子结束（强边界）和句中停顿（弱边界）
_STRONG = "。！？!?；;\n…."
_WEAK = "，,、：:"
_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)、])\s+", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3}|~~|`+)")
_SPACES = re.compile(r"[ \t\u3000]+")


@dataclass
class SegmenterSettings:
    first_min_chars: int = 8  # 第一段在逗号处切开所需的最少字数
    target_chars: int = 60  # 后续段攒到这么多字再切
    max_chars: int = 120  # 一段最长（找不到标点时硬切）
    stall_force_ms: int = 1200  # 流停住这么久，没有标点也把攒着的放出去

    @classmethod
    def from_config(cls, config) -> "SegmenterSettings":
        values = {}
        if isinstance(config, dict):
            for name in cls.__dataclass_fields__:
                if name in config:
                    try:
                        values[name] = int(config[name])
                    except (TypeError, ValueError):
                        pass
        s = cls(**values)
        s.first_min_chars = max(1, s.first_min_chars)
        s.target_chars = max(s.first_min_chars, s.target_chars)
        s.max_chars = max(s.target_chars, s.max_chars)
        s.stall_force_ms = max(0, s.stall_force_ms)
        return s


def clean_for_speech(text: str) -> str:
    """去掉朗读时没意义的符号；没有可读内容时返回空串。"""
    if not text:
        return ""
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = "".join(
        ch for ch in text
        if ch in "°℃℉"
        or not (unicodedata.category(ch) in ("So", "Cs", "Co") or 0xFE00 <= ord(ch) <= 0xFE0F or ch == "\u200d")
    )
    text = text.replace("\r", "").replace("\n", " ")
    text = _SPACES.sub(" ", text).strip()
    if not any(ch.isalnum() for ch in text):
        return ""
    return text


def _is_english_sentence(text: str) -> bool:
    """没有中文、至少 3 个英文单词：像模型漏出来的英文思考（"Let me check the sensor."）。"""
    return not _CJK.search(text) and len(_LATIN_WORD.findall(text)) >= 3


class ReplySegmenter:
    """流式文本 → 朗读段。feed() / on_stall() / finish() 返回可以送去合成的段。"""

    def __init__(self, settings: SegmenterSettings | None = None):
        self.settings = settings or SegmenterSettings()
        self._pending = ""
        self._emitted = 0  # 已经放出去的段数
        self._in_think = False
        self._raw_tail = ""  # <think> 标签可能被拆在两个 delta 之间
        self._held: list[str] = []  # 开头的英文句子，等看到中文再决定
        self._seen_cjk = False
        self.dropped: list[str] = []  # 被当成英文思考丢掉的句子（日志用）

    @property
    def emitted(self) -> int:
        return self._emitted

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._pending += self._strip_think(delta)
        return self._take(force=False)

    def on_stall(self, stalled_ms: float) -> list[str]:
        """流停住了：完整的句子先放出去；停够 stall_force_ms 时剩下的也放出去。"""
        if not self._pending.strip():
            return []
        cut = self._last_boundary(self._pending, _STRONG + _WEAK)
        if cut > 0:
            head, self._pending = self._pending[:cut], self._pending[cut:]
            return self._emit(head)
        if stalled_ms >= self.settings.stall_force_ms:
            head, self._pending = self._pending, ""
            return self._emit(head)
        return []

    def finish(self) -> list[str]:
        out = self._take(force=True)
        if self._held:
            # 整个回答都没有中文：英文照常读
            held, self._held = self._held, []
            out = held + out
            self._emitted += len(held)
        return out

    # ---- 内部 ----

    def _strip_think(self, delta: str) -> str:
        text = self._raw_tail + delta
        self._raw_tail = ""
        out = []
        while text:
            if self._in_think:
                end = text.find("</think>")
                if end < 0:
                    # 留下可能是半个结束标签的尾巴
                    self._raw_tail = text[-7:]
                    return "".join(out)
                text = text[end + len("</think>"):]
                self._in_think = False
                continue
            start = text.find("<think>")
            if start < 0:
                # 结尾可能是半个 <think>
                for n in range(min(6, len(text)), 0, -1):
                    if "<think>".startswith(text[-n:]):
                        self._raw_tail = text[-n:]
                        text = text[:-n]
                        break
                out.append(text)
                break
            out.append(text[:start])
            text = text[start + len("<think>"):]
            self._in_think = True
        return "".join(out)

    def _take(self, force: bool) -> list[str]:
        out: list[str] = []
        s = self.settings
        while self._pending:
            text = self._pending
            if self._emitted == 0 and not out:
                # 第一段：第一个强边界，或够长后的第一个弱边界
                cut = self._first_boundary(text, s.first_min_chars)
                if cut <= 0 and len(text) >= s.max_chars:
                    cut = s.max_chars
            elif len(text) >= s.target_chars:
                cut = self._split_position(text)
            else:
                cut = 0
            if cut <= 0:
                if force:
                    cut = len(text)
                else:
                    break
            head, self._pending = text[:cut], text[cut:]
            out.extend(self._emit(head))
        return out

    def _emit(self, raw: str) -> list[str]:
        text = clean_for_speech(raw)
        if not text:
            return []
        if not self._seen_cjk:
            if _CJK.search(text):
                self._seen_cjk = True
                if self._held:
                    self.dropped.extend(self._held)
                    self._held = []
            elif _is_english_sentence(text):
                self._held.append(text)
                return []
        self._emitted += 1
        return [text]

    @staticmethod
    def _first_boundary(text: str, weak_min: int) -> int:
        for i, ch in enumerate(text):
            if ch in _STRONG and ReplySegmenter._is_real_boundary(text, i):
                return i + 1
            if ch in _WEAK and len(text[:i].strip()) >= weak_min and ReplySegmenter._is_real_boundary(text, i):
                return i + 1
        return 0

    def _split_position(self, text: str) -> int:
        """在 max_chars 以内找最后一个句末标点（不能太靠前）；放不下一整句时才在逗号处切。"""
        s = self.settings
        window = text[: s.max_chars]
        minimum = max(1, s.target_chars // 2)
        cut = self._last_boundary(window, _STRONG)
        if cut >= minimum:
            return cut
        if len(text) < s.max_chars:
            return 0  # 等这句话说完
        cut = self._last_boundary(window, _STRONG + _WEAK)
        return cut if cut >= minimum else s.max_chars

    @staticmethod
    def _last_boundary(text: str, marks: str) -> int:
        for i in range(len(text) - 1, -1, -1):
            if text[i] in marks and ReplySegmenter._is_real_boundary(text, i):
                return i + 1
        return 0

    @staticmethod
    def _is_real_boundary(text: str, i: int) -> bool:
        """数字里的逗号 / 冒号 / 小数点（"1,000"、"10:30"、"3.5"）不算边界；
        英文句号后面要跟空白才算。还看不到下一个字时先不切（可能在下一个 delta 里）。"""
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else None
        # 链接里面不切："https://…"、"[文字](…"
        if ch == ":" and (nxt == "/" or (nxt is None and text[max(0, i - 5):i].lower().endswith(("http", "https")))):
            return False
        link = text.rfind("](", 0, i)
        if link >= 0 and text.find(")", link, i) < 0:
            return False
        url = text.rfind("://", 0, i)
        if url >= 0 and not any(c.isspace() or ord(c) > 127 for c in text[url:i]):
            return False
        if ch == ".":
            return nxt is not None and nxt.isspace()
        if ch in ",:" and i > 0 and text[i - 1].isdigit() and (nxt is None or nxt.isdigit()):
            return False
        return True
