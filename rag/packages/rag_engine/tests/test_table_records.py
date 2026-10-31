"""1 行の記録の根拠を同じ表に組み直し、表の対応の検査で読む（#2594）。根拠・文は合成。"""

from typing import Any

import pytest

from rag_engine.generation.table_binding import unbound_table_values
from rag_engine.generation.table_records import record_tables, record_values, table_record

CELLS = [("A", ["区分"]), ("B", ["方式"]), ("C", ["上限値"])]
RECORDS = {
    2: "区分: 区分 A / 方式: 方式 X / 上限値: 120",
    3: "区分: 区分 B / 方式: 方式 Y / 上限値: 300",
    4: "区分: 区分 C / 方式: 方式 Y / 上限値: 45",
}


def _items(records: dict[int, str], cells: list[tuple[str, list[str]]] = CELLS, group: str = "g1") -> list[dict[str, Any]]:
    return [{"id": f"c{row}", "text": text, "table_record": {"group": group, "row": row, "cells": cells}} for row, text in records.items()]


def test_record_values_follow_the_header_path_of_each_cell() -> None:
    assert record_values(RECORDS[2], CELLS) == ["区分 A", "方式 X", "120"]
    # 複数段の表頭（列名に区切りを含む）・補った列名（同じ名前の列）・役割の表示を付けた列名。
    cells = [("A", ["項目"]), ("B", ["前期", "下限"]), ("C", ["前期", "上限"])]
    assert record_values("項目: 項目 Q / 前期 / 下限: 30 / 前期 / 上限: 40", cells) == ["項目 Q", "30", "40"]
    assert record_values("名称: 名称 s2 / 値: 8 / 値_1: 80", [("A", ["名称"]), ("B", ["値"]), ("C", ["値"])]) == ["名称 s2", "8", "80"]
    assert record_values("既定値［資料の既定値］: 5 / 名前: 項目甲", [("A", ["既定値"]), ("B", ["名前"])]) == ["5", "項目甲"]
    # 値の中の区切りも、表頭の段と合う分け方が 1 通りなら値に残す。
    assert record_values("区分: 区分 A / 備考: 甲 / 乙", [("A", ["区分"]), ("B", ["備考"])]) == ["区分 A", "甲 / 乙"]


@pytest.mark.parametrize(
    ("text", "cells"),
    [
        # 分け方が 2 通り（値「x / 列: y」と、2 つ目のセル）。
        ("列: a / 列: b / 列: c", [("A", ["列"]), ("B", ["列"])]),
        # 表頭の段と列名が合わない。
        ("区分: 区分 A / 名前: 項目甲", [("A", ["区分"]), ("B", ["方式"])]),
        # セルの数より部分が少ない・前に別の文がある。
        ("区分: 区分 A", [("A", ["区分"]), ("B", ["方式"])]),
        ("見出し\n区分: 区分 A / 方式: 方式 X", [("A", ["区分"]), ("B", ["方式"])]),
    ],
    ids=["ambiguous", "header-mismatch", "too-few", "prefix"],
)
def test_record_values_are_not_guessed(text: str, cells: list[tuple[str, list[str]]]) -> None:
    assert record_values(text, cells) is None


def test_records_of_the_same_table_become_one_table_in_row_order() -> None:
    items = list(reversed(_items(RECORDS)))
    # 別の表の根拠・1 行だけの表・位置の無い根拠は組み直さない。
    items += _items({5: "区分: 区分 Z / 方式: 方式 Z / 上限値: 9"}, group="g2")
    items.append({"id": "x", "text": "区分 B の上限値は 45 です。"})
    tables = record_tables(items)
    assert tables == [
        "<table>\n<tr><th>区分</th><th>方式</th><th>上限値</th></tr>\n"
        "<tr><td>区分 A</td><td>方式 X</td><td>120</td></tr>\n"
        "<tr><td>区分 B</td><td>方式 Y</td><td>300</td></tr>\n"
        "<tr><td>区分 C</td><td>方式 Y</td><td>45</td></tr>\n</table>"
    ]


def test_check_finds_values_bound_across_records_only_with_rebuilt_table() -> None:
    """別の行の記録の値を結び付けた文は、組み直した表で見つかる。正しい文・列挙は疑わない。"""
    items = _items(RECORDS)
    texts = [item["text"] for item in items]
    rebuilt = texts + record_tables(items)
    wrong = "区分 B の上限値は 45 です。"
    # 根拠ごとの本文（1 行ずつ）では表として読めない。
    assert unbound_table_values(wrong, texts) == []
    assert unbound_table_values(wrong, rebuilt) == [("45", "区分 B")]
    assert unbound_table_values("区分 B の上限値は 300 です。", rebuilt) == []
    assert unbound_table_values("区分 A は 120、区分 C は 45 です。", rebuilt) == []


def test_rows_without_the_label_cell_are_left_out() -> None:
    """区分の列が空の行（縦の結合の続きと空欄を見分けられない）は入れず、上の区分と結び付けた正しい文を疑わない。"""
    cells = [("A", ["分類"]), ("B", ["名称"]), ("C", ["番号"])]
    items = _items({2: "分類: 分類 K / 名称: 名称 k1 / 番号: 101", 4: "分類: 分類 L / 名称: 名称 l1 / 番号: 201"}, cells)
    items += _items({3: "名称: 名称 k2 / 番号: 102"}, [("B", ["名称"]), ("C", ["番号"])])
    rebuilt = [item["text"] for item in items] + record_tables(items)
    assert "名称 k2" not in rebuilt[-1]
    assert unbound_table_values("分類 K の番号は 102 です。", rebuilt) == []
    assert unbound_table_values("分類 K の番号は 201 です。", rebuilt) == [("201", "分類 K")]


def test_index_column_and_numbers_inside_names_are_not_values() -> None:
    """先頭が行の番号の列なら次の列を区分・名前にし、名前の中の数字（「部品 1」の「1」）を値として読まない。"""
    cells = [("A", ["項番"]), ("B", ["名称"]), ("C", ["数量"]), ("D", ["単価"])]
    rows = {2: ("1", "部品 1", "3", "10"), 3: ("2", "部品 2", "1", "20"), 5: ("4", "部品 4", "3", "40")}
    items = _items({row: f"項番: {a} / 名称: {b} / 数量: {c} / 単価: {d}" for row, (a, b, c, d) in rows.items()}, cells)
    rebuilt = [item["text"] for item in items] + record_tables(items)
    for claim in (
        "部品 1 の単価は 10 です。",
        "部品 1 の数量は 3 です。",
        "部品 2 の数量は 1 です。",
        "部品 1 と部品 4 の数量はどちらも 3 です。",
    ):
        assert unbound_table_values(claim, rebuilt) == [], claim
    assert unbound_table_values("部品 2 の単価は 40 です。", rebuilt) == [("40", "部品 2")]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "g",
        {"group": "", "row": 2, "cells": [["A", ["x"]]]},
        {"group": "g", "row": 0, "cells": [["A", ["x"]]]},
        {"group": "g", "row": True, "cells": [["A", ["x"]]]},
        {"group": "g", "row": 2, "cells": []},
        {"group": "g", "row": 2, "cells": [["a", ["x"]]]},
        {"group": "g", "row": 2, "cells": [["A", "x"]]},
        {"group": "g", "row": 2, "cells": [["A", ["x"]], ["A", ["y"]]]},
    ],
    ids=["none", "string", "empty-group", "zero-row", "bool-row", "no-cells", "lower-column", "path-string", "duplicate-column"],
)
def test_malformed_table_records_are_ignored(raw: Any) -> None:
    assert table_record({"table_record": raw}) is None
