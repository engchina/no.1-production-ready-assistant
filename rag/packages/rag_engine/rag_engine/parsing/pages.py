"""ユーザー入力されたページ範囲を安全なページ番号列へ変換する。"""

from __future__ import annotations

import re
import unicodedata

from rag_parser_core.language.markers import marker_patterns


def _range_separators() -> re.Pattern[str]:
    """範囲の記号(言語資源。#2181)。"""
    return re.compile(marker_patterns().range_dash)


_PAGE_PART = re.compile(r"(\d+)(?:-(\d+))?")


def parse_page_range(value: str, page_count: int, default_limit: int = 1) -> list[int]:
    """1,3-5 のようなページ指定を 1-based のページ番号へ変換する。

    全角数字・全角ハイフン・波ダッシュも受け付ける。範囲は展開前に 1..page_count へ
    切り詰めるため、処理量は入力値ではなくページ数に比例する。解釈できない指定と、
    有効なページが 1 つも無い指定は ValueError。
    """
    # NFKC で全角の数字・ハイフン・カンマを半角へ寄せてから、残る範囲記号を統一する。
    cleaned = _range_separators().sub("-", unicodedata.normalize("NFKC", value or "")).strip()
    if not cleaned:
        return list(range(1, min(page_count, default_limit) + 1))
    if cleaned.lower() in {"all", *marker_patterns().all_pages_words}:
        return list(range(1, page_count + 1))

    pages: set[int] = set()
    for part in re.split(r"[,、\s]+", cleaned):
        if not part:
            continue
        match = _PAGE_PART.fullmatch(part)
        if match is None:
            raise ValueError(f"ページ指定「{part}」を解釈できません。「1,3-5」のように数字と範囲で指定してください。")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            start, end = end, start
        pages.update(range(max(start, 1), min(end, page_count) + 1))

    if not pages:
        raise ValueError(f"有効なページ番号がありません。1 から {page_count} の範囲で指定してください。")
    return sorted(pages)
