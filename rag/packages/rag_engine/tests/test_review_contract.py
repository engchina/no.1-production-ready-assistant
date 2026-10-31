"""原判定・適用方針・引用修復を区別する契約。実モデルは呼ばない。"""

import pytest

from rag_engine.generation.grounded import (
    CheckedItem,
    answer_use,
    applicability_policy,
    apply_audit,
    recover_audited_quotes,
    verify,
)
from rag_engine.models.llm import GroundedAudit, GroundedDraft, GroundedItem, GroundedItemReview


def span(text: str) -> dict:
    return {"evidence_id": "e1", "text": text, "source_id": "c1", "source": "source.pdf", "page": 1, "tags": []}


def audit(applicability: str, support: str = "supported") -> GroundedAudit:
    return GroundedAudit.model_validate(
        {
            "reviews": [
                {"index": 0, "support": support, "applicability": applicability, "reason": "判定理由", "condition": "条件Aが成立するとき"}
            ],
            "summary_supported": True,
            "goal_alignment": "aligned",
        }
    )


@pytest.mark.parametrize("applicability", ["unknown", "not_applicable"])
def test_applicability_preserves_observation_without_rejecting_a_prerequisite(applicability: str) -> None:
    observed = audit(applicability)
    policy = applicability_policy(observed)
    review = policy.reviews[0]
    assert review.applicability == observed.reviews[0].applicability == applicability
    assert review.observed_applicability == applicability
    assert review.applicability_action == "record_only"
    assert review.applicability_policy_reason
    entry = CheckedItem(GroundedItem(kind="rule", text="前提となる手順です。"), span("前提となる手順です。"))
    apply_audit([entry], policy)
    assert entry.published and not entry.removed and not entry.marks
    assert not entry.confirmed and entry.item.applies == "unverified"
    assert answer_use(entry) == "reference"
    assert entry.review_assessment["applicability"] == applicability
    assert entry.review_assessment["applicability_action"] == "record_only"
    assert observed.reviews[0].applicability_action == "apply"  # 元の応答は書き換えない。


def test_record_only_applicability_does_not_hide_a_contradiction() -> None:
    entry = CheckedItem(GroundedItem(kind="rule", text="処理は不要です。"), span("処理が必要です。"))
    apply_audit([entry], applicability_policy(audit("unknown", "contradicted")))
    assert entry.removed and entry.withdrawn
    assert entry.review_assessment["support"] == "contradicted"


def test_explicit_source_condition_still_applies() -> None:
    entry = CheckedItem(GroundedItem(kind="rule", text="処理できます。"), span("条件Aが成立するとき、処理できます。"))
    apply_audit([entry], applicability_policy(audit("conditional")))
    assert entry.item.applies == "conditional"
    assert entry.item.condition == "条件Aが成立する"
    assert entry.review_assessment["applicability_action"] == "apply"


def test_policy_is_not_part_of_the_model_response_contract() -> None:
    properties = GroundedItemReview.model_json_schema()["properties"]
    assert not {"applicability_action", "observed_applicability", "applicability_policy_reason"} & properties.keys()


def test_near_quote_is_a_candidate_until_a_verbatim_support_quote_is_checked() -> None:
    original = "入力した情報を確認してから、画面の保存ボタンを押してください。"
    misquoted = "入力した情報を確認してから、画面の保存ボタンを押してくださいます。"
    draft = GroundedDraft(
        items=[GroundedItem(kind="rule", text="入力後に保存します。", citations=[{"evidence_id": "e1", "quote": misquoted}])],
        summary="",
        confidence="low",
    )
    checked, _ = verify("操作を教えてください。", draft, [span(original)])
    entry = checked[0]
    assert entry.quote_match == "fuzzy"
    assert entry.unmatched and not entry.confirmed
    assert entry.citation_resolution["requested_quote"] == misquoted
    assert entry.citation_resolution["resolved_quote"] == original
    policy = applicability_policy(audit("matched"))
    # 支持の判定だけでは引用一致を昇格しない。
    apply_audit(checked, policy)
    assert entry.unmatched and not entry.confirmed
    policy.reviews[0].support_quote = original
    assert recover_audited_quotes("操作を教えてください。", checked, policy, [span(original)]) == {0}
    assert not entry.unmatched and entry.quote_match == "audited_paraphrase"
    assert entry.citation_resolution["requested_quote"] == misquoted
    assert entry.citation_resolution["support_check"] == "audited_paraphrase"


def test_model_output_cannot_override_the_application_policy():
    review = GroundedItemReview(
        index=0,
        support="supported",
        applicability="conditional",
        condition="条件A",
        reason="原文の条件",
        applicability_action="record_only",
    )
    result = applicability_policy(audit("conditional").model_copy(update={"reviews": [review]}))
    assert result.reviews[0].applicability_action == "apply"
    assert result.reviews[0].observed_applicability == "conditional"
