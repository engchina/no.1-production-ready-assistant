"""固定キーの部分失敗で、有効な主張を失わず局所結果を未検証に保つ。"""

import json

import pytest
from audit_fixtures import legacy_audit_to_wire_fixture
from test_citation_audit_boundary import OPEN, QUESTION, evidence
from test_guide_contracts import contract

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.audit_wire import decode_audit_wire, is_audit_wire
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedDraft, GroundedItem


def run(*, request_case="valid", omit_requirement=False, retry_case=None, use_alias=False, first_contradiction=False):
    spans = [evidence()]
    claim = "設定画面は開きません。" if first_contradiction else OPEN
    if use_alias:
        spans[0]["source_aliases"] = ["guide-source"]
    calls = []
    contracts = [contract("open", OPEN, "設定を保存します。")]

    def parser(system, prompt, settings, schema, **kwargs):
        calls.append(schema)
        if schema is generation_draft_wire_model():
            return GroundedDraft(
                items=[
                    GroundedItem(kind="operation", request_id="Q1", text=claim, citations=[GroundedCitation(evidence_id="E1", lines="1")])
                ],
                summary="",
                confidence="low",
            )
        assert is_audit_wire(schema)
        inputs = json.loads(prompt)
        assert inputs["confirmed_premises"] == [{"question": "操作条件", "answer": "条件A"}]
        assert inputs["guide_steps"][0]["evidence_ids"] == [spans[0]["evidence_id"]]
        candidates = inputs["items"][0]["guide_step_candidates"]
        assert len(candidates) == 1 and candidates[0]["step_id"] == "open"
        assert candidates[0]["association"] == "source_only"
        assert candidates[0]["matched_evidence_ids"] == [spans[0]["evidence_id"]]
        assert [row["id"] for row in candidates[0]["candidate_requirements"]] == ["open:0", "open:1"]
        assert schema.expected_request_ids == ("Q1",)
        assert schema.expected_requirement_ids == ("open:0", "open:1")
        if retry_case and len(calls) == 3:
            if retry_case == "failure":
                raise RuntimeError("合成の再監査失敗")
            empty = GroundedAudit(reviews=[], summary_supported=False, goal_alignment="partial")
            wire = legacy_audit_to_wire_fixture(empty, schema)
            if retry_case == "partial":
                valid = GroundedAudit(
                    reviews=[{"index": 0, "support": "supported", "applicability": "matched", "reason": ""}],
                    request_reviews=[{"request_id": "Q1", "status": "partial", "reason": "合成の部分回答"}],
                    summary_supported=False,
                    goal_alignment="partial",
                )
                wire = legacy_audit_to_wire_fixture(valid, schema)
                wire["reviews"][0]["support"] = "invalid-status"
            return decode_audit_wire(json.dumps(wire), schema)
        canonical = GroundedAudit(
            reviews=[
                {"index": 0, "support": "contradicted" if first_contradiction else "supported", "applicability": "matched", "reason": ""}
            ],
            summary_supported=True,
            goal_alignment="partial",
            request_reviews=[{"request_id": "Q1", "status": "partial", "reason": "保存は未説明"}],
            guide_requirement_reviews=[
                {"requirement_id": "open:0", "answer_state": "explained", "answer_quote": OPEN, "item_indexes": [0], "reason": ""},
                {"requirement_id": "open:1", "answer_state": "not_mentioned", "answer_quote": "", "item_indexes": [], "reason": "未説明"},
            ],
        )
        wire = legacy_audit_to_wire_fixture(canonical, schema)
        if retry_case:
            wire["guide_requirement_reviews"]["open:0"]["answer_quote"] = "回答にはない文"
        if omit_requirement:
            wire["guide_requirement_reviews"].pop("open:1")
        if request_case == "missing":
            wire["request_reviews"].clear()
        elif request_case == "invalid":
            wire["request_reviews"]["Q1"]["status"] = "invalid-status"
        elif request_case == "unknown":
            wire["request_reviews"]["Q999"] = dict(wire["request_reviews"].pop("Q1"))
        raw = json.dumps(wire, ensure_ascii=False)
        if request_case == "duplicate":
            row = json.dumps(wire["request_reviews"]["Q1"], ensure_ascii=False)
            raw = raw.replace('"request_reviews": {"Q1": ' + row + "}", '"request_reviews": {"Q1": ' + row + ', "Q1": ' + row + "}")
        return decode_audit_wire(raw, schema)

    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(spans),
        image_paths=(),
        provider_id=None,
        parse_text=parser,
        parse_images=parser,
        audit_mode="detailed",
        guide_steps=[("open", "画面を開いて保存する", ["guide-source" if use_alias else spans[0]["source_id"]])],
        guide_step_contracts=contracts,
        confirmed_premises=(("操作条件", "条件A"),),
    )
    assert len(calls) == (3 if retry_case else 2) and result.audit_calls == (2 if retry_case else 1)
    entry = next(row for row in result.checked if row.item.text == claim)
    assert entry.published and entry.confirmed and not entry.removed
    assert result.verified == 0  # 原文照合は意味支持の認証ではない。
    return result


