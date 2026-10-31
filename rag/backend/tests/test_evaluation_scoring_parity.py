"""RAG の評価が使う platform の採点の部品（`pr_evaluation_scoring`。#3240）と RAG の定義の一致。

採点の部品を platform へ出したとき、RAG 自身の評価の結果が変わらないことを確かめる。

- 表の行の照合: RAG の表の構造の解析（`rag_engine.parsing.table_structure`。取り込みの表の行と
  同じ）と、platform の `table_row_texts` が同じ行の本文を返す（照合は空白を無視するので、正規化
  した形で比べる）。
- 対応の語彙: RAG の評価の `EvaluationOutcome` と platform の `OUTCOMES` が同じ。
"""

from __future__ import annotations

from typing import get_args

import pytest
from pr_evaluation_scoring import OUTCOMES, normalize_for_match, table_row_texts
from rag_engine.parsing.table_structure import parse_html_table_structure

from app.rag.evaluation_handling import EVALUATION_OUTCOMES
from app.schemas.evaluation import EvaluationOutcome

TABLES = {
    "simple": "<table><tr><th>項目</th><th>値</th></tr><tr><td>上限</td><td>30</td></tr></table>",
    "omitted-end-tags": "<table><tr><td>A<td>B<tr><td>C<td>D</table>",
    "nested": (
        "<table><tr><td>外<table><tr><td>内 1</td><td>内 2</td></tr><tr><td>内 3</td></tr>"
        "</table></td><td>右</td></tr></table>"
    ),
    "caption-and-br": (
        "<table><caption>見出し</caption><tr><td>1 行目<br>続き</td><td>&amp; 記号</td></tr>"
        "</table>"
    ),
    "spans": (
        '<table><tr><td rowspan="2">結合</td><td colspan="2">横</td></tr>'
        "<tr><td>x</td><td>y</td></tr></table>"
    ),
    "two-tables-and-text": (
        "前<table><tr><td>a</td></tr></table>間<table><tr><td>b</td><td>c</td></tr></table>後"
    ),
    "empty-rows": "<table><tr></tr><tr><td></td></tr><tr><td>値</td></tr></table>",
    "upper-case-tags": "<TABLE><TR><TD>大文字</TD><TD>タグ</TD></TR></TABLE>",
}


def _rag_rows(html: str) -> list[str]:
    structure = parse_html_table_structure(html)
    return [
        normalize_for_match(" ".join(cell["text"] for cell in row["cells"]))
        for row in structure.get("rows", [])
    ]


@pytest.mark.parametrize("html", TABLES.values(), ids=list(TABLES))
def test_table_rows_match_the_rag_table_structure(html: str) -> None:
    assert [normalize_for_match(text) for text in table_row_texts(html)] == _rag_rows(html)


def test_outcome_vocabulary_is_the_platform_vocabulary() -> None:
    assert tuple(get_args(EvaluationOutcome)) == OUTCOMES
    assert frozenset(OUTCOMES) == EVALUATION_OUTCOMES
