"""ガイドの業務範囲と資料の適用性を分離する採用契約。意味判定は合成oracle。"""

import json
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from audit_fixtures import legacy_audit_to_wire_fixture
from openai.lib._parsing._responses import type_to_text_format_param

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.generation.confirmed_context import confirmed_premise_records
from rag_engine.generation.guide_authority import authoritative_guide_inputs, bind_guide_authority
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.models.audit_wire import audit_wire_schema, decode_audit_wire, is_audit_wire
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedDraft, GroundedItem, GroundedItemReview, GroundedRequestReview

QUESTION = "対象の設定を複製する手順は？"
CORRECT = "対象を選び、複製ボタンを押してから参照元を選択します。"
REVERSED = "対象を選び、参照元を選択してから複製ボタンを押します。"
SWAPPED = "参照元を選び、複製ボタンを押してから設定対象を選択します。"
RELATED = "画面の状態と一覧の件数を確認できます。"
SCOPE: list[dict[str, Any]] = [
    {"step_id": "copy", "required": True, "depends_on": []},
    {"step_id": "finish", "required": True, "depends_on": ["copy"]},
    {"step_id": "alternative", "required": False, "depends_on": []},
]
CANDIDATES: dict[int, list[dict[str, Any]]] = {
    0: [
        {
            "step_id": "copy",
            "association": "source_only",
            "matched_evidence_ids": ["stable-a"],
            "candidate_requirements": [{"id": "copy:0", "text": "対象、複製操作、参照元を順に説明する"}],
        }
    ],
}


def assessment(*, support: str = "supported", source: str | None = "matched", source_condition: str = "") -> GroundedAudit:
    return GroundedAudit.model_validate(
        {
            "reviews": [
                {
                    "index": 0,
                    "support": support,
                    "applicability": "not_applicable",
                    "reason": "合成の範囲観察",
                    "source_applicability": source,
                    "source_condition": source_condition,
                }
            ],
            "summary_supported": False,
            "goal_alignment": "partial",
        }
    )


def checked(text: str = CORRECT, *, evidence: str = CORRECT) -> grounded.CheckedItem:
    span = {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "child-a",
        "source": "synthetic.pdf",
        "text": evidence,
        "function": ("synthetic.pdf", "合成操作"),
        "tags": [],
    }
    proposal = GroundedItem(
        kind="operation",
        text=text,
        request_id="Q1",
        applies="matched",
        citations=[GroundedCitation(evidence_id="stable-a", quote=evidence)],
    )
    entries, dropped = grounded.verify(QUESTION, GroundedDraft(items=[proposal], summary="", confidence="low"), [span])
    assert not dropped and len(entries) == 1
    return entries[0]


def apply(entry: grounded.CheckedItem, raw: GroundedAudit) -> GroundedAudit:
    result = grounded.applicability_policy(bind_guide_authority(raw, SCOPE, CANDIDATES))
    grounded.apply_audit([entry], result)
    return result


def test_required_business_step_does_not_inherit_model_scope_rejection_when_source_matches() -> None:
    entry = checked()
    result = apply(entry, assessment())
    review = result.reviews[0]
    assert review.observed_applicability == "not_applicable"
    assert review.applicability == "matched"
    assert entry.review_assessment["effective_support"] == "supported"
    assert entry.item.applies == "matched" and entry.confirmed and entry.published
    assert grounded.answer_use(entry) != "reference"
    assert review.business_scope == {
        "source": "business_guide",
        "required_step_ids": ["copy", "finish"],
        "unresolved_step_ids": ["alternative"],
        "candidate_step_ids": ["copy"],
        "association": "source_only",
    }


@pytest.mark.parametrize("source", ["unknown", "not_applicable"])
def test_authoritative_scope_does_not_override_unknown_or_mismatched_source(source: str) -> None:
    entry = checked()
    result = apply(entry, assessment(source=source))
    assert result.reviews[0].business_scope["required_step_ids"] == ["copy", "finish"]
    assert entry.published and not entry.confirmed and entry.item.applies == "unverified"
    assert grounded.answer_use(entry) == "reference"


def test_real_source_condition_is_retained_without_treating_unknown_branch_as_confirmed() -> None:
    condition = "承認済みの場合に限り"
    source = condition + CORRECT
    entry = checked(evidence=source)
    result = apply(entry, assessment(source="conditional", source_condition=condition))
    assert result.reviews[0].applicability == "conditional"
    assert entry.item.applies == "conditional" and entry.item.condition
    assert condition in source and entry.item.condition in source
    assert entry.item.condition in grounded.render("", [entry])


