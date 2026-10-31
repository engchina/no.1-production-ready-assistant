"""モデルの範囲修復が失敗しても、同じ出典の逐語原文が一意なら番号だけ復元する。"""

from typing import Any

import pytest
from test_citation_range_repair import CLAIM, LONG, QUESTION, evidence, proposal, response, run_first

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_range_repair import apply_range_repair
from rag_engine.models.llm import RequestCoverage, RequestEvidenceScope


def prepared(*, text=LONG, quote=None, relation="direct", request_id="Q1"):
    spans = evidence(text)
    draft = proposal(0, 0)
    scope = RequestEvidenceScope(
        evidence_id="E1", target="合成の操作", relation=relation, link_quote=LONG.splitlines()[2] if quote is None else quote
    )
    draft.request_coverage = [RequestCoverage(request_id=request_id, evidence_ids=["E1"], evidence_scope=[scope])]
    first = run_first(spans, draft)
    targets = grounded.citation_range_targets(first.draft, spans, first.checked, first.dropped)
    failed, diagnostic = apply_range_repair(first.draft, response(1, 999), targets)
    return spans, first, targets, failed, diagnostic


def test_failed_model_range_is_recovered_from_same_request_source_and_exact_quote():
    spans, first, targets, failed, diagnostic = prepared()
    before = first.draft.model_dump()
    fixed, result = grounded.repair_ranges_from_plan_quotes(failed, spans, targets, diagnostic)
    assert result["status"] == "completed"
    assert result["plan_quote_fallback"] == [{"item_index": 0, "citation_index": 0}]
    assert result["model_rejected"][0]["reason_code"] == "citation_range_out_of_bounds"
    assert fixed.items[0].citations[0]._wire_range == (3, 3)
    assert fixed.items[0].text == CLAIM
    expected = first.draft.model_copy(deep=True)
    expected.items[0].citations[0].lines = "3"
    assert fixed.model_dump() == expected.model_dump()
    assert first.draft.model_dump() == before
    checked, dropped = grounded.verify(QUESTION, fixed, spans)
    assert not grounded.citation_range_issues(checked, dropped)


@pytest.mark.parametrize(
    "change", ["linked", "no_quote", "wrong_quote", "duplicate_quote", "wrong_request", "duplicate_scope", "ambiguous_source"]
)
def test_plan_fallback_keeps_unconfirmed_when_source_or_original_is_ambiguous(change):
    options = {}
    if change == "linked":
        options["relation"] = "linked"
    if change == "no_quote":
        options["quote"] = ""
    if change == "wrong_quote":
        options["quote"] = "根拠に存在しない原文の文字列です。"
    if change == "duplicate_quote":
        options["text"] = LONG + "\n" + LONG.splitlines()[2]
    if change == "wrong_request":
        options["request_id"] = "Q2"
    spans, first, targets, failed, diagnostic = prepared(**options)
    if change == "duplicate_scope":
        failed.request_coverage[0].evidence_scope *= 2
    if change == "ambiguous_source":
        spans.append({**spans[0], "evidence_id": "another"})
    fixed, result = grounded.repair_ranges_from_plan_quotes(failed, spans, targets, diagnostic)
    assert not result.get("plan_quote_fallback")
    assert fixed.model_dump() == failed.model_dump()


def test_real_round_recovers_failed_range_without_another_model_call_or_body_edit():
    spans, first, _targets, _failed, _diagnostic = prepared()
    calls = []

    def parser(*args: Any, **kwargs: Any):
        calls.append(1)
        return response(1, 999)

    second = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="同じ根拠",
        image_paths=(),
        provider_id=None,
        parse_text=parser,
        parse_images=parser,
        previous=first.checked,
        previous_draft=first.draft,
        previous_dropped=first.dropped,
        audit_mode="none",
    )
    assert len(calls) == 1 and second.audit_calls == 0
    assert second.timing["citation_range_repair"]["plan_quote_fallback"]
    assert second.draft.items[0].text == first.draft.items[0].text
    assert not grounded.citation_range_issues(second.checked, second.dropped)


@pytest.mark.parametrize("valid", [True, False])
def test_valid_range_or_identity_rejection_is_not_overwritten(valid):
    spans, first, targets, _failed, _diagnostic = prepared()
    model_response = response(3, 3) if valid else response(1, 999, identity="E2")
    candidate, diagnostic = apply_range_repair(first.draft, model_response, targets)
    fixed, result = grounded.repair_ranges_from_plan_quotes(candidate, spans, targets, diagnostic)
    assert not result.get("plan_quote_fallback")
    assert fixed.model_dump() == candidate.model_dump()


@pytest.mark.parametrize("fragmented", [True, False])
def test_fragmented_original_or_trusted_heading_is_not_used(fragmented):
    title = "合成資料の十分に長い節見出し"
    if fragmented:
        quote = LONG.splitlines()[0] + LONG.splitlines()[2]
        spans, _first, targets, failed, diagnostic = prepared(quote=quote)
    else:
        spans, _first, targets, failed, diagnostic = prepared(text=title + "\n" + LONG, quote=title)
        spans[0].update(section_path=[title], section_path_sources=["section_header"])
    fixed, result = grounded.repair_ranges_from_plan_quotes(failed, spans, targets, diagnostic)
    assert not result.get("plan_quote_fallback") and fixed.model_dump() == failed.model_dump()


def test_partial_model_success_is_kept_when_only_other_citation_needs_original_alignment():
    from rag_engine.generation.citation_range_repair import CitationRangeRepair
    from rag_engine.models.llm import GroundedCitation

    spans, first, _targets, _failed, _diagnostic = prepared()
    spans.append({**spans[0], "evidence_id": "stable-b", "label": "E2", "source_id": "source-b"})
    draft = first.draft.model_copy(deep=True)
    draft.items[0].citations.append(GroundedCitation(evidence_id="E2", lines="0"))
    draft.items[0].citations[-1]._wire_range = (0, 0)
    plan = draft.request_coverage[0]
    plan.evidence_ids.append("E2")
    plan.evidence_scope.append(plan.evidence_scope[0].model_copy(update={"evidence_id": "E2"}))
    checked, dropped = grounded.verify(QUESTION, draft, spans)
    targets = grounded.citation_range_targets(draft, spans, checked, dropped)
    model = CitationRangeRepair(ranges=[response(3, 3).ranges[0], response(1, 999, index=1, identity="E2").ranges[0]])
    candidate, diagnostic = apply_range_repair(draft, model, targets)
    fixed, result = grounded.repair_ranges_from_plan_quotes(candidate, spans, targets, diagnostic)
    assert result["status"] == "completed"
    assert result["plan_quote_fallback"] == [{"item_index": 0, "citation_index": 1}]
    assert [ref._wire_range for ref in fixed.items[0].citations] == [(3, 3), (3, 3)]
