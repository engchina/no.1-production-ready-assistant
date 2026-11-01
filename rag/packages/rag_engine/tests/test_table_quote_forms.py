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


# #112 の再現（稼働中の設定で同じ回答・根拠を 30 回監査した 1 回）: 監査の構造化出力が改行を二重に escape し、
# そのときの応答のすべての引用に「\n」の 2 字（逆斜線と n）が残って quote_unmatched になった（run_803ced7b… と同じ形）。
ESCAPED_TABLE_QUOTE = (
    "第3条(出張旅費)\\n<table><tbody><tr><td>区分</td><td>宿泊費の上限(1泊)</td><td>日当</td></tr>"
    "<tr><td>一般社員</td><td>12,000円</td><td>2,000円</td></tr>"
    "<tr><td>課長以上</td><td>15,000円</td><td>3,000円</td></tr></tbody></table>"
)
ESCAPED_HEADER_QUOTE = "株式会社サンプル 勤務・出張・経費規程\\n規程番号 HR-2026-07 / 第3版 / 施行日 2026年10月1日 / 主管部署 人事総務部"


def test_line_breaks_escaped_twice_by_the_model_are_read_as_line_breaks() -> None:
    assert "\\n" in ESCAPED_TABLE_QUOTE and "\n" not in ESCAPED_TABLE_QUOTE
    assert grounded.quote_in_text(ESCAPED_TABLE_QUOTE, TABLE)
    assert grounded.quote_in_text(ESCAPED_HEADER_QUOTE, HEADER)
    assert grounded.quote_in_text("一般社員</td><td>12,000円\\r\\n</td>", TABLE)
    # 読み直すのは改行の escape だけで、値・語の並びの違いは今までどおり通さない。
    assert not grounded.quote_in_text(ESCAPED_TABLE_QUOTE.replace("12,000円", "15,000円", 1), TABLE)
    assert not grounded.quote_in_text("第3条(出張旅費)\\x<table>", TABLE)
    # 原文がその 2 字を持つとき（コードの例など）は、原文の字のまま照合する。
    code = "print('a\\nb')"
    assert grounded.quote_in_text("print('a\\nb')", code)
    assert not grounded.quote_in_text("print('a\nb')", code)


def test_two_evidence_items_joined_with_a_slash_do_not_match() -> None:
    """#112 の再現（21 回のうち 2 回。run_13a17c51… と同じ形）: 監査が 2 つの根拠の本文を「 / 」でつないで 1 つの引用に
    した。斜線は原文の区切りの記号（「／」）と同じ字なので、つなぎの印とは読めない（照合を広げない。監査し直しで直させる）。"""
    joined = (
        "株式会社サンプル 勤務・出張・経費規程\n規程番号 HR-2026-07 / 第3版 / 施行日 2026年10月1日 / 主管部署 人事総務部 / "
        "第3条(出張旅費)\n<table><tbody><tr><td>区分</td><td>宿泊費の上限(1泊)</td><td>日当</td></tr>..."
    )
    assert not grounded.quote_in_text(joined, f"{HEADER}\n{TABLE}")
    # 指示どおり「…」でつなげば照合する。
    assert grounded.quote_in_text(joined.replace(" / 第3条", "…第3条"), f"{HEADER}\n{TABLE}")


# #119 の再現（synthetic-test-5）: 監査が表の離れた行・各 chunk の見出しを「 / 」でつないで写した（行の順も入れ替えた）。
SLASH_TABLE_QUOTE = "第3条(出張旅費) … 課長以上 | 15,000円 | 3,000円 / 一般社員 | 12,000円 | 2,000円"


def test_slash_joined_fragments_match_when_each_fragment_is_verbatim() -> None:
    assert grounded.quote_in_text(SLASH_TABLE_QUOTE, TABLE)
    assert grounded.quote_in_text("課長以上 | 15,000円 | 3,000円 ／ 一般社員 | 12,000円", TABLE)
    # 断片の値を入れ替えた引用・原文に無い断片を含む引用は通さない。
    assert not grounded.quote_in_text(SLASH_TABLE_QUOTE.replace("15,000円", "12,000円"), TABLE)
    assert not grounded.quote_in_text("一般社員 | 12,000円 / 部長 | 20,000円", TABLE)
    # 短いセルを組み替えた引用（区分と値の取り違え）は、各セルが原文にあっても通さない。
    assert not grounded.quote_in_text("一般社員 / 15,000円", TABLE)
    # 区切りの無い斜線（「1泊/2,000円」）は断片の区切りにしない。
    assert not grounded.quote_in_text("一般社員/12,000円/課長以上", TABLE)


def test_slash_joined_quote_across_several_evidence_spans_resolves() -> None:
    from types import SimpleNamespace

    from rag_engine.evaluation import answer_eval

    catalog = {
        "E1": {"id": "c0", "evidence_id": "E1", "text": HEADER},
        "E2": {"id": "c3", "evidence_id": "E2", "text": TABLE},
    }
    claim = SimpleNamespace(evidence_id="E1,E2", evidence_quote="株式会社サンプル 勤務・出張・経費規程 / 第3条(出張旅費)")
    assert [span["id"] for span in answer_eval._spans_for_ids(claim, catalog)] == ["c0", "c3"]
    # どの片段にも無い断片があれば今までどおり結び付けない。
    claim.evidence_quote = "株式会社サンプル 勤務・出張・経費規程 / 第9条(社員食堂)"
    assert answer_eval._spans_for_ids(claim, catalog) is None
