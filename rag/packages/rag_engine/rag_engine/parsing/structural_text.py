"""本文の意味を判定せず、構造だけの行を識別する。"""

import re
import unicodedata

_TABLE_SEPARATOR = re.compile(r"\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?|\|?\s*:?-{3,}:?\s*|\*{3,}|_{3,}")
_MARKUP_ONLY = re.compile(r"[*_`>#|~\-\s]+")


def is_table_rule(text: str) -> bool:
    """Markdown の表の区切り・区切り線・書式記号だけの行か。"""
    return bool(_TABLE_SEPARATOR.fullmatch(text.strip()) or _MARKUP_ONLY.fullmatch(text.strip()))


def structural_only(text: str) -> bool:
    """短い語・値・記号を一律に除かず、構造的な行だけの本文を識別する。"""
    lines = [unicodedata.normalize("NFKC", line).strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(
        re.fullmatch(r"(?:=\s*){3,}", line) is not None or (len(re.sub(r"\s", "", line)) >= 3 and is_table_rule(line)) for line in lines
    )
