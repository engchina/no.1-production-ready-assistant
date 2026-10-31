"""Markdown の体裁の正規化。主張かどうかの意味の判定は呼び出し側で行う。"""

import re
import unicodedata

_BOLD_NOTE = re.compile(
    r"\s*\*\*(?P<title>[^*\n]+)\*\*\s*(?P<note>（[^（）\n]+）|\([^()\n]+\))\s*[:：]?\s*"
)


def normalize_heading_markup(text: str) -> str:
    """太字の外の括弧も見出しの語に含め、注記中の断定も検査できる形にする。"""
    match = _BOLD_NOTE.fullmatch(text)
    if match:
        return f"**{match['title']}{match['note']}**"
    return text


def is_blank_text(text: str) -> bool:
    """空白と不可視の書式文字だけの行か。本文内の書式文字は変更しない。"""
    return all(char.isspace() or unicodedata.category(char) == "Cf" for char in text)
