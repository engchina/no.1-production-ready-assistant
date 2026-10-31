"""要求別の適用範囲とガイドの候補資料を分離する。実サービス呼出はない。"""

from typing import Any

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem, GuideRequirementCoverage, RequestCoverage

METHOD = "機能Zを備える対象では、設定画面で項目を変更して登録します。"


def run(relation: str | None, *, guide_candidate: bool = False) -> tuple[Any, list[str]]:
    span = {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "source-a",
        "source": "sample.pdf",
        "function": ("sample.pdf", "合成方法"),
        "text": METHOD,
    }
    grounded.format_functions([span])
    plan: dict[str, Any] = {"request_id": "Q1", "evidence_ids": [], "evidence_scope": []}
    if relation is not None:
        plan["evidence_scope"] = [
            {"evidence_id": "E1", "target": "対象A", "relation": relation, "link_quote": METHOD if relation == "linked" else ""}
        ]
        if relation != "unrelated":
            plan["evidence_ids"] = ["E1"]
    draft = GroundedDraft(
        request_coverage=[RequestCoverage.model_validate(plan)],
        guide_coverage=[GuideRequirementCoverage(requirement_id="method:0", evidence_ids=["E1"], gap="")] if guide_candidate else [],
        items=[
            GroundedItem(
                kind="operation",
                request_id="Q1",
                text=METHOD,
                applies="conditional",
                condition="機能Zを備える対象に限る",
                citations=[GroundedCitation(evidence_id="E1", lines="1")],
            )
        ],
        summary="",
        confidence="low",
    )
    calls: list[str] = []

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> GroundedDraft:
        calls.append(schema.__name__)
        assert schema is generation_draft_wire_model()
        # 保存済み canonical fixture の再生。wire の伝送・意味判定を装わない。
        return draft

    result = grounded.run_round(
        "対象Aの現在値は不明です。機能Zを備える場合の変更方法は？",
        [span],
        get_settings(environ={}, dotenv_path=None),
        prompt="合成の入力",
        image_paths=(),
        provider_id=None,
        parse_text=parser,
        parse_images=parser,
        audit_mode="none",
    )
    assert calls == ["GroundedDraftWire"]
    assert result.audit_calls == 0
    return result, calls


@pytest.mark.parametrize("guide_candidate", [False, True])
def test_explicit_unrelated_source_is_not_admitted_by_a_guide_candidate(guide_candidate: bool) -> None:
    result, _ = run("unrelated", guide_candidate=guide_candidate)
    assert not any(row.item.text == METHOD and row.published for row in result.checked)
    assert any(row.get("scope_excluded") and not row.get("kept") for row in result.dropped)


@pytest.mark.parametrize("relation", ["direct", "linked"])
def test_valid_scope_retains_conditional_common_method_without_semantic_certification(relation: str) -> None:
    result, _ = run(relation, guide_candidate=True)
    entry = next(row for row in result.checked if row.item.text == METHOD)
    assert entry.published
    assert entry.review_assessment == {"effective_support": "unknown"}
    assert entry.item.text.startswith("機能Zを備える対象では")
    assert entry.item.quote == METHOD
    assert result.review_execution == "not_run"


def test_undecided_initial_scope_retains_cited_method_as_unverified_without_extra_call() -> None:
    result, _ = run(None)
    entry = next(row for row in result.checked if row.item.text == METHOD)
    assert entry.published and not entry.confirmed
    assert entry.item.text.startswith("機能Zを備える対象では")
    assert any(mark["code"] == "unassessed" for mark in entry.marks)
    assert any(row.get("scope_excluded") and row.get("kept") for row in result.dropped)
    assert result.review_execution == "not_run"
