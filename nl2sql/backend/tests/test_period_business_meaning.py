"""期間の意味の確認と、日時型として安全に扱えるかを分離する。"""

from __future__ import annotations

from datetime import date

import pytest

from app.features.nl2sql.ontology_models import (
    QuerySession,
    RequirementItem,
    RequirementPeriod,
    RequirementPhase,
    RequirementSection,
    RequirementState,
    RequirementStatus,
    SqlRequirementDraft,
)
from app.features.nl2sql.sql_requirement import (
    ColumnInfo,
    LlmRequirementOutput,
    RequirementContext,
    TableInfo,
    _cap_open_questions,
    apply_answer,
    build_draft,
    build_requirement_state,
    enforce_requirement_rules,
)


def context(question: str, data_type: str = "VARCHAR2") -> RequirementContext:
    key = "APP.TABLE_A"
    column = ColumnInfo(
        key=key + ".TIME_A",
        display="TABLE_A.TIME_A",
        name="TIME_A",
        logical_name="受付日時",
        data_type=data_type,
        nullable=True,
        comment="処理を受け付けた日時",
        domain_name="",
    )
    table = TableInfo(
        key=key,
        owner="APP",
        name="TABLE_A",
        logical_name="対象データ",
        comment="",
        table_type="TABLE",
        columns={column.key: column},
    )
    return RequirementContext(
        question=question,
        tables={key: table},
        relationships=[],
        profile_context={},
        ontology_concepts=[],
        evidence_corpus="",
        fiscal_year_start=None,
        today=date(2026, 10, 1),
        table_lookup={key: key, "TABLE_A": key},
        column_lookup={column.key: key, "TABLE_A.TIME_A": key, "TIME_A": key},
    )


def draft(question: str, column: str = "") -> SqlRequirementDraft:
    return SqlRequirementDraft(
        question_original=question,
        items=[
            RequirementItem(
                id="from-a",
                key="from_a",
                section=RequirementSection.FROM,
                label_ja="対象",
                status=RequirementStatus.CONFIRMED,
                tables=["APP.TABLE_A"],
            ),
            RequirementItem(
                id="select-a",
                key="select_a",
                section=RequirementSection.SELECT,
                label_ja="件数",
                status=RequirementStatus.CONFIRMED,
                value_ja="件数",
                sql_hint="COUNT(*)",
            ),
            RequirementItem(
                id="period-a",
                key="period_a",
                section=RequirementSection.PERIOD,
                label_ja="期間",
                status=RequirementStatus.INFERRED,
                period=RequirementPeriod(
                    expression_ja="今月", relative_kind="this_month", date_column=column
                ),
            ),
        ],
    )


def state(value: SqlRequirementDraft, review_round: int = 0) -> RequirementState:
    return build_requirement_state(
        QuerySession(
            id="s",
            profile_id="p",
            profile_view_id="v",
            ontology_revision_id="r",
            original_question=value.question_original,
            requirement=value,
            requirement_review_round=review_round,
        ),
        max_review_rounds=3,
    )


def test_unknown_date_meaning_asks_business_question_and_cannot_bypass_at_limit() -> None:
    question = "今月の件数"
    result = enforce_requirement_rules(draft(question), context(question))
    item = next(i for i in result.items if i.key == "period_date_column")
    assert "どの出来事・処理の日時" in item.question_ja
    assert "Schema" not in item.question_ja
    assert "列名" not in item.question_ja
    assert not item.candidates
    assert state(result).status == RequirementPhase.NEEDS_ANSWER
    assert not state(result).can_apply
    assert not state(result, review_round=3).can_apply


@pytest.mark.parametrize(
    "question", ["今月の受付日時を基準とする件数", "今月の受付日時の件数。保存形式はYYYYMMDDです"]
)
def test_known_meaning_does_not_repeat_question_or_guess_text_date_format(question: str) -> None:
    result = enforce_requirement_rules(draft(question, "APP.TABLE_A.TIME_A"), context(question))
    status = state(result)
    assert status.status == RequirementPhase.UNANSWERABLE
    assert not status.can_apply
    assert "安全な日付変換" in status.message_ja
    assert "データ管理者" in status.message_ja
    assert all(not i.question_ja for i in result.items if i.key == "period_date_column")
    period = next(i for i in result.items if i.period)
    assert period.period is not None
    assert period.period.date_column == ""
    assert period.sql_hint == ""
    assert "TO_DATE" not in result.sql_skeleton
    assert "YYYYMMDD" not in result.sql_skeleton


