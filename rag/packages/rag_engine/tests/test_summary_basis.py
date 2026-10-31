"""摘要の依存先を最終支持済み本文へ全文で結び、無関係な撤回で正しい摘要を失わない。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedAudit, GroundedDraft, GroundedFactAudit, GroundedItem

FIRST = "対象Aの設定を開きます。"
SECOND = "内容を確認して保存します。"
SUMMARY = "設定は2段階で行います。"
BAD = "保存後は自動で通知されます。"


def entry(text, *, support="supported", removed=False):
    result = grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, applies="matched"),
        span={"evidence_id": "E1", "text": text},
    )
    result.review_assessment = {"effective_support": support}
    if removed:
        result.mark("contradicted", "原文と矛盾")
        result.removed = True
    elif support != "supported":
        result.mark("unassessed", "未確認")
    return result


def round_value(basis, *, summary=SUMMARY, checked=None, supported=True):
    audit = GroundedAudit(reviews=[], summary_supported=supported, summary_basis=basis, goal_alignment="aligned")
    checked = checked if checked is not None else [entry(FIRST), entry(SECOND), entry(BAD, removed=True)]
    return grounded.Round(summary, GroundedDraft(summary=summary, items=[], confidence="high"), checked, [], audit, audit_calls=1)


def test_mixed_body_summary_uses_only_final_supported_items():
    current = round_value([FIRST, SECOND])
    result = grounded.finalize_summary(current)
    assert result.summary == SUMMARY and not result.summary_reason
    assert result.checked is current.checked


@pytest.mark.parametrize("basis", [[], ["本文にない説明"], [FIRST[:5]], [FIRST, BAD], [FIRST + "\n" + SECOND]])
def test_missing_forged_partial_or_withdrawn_basis_is_not_valid(basis):
    result = grounded.finalize_summary(round_value(basis))
    assert result.summary == grounded.answer_labels().neutral_summary
    assert result.summary_reason


def test_unknown_item_cannot_support_summary_even_when_quote_exists():
    current = round_value([FIRST], checked=[entry(FIRST, support="unknown"), entry(SECOND)])
    assert grounded.finalize_summary(current).summary == grounded.answer_labels().neutral_summary


def test_deterministic_confirmed_without_semantic_support_cannot_bind_mixed_summary():
    first = entry(FIRST)
    first.review_assessment = {}
    current = round_value([FIRST], checked=[first, entry(BAD, removed=True)])
    assert grounded.finalize_summary(current).summary == grounded.answer_labels().neutral_summary


def test_unsupported_summary_is_not_saved_by_valid_basis():
    assert grounded.finalize_summary(round_value([FIRST, SECOND], supported=False)).summary == grounded.answer_labels().neutral_summary


def test_original_compound_basis_does_not_bind_a_salvaged_fragment():
    fragment = entry(FIRST)
    fragment.review_assessment["partial_salvage"] = {"status": "retained"}
    current = round_value([FIRST + BAD], checked=[fragment])
    assert grounded.finalize_summary(current).summary == grounded.answer_labels().neutral_summary


def test_basis_survives_reordering_without_indices():
    current = round_value([FIRST, SECOND], checked=[entry(BAD, removed=True), entry(SECOND), entry(FIRST)])
    assert grounded.finalize_summary(current).summary == SUMMARY


@pytest.mark.parametrize("execution", ["not_run", "error", "timed_out"])
def test_unfinished_audit_does_not_approve_summary_basis(execution):
    current = round_value([FIRST, SECOND])
    if execution == "not_run":
        current.audit_calls = 0
    elif execution == "error":
        current.timing["audit_error_type"] = "ValueError"
    else:
        current.audit_cut = True
        current.audit = None
    assert grounded.finalize_summary(current).summary == grounded.answer_labels().neutral_summary


def test_all_confirmed_body_keeps_previous_behavior_without_basis():
    current = round_value([], checked=[entry(FIRST), entry(SECOND)])
    assert grounded.finalize_summary(current).summary == SUMMARY


@pytest.mark.parametrize("schema", [GroundedAudit, GroundedFactAudit])
def test_summary_basis_is_optional_for_older_audit_outputs(schema):
    assert schema(reviews=[], summary_supported=True, goal_alignment="aligned").summary_basis == []


@pytest.mark.parametrize("mutation", ["none", "text", "source", "version", "quote", "application", "application_state", "summary"])
def test_application_summary_requires_unchanged_verified_dependency_snapshots(mutation):
    from rag_engine.generation.applications import CheckedApplication
    from rag_engine.models.llm import GroundedApplication, GroundedCitation

    first = entry(FIRST)
    first.item.citations = [GroundedCitation(evidence_id="E1", quote=FIRST)]
    current = round_value([], checked=[first], supported=False)
    application = CheckedApplication(
        GroundedApplication(reasoning="確認した設定を適用します。", result=SUMMARY), entries=[first], source="step"
    )
    current.applications = [application]
    basis = grounded.summary_basis_snapshot([first])
    state = grounded.application_summary_snapshot([application])
    if mutation == "text":
        first.item.text += "追加の操作"
    elif mutation == "source":
        first.span["source_id"] = "another-source"
    elif mutation == "version":
        first.span["version"] = "another-version"
    elif mutation == "quote":
        first.item.citations[0].quote = SECOND
    elif mutation == "application":
        application.application.result = "別の結果"
    elif mutation == "application_state":
        application.reject("当てはめを撤回")
    elif mutation == "summary":
        current.summary = "別の結論"
    result = grounded.finalize_summary(current, application_summary=SUMMARY, application_basis=basis, application_state=state)
    assert (result.summary == SUMMARY) is (mutation == "none")
    assert result.checked is current.checked
