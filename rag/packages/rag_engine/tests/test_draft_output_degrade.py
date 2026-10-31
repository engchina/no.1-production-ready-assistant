"""完了したが大きすぎる・壊れた回答文の生成の出力の扱い（#3849）を、決定論の応答で確かめる。

- 同じ内容の計画・説明の繰り返し（長い出力でモデルが計画を上限まで繰り返す）は、検証の前に最初の 1 件へ寄せ、
  寄せた数を診断に残す。内容の違う entry は落とさず、上限を超えれば従来どおり不合格。
- 1 組目の生成の出力が再実行の後も契約を満たさなければ、回答全体を失敗にせず、関係する資料の記載を示す。
"""

import json

import pytest
from pydantic import ValidationError
from test_grounded_answer import LIST_TEXT, QUESTION, FakeModel, audit, context, draft, ids, item, record, run

from rag_engine.adapters.oci import InvalidStructuredOutput, LlmRunawayOutput
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedDraft

GOOD = "一覧表を選択し、処理を選択します。"
PLAN_LIMIT = 64
ITEM_LIMIT = 40


def _context():
    ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
    return ctx, ids(ctx)[LIST_TEXT]


def _plan(eid: str, request_id: str = "Q1") -> dict:
    return {
        "request_id": request_id,
        "evidence_scope": [{"evidence_id": eid, "target": "集計表出力", "relation": "direct"}],
        "evidence_ids": [eid],
    }


def _wire_item(text: str, start: int = 1) -> dict:
    return {
        "kind": "operation",
        "request_id": "Q1",
        "text": text,
        "citations": [{"evidence_id": "E1", "line_start": start, "line_end": start}],
    }


def _wire_json(**fields) -> str:
    return json.dumps({"items": [_wire_item(GOOD)], "summary": "", "confidence": "high", **fields}, ensure_ascii=False)


def _oversized_items_error() -> ValidationError:
    """内容の違う説明が上限（40 件）を超える出力の、SDK の検証と同じ例外。"""
    payload = _wire_json(items=[_wire_item(f"合成の手順 {number}", number) for number in range(ITEM_LIMIT + 1)])
    with pytest.raises(ValidationError) as raised:
        generation_draft_wire_model().model_validate_json(payload)
    return raised.value


def test_64_identical_plans_are_collapsed_and_recorded_without_changing_the_schema():
    wire = generation_draft_wire_model()
    parsed = wire.model_validate_json(_wire_json(request_coverage=[_plan("E1")] * PLAN_LIMIT))

    assert [plan.request_id for plan in parsed.request_coverage] == ["Q1"]
    assert parsed._collapsed_duplicates == {"request_coverage": PLAN_LIMIT - 1}
    canonical = canonical_generation_draft(parsed)
    assert len(canonical.request_coverage) == 1
    assert canonical._collapsed_duplicates == {"request_coverage": PLAN_LIMIT - 1}
    # モデルに渡す schema（上限）は変えない。
    assert wire.model_json_schema()["properties"]["request_coverage"]["maxItems"] == PLAN_LIMIT


def test_distinct_entries_are_kept_and_a_distinct_overflow_still_fails():
    wire = generation_draft_wire_model()
    # 内容の違う entry は寄せない（順序も保つ）。
    other = {**_plan("E1"), "evidence_ids": []}
    parsed = wire.model_validate_json(_wire_json(request_coverage=[_plan("E1"), other, _plan("E1"), other]))
    assert [plan.evidence_ids for plan in parsed.request_coverage] == [["E1"], []]
    assert parsed._collapsed_duplicates == {"request_coverage": 2}
    # 上限は寄せた後の件数で判定する。内容の違う entry が上限を超えれば不合格のまま（切り詰めない）。
    distinct = [_plan("E1", f"Q{number}") for number in range(PLAN_LIMIT + 1)]
    with pytest.raises(ValidationError, match="request_coverage"):
        wire.model_validate_json(_wire_json(request_coverage=distinct))
    assert "items" in str(_oversized_items_error())


def test_identical_items_are_collapsed_and_applications_follow_the_kept_item():
    first, second, third = (_wire_item(text, start) for start, text in enumerate(("手順 A", "手順 B", "手順 C"), 1))
    application = {"premises": [], "reasoning": "合成の当てはめ", "result": "合成の結果"}
    parsed = GroundedDraft.model_validate(
        {
            "items": [{**first, "citations": []}, {**second, "citations": []}, {**first, "citations": []}, {**third, "citations": []}],
            "applications": [{**application, "item_indexes": [2, 3]}, {**application, "item_indexes": [0, 2]}],
            "summary": "",
            "confidence": "high",
        }
    )
    assert [entry.text for entry in parsed.items] == ["手順 A", "手順 B", "手順 C"]
    assert [entry.item_indexes for entry in parsed.applications] == [[0, 2], [0]]
    assert parsed._collapsed_duplicates == {"items": 1}


