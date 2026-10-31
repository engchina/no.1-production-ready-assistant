"""文字の形の同一視（全文検索の索引・質問・回答のモデルに渡す根拠で共通。#1336・#1350）。

資料は英数字・記号を全角（``ＨＲＭ`` ``ＳＹＳ－１０４``）や半角カナ（``ｼｽﾃﾑ``）で書くことが多く、質問・別の
資料の半角（``HRM``）と同じ語に見えない。ここで文字の形を 1 つにそろえる。改行と大文字小文字は残す
（語の区切り・大文字小文字の無視は使う側が決める）。

- ``fold_text``: NFKC と波線・ダッシュの同一視。全文検索の索引（``normalize_text_search_index_text``。#1336）と
  引用の照合に使う。丸数字・ローマ数字・㈱ なども同じ形にする（``①`` → ``1``）。
- ``fold_text_with_positions``: ``fold_text`` の形と、各文字の元の位置。そろえた形で見つけた引用を原文の範囲へ戻す。
- ``find_width_folded``: ``fold_width`` の形で空白を除いて写しを探し、原文の範囲を返す（引用の写しの照合と同じ比べ方）。
- ``fold_width``: 全角・半角の違い（Unicode の ``<wide>`` / ``<narrow>`` の互換文字）だけを NFKC の形にする。
  回答のモデルと、モデルの説明を根拠と比べる検査に渡す形（#1350）。``fold_text`` の一部なので、モデルがこの形で
  引用しても ``fold_text`` の照合で原文に一致する。丸数字（``①``）は手順の区切り、``Ⅴ`` は章の番号としてモデルと
  検査（手順番号での分割・箇条書きの番号の除外）が読むので残す。
- ``fold_model_view``: JSON に入れる値（dict / list の中の文字列）を ``fold_width`` でそろえる。JSON にしてから
  そろえると、全角の引用符・円記号（``＂`` ``＼``）が JSON の記号になって壊れるため、値の側でそろえる。

画面に出す本文・保存する根拠・引用の原文・embedding には使わない（それらは資料の表記のまま）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

# 波線とダッシュ。NFKC は全角の「～」「－」だけを半角にし、波ダッシュ「〜」や各種のダッシュを残すため、別に同一視する。
SYMBOL_FOLD = str.maketrans(
    {
        **dict.fromkeys("〜～", "~"),
        **dict.fromkeys("－‐‑‒–—―−", "-"),
    }
)
# 改行（\n）とタブ以外の制御文字。連続をまとめて 1 つの空白にする。
_CONTROL_RUN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# 直前の文字と合わせて NFKC を取る文字（結合文字と半角の濁点・半濁点）。「ｶﾞ」は 1 文字の「ガ」になる。
_HALFWIDTH_SOUND_MARKS = frozenset("ﾞﾟ")
# 全角・半角の互換文字（全角の英数字・記号、全角の空白、半角カナ・半角の記号）と、その NFKC の形。
_WIDTH_FOLD = str.maketrans(
    {
        chr(code): unicodedata.normalize("NFKC", chr(code))
        for code in (0x3000, *range(0xFF00, 0xFFF0))
        if unicodedata.decomposition(chr(code)).startswith(("<wide>", "<narrow>"))
    }
)
# 半角カナの濁点・半濁点（NFKC で結合文字になる）と直前のカナ。1 文字に合成する。
_SOUND_MARK_PAIR = re.compile(r".[゙゚]")


def fold_text(text: Any) -> str:
    """NFKC・波線とダッシュの同一視・制御文字の空白化で、文字の形をそろえます。改行と大文字小文字は残します。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).translate(SYMBOL_FOLD)
    return _CONTROL_RUN.sub(" ", normalized)


