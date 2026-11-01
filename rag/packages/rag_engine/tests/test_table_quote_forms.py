"""表の根拠の引用の照合（#108）。

最終の検証（`rag_validate_answer`）の監査モデルは、HTML の表の根拠を、tag のまま・Markdown の表に写して・
全角を半角にそろえて引用する。どれも原文の語の並びのままなら照合を通し、値・行の対応が違う引用は今までどおり
通さない（同じ表の区分と値の取り違えを照合で見逃さない）。根拠は合成の再試験の「社内規程サンプル.pdf」の第3条の表。
"""

import pytest

from rag_engine.generation import grounded

TABLE = (
    "第3条（出張旅費）\n<table><tbody><tr><td>区分</td><td>宿泊費の上限（1泊）</td><td>日当</td></tr>"
    "<tr><td>一般社員</td><td>12,000円</td><td>2,000円</td></tr>"
    "<tr><td>課長以上</td><td>15,000円</td><td>3,000円</td></tr></tbody></table>"
)
HEADER = "株式会社サンプル 勤務・出張・経費規程\n規程番号 HR-2026-07 ／ 第3版 ／ 施行日 2026年10月1日 ／ 主管部署 人事総務部"


@pytest.mark.parametrize(
    "quote",
    [
        # 実際の監査の引用（全角の括弧・斜線を半角にそろえ、tag のまま写した表）。
        "第3条(出張旅費)\n<table><tbody><tr><td>区分</td><td>宿泊費の上限(1泊)</td><td>日当</td></tr>"
        "<tr><td>一般社員</td><td>12,000円</td><td>2,000円</td></tr></tbody></table>",
        "| 一般社員 | 12,000円 | 2,000円 |",
        "| 区分 | 宿泊費の上限(1泊) | 日当 |\n| 一般社員 | 12,000円 | 2,000円 |",
        # Markdown の表の区切りの行は、HTML の tag と同じく表の形だけ（語を持たない）。
        "| 区分 | 宿泊費の上限(1泊) | 日当 |\n|---|---|---|\n| 一般社員 | 12,000円 | 2,000円 |",
        "区分 | 宿泊費の上限(1泊) | 日当\n:---|---:|:---:\n一般社員 | 12,000円 | 2,000円",
    ],
)
def test_table_quotes_in_the_original_order_match(quote: str) -> None:
    assert grounded.quote_in_text(quote, TABLE)


@pytest.mark.parametrize(
    "quote",
    [
        # 別の行の値（課長以上の宿泊費）を一般社員の行に写した引用。
        "| 区分 | 宿泊費の上限(1泊) | 日当 |\n|---|---|---|\n| 一般社員 | 15,000円 | 2,000円 |",
        "| 一般社員 | 12,000円 | 3,000円 |",
        # 列の見出しと値を組み替えた言い換え（原文の語の並びではない）。
        "区分: 一般社員 / 宿泊費の上限(1泊): 12,000円",
        # 区切りの行ではない単独の「---」は文の記号として残す。
        "一般社員 12,000円\n---\n課長以上 15,000円",
    ],
)
def test_table_quotes_with_other_values_or_order_do_not_match(quote: str) -> None:
    assert not grounded.quote_in_text(quote, TABLE)


def test_header_quote_matches_with_folded_slashes_only() -> None:
    assert grounded.quote_in_text("規程番号 HR-2026-07 / 第3版 / 施行日 2026年10月1日", HEADER)
    # 区切りの斜線を落とした引用は語の並びが変わるので通さない（照合を同値の外に広げない）。
    assert not grounded.quote_in_text("規程番号 HR-2026-07 第3版 施行日 2026年10月1日", HEADER)
