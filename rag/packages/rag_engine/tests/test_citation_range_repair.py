"""整数引用の修復は範囲だけを変更し、本文の正しさや意味支持を認定しない。"""

import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError
from test_cite_lines import LONG, SHORT

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.citation_range_repair import CitationRangeRepair, apply_range_repair
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedDraft
from rag_engine.resources.runtime import resolve_prompt

QUESTION = "対象を選択して確定する方法は？"
CLAIM = "一覧から対象を選択します。"


def proposal(start: int | None = 0, end: int | None = 0, *, second: bool = False) -> GroundedDraft:
    citations = [{"evidence_id": "E1", "line_start": start, "line_end": end}]
    if second:
        citations.append({"evidence_id": "E2", "line_start": 0, "line_end": 0})
    wire = generation_draft_wire_model().model_validate(
        {
            "items": [
                {
                    "kind": "operation",
                    "request_id": "Q1",
                    "text": CLAIM,
                    "applies": "conditional",
                    "condition": "対象が選択済みの場合",
                    "citations": citations,
                }
            ],
            "summary": "対象が選択済みなら操作を案内します。",
            "confidence": "low",
        }
    )
    return canonical_generation_draft(wire)


def target(index: int = 0, *, identity: str = "E1", count: int = 5, numbered: bool = True) -> dict[str, Any]:
    return {"item_index": 0, "citation_index": index, "evidence_id": identity, "unit_count": count, "numbered": numbered}


def response(start: int | None, end: int | None, *, index: int = 0, identity: str = "E1") -> CitationRangeRepair:
    return CitationRangeRepair.model_validate(
        {"ranges": [{"item_index": 0, "citation_index": index, "evidence_id": identity, "line_start": start, "line_end": end}]}
    )


def evidence(
    text: str = LONG, *, identity: str = "stable-a", source: str = "source-a", origin: str = "document_text"
) -> list[dict[str, Any]]:
    spans = [
        {
            "evidence_id": identity,
            "label": "E1",
            "source_id": source,
            "source": "synthetic.pdf",
            "page": 1,
            "text": text,
            "origin": origin,
            "function": ("synthetic.pdf", "合成操作"),
            "tags": [],
        }
    ]
    grounded.format_functions(spans)
    return spans


@pytest.mark.parametrize(
    "start,end,reason", [(0, 0, "citation_range_out_of_bounds"), (3, 2, "citation_range_invalid"), (1, 999, "citation_range_out_of_bounds")]
)
def test_invalid_integer_range_stays_invalid_without_clamping_or_silent_recovery(start: int, end: int, reason: str) -> None:
    draft = proposal(start, end)
    spans = evidence()
    cited, error = grounded.cited_quote(draft.items[0], spans)
    assert error and not cited.quote
    assert grounded._citation_extraction(cited)["reason_code"] == reason
    repaired, diagnostic = apply_range_repair(draft, response(start, end), [target(count=len(grounded.citation_units(LONG)))])
    assert repaired.model_dump() == draft.model_dump()
    assert repaired.items[0].citations[0]._wire_range == (start, end)
    assert diagnostic["status"] == "partial" and diagnostic["rejected"][0]["reason_code"] == reason


def test_success_changes_only_the_target_range_and_drops_its_stale_proof() -> None:
    draft = proposal(second=True)
    draft.items[0].citations[0]._extraction = {"status": "failed", "reason_code": "citation_range_out_of_bounds"}
    before = deepcopy(draft.model_dump())
    repaired, diagnostic = apply_range_repair(draft, response(2, 3), [target()])
    expected = deepcopy(before)
    expected["items"][0]["citations"][0]["lines"] = "2-3"
    assert repaired.model_dump() == expected
    assert draft.model_dump() == before and repaired is not draft
    assert repaired.items[0].citations[0]._wire_range == (2, 3)
    assert repaired.items[0].citations[0]._extraction == {}
    assert repaired.items[0].citations[1]._wire_range == (0, 0)
    assert diagnostic["scope"] == "citation_range_only" and diagnostic["status"] == "completed"


