"""未校正の意味観察は診断に留め、決定論的な引用検査と分離する。実モデル呼出0。"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from audit_fixtures import legacy_audit_to_wire_fixture

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, crag_generation, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.models.audit_wire import decode_audit_wire, is_audit_wire
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

QUESTION = "設定を保存する方法は？"
OPEN = "設定画面を開きます。"
SAVE = "承認済みの場合だけ、選択した項目を保存します。"
BODY = OPEN + "\n" + SAVE


def draft(summary: str = "") -> GroundedDraft:
    return GroundedDraft(
        items=[
            GroundedItem(
                kind="operation",
                request_id="Q1",
                text=text,
                applies="conditional" if index else "matched",
                condition="承認済み" if index else "",
                citations=[GroundedCitation(evidence_id="E1", quote=text)],
            )
            for index, text in enumerate((OPEN, SAVE))
        ],
        summary=summary,
        confidence="low",
    )


def spans() -> list[dict[str, Any]]:
    result = [
        {
            "evidence_id": "stable-a",
            "label": "E1",
            "source_id": "child-a",
            "source": "synthetic.pdf",
            "page": 1,
            "text": BODY,
            "function": ("synthetic.pdf", "合成設定"),
            "tags": [],
        }
    ]
    grounded.format_functions(result)
    return result


class Model:
    def __init__(self, support: str, source: str, *, negative: bool = True, summary: str = "") -> None:
        self.support, self.source, self.negative, self.summary = support, source, negative, summary
        self.calls: list[str] = []
        self.audits: list[dict[str, Any]] = []

    def __call__(self, system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        self.calls.append(schema.__name__)
        if schema is generation_draft_wire_model():
            return draft(self.summary)
        assert is_audit_wire(schema)
        self.audits.append(json.loads(prompt))
        payload = {
            "reviews": [
                {
                    "index": index,
                    "support": self.support,
                    "applicability": self.source,
                    "source_applicability": self.source,
                    "reason": "合成の未校正意味観察",
                }
                for index in range(2)
            ],
            "request_reviews": [
                {
                    "request_id": "Q1",
                    "status": "missing" if self.negative else "addressed",
                    "evidence_status": "missing" if self.negative else "unknown",
                    "reason": "合成の未校正材料観察",
                }
            ],
            "guide_requirement_reviews": [
                {"requirement_id": identity, "answer_state": "not_mentioned", "item_indexes": [], "reason": "合成の未校正欠落観察"}
                for identity in schema.expected_requirement_ids
            ],
            "summary_supported": not self.negative,
            "goal_alignment": "off_target" if self.negative else "aligned",
        }
        return decode_audit_wire(json.dumps(legacy_audit_to_wire_fixture(payload, schema), ensure_ascii=False), schema)


def run(model: Model, *, mode: str = "standard") -> grounded.Round:
    return grounded.run_round(
        QUESTION,
        spans(),
        get_settings(environ={}, dotenv_path=None),
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=model,
        audit_mode=mode,
        guide_steps=[("save", "保存の説明", ["child-a"])],
        guide_step_contracts=[{"step_id": "save", "requirements": [{"id": "save:0", "text": SAVE}], "depends_on": []}],
    )


@pytest.mark.parametrize("support", ["contradicted", "unsupported", "unknown"])
def test_uncalibrated_negative_observation_does_not_remove_or_reclassify_the_body_as_error(support: str) -> None:
    model = Model(support, "not_applicable")
    result = run(model)
    entries = [row for row in result.checked if not row.added]
    assert [row.item.text for row in entries if row.published] == [OPEN, SAVE]
    assert not any(row.removed for row in entries)
    assert entries[0].item.applies == "matched"
    assert entries[1].item.applies == "conditional" and entries[1].item.condition == "承認済み"
    assert not any(mark["code"] in {"unsupported", "target_mismatch", "contradicted"} for row in entries for mark in row.marks)
    rendered = grounded.render("", result.checked)
    assert rendered.index(OPEN) < rendered.index(SAVE)
    assert "承認済み" in rendered
    assert len(model.calls) == 2 and result.audit_calls == 1


def test_uncalibrated_missing_material_and_guide_or_off_target_do_not_start_more_generation() -> None:
    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=BODY,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        chunk_level="parent",
        source_run_id="run",
        metadata={"section_path": ["合成設定"]},
    )
    context = AnswerContext(records=[record], text=BODY)
    model = Model("contradicted", "not_applicable")
    runtime = RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=Path("unused.json"),
        guide_steps=(("save", "保存の説明", ("run:p",)),),
        guide_step_contracts=({"step_id": "save", "requirements": [{"id": "save:0", "text": SAVE}], "depends_on": []},),
        guide_task_scope=(
            {
                "step_id": "save",
                "title": "保存の説明",
                "required": True,
                "requirements": [{"id": "save:0", "text": SAVE}],
                "depends_on": [],
            },
        ),
    )

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    settings = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="standard", answer_max_correction_rounds=2)
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = answering.synthesize_grounded_answer(QUESTION, context, settings, runtime_knowledge=runtime, image_prompt_mode="text_only")
    assert len(model.calls) == 2 and len(model.audits) == 1
    assert result.response.answer_text.index(OPEN) < result.response.answer_text.index(SAVE)
    assert "承認済み" in result.response.answer_text


@pytest.mark.parametrize("identity,start,reason", [("unknown", 1, "citation_id_invalid"), ("E1", 0, "citation_range_out_of_bounds")])
def test_code_invalid_id_or_range_remains_unverified(identity: str, start: int, reason: str) -> None:
    proposal = canonical_generation_draft(
        generation_draft_wire_model().model_validate(
            {
                "items": [
                    {"kind": "operation", "text": OPEN, "citations": [{"evidence_id": identity, "line_start": start, "line_end": start}]}
                ],
                "summary": "",
                "confidence": "low",
            }
        )
    )
    checked, dropped = grounded.verify(QUESTION, proposal, spans())
    assert checked and not any(row.confirmed for row in checked)
    assert any(row.get("reason_code") == reason or row.get("citation_resolution", {}).get("reason_code") == reason for row in dropped)
    assert any(mark["code"] == reason for row in checked for mark in row.marks)


def test_model_supported_is_not_reported_as_verified_semantics_while_citation_validity_remains_separate() -> None:
    result = run(Model("supported", "matched", negative=False))
    dimensions = result.trace()["validation_dimensions"]
    assert all(citation["status"] == "matched" for row in dimensions["citation_validity"]["items"] for citation in row["citations"])
    assert all(row["status"] == "unknown" for row in dimensions["claim_support"]["items"])
    assert all(row.item.text in {OPEN, SAVE} and row.published for row in result.checked if not row.added)
    assert all(row["status"] == "unknown" and row.get("evidence_status", "unknown") == "unknown" for row in result.unanswered)


def test_raw_negative_observation_remains_available_without_becoming_the_effective_verdict() -> None:
    model = Model("contradicted", "not_applicable")
    result = run(model)
    observation = result.timing["semantic_audit_observation"]
    assert observation["adopted"] is False
    raw = observation["audit"]
    assert raw["reviews"][0]["support"] == "contradicted"
    assert raw["reviews"][0]["source_applicability"] == "not_applicable"
    assert raw["reviews"][0]["reason"] == "合成の未校正意味観察"
    assert raw["request_reviews"][0]["evidence_status"] == "missing"
    assert raw["request_reviews"][0]["reason"] == "合成の未校正材料観察"
    assert raw["guide_requirement_reviews"][0]["answer_state"] == "not_mentioned"
    assert raw["goal_alignment"] == "off_target"
    assert result.timing["semantic_audit_policy"] == {"mode": "diagnostic_only", "calibration": "not_calibrated"}
    assert all(row.review_assessment["effective_support"] == "unknown" for row in result.checked)
    assert [row.item.quote for row in result.checked] == [OPEN, SAVE]
    assert all(row.item.citations[0].evidence_id == "stable-a" for row in result.checked)
    assert result.unanswered and all(row["evidence_status"] == "unknown" for row in result.unanswered)
    assert not result.feedback()


def test_nonempty_summary_survives_finalization_and_outer_answer_without_another_model_call() -> None:
    summary = "承認済みの場合の保存方法を案内します。"
    direct_model = Model("contradicted", "not_applicable", summary=summary)
    current = run(direct_model)
    assert current.summary == summary
    finalized = grounded.finalize_summary(current)
    assert finalized.summary == summary and [row.item.text for row in finalized.checked] == [OPEN, SAVE]
    assert len(direct_model.calls) == 2

    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=BODY,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        chunk_level="parent",
        source_run_id="run",
        metadata={"section_path": ["合成設定"]},
    )
    context = AnswerContext(records=[record], text=BODY)
    model = Model("contradicted", "not_applicable", summary=summary)

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    settings = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="standard", answer_max_correction_rounds=2)
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = answering.synthesize_grounded_answer(QUESTION, context, settings, image_prompt_mode="text_only")
    assert result.response.answer_text.startswith(summary)
    assert result.response.answer_text.index(OPEN) < result.response.answer_text.index(SAVE)
    assert len(model.calls) == 2 and len(model.audits) == 1


def test_partial_crag_projection_does_not_omit_useful_items_or_replace_the_summary() -> None:
    summary = "確認した条件に沿う操作を説明します。"
    model = Model("contradicted", "not_applicable", summary=summary)
    current = run(model)
    original = [(row.item.model_dump(), row.published) for row in current.checked]
    calls_before = list(model.calls)
    view = {
        "required_aspect_coverage": "partial",
        "answerable_parts": [{"status": "supported", "evidence": [{"evidence_ids": ["stable-a"], "quote": OPEN}]}],
    }
    projected, diagnostic = crag_generation.project_partial_answer(current, view, spans(), audited=True)
    assert [(row.item.model_dump(), row.published) for row in projected.checked] == original
    assert projected.summary == summary
    assert diagnostic == {"applied": False, "reason": "semantic_audit_diagnostic_only"}
    assert model.calls == calls_before and len(model.calls) == 2


def test_disabled_audit_does_not_create_a_completed_semantic_observation() -> None:
    model = Model("contradicted", "not_applicable")
    result = run(model, mode="none")
    assert len(model.calls) == 1 and not model.audits and result.audit_calls == 0
    assert result.review_execution == "not_run"
    dimensions = result.trace()["validation_dimensions"]
    assert dimensions["claim_support"]["execution"] == "not_run"
    assert all(row["status"] == "unknown" for row in dimensions["claim_support"]["items"])
    assert "semantic_audit_observation" not in result.timing