def observed_audit(result):
    observation = result.timing["semantic_audit_observation"]
    assert observation["adopted"] is False
    assert not result.audit.reviews
    assert all(row.status == "unknown" for row in result.audit.request_reviews)
    return GroundedAudit.model_validate(observation["audit"])


def test_missing_guide_observation_preserves_valid_results_without_reauditing():
    result = run(omit_requirement=True)
    assert result.review_execution == "partial"
    assert observed_audit(result).request_reviews[0].status == "partial"
    assert result.timing["audit_contract"]["requirements"] == {"open:0": "valid", "open:1": "missing"}
    assert result.guide_steps[0]["status"] != "explained"
    assert result.guide_steps[0]["requirements"][1]["status"] == "unknown"


@pytest.mark.parametrize("case", ["missing", "invalid", "unknown", "duplicate"])
def test_bad_request_binding_stays_unverified_and_preserves_the_other_review(case):
    result = run(request_case=case)
    assert result.review_execution == "partial"
    assert not result.addressed
    assert len(result.unreviewed_requests) == 1
    row = result.unreviewed_requests[0]
    assert row["request_id"] == "Q1" and row["status"] == "unknown"
    assert row["evidence_status"] == "unknown" and not row["evidence_ids"]
    assert row["review_execution"] == ("error" if case in {"invalid", "duplicate"} else "not_run")
    assert observed_audit(result).reviews[0].support == "supported"
    assert observed_audit(result).guide_requirement_reviews[0].requirement_id == "open:0"
    dimension = result.trace()["validation_dimensions"]["answer_completeness"]
    assert dimension["execution"] == "partial"
    if case == "duplicate":
        assert dimension["contract_validation"]["duplicate_request_ids"] == ["Q1"]
    elif case == "invalid":
        assert dimension["contract_validation"]["invalid_request_result_ids"] == ["Q1"]


def test_complete_wire_is_recorded_separately_from_partial_business_coverage():
    result = run()
    assert result.review_execution == "completed"
    assert result.timing["audit_contract"]["strict_wire_validation"] == "passed"
    assert observed_audit(result).request_reviews[0].status == "partial"
    assert result.guide_steps[0]["status"] != "explained"


def test_guide_source_alias_uses_the_same_ids_as_actual_audit_evidence():
    result = run(use_alias=True)
    assert observed_audit(result).reviews[0].support == "supported"


@pytest.mark.parametrize("case", ["failure", "empty"])
def test_failed_conditional_retry_keeps_valid_first_reviews_and_discards_bad_observation(case):
    result = run(retry_case=case)
    assert result.review_execution == ("error" if case == "failure" else "partial")
    assert observed_audit(result).reviews[0].support == "supported"
    assert observed_audit(result).request_reviews[0].status == "partial"
    assert [row.requirement_id for row in observed_audit(result).guide_requirement_reviews] == ["open:1"]
    assert result.guide_steps[0]["requirements"][0]["status"] == "unknown"


def test_partial_second_audit_preserves_raw_contradiction_without_adopting_it():
    result = run(retry_case="partial", first_contradiction=True)
    assert result.review_execution == "partial"
    assert observed_audit(result).reviews[0].support == "contradicted"
    assert result.timing["audit_contract"]["strict_wire_validation"] == "failed"
    merged = result.timing["audit_contract"]["retry_merge"]
    assert merged["row_sources"]["reviews"] == [{"key": 0, "attempt": 1}]
    assert merged["row_sources"]["request_reviews"] == [{"key": "Q1", "attempt": 2}]