def test_range_from_a_longer_other_block_does_not_apply_to_the_shorter_target_block() -> None:
    draft = proposal(second=True)
    raw = {
        "ranges": [
            {"item_index": 0, "citation_index": 0, "evidence_id": "E1", "line_start": 3, "line_end": 3},
            {"item_index": 0, "citation_index": 1, "evidence_id": "E2", "line_start": 3, "line_end": 3},
        ]
    }
    repaired, diagnostic = apply_range_repair(draft, CitationRangeRepair.model_validate(raw), [target(), target(1, identity="E2", count=2)])
    assert repaired.items[0].citations[0]._wire_range == (3, 3)
    assert repaired.items[0].citations[1]._wire_range == (0, 0)
    assert diagnostic["rejected"] == [{"item_index": 0, "citation_index": 1, "reason_code": "citation_range_out_of_bounds"}]
    assert diagnostic["status"] == "partial"


def test_numbered_unit_includes_unnumbered_physical_newlines() -> None:
    # 画像からの生成説明・OCR の根拠は、v5 でも短い行を 1 単位にまとめる（番号の無い改行を含む）。
    text = "画面を開きます。\n対象を選びます。\n" + "補足説明。" * 10 + "\n" + LONG
    units = grounded.citation_units(text, image_text=True)
    start, end = units[0]
    assert "\n" in text[start:end]
    rendered = grounded.numbered_text(text)
    assert rendered.startswith("1| ") and "\n対象を選びます。" in rendered
    draft, diagnostic = apply_range_repair(proposal(), response(1, 1), [target(count=len(units))])
    cited, error = grounded.cited_quote(draft.items[0], evidence(text, origin="image_extraction"))
    assert not error and cited.quote == text[start:end]
    assert diagnostic["status"] == "completed"


def test_short_unnumbered_multiline_block_uses_null_for_whole_text() -> None:
    # 旧形式の未番号根拠を、現在の生成表示へ変換せず検証する。
    spans = [dict(span) for span in evidence(SHORT)]
    assert grounded.numbered_text(SHORT) == SHORT and "\n" in SHORT
    repaired, diagnostic = apply_range_repair(
        proposal(), response(None, None), [target(count=len(grounded._legacy_citation_units(SHORT)), numbered=False)]
    )
    cited, error = grounded.cited_quote(repaired.items[0], spans)
    assert not error and cited.quote == SHORT and repaired.items[0].citations[0]._wire_range == (None, None)
    assert diagnostic["status"] == "completed"


def test_one_one_on_an_unnumbered_single_unit_block_is_the_whole_text_like_null() -> None:
    """番号なしで単位 1 つの根拠では (1,1) も null と同じ全文の指定として受け、通常の照合と同じ結果にする（#3778）。"""
    spans = [dict(span) for span in evidence(SHORT)]
    assert len(grounded._legacy_citation_units(SHORT)) == 1
    repaired, diagnostic = apply_range_repair(proposal(), response(1, 1), [target(count=1, numbered=False)])
    assert diagnostic["status"] == "completed" and not diagnostic["rejected"]
    citation = repaired.items[0].citations[0]
    assert (citation.lines, citation._wire_range) == ("1", (1, 1))
    cited, error = grounded.cited_quote(repaired.items[0], spans)
    extraction = grounded._citation_extraction(cited)
    assert not error and cited.quote == SHORT
    assert (extraction["quote_origin"], extraction["requested_range_status"]) == ("whole_text", "not_required")
    assert repaired.items[0].text == CLAIM