@pytest.mark.parametrize("text,support", [(REVERSED, "contradicted"), (SWAPPED, "contradicted"), (SWAPPED, "unsupported")])
def test_required_guide_scope_does_not_rescue_reversed_order_or_swapped_objects(text: str, support: str) -> None:
    entry = checked(text)
    result = apply(entry, assessment(support=support))
    assert result.reviews[0].support == support
    assert entry.review_assessment["effective_support"] == support
    assert not entry.confirmed
    if support == "contradicted":
        assert entry.removed and not entry.published
    else:
        assert entry.published and any(mark["code"] == "unsupported" for mark in entry.marks)


@pytest.mark.parametrize("support", ["unsupported", "unknown"])
def test_same_chunk_and_exact_quote_do_not_prove_operation_support(support: str) -> None:
    entry = checked(evidence=RELATED)
    assert entry.quote_match == "exact"
    result = apply(entry, assessment(support=support))
    assert result.reviews[0].business_scope["candidate_step_ids"] == ["copy"]
    assert entry.review_assessment["effective_support"] == support
    assert not entry.confirmed


def test_missing_material_is_not_created_by_authoritative_guide_requirements() -> None:
    raw = assessment()
    raw.request_reviews = [GroundedRequestReview(request_id="Q1", status="missing", reason="直接材料が未取得", evidence_status="missing")]
    result = bind_guide_authority(raw, SCOPE, {})
    assert result.reviews[0].business_scope["required_step_ids"] == ["copy", "finish"]
    assert result.reviews[0].business_scope["candidate_step_ids"] == []
    assert result.request_reviews[0].evidence_status == "missing"
    assert result.request_reviews[0].evidence_ids == []
    assert result.guide_requirement_reviews == []
    assert result.reviews[0].support == raw.reviews[0].support


def test_shared_source_candidates_do_not_become_completion_or_resolve_unknown_branch() -> None:
    candidates = {
        0: [*CANDIDATES[0], {"step_id": "finish", "association": "source_only"}, {"step_id": "alternative", "association": "source_only"}]
    }
    result = bind_guide_authority(assessment(), SCOPE, candidates)
    scope = result.reviews[0].business_scope
    assert scope["candidate_step_ids"] == ["copy", "finish", "alternative"]
    assert scope["required_step_ids"] == ["copy", "finish"]
    assert scope["unresolved_step_ids"] == ["alternative"]
    assert not result.guide_requirement_reviews


def test_legacy_mixed_applicability_does_not_silently_become_source_verified() -> None:
    entry = checked()
    result = apply(entry, assessment(source=None))
    assert result.reviews[0].source_applicability is None
    assert result.reviews[0].observed_applicability == "not_applicable"
    assert not entry.confirmed and entry.item.applies == "unverified"


def test_business_scope_is_rebuilt_by_program_and_not_part_of_model_schema() -> None:
    raw = assessment()
    raw.reviews[0].business_scope = {"required_step_ids": ["forged"], "source": "model"}
    result = bind_guide_authority(raw, SCOPE, CANDIDATES)
    assert result.reviews[0].business_scope["source"] == "business_guide"
    assert result.reviews[0].business_scope["required_step_ids"] == ["copy", "finish"]
    assert "business_scope" not in GroundedItemReview.model_json_schema()["properties"]