def test_duplicated_plan_answer_keeps_one_plan_and_records_the_collapse():
    ctx, eid = _context()
    duplicated = {**draft(item(eid, GOOD, GOOD, request_id="Q1")), "request_coverage": [_plan(eid)] * PLAN_LIMIT}
    model = FakeModel([duplicated], [audit((0, "supported", "matched", ""))])

    result = run(model, ctx).response

    assert GOOD in result.answer_text
    trace = result.generation_trace
    assert len(trace["grounded_draft"]["request_coverage"]) == 1
    output = trace["rounds"][0]["generation_citation_trace"]["outputs"][0]
    assert output["collapsed_duplicates"] == {"request_coverage": PLAN_LIMIT - 1}
    assert len(output["canonical_output"]["request_coverage"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        _oversized_items_error,
        lambda: LlmRunawayOutput("合成の反復", "runaway_repetition", None),
        lambda: InvalidStructuredOutput("生成文に入力にはない Unicode の置換文字が含まれています（欄: items[0].text）。"),
    ],
    ids=["oversized_items", "runaway_repetition", "replacement_character"],
)
def test_invalid_first_generation_shows_related_quotes_instead_of_failing(failure):
    ctx, _eid = _context()
    error = failure()

    def broken(prompt):
        raise error

    model = FakeModel([broken], [audit()])
    result = run(model, ctx).response

    # 回答全体を失敗にせず、関係する資料の記載を原文のみ提示（適用は未確認）で示す条件つきの回答にする。
    assert result.answer_text.startswith(grounded.answer_labels().neutral_summary)
    assert not result.answer_text.startswith(grounded.answer_labels().deadline_summary)
    assert result.envelope["outcome"] == "conditional"
    assert [entry["applies"] for entry in result.envelope["items"]] == ["quote_only"]
    assert GOOD in result.answer_text
    assert result.confidence == "low"
    assert result.needs_human_review
    trace = result.generation_trace
    assert trace["generation_failure"] == {"error": type(error).__name__, "stage": "answer.generate"}
    assert trace["rounds"][0]["error"] == type(error).__name__
    assert "stop" not in trace["round_budget"]
    # 監査は呼ばない（草稿が無い）。
    assert [name for name, _ in model.prompts] == ["GroundedDraftWire"]


def test_invalid_correction_round_keeps_the_adopted_answer_and_names_the_original_error():
    ctx, eid = _context()
    first = draft(item(eid, GOOD, GOOD), item(eid, "CSVを保存先へ出力します。", "CSVを C:\\temp へ出力します。"))
    error = _oversized_items_error()

    def broken(prompt):
        raise error

    model = FakeModel([first, broken], [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""))])
    result = run(model, ctx).response

    assert GOOD in result.answer_text
    assert "generation_failure" not in result.generation_trace
    assert result.generation_trace["rounds"][-1]["error"] == "ValidationError"
    warnings = [row for row in result.envelope["answer_warnings"] if row["code"] == "correction_failed"]
    assert [row["detail"] for row in warnings] == ["ValidationError"]


def test_invalid_plan_repair_keeps_the_draft_before_the_repair():
    ctx, eid = _context()
    # 対象の照合の無い計画（作り直しの対象の不整合）。
    unscoped = {**draft(item(eid, GOOD, GOOD, request_id="Q1")), "request_coverage": [{**_plan(eid), "evidence_scope": []}]}
    error = _oversized_items_error()

    def broken(prompt):
        raise error

    model = FakeModel([unscoped, broken], [audit((0, "supported", "matched", ""))])
    current = grounded.run_round(
        QUESTION,
        _grounded_spans(QUESTION, ctx, (), None),
        None,
        prompt="p",
        image_paths=[],
        provider_id=None,
        parse_text=model,
        parse_images=model,
        invalid_output=lambda exc: isinstance(exc, ValidationError | InvalidStructuredOutput),
    )

    # 作り直す前の説明を捨てず、残った不整合は記録して印の判定に回す。
    assert [entry.item.text for entry in current.checked if entry.item.text == GOOD]
    assert current.timing["request_plan_repair"] == {"calls": 1, "scope": "request_plan_and_draft", "error": "ValidationError"}
    assert current.timing["request_plan_unsettled"]
    assert [name for name, _ in model.prompts][:2] == ["GroundedDraftWire", "GroundedDraftWire"]