@pytest.mark.parametrize(
    "start,end,count",
    [(0, 0, 1), (0, 1, 1), (2, 2, 1), (3, 5, 1), (1, 2, 1), (1, None, 1), (3, 2, 3), (1, 1, 3), (1, 3, 3)],
    ids=[
        "zero",
        "zero-start",
        "out-of-bounds",
        "out-of-bounds-range",
        "past-end",
        "half-null",
        "reversed",
        "multi-unit-one",
        "multi-unit-all",
    ],
)
def test_other_integers_on_unnumbered_blocks_stay_rejected_without_clamping(start: int, end: int | None, count: int) -> None:
    draft = proposal()
    repaired, diagnostic = apply_range_repair(draft, response(start, end), [target(count=count, numbered=False)])
    assert diagnostic["status"] == "partial" and not diagnostic["adopted"]
    assert diagnostic["rejected"] == [{"item_index": 0, "citation_index": 0, "reason_code": "citation_range_invalid"}]
    # 拒んだ指定は採らず、元の範囲・本文をそのまま残す。
    assert repaired.model_dump() == draft.model_dump() and repaired.items[0].citations[0]._wire_range == (0, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "別の操作に変更"),
        ("kind", "rule"),
        ("request_id", "Q2"),
        ("applies", "matched"),
        ("condition", "条件を削除"),
        ("summary", "無条件に成功"),
    ],
)
def test_repair_schema_rejects_any_body_or_applicability_edit(field: str, value: str) -> None:
    draft = proposal()
    before = draft.model_dump()
    raw = response(2, 2).model_dump()
    raw["ranges"][0][field] = value
    with pytest.raises(ValidationError):
        CitationRangeRepair.model_validate(raw)
    assert draft.model_dump() == before


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_repairs_are_rejected_without_last_wins_and_other_target_survives(reverse: bool) -> None:
    rows = [response(1, 1).ranges[0], response(2, 2).ranges[0]]
    if reverse:
        rows.reverse()
    rows.append(response(1, 1, index=1, identity="E2").ranges[0])
    repaired, diagnostic = apply_range_repair(
        proposal(second=True), CitationRangeRepair(ranges=rows), [target(), target(1, identity="E2", count=2)]
    )
    assert repaired.items[0].citations[0]._wire_range == (0, 0)
    assert repaired.items[0].citations[1]._wire_range == (1, 1)
    assert [row["reason_code"] for row in diagnostic["rejected"]] == ["duplicate_target", "duplicate_target"]


def test_missing_unknown_or_rebound_target_does_not_modify_the_original_citation() -> None:
    draft = proposal()
    for repair, reason in [
        (CitationRangeRepair(ranges=[]), "missing_target"),
        (response(1, 1, index=9), "unknown_target"),
        (response(1, 1, identity="E2"), "evidence_id_changed"),
    ]:
        repaired, diagnostic = apply_range_repair(draft, repair, [target()])
        assert repaired.model_dump() == draft.model_dump()
        assert diagnostic["adopted"] == []
        assert reason in {row["reason_code"] for row in diagnostic["rejected"]}


def run_first(spans: list[dict[str, Any]], draft: GroundedDraft | None = None) -> grounded.Round:
    def generation(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        assert schema is generation_draft_wire_model()
        return draft if draft is not None else proposal()

    return grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=generation,
        parse_images=generation,
        audit_mode="none",
    )


def test_existing_correction_round_uses_one_range_only_call_and_does_not_run_disabled_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    spans = evidence()
    first = run_first(spans)
    original = first.draft.model_dump()
    configured_system = "## Step 1: 指定された引用の範囲だけを修復する"
    original_resolver = resolve_prompt

    def resolve(key: str, fallback: str) -> str:
        return configured_system if key == "generation.citationRepair" else original_resolver(key, fallback)

    monkeypatch.setattr(grounded, "resolve_prompt", resolve)
    calls: list[dict[str, Any]] = []

    def repair(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> CitationRangeRepair:
        assert schema is CitationRangeRepair
        assert system == configured_system
        calls.append(json.loads(prompt.split("\n", 1)[1]))
        return response(3, 3)

    repaired = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="同じ根拠",
        image_paths=(),
        provider_id=None,
        parse_text=repair,
        parse_images=repair,
        previous=first.checked,
        previous_draft=first.draft,
        previous_dropped=first.dropped,
        audit_mode="none",
    )
    assert len(calls) == 1 and repaired.audit_calls == 0
    assert calls[0]["immutable_draft"] == original
    assert calls[0]["targets"][0]["evidence_id"] == "E1"
    assert repaired.draft.items[0].text == first.draft.items[0].text
    assert repaired.draft.items[0].condition == first.draft.items[0].condition
    assert repaired.draft.items[0].citations[0]._wire_range == (3, 3)
    assert repaired.timing["generation_calls"] == 1
    assert repaired.timing["citation_range_repair"]["status"] == "completed"
    assert repaired.review_execution == "not_run"
    captured = repaired.citation_trace["outputs"][0]
    assert captured["parsed_output_representation"] == "citation_range_repair"
    assert captured["input_system_prompt"] == configured_system
    assert captured["raw_output_status"] == "not_recorded"
    assert captured["parsed_output"]["ranges"][0]["line_start"] == 3


