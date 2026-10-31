"""質問の欄・管理 SQL・生成 SQL の字句の照合の境界と計算量（#1646）。

CodeQL の多項式 ReDoS を、位置の検索と文字列の操作・1 回の走査に置き換えた。
結果は以前の正規表現と同じに保ち（閉じないコメントだけは docstring どおり拒否側の None）、
悪意のある長い入力でも 1 秒以内に終わることを確かめる。
"""

from __future__ import annotations

import time

from app.features.nl2sql.service import (
    _comment_on_column_ref,
    _ends_with_limit_clause,
    _parse_structured_question_slots,
    _policy_sql_tokens,
    _split_question_slot_line,
    _strip_expression_alias,
)

_LONG_SPACES = " " * 50_000


def test_question_slot_line_splits_at_first_colon() -> None:
    assert _split_question_slot_line("  抽出条件 ： 2024年 : 4月") == ("抽出条件", "2024年 : 4月")
    assert _split_question_slot_line("対象テーブル:") == ("対象テーブル", "")
    assert _split_question_slot_line("区切りが無い行") is None
    assert _split_question_slot_line("   : 値") is None
    assert _split_question_slot_line("あ" * 81 + ": 値") is None

    slots = _parse_structured_question_slots("対象テーブル: ORDERS\n抽出条件：\n  金額が 100 以上")
    assert slots.has_template
    assert slots.slots == {"対象テーブル": "ORDERS", "抽出条件": "金額が 100 以上"}


def test_comment_on_column_ref_reads_target_before_is() -> None:
    assert _comment_on_column_ref("COMMENT ON COLUMN T1.ID IS 'ID'") == "T1.ID"
    assert _comment_on_column_ref("comment  on\ncolumn  S.T.C\n  is 'x'") == "S.T.C"
    assert _comment_on_column_ref("COMMENT ON COLUMN \"A IS\".B IS 'x'") == '"A'
    assert _comment_on_column_ref("COMMENT ON COLUMN T1.ID ISX") is None
    assert _comment_on_column_ref("COMMENT ON TABLE T1 IS 'x'") is None


def test_strip_expression_alias_and_trailing_limit() -> None:
    assert _strip_expression_alias("SUM(AMOUNT)  AS  TOTAL") == "SUM(AMOUNT)"
    assert _strip_expression_alias("CASTER") == "CASTER"
    assert _ends_with_limit_clause("SELECT * FROM T LIMIT 10")
    assert _ends_with_limit_clause("SELECT * FROM T\nlimit  5 ; \n")
    assert not _ends_with_limit_clause("SELECT * FROM T LIMIT 5;;")
    assert not _ends_with_limit_clause("SELECT LIMIT FROM T")


def test_policy_sql_tokens_handles_quotes_and_comments() -> None:
    statement = "ALTER TABLE \"A\"\"B\" /* c */ MODIFY (x ANNOTATIONS (k 'it''s')) -- tail\n;"
    assert _policy_sql_tokens(statement) == [
        *["ALTER", "TABLE", '"A""B"', "MODIFY", "(", "x"],
        *["ANNOTATIONS", "(", "k", "'it''s'", ")", ")"],
    ]
    assert _policy_sql_tokens("SELECT 'open") is None
    assert _policy_sql_tokens('SELECT "open') is None
    # 閉じないコメントは、以前の `/` と `*` の字句ではなく拒否側の None にする。
    assert _policy_sql_tokens("ALTER TABLE T /* open") is None


def test_text_scanning_finishes_quickly_on_malicious_input() -> None:
    started = time.perf_counter()
    assert _split_question_slot_line(f"{_LONG_SPACES}x") is None
    assert _comment_on_column_ref(f"COMMENT ON COLUMN a{_LONG_SPACES}x") is None
    assert _strip_expression_alias(f"a{_LONG_SPACES}x") == "a"
    assert not _ends_with_limit_clause(f"a{_LONG_SPACES}x")
    assert _policy_sql_tokens("/*a" * 20_000) is None
    assert _policy_sql_tokens('"' + '""' * 20_000) is None
    assert time.perf_counter() - started < 1.0
