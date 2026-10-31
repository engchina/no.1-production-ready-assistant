"""WHERE 句の分割と条件の読み下しの境界と計算量（#1646）。

CodeQL の多項式 ReDoS（`(.+?)\\s+AND\\s+(.+)$` や毎文字の `text[index:]` への照合）を、
前置きだけの照合と文字列の操作に置き換えた。読み下しの結果は以前と同じに保つ。
"""

from __future__ import annotations

import time

from app.features.nl2sql.logical_steps import _condition_business, _split_conditions


def test_split_conditions_keeps_between_and_and_splits_top_level_connectors() -> None:
    assert _split_conditions("A BETWEEN 1 AND 2 AND B = 3 OR (C = 1 AND D = 2)") == [
        ("", "A BETWEEN 1 AND 2"),
        ("かつ", "B = 3"),
        ("または", "(C = 1 AND D = 2)"),
    ]
    assert _split_conditions("A = 1\n  and\tB = 2") == [("", "A = 1"), ("かつ", "B = 2")]


def test_split_conditions_does_not_treat_identifier_suffix_as_between() -> None:
    # 以前は `text[index:]` の先頭で `\b` が成り立ち、`IN_BETWEEN` を BETWEEN とみなして
    # 次の AND で分けなかった。
    assert _split_conditions("IN_BETWEEN = 1 AND B = 2") == [
        ("", "IN_BETWEEN = 1"),
        ("かつ", "B = 2"),
    ]


def test_condition_business_reads_between_and_like() -> None:
    assert _condition_business("A BETWEEN 1 AND 2", None) == "Aが1〜2の範囲の"
    assert _condition_business("A not  between 'x' and 'y'", None) == "Aがx〜yの範囲外の"
    assert _condition_business("A LIKE '%x%'", None) == "Aがxを含む"
    assert _condition_business("A NOT LIKE '%x%'", None) == "Aがxを含まない"
    # 空の値・改行をまたぐ値は読み下さない（推測で意味を作らない）。
    assert _condition_business("A BETWEEN   AND 2", None) == "指定条件に合う"
    assert _condition_business("A BETWEEN 1\n2 AND 3", None) == "指定条件に合う"
    assert _condition_business("A LIKE '%x\n%'", None) == "指定条件に合う"


def test_split_and_condition_finish_quickly_on_long_whitespace() -> None:
    spaces = " " * 50_000
    started = time.perf_counter()
    assert _split_conditions(f"A{spaces}B") == [("", f"A{spaces}B")]
    assert _condition_business(f"A BETWEEN {spaces}x", None) == "指定条件に合う"
    assert _condition_business(f"A LIKE{spaces}x", None) == "Aがxを含む"
    assert time.perf_counter() - started < 1.0