@pytest.mark.parametrize("change", ["identity", "text"])
def test_repair_does_not_use_a_rebound_label_or_changed_source_version(change: str) -> None:
    spans = evidence()
    first = run_first(spans)
    changed = evidence(identity="stable-b") if change == "identity" else evidence(text=LONG + "別の補足。")
    assert grounded.citation_range_targets(first.draft, changed, first.checked) == []

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("変更された根拠では修復を呼ばない")

    second = grounded.run_round(
        QUESTION,
        changed,
        get_settings(environ={}, dotenv_path=None),
        prompt="変更後",
        image_paths=(),
        provider_id=None,
        parse_text=unused,
        parse_images=unused,
        previous=first.checked,
        previous_draft=first.draft,
        previous_dropped=first.dropped,
        audit_mode="none",
    )
    assert second.timing["generation_calls"] == 0
    assert second.timing["citation_range_repair"]["status"] == "not_run"
    assert second.draft.model_dump() == first.draft.model_dump()


@pytest.mark.parametrize("reject_body_change", [False, True])
def test_outer_budget_allows_only_one_repair_instead_of_another_full_generation(reject_body_change: bool) -> None:
    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=LONG,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        source_run_id="run",
        chunk_level="parent",
        metadata={"section_path": ["合成操作"]},
    )
    context = AnswerContext(records=[record], text=LONG)
    calls: list[str] = []

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is generation_draft_wire_model():
            return proposal()
        assert schema is CitationRangeRepair
        if reject_body_change:
            raw = response(3, 3).model_dump()
            raw["ranges"][0]["text"] = "修復時に創作した別操作"
            return CitationRangeRepair.model_validate(raw)
        return response(3, 3)

    config = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="none", answer_max_correction_rounds=1)
    with bind_dependencies(AnswerDependencies(unused, unused, parser, unused, unused)):
        result = answering.synthesize_grounded_answer(QUESTION, context, config, image_prompt_mode="text_only")
    assert calls == ["GroundedDraftWire", "CitationRangeRepair"]
    assert result.response.answer_text
    assert CLAIM in result.response.answer_text
    assert "修復時に創作した別操作" not in result.response.answer_text
    if not reject_body_change:
        assert result.response.generation_trace["llm_calls"] == 2


def test_valid_repair_is_retained_but_extra_unknown_target_is_not_reported_fully_completed() -> None:
    ranges = [response(2, 2).ranges[0], response(1, 1, index=9).ranges[0]]
    repaired, diagnostic = apply_range_repair(proposal(), CitationRangeRepair(ranges=ranges), [target()])
    assert repaired.items[0].citations[0]._wire_range == (2, 2)
    assert diagnostic["adopted"] == [{"item_index": 0, "citation_index": 0}]
    assert diagnostic["rejected"] == [{"item_index": 0, "citation_index": 9, "reason_code": "unknown_target"}]
    assert diagnostic["status"] == "partial"


def test_all_invalid_multiple_citations_can_repair_from_dropped_proofs_without_losing_body() -> None:
    spans = evidence()
    spans.append({**spans[0], "evidence_id": "stable-b", "label": "E2", "source_id": "source-b"})
    grounded.format_functions(spans)
    first = run_first(spans, proposal(second=True))
    targets = grounded.citation_range_targets(first.draft, spans, (), first.dropped)
    assert {(row["item_index"], row["citation_index"], row["evidence_id"]) for row in targets} == {(0, 0, "E1"), (0, 1, "E2")}
    calls = []

    def repair(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> CitationRangeRepair:
        assert schema is CitationRangeRepair
        calls.append(schema)
        return CitationRangeRepair(ranges=[response(3, 3).ranges[0], response(3, 3, index=1, identity="E2").ranges[0]])

    second = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="同じ根拠",
        image_paths=(),
        provider_id=None,
        parse_text=repair,
        parse_images=repair,
        previous=first.checked,
        previous_draft=first.draft,
        previous_dropped=first.dropped,
        audit_mode="none",
    )
    assert len(calls) == 1 and second.audit_calls == 0
    assert second.timing["citation_range_repair"]["status"] == "completed"
    assert second.draft.items[0].text == first.draft.items[0].text
    assert [ref._wire_range for ref in second.draft.items[0].citations] == [(3, 3), (3, 3)]


def test_range_feedback_keeps_original_draft_index_after_items_were_filtered() -> None:
    first = run_first(evidence())
    assert first.checked
    entry = first.checked[0]
    entry.draft_index = 2
    feedback = grounded.citation_range_issues([entry])
    assert feedback and {row["item_index"] for row in feedback} == {2}