def test_run_round_delivers_authoritative_scope_and_keeps_supported_operation_as_operation() -> None:
    span = {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "child-a",
        "source": "synthetic.pdf",
        "text": CORRECT,
        "function": ("synthetic.pdf", "合成操作"),
        "tags": [],
    }
    grounded.format_functions([span])
    calls: list[str] = []
    audit_inputs: list[dict[str, Any]] = []

    def parse(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is generation_draft_wire_model():
            return GroundedDraft(
                items=[
                    GroundedItem(
                        kind="operation",
                        request_id="Q1",
                        text=CORRECT,
                        applies="matched",
                        citations=[GroundedCitation(evidence_id="E1", quote=CORRECT)],
                    )
                ],
                summary="",
                confidence="low",
            )
        assert is_audit_wire(schema)
        audit_inputs.append(json.loads(prompt))
        payload = assessment().model_dump()
        payload["request_reviews"] = [{"request_id": "Q1", "status": "partial", "reason": "残る手順は未説明", "evidence_status": "unknown"}]
        payload["guide_requirement_reviews"] = [
            {"requirement_id": "copy:0", "answer_state": "explained", "answer_quote": CORRECT, "item_indexes": [0], "reason": ""}
        ]
        wire = legacy_audit_to_wire_fixture(payload, schema)
        # 旧観察はcanonical fixtureだけにあり、実wireのモデル出力では求めない。
        assert not {"applicability", "condition", "business_scope"} & wire["reviews"][0].keys()
        assert wire["reviews"][0]["source_applicability"] == "matched"
        assert wire["reviews"][0]["source_condition"] == ""
        return decode_audit_wire(json.dumps(wire, ensure_ascii=False), schema)

    result = grounded.run_round(
        QUESTION,
        [span],
        get_settings(environ={}, dotenv_path=None),
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=parse,
        parse_images=parse,
        audit_mode="detailed",
        guide_steps=[("copy", "複製手順", ["child-a"])],
        guide_step_contracts=[{"step_id": "copy", "requirements": [{"id": "copy:0", "text": CORRECT}], "depends_on": []}],
        guide_task_scope=SCOPE,
    )
    assert len(calls) == 2 and result.audit_calls == 1 and len(audit_inputs) == 1
    actual = audit_inputs[0]
    assert actual["guide_task_scope"] == SCOPE
    assert actual["items"][0]["citations"] == [{"evidence_id": "stable-a", "quote": CORRECT}]
    assert actual["items"][0]["guide_step_candidates"][0]["step_id"] == "copy"
    assert actual["items"][0]["guide_step_candidates"][0]["association"] == "source_only"
    entry = next(row for row in result.checked if row.item.text == CORRECT)
    assert entry.published and entry.confirmed and entry.item.applies == "matched"
    assert grounded.answer_use(entry) != "reference"
    assert "操作手順" in grounded.render(result.summary, result.checked)
    assert result.audit is not None
    assert result.audit.reviews == []
    raw = result.timing["semantic_audit_observation"]["audit"]["reviews"][0]
    assert raw["source_applicability"] == "matched"
    assert raw["business_scope"]["required_step_ids"] == ["copy", "finish"]
    assert result.timing["semantic_audit_observation"]["adopted"] is False
    assert entry.review_assessment["effective_support"] == "unknown"


def test_actual_strict_audit_schema_only_requires_source_applicability_without_business_scope() -> None:
    schema = audit_wire_schema(GroundedAudit, ("Q1",), ())
    strict = type_to_text_format_param(schema)
    assert strict is not None and strict["type"] == "json_schema"
    output_schema = cast(dict[str, Any], strict["schema"])
    reference = output_schema["properties"]["reviews"]["items"]["$ref"]
    review_schema = output_schema["$defs"][reference.rsplit("/", 1)[-1]]
    properties = review_schema["properties"]
    assert not {"applicability", "condition", "business_scope"} & properties.keys()
    assert {"source_applicability", "source_condition"} <= set(review_schema["required"])
    source = properties["source_applicability"]
    assert source["type"] == "string" and "anyOf" not in source
    assert set(source["enum"]) == {"matched", "conditional", "not_applicable", "unknown"}
    assert properties["source_condition"]["type"] == "string"
    assert review_schema["additionalProperties"] is False


def test_authoritative_inputs_keep_required_steps_without_material_and_override_legacy_business_plan() -> None:
    scope = [
        {
            "step_id": "copy",
            "title": "コードが選択した操作",
            "required": True,
            "depends_on": [],
            "requirements": [{"id": "copy:0", "text": "対象と順序の説明", "kind": "completion"}],
        },
        {
            "step_id": "missing",
            "title": "資料未取得の必須操作",
            "required": True,
            "depends_on": ["copy"],
            "requirements": [{"id": "missing:0", "text": "終了の説明", "kind": "completion"}],
        },
        {"step_id": "unresolved", "title": "未確定の分岐", "required": False, "depends_on": [], "requirements": []},
    ]
    previous_steps = [("outside", "旧計画の対象外操作", ["unrelated-source"]), ("copy", "旧見出し", ["actual-source"])]
    previous_contracts = [
        {"step_id": "outside", "requirements": [{"id": "outside:0", "text": "対象外"}]},
        {"step_id": "copy", "depends_on": ["outside"], "requirements": [{"id": "obsolete", "text": "旧計画の内容"}]},
    ]
    before = deepcopy((scope, previous_steps, previous_contracts))
    steps, optional, contracts = authoritative_guide_inputs(scope, previous_steps, previous_contracts)
    assert steps == (
        ("copy", "コードが選択した操作", ["actual-source"]),
        ("missing", "資料未取得の必須操作", ()),
        ("unresolved", "未確定の分岐", ()),
    )
    assert optional == ("unresolved",)
    assert [row["step_id"] for row in contracts] == ["copy", "missing", "unresolved"]
    assert contracts[0]["requirements"] == scope[0]["requirements"] and contracts[0]["depends_on"] == []
    assert contracts[1]["requirements"] == scope[1]["requirements"] and contracts[1]["depends_on"] == ["copy"]
    assert all(not {"evidence_ids", "source_ids"} & row.keys() for row in contracts)
    assert {identity for _step, _title, sources in steps for identity in sources} == {"actual-source"}
    assert before == (scope, previous_steps, previous_contracts)


@pytest.mark.parametrize("mode", ["none", "standard"])
def test_generation_and_observation_preserve_confirmed_values_and_origins_without_inventing_free_text_premises(mode: str) -> None:
    pairs = (("操作条件", "条件A"), ("対象の種類", "種類B"))
    origins = (("操作条件", "条件A", "user"), ("対象の種類", "種類B", "question"), ("未確認条件", "仮定値", "assumption"))
    expected = [
        {"question": "操作条件", "answer": "条件A", "source": "user"},
        {"question": "対象の種類", "answer": "種類B", "source": "question"},
    ]
    assert confirmed_premise_records(pairs, origins) == expected
    config = replace(
        get_settings(environ={}, dotenv_path=None),
        answer_audit_mode=mode,
        answer_max_correction_rounds=1,
        confirmed_premises=pairs,
        confirmed_premise_sources=origins,
    )
    record = AnswerRecord(
        id="p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=CORRECT,
        source="synthetic.pdf",
        chunk_id="p",
        chunk_uid="run:p",
        source_run_id="run",
        chunk_level="parent",
        metadata={"section_path": ["合成操作"]},
    )
    context = AnswerContext(records=[record], text=CORRECT)
    runtime = RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=Path("unused.json"),
        scope_context="未確認条件は不明。仮定値なら条件Cかもしれません。",
        unconfirmed_conditions=({"question": "未確認条件", "answer": "不明"},),
    )
    generated: list[str] = []
    audited: list[dict[str, Any]] = []

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        if schema is generation_draft_wire_model():
            generated.append(prompt)
            identity = "E1"
            claim = SWAPPED if mode == "standard" and len(generated) == 1 else CORRECT
            return GroundedDraft(
                items=[
                    GroundedItem(
                        kind="operation",
                        text=claim,
                        request_id="Q1",
                        applies="matched",
                        citations=[GroundedCitation(evidence_id=identity, quote=CORRECT)],
                    )
                ],
                summary="",
                confidence="low",
            )
        assert is_audit_wire(schema)
        audited.append(json.loads(prompt))
        first = len(audited) == 1
        payload = assessment(source="matched", support="unsupported" if first else "supported").model_dump()
        payload["summary_supported"] = True
        identity = audited[-1]["items"][0]["evidence_id"]
        payload["request_reviews"] = [
            {
                "request_id": "Q1",
                "status": "partial",
                "reason": "対象と参照元の修正が必要",
                "evidence_status": "available",
                "evidence_ids": [identity],
                "missing_content": CORRECT,
                "evidence_support": [{"evidence_id": identity, "source": "synthetic.pdf", "quote": CORRECT}],
            }
            if first
            else {"request_id": "Q1", "status": "addressed", "reason": "操作は説明済み"}
        ]
        return decode_audit_wire(json.dumps(legacy_audit_to_wire_fixture(payload, schema), ensure_ascii=False), schema)

    with bind_dependencies(AnswerDependencies(unused, unused, parser, unused, unused)):
        answering.synthesize_grounded_answer(QUESTION, context, config, runtime_knowledge=runtime, image_prompt_mode="text_only")
    assert len(generated) == 1
    for prompt in generated:
        match = re.search(r"confirmed_premises:\n(\[[^\n]*\])", prompt)
        assert match is not None
        assert json.loads(match[1]) == expected
    if mode == "none":
        assert not audited
    else:
        assert len(audited) == 1
        assert all(row["confirmed_premises"] == expected for row in audited)
