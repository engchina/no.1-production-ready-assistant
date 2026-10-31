"""回答前の確認の会話に残す内容（#1839）。

チャットの回答の利用者の側に「回答前に確認した内容」（利用者が答えた・直した項目）と、確認を
飛ばしたことを残す。ジョブの要件（`sql_requirement`）から作り、生成は変えない。
"""

from __future__ import annotations

from app.features.nl2sql.models import (
    JobCreateRequest,
    RequirementAnswerRecord,
    SqlRequirementSpec,
    SqlRequirementSpecItem,
)


def _spec() -> SqlRequirementSpec:
    return SqlRequirementSpec(
        session_id="s1",
        profile_id="p1",
        clarified_question="APP.ORDERS の今月の件数",
        tables=["APP.ORDERS"],
        items=[
            SqlRequirementSpecItem(
                section="from",
                section_label_ja="使用テーブル",
                label_ja="対象の表",
                value_ja="APP.ORDERS",
                status="confirmed",
            ),
            SqlRequirementSpecItem(
                section="period",
                section_label_ja="期間",
                label_ja="集計の期間",
                value_ja="今月",
                status="answered",
            ),
            SqlRequirementSpecItem(
                section="other",
                section_label_ja="その他",
                label_ja="",
                value_ja="キャンセルを除く",
                status="answered",
            ),
            SqlRequirementSpecItem(
                section="where", label_ja="絞り込み", value_ja="", status="answered"
            ),
        ],
    )


def test_requirement_answers_are_the_items_the_user_answered() -> None:
    request = JobCreateRequest(question="今月の注文は？", sql_requirement=_spec())

    assert request.requirement_answers == [
        RequirementAnswerRecord(prompt="集計の期間", answer="今月"),
        RequirementAnswerRecord(prompt="その他", answer="キャンセルを除く"),
    ]


def test_requirement_answers_are_empty_without_requirement() -> None:
    request = JobCreateRequest(question="今月の注文は？", requirement_skipped=True)

    assert request.requirement_answers == []
    assert request.requirement_skipped is True


def test_stored_request_without_skip_flag_reads_as_not_skipped() -> None:
    stored = JobCreateRequest.model_validate({"question": "今月の注文は？"})

    assert stored.requirement_skipped is False