@pytest.mark.parametrize("limit", [0, 1, 2])
@pytest.mark.parametrize("fix_content", [True, False])
def test_range_repair_preserves_content_correction_within_configured_rounds(limit: int, fix_content: bool) -> None:
    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=LONG,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        source_run_id="run",
        chunk_level="parent",
        metadata={"section_path": ["合成操作"]},
    )
    context = AnswerContext(records=[record], text=LONG)
    incorrect = "一覧から対象を選択し、保存ボタンを押します。"
    calls = []

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is CitationRangeRepair:
            return response(3, 3)
        assert schema is generation_draft_wire_model()
        draft = proposal(0, 0) if len(calls) == 1 else proposal(3, 3)
        draft.items[0].text = incorrect if len(calls) == 1 or not fix_content else CLAIM
        return draft

    config = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="none", answer_max_correction_rounds=limit)
    with bind_dependencies(AnswerDependencies(unused, unused, parser, unused, unused)):
        result = answering.synthesize_grounded_answer(QUESTION, context, config, image_prompt_mode="text_only")
    assert len(calls) == limit + 1
    if limit == 2:
        assert calls == ["GroundedDraftWire", "CitationRangeRepair", "GroundedDraftWire"]
        if fix_content:
            assert incorrect not in result.response.answer_text
            assert CLAIM in result.response.answer_text
        else:
            assert incorrect not in result.response.answer_text
            assert result.response.generation_trace["button_operation_fallback"]["item_indexes"] == [0]
            assert "確認が必要な点" in result.response.answer_text
            assert result.response.generation_trace["round_budget"]["stop"]["reason"] == "max_correction_rounds"
    else:
        assert calls == ["GroundedDraftWire", "CitationRangeRepair"][: limit + 1]


def test_unchanged_claim_recovers_previous_range_without_another_model_call() -> None:
    spans = evidence()
    old = run_first(spans, proposal(3, 3))
    assert old.checked[0].confirmed
    draft, restored = grounded.restore_previous_citation_ranges(QUESTION, proposal(0, 0), spans, old.checked, old.draft)
    assert restored == [{"item_index": 0, "citation_index": 0}]
    assert draft.items[0].citations[0]._wire_range == (3, 3)
    assert draft.items[0].text == CLAIM


@pytest.mark.parametrize("change", ["identity", "text", "claim", "condition", "source"])
def test_previous_range_is_not_reused_after_claim_or_source_changes(change: str) -> None:
    spans = evidence()
    old = run_first(spans, proposal(3, 3))
    candidate = proposal(0, 0)
    if change == "identity":
        spans = evidence(identity="other")
    elif change == "source":
        spans = evidence(source="other")
    elif change == "text":
        spans = evidence(text=LONG + "変更。")
    else:
        setattr(candidate.items[0], "text" if change == "claim" else "condition", "変更")
    draft, restored = grounded.restore_previous_citation_ranges(QUESTION, candidate, spans, old.checked, old.draft)
    assert not restored and draft.items[0].citations[0]._wire_range == (0, 0)


def test_unchanged_claim_restores_heading_only_regression() -> None:
    spans = evidence("1. 合成操作\n" + LONG)
    spans[0]["section_path"] = ["1. 合成操作"]
    spans[0]["section_path_sources"] = ["section_header"]
    grounded.format_functions(spans)
    old = run_first(spans, proposal(4, 4))
    bad = proposal(1, 1)
    checked, dropped = grounded.verify(QUESTION, bad, spans)
    assert grounded.citation_range_issues(checked, dropped)[0]["reason_code"] == "citation_heading_only"
    fixed, restored = grounded.restore_previous_citation_ranges(QUESTION, bad, spans, old.checked, old.draft)
    assert restored and fixed.items[0].citations[0]._wire_range == (4, 4)


def test_button_fallback_preserves_supported_steps_and_original_references() -> None:
    spans = evidence()
    draft = proposal(3, 3)
    good, _ = grounded.verify(QUESTION, draft, spans)
    unsafe = deepcopy(good[0])
    unsafe.draft_index = 1
    unsafe.item.text = "原文にない保存ボタンを押します。"
    unsafe.button_name_issues = ["保存"]
    original = unsafe.item.quote
    assert grounded.withhold_unconfirmed_button_operations([good[0], unsafe]) == [1]
    assert not good[0].quote_only and good[0].published
    assert unsafe.quote_only and not unsafe.published
    assert unsafe.item.quote == original
    assert grounded.withhold_unconfirmed_button_operations([good[0], unsafe]) == []
