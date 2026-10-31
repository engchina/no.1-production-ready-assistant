"""語の照合の正規化（NFKC・大小文字・空白を無視する）。"""

from __future__ import annotations

import unicodedata


def normalize_for_match(text: str) -> str:
    """照合の形（NFKC・casefold・空白をすべて除く）。

    全角・半角の違いと、改行・空白の入り方で一致が変わらないようにする。
    """
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def contains_normalized(text: str, phrase: str) -> bool:
    """`phrase` が `text` に含まれるか（NFKC・大小文字・空白を無視）。空の語は含まれない。"""
    needle = normalize_for_match(phrase)
    return bool(needle) and needle in normalize_for_match(text)


def normalize_name(value: object) -> str:
    """名前（シート名など）の比較の形（NFKC・前後の空白なし・casefold）。空白は残す。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
