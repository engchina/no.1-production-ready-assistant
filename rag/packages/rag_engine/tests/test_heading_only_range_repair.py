"""節の見出しの行だけを指す引用は、範囲だけを直す修復で同じ根拠の本文の行へ選び直す（#3854）。

説明の文・根拠・ほかの出典は変えない。修復した範囲は通常の照合に掛け、まだ見出しだけなら印を付けたまま残す。
"""

import json
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.citation_range_repair import CitationRangeRepair
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedDraft

QUESTION = "対象を選択して確定する方法は？"
HEADING = "合成手続の場合"
BODY = "一覧から対象を選択し、確定ボタンを押します。"
TEXT = f"【{HEADING}】\n{BODY}"
CLAIM = "一覧から対象を選択し、確定ボタンを押します。"
OTHER = "画面を開きます。\n抽出条件を入力します。"
OTHER_CLAIM = "画面を開きます。"


def span(text: str = TEXT, *, identity: str = "stable-a", label: str = "E1", heading: str = HEADING) -> dict[str, Any]:
    return {
        "evidence_id": identity,
        "label": label,
        "source_id": f"source-{identity}",
        "source": "synthetic.pdf",
        "page": 1,
        "text": text,
        "origin": "document_text",
        "function": ("synthetic.pdf", heading),
        "section_path": [heading],
        "section_path_sources": ["section_header"],
        "tags": [],
    }


def evidence(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    spans = [span(), *extra]
    grounded.format_functions(spans)
    return spans


def item(text: str, *citations: tuple[str, int, int]) -> dict[str, Any]:
    return {
        "kind": "operation",
        "request_id": "Q1",
        "text": text,
        "citations": [{"evidence_id": label, "line_start": start, "line_end": end} for label, start, end in citations],
    }


def proposal(*items: dict[str, Any]) -> GroundedDraft:
    wire = generation_draft_wire_model().model_validate(
        {"items": list(items) or [item(CLAIM, ("E1", 1, 1))], "summary": "対象を選択して確定します。", "confidence": "medium"}
    )
    return canonical_generation_draft(wire)


def ranges(*rows: tuple[int, int, str, int, int]) -> CitationRangeRepair:
    return CitationRangeRepair.model_validate(
        {
            "ranges": [
                {"item_index": i, "citation_index": c, "evidence_id": label, "line_start": start, "line_end": end}
                for i, c, label, start, end in rows
            ]
        }
    )


def run(spans: list[dict[str, Any]], parser: Any, *, first: grounded.Round | None = None) -> grounded.Round:
    return grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=parser,
        parse_images=parser,
        previous=first.checked if first else (),
        previous_draft=first.draft if first else None,
        previous_dropped=first.dropped if first else (),
        audit_mode="none",
    )