def fold_width(text: Any) -> str:
    """全角・半角の違いだけを NFKC の形にそろえます（``ＨＲＭ`` → ``HRM``、``ｶﾞｲﾄﾞ`` → ``ガイド``）。

    丸数字・ローマ数字・波ダッシュなどの他の互換文字、改行と大文字小文字は残します。
    """
    folded = str(text or "").translate(_WIDTH_FOLD)
    if "゙" not in folded and "゚" not in folded:
        return folded
    return _SOUND_MARK_PAIR.sub(lambda match: unicodedata.normalize("NFC", match.group()), folded)


def _clusters(text: str) -> list[tuple[int, str]]:
    """(開始位置, 文字の塊)。塊は基底の文字と、それに続く結合文字・半角の濁点。"""
    clusters: list[tuple[int, str]] = []
    for index, char in enumerate(text):
        if clusters and (unicodedata.combining(char) or char in _HALFWIDTH_SOUND_MARKS):
            start, chars = clusters[-1]
            clusters[-1] = (start, chars + char)
        else:
            clusters.append((index, char))
    return clusters


def fold_text_with_positions(text: Any) -> tuple[str, list[int]]:
    """``fold_text`` と同じ形の文字列と、その各文字が元の文字列のどの位置から来たかを返します。

    NFKC は文字の数を変える（「㈱」→「(株)」、「ｶﾞ」→「ガ」）ので、そろえた形で見つけた範囲を原文の範囲へ
    戻すには位置の対応が要る。塊（基底の文字と結合文字）ごとに NFKC を取り、塊の文字はすべて塊の先頭の位置に
    対応させる。
    """
    value = str(text or "")
    chars: list[str] = []
    positions: list[int] = []
    previous_control = False
    for start, cluster in _clusters(value):
        if len(cluster) == 1 and _CONTROL.match(cluster):
            if not previous_control:
                chars.append(" ")
                positions.append(start)
            previous_control = True
            continue
        previous_control = False
        for char in unicodedata.normalize("NFKC", cluster).translate(SYMBOL_FOLD):
            chars.append(" " if _CONTROL.match(char) else char)
            positions.append(start)
    return "".join(chars), positions


def find_width_folded(text: Any, quote: Any) -> tuple[int, int] | None:
    """``fold_width`` の形で空白を除いた写しが ``text`` にあれば、最初に現れる原文の範囲 (start, end) を返します。

    比べ方は「表記の幅と空白だけを同一視する」引用の写しの照合と同じで、意味の近さ・部分の一致には広げない。
    範囲は文字の塊（基底の文字と結合文字・半角の濁点）の単位で原文へ戻す。
    """
    target = "".join(fold_width(quote).split())
    value = str(text or "")
    if not target or not value:
        return None
    chars: list[str] = []
    bounds: list[tuple[int, int]] = []
    for start, cluster in _clusters(value):
        for char in fold_width(cluster):
            if not char.isspace():
                chars.append(char)
                bounds.append((start, start + len(cluster)))
    at = "".join(chars).find(target)
    if at < 0:
        return None
    start, end = bounds[at][0], bounds[at + len(target) - 1][1]
    # 塊ごとの形が全体の形と違う稀な場合は、範囲を返さない（照合と同じ形で確かめた範囲だけを返す）。
    return (start, end) if target in "".join(fold_width(value[start:end]).split()) else None


def fold_model_view(value: Any) -> Any:
    """dict / list / tuple の中の文字列（dict の文字列の key を含む）を ``fold_width`` でそろえた写しを返します。

    key もそろえるのは、値の側の名前（機能の見出し）と、それを key にした dict（機能ごとの本文）を同じ形で
    結び付けられるようにするため。文字列以外の値は変えない。
    """
    if isinstance(value, str):
        return fold_width(value)
    if isinstance(value, Mapping):
        return {fold_width(key) if isinstance(key, str) else key: fold_model_view(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [fold_model_view(item) for item in value]
    return value


__all__ = ["SYMBOL_FOLD", "find_width_folded", "fold_model_view", "fold_text", "fold_text_with_positions", "fold_width"]
