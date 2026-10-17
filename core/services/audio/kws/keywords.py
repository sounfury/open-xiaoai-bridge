import re
import unicodedata
from pathlib import Path


def init_project_context():
    """动态导入父模块"""
    import os
    import sys

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../..")
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


init_project_context()

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path
from core.utils.logger import logger


def should_generate_keywords():
    """Return whether keyword generation should run."""
    import os

    xiaozhi_enabled = os.environ.get("XIAOZHI_ENABLE", "").lower() in ("1", "true", "yes")
    # 兼容 OPENCLAW_ENABLE (新) 和 OPENCLAW_ENABLED (旧)
    openclaw_env = os.environ.get("OPENCLAW_ENABLE") or os.environ.get("OPENCLAW_ENABLED") or ""
    openclaw_enabled = openclaw_env.lower() in ("1", "true", "yes")
    openai_enabled = os.environ.get("OPENAI_ENABLE", "").lower() in (
        "1",
        "true",
        "yes",
    )
    qwenpaw_enabled = os.environ.get("QWENPAW_ENABLE", "").lower() in (
        "1",
        "true",
        "yes",
    )
    muse_enabled = os.environ.get("MUSE_ENABLE", "").lower() in (
        "1",
        "true",
        "yes",
    )

    if (
        not xiaozhi_enabled
        and not openclaw_enabled
        and not openai_enabled
        and not qwenpaw_enabled
        and not muse_enabled
    ):
        return False, "XIAOZHI_ENABLE, OPENCLAW_ENABLE/OPENCLAW_ENABLED, OPENAI_ENABLE, QWENPAW_ENABLE and MUSE_ENABLE are all disabled"

    return True, ""


def _keyword_text(keyword: str) -> str:
    """KWS 关键词文本：NFKC（全角 → 半角）、英文转大写（BPE 词表是大写）、合并多余空格。"""
    return " ".join(unicodedata.normalize("NFKC", keyword).upper().split())


def get_keyword_texts(config_getter) -> list[str]:
    """wakeup.keywords + 小爱管家 KWS 子集（MUSE_ENABLE=1 时，见 core/muse.py），去重保序。"""
    from core.muse import kws_wake_words

    keywords = list(config_getter("wakeup.keywords", []) or [])
    keywords += kws_wake_words(config_getter)
    texts: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            continue
        text = _keyword_text(keyword)
        if text and text not in texts:
            texts.append(text)
    return texts


def get_args():
    config = ConfigManager.instance()
    tokens_type = "cjkchar+bpe"
    tokens = get_model_file_path("tokens.txt")
    bpe_model = get_model_file_path("bpe.model")
    output = get_model_file_path("keywords.txt")
    texts = get_keyword_texts(config.get_app_config)
    return locals()


def encode_keywords(texts, text2token, **kwargs) -> list[list[str]]:
    """逐个编码关键词；编码失败（含 OOV 字符）的跳过并警告，不影响其他关键词。

    sherpa_onnx.text2token 遇到 OOV 只会 print 并静默丢掉该条，
    返回列表会变短，所以这里一条一条编码，才能知道是哪一条被跳过。
    """
    encoded: list[list[str]] = []
    for text in texts:
        try:
            result = text2token([text], **kwargs)
        except Exception as exc:
            logger.warning(
                f"关键词 {text!r} 编码失败，已跳过: {type(exc).__name__}: {exc}",
                module="KWS",
            )
            continue
        tokens = result[0] if result else None
        if not tokens:
            logger.warning(
                f"关键词 {text!r} 含有模型词表（tokens.txt）里没有的字符，已跳过",
                module="KWS",
            )
            continue
        encoded.append(list(tokens))
    return encoded


def format_keyword_line(tokens: list[str]) -> str:
    """纯英文关键词不带 @phrase（KWS 返回拼接的 token，如 "hi▁muse"），其余带 @原文。"""
    line = "".join(tokens)
    if re.match(r"^[▁A-Z\s]+$", line):
        return " ".join(tokens)
    return " ".join(tokens) + f" @{line}"


def main():
    should_run, reason = should_generate_keywords()
    if not should_run:
        logger.debug(f"Keyword generation skipped: {reason}", module="KWS")
        return 0

    required_files = [
        get_model_file_path("tokens.txt"),
        get_model_file_path("bpe.model"),
    ]
    missing_files = [path for path in required_files if not Path(path).is_file()]
    if missing_files:
        logger.debug(
            "Keyword generation failed: missing model files: "
            f"{', '.join(missing_files)}",
            module="KWS",
        )
        return 1

    from sherpa_onnx import text2token

    args = get_args()
    encoded_texts = encode_keywords(
        args["texts"],
        text2token,
        tokens=args["tokens"],
        tokens_type=args["tokens_type"],
        bpe_model=args["bpe_model"],
    )
    if not encoded_texts:
        logger.error("Keyword generation failed: no keyword could be encoded", module="KWS")
        return 1
    with open(args["output"], "w", encoding="utf8") as f:
        for txt in encoded_texts:
            f.write(format_keyword_line(txt) + "\n")
    logger.debug(
        f"Keyword file generated: {args['output']} ({len(encoded_texts)}/{len(args['texts'])} keywords)",
        module="KWS",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