def run_first(spans: list[dict[str, Any]], draft: GroundedDraft | None = None) -> grounded.Round:
    def generation(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        assert schema is generation_draft_wire_model()
        return draft if draft is not None else proposal()

    return run(spans, generation)


def codes(round_: grounded.Round, text: str = CLAIM) -> list[str]:
    return [mark["code"] for entry in round_.checked if entry.item.text == text for mark in entry.marks]


def test_heading_only_citation_becomes_a_range_repair_target_with_a_hint() -> None:
    spans = evidence()
    first = run_first(spans)
    assert codes(first) == ["citation_heading_only"]
    assert grounded.citation_range_issues(first.checked, first.dropped) == [
        {"item_index": 0, "evidence_id": "E1", "reason_code": "citation_heading_only"}
    ]
    targets = grounded.citation_range_targets(first.draft, spans, first.checked, first.dropped)
    assert targets == [
        {
            "item_index": 0,
            "citation_index": 0,
            "evidence_id": "E1",
            "unit_count": 2,
            "numbered": True,
            "reason_code": "citation_heading_only",
            "hint": grounded.HEADING_ONLY_REASON,
        }
    ]
    # 外した出典の記録（dropped）だけでは見出しだけの対象にしない（印の付いた説明の出典で決める）。
    assert grounded.citation_range_targets(first.draft, spans, (), first.dropped) == []
    assert grounded.pending_feedback_labels(first.feedback()) == ["節の見出しだけを指す引用 1 件", "原文と一致しない引用 1 件"]


@pytest.mark.parametrize("repaired_range", [(1, 2), (2, 2)], ids=["heading_and_body", "body_only"])
def test_repaired_range_in_the_same_block_is_verified_and_the_mark_is_removed(repaired_range: tuple[int, int]) -> None:
    spans = evidence()
    first = run_first(spans)
    calls: list[dict[str, Any]] = []

    def repair(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> CitationRangeRepair:
        assert schema is CitationRangeRepair
        calls.append(json.loads(prompt.split("\n", 1)[1]))
        return ranges((0, 0, "E1", *repaired_range))

    second = run(spans, repair, first=first)
    assert len(calls) == 1 and second.audit_calls == 0
    assert calls[0]["targets"][0]["reason_code"] == "citation_heading_only"
    assert calls[0]["targets"][0]["hint"] == grounded.HEADING_ONLY_REASON
    assert second.timing["citation_range_repair"]["status"] == "completed"
    assert second.draft.items[0].text == first.draft.items[0].text
    assert second.draft.items[0].citations[0]._wire_range == repaired_range
    assert codes(second) == []
    assert grounded.citation_range_issues(second.checked, second.dropped) == []
    assert grounded.better(second, first)


def test_repair_that_still_points_at_the_heading_keeps_the_item_and_the_mark() -> None:
    spans = evidence()
    first = run_first(spans)
    second = run(spans, lambda *args, **kwargs: ranges((0, 0, "E1", 1, 1)), first=first)
    assert second.timing["citation_range_repair"]["status"] == "completed"
    assert second.draft.items[0].text == CLAIM
    assert codes(second) == ["citation_heading_only"]
    assert grounded.feedback_signature(second.feedback()) == grounded.feedback_signature(first.feedback())


def test_repair_cannot_move_a_heading_only_citation_to_another_block() -> None:
    spans = evidence(span(OTHER, identity="stable-b", label="E2", heading="合成画面"))
    first = run_first(spans)
    second = run(spans, lambda *args, **kwargs: ranges((0, 0, "E2", 1, 1)), first=first)
    assert second.timing["citation_range_repair"]["rejected"] == [
        {"item_index": 0, "citation_index": 0, "reason_code": "evidence_id_changed"}
    ]
    assert second.draft.items[0].citations[0].evidence_id == "E1"
    assert codes(second) == ["citation_heading_only"]


@pytest.mark.parametrize("case", ["body_range", "confirmed_by_other_citation", "legacy_quote"])
def test_no_range_repair_call_without_a_heading_only_target(case: str) -> None:
    spans = evidence(span(CLAIM, identity="stable-b", label="E2", heading="合成画面"))
    if case == "body_range":
        draft = proposal(item(CLAIM, ("E1", 2, 2)))
    elif case == "confirmed_by_other_citation":
        draft = proposal(item(CLAIM, ("E1", 1, 1), ("E2", 1, 1)))
    else:
        draft = GroundedDraft.model_validate(
            {
                "items": [
                    {
                        "kind": "operation",
                        "request_id": "Q1",
                        "text": CLAIM,
                        "citations": [{"evidence_id": "E1", "quote": f"【{HEADING}】"}],
                    }
                ],
                "summary": "対象を選択して確定します。",
                "confidence": "medium",
            }
        )
    first = run_first(spans, draft)
    if case == "legacy_quote":
        # 整数範囲でない旧形式の引用は範囲だけを直せないので、草稿の作り直しの指摘（dropped_items）に残す。
        assert codes(first) == ["citation_heading_only"]
        assert first.feedback()["dropped_items"]
    else:
        assert codes(first) == []
    assert grounded.citation_range_issues(first.checked, first.dropped) == []
    assert grounded.citation_range_targets(first.draft, spans, first.checked, first.dropped) == []


def test_heading_only_and_range_errors_share_one_repair_call() -> None:
    spans = evidence(span(OTHER, identity="stable-b", label="E2", heading="合成画面"))
    first = run_first(spans, proposal(item(CLAIM, ("E1", 1, 1)), item(OTHER_CLAIM, ("E2", 0, 0))))
    assert {(row["item_index"], row["reason_code"]) for row in grounded.citation_range_issues(first.checked, first.dropped)} == {
        (0, "citation_heading_only"),
        (1, "citation_range_out_of_bounds"),
    }
    calls: list[dict[str, Any]] = []

    def repair(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> CitationRangeRepair:
        calls.append(json.loads(prompt.split("\n", 1)[1]))
        return ranges((0, 0, "E1", 1, 2), (1, 0, "E2", 1, 1))

    second = run(spans, repair, first=first)
    assert len(calls) == 1
    assert [(row["item_index"], row["reason_code"], "hint" in row) for row in calls[0]["targets"]] == [
        (0, "citation_heading_only", True),
        (1, "citation_range_out_of_bounds", False),
    ]
    assert second.timing["generation_calls"] == 1
    assert second.timing["citation_range_repair"]["status"] == "completed"
    assert codes(second) == [] and codes(second, OTHER_CLAIM) == []


def test_answer_without_audit_repairs_a_heading_only_range_with_one_extra_call() -> None:
    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=TEXT,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        source_run_id="run",
        chunk_level="parent",
        metadata={"section_path": [HEADING], "section_path_sources": ["section_header"]},
    )
    calls: list[str] = []

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is generation_draft_wire_model():
            return proposal()
        assert schema is CitationRangeRepair
        return ranges((0, 0, "E1", 1, 2))

    config = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="none")
    with bind_dependencies(AnswerDependencies(unused, unused, parser, unused, unused)):
        result = answering.synthesize_grounded_answer(
            QUESTION, AnswerContext(records=[record], text=TEXT), config, image_prompt_mode="text_only"
        )
    assert calls == ["GroundedDraftWire", "CitationRangeRepair"]
    rounds = result.response.generation_trace["rounds"]
    assert [round_["accepted"] for round_ in rounds] == [True, True]
    assert rounds[1]["timing"]["citation_range_repair"]["status"] == "completed"
    assert CLAIM in result.response.answer_text