def test_business_answer_is_preserved_and_unresolved_conversion_blocks_generation() -> None:
    question = "今月の件数"
    ctx = context(question)
    initial = enforce_requirement_rules(draft(question), ctx)
    item = next(i for i in initial.items if i.key == "period_date_column")
    result = apply_answer(
        initial, item_id=item.id, selected_candidate_ids=[], free_text="受付日時", context=ctx
    )
    answered = next(i for i in result.items if i.id == item.id)
    assert answered.answered and answered.answer_ja == "受付日時"
    assert state(result).status == RequirementPhase.UNANSWERABLE
    assert not state(result, review_round=3).can_apply
    assert not answered.question_ja
    # 再検査がこの項目を出し忘れても、明示回答を失わず技術名の確認へ戻らない。
    reviewed = build_draft(
        LlmRequirementOutput(
            clarified_question_ja="", items=[], answerable=True, unanswerable_reason_ja=""
        ),
        ctx,
        question=question,
        previous=result,
    )
    assert state(reviewed).status == RequirementPhase.UNANSWERABLE
    assert next(i for i in reviewed.items if i.id == item.id).answer_ja == "受付日時"
    assert not any(i.question_ja for i in reviewed.items if i.key == "period_date_column")


@pytest.mark.parametrize("data_type", ["DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"])
def test_typed_date_resolves_without_business_question(data_type: str) -> None:
    question = "今月の受付日時を基準とする件数"
    value = draft(question)
    period = next(i.period for i in value.items if i.period)
    assert period is not None
    period.expression_ja = "2026-09-01〜2026-09-30"
    period.relative_kind = "other"
    result = enforce_requirement_rules(value, context(question, data_type))
    assert result.unanswerable_reason_ja == ""
    assert not any(i.key == "period_date_column" for i in result.items)
    item = next(i for i in result.items if i.period)
    assert item.period is not None and item.period.date_column == "APP.TABLE_A.TIME_A"
    assert "TABLE_A.TIME_A" in item.sql_hint
    assert state(result).can_apply


def test_no_period_does_not_require_a_date_type() -> None:
    question = "すべての件数"
    value = draft(question)
    value.items = [i for i in value.items if i.period is None]
    result = enforce_requirement_rules(value, context(question))
    assert state(result).can_apply
    assert not result.unanswerable_reason_ja


def test_unresolved_date_binding_is_not_inferred_by_open_question_cap() -> None:
    items = [
        RequirementItem(
            id=f"join-{i}",
            key=f"join_{i}",
            section=RequirementSection.JOIN,
            label_ja="関係の確認",
            status=RequirementStatus.MISSING,
        )
        for i in range(3)
    ]
    binding = RequirementItem(
        id="date-a",
        key="period_date_column",
        section=RequirementSection.PERIOD,
        label_ja="基準の日時",
        status=RequirementStatus.MISSING,
        question_ja="日時の意味を教えてください",
    )
    items.append(binding)
    assert _cap_open_questions(items) == 0
    assert binding.status == RequirementStatus.MISSING
    assert binding.question_ja


@pytest.mark.parametrize("status", [RequirementStatus.MISSING, RequirementStatus.INFERRED])
def test_stale_unanswered_string_binding_cannot_bypass_review_limit(
    status: RequirementStatus,
) -> None:
    question = "今月の件数"
    ctx = context(question)
    dimension = ColumnInfo(
        key="APP.TABLE_A.DIM_A",
        display="TABLE_A.DIM_A",
        name="DIM_A",
        logical_name="分類",
        data_type="NUMBER",
        nullable=True,
        comment="",
        domain_name="",
    )
    ctx.tables["APP.TABLE_A"].columns[dimension.key] = dimension
    ctx.column_lookup[dimension.key] = "APP.TABLE_A"
    value = draft(question, "APP.TABLE_A.TIME_A")
    period_item = next(i for i in value.items if i.period)
    period_item.columns = ["APP.TABLE_A.TIME_A", dimension.key]
    period_item.sql_hint = "TABLE_A.TIME_A >= DATE '2026-10-01'"
    value.items.append(
        RequirementItem(
            id="old-binding",
            key="period_date_column",
            section=RequirementSection.PERIOD,
            label_ja="旧日時",
            status=status,
            columns=["APP.TABLE_A.TIME_A"],
            answered=False,
        )
    )
    result = enforce_requirement_rules(value, ctx)
    binding = next(i for i in result.items if i.key == "period_date_column")
    assert binding.columns == []
    assert binding.status == RequirementStatus.MISSING
    assert "どの出来事・処理の日時" in binding.question_ja
    assert not state(result, review_round=3).can_apply
    period_result = next(i for i in result.items if i.period)
    assert period_result.columns == [dimension.key]
    assert period_result.sql_hint == ""
