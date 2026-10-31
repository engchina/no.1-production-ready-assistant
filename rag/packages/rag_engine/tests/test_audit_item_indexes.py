"""監査後に重複を除いても、段落とガイドの案内の参照を保つ（#3075）。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem


def entry(text="対象を送信します。", *, quote=None):
    return grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, citations=[GroundedCitation(evidence_id="", quote=quote or text)]),
        span={"text": quote or text, "evidence_id": "a"},
    )


def observation(indexes, quote):
    return {
        "requirement_id": "save:0",
        "answer_state": "explained",
        "item_indexes": indexes,
        "answer_quote": quote,
        "reason": "",
    }


def audit(review):
    return GroundedAudit(
        reviews=[{"index": 2, "support": "supported", "applicability": "matched", "reason": ""}],
        guide_requirement_reviews=[review],
        summary_supported=True,
        goal_alignment="aligned",
    )


def test_item_reviews_and_requirements_follow_the_remaining_items():
    items = [entry(), entry(), entry("保存します。")]
    checked, result = grounded.deduplicate_audited_items(items, audit(observation([2], "保存します。")), 0)
    assert len(checked) == 2
    assert checked[1] is items[2]
    assert result.reviews[0].index == 1
    assert result.guide_requirement_reviews[0].item_indexes == [1]
    assert grounded.requirement_quote_matches(result.guide_requirement_reviews[0], checked)


def test_an_exact_duplicate_can_reference_the_retained_description():
    items = [entry(), entry(), entry("保存します。")]
    checked, result = grounded.deduplicate_audited_items(items, audit(observation([1, 0], items[0].item.text)), 0)
    assert result.guide_requirement_reviews[0].item_indexes == [0]
    assert grounded.requirement_quote_matches(result.guide_requirement_reviews[0], checked)


def test_notice_indexes_follow_the_new_item_count():
    items = [entry(), entry(), entry("保存します。")]
    notices = ["窓口へ連絡してください。", "処理の条件を確認してください。"]
    checked, result = grounded.deduplicate_audited_items(items, audit(observation([4], notices[1])), len(notices))
    assert result.guide_requirement_reviews[0].item_indexes == [3]
    assert grounded.requirement_quote_matches(result.guide_requirement_reviews[0], checked, notices)


@pytest.mark.parametrize("index", [-1, 5, 99])
def test_invalid_indexes_do_not_become_item_or_notice_references(index):
    items = [entry(), entry(), entry("保存します。")]
    checked, result = grounded.deduplicate_audited_items(items, audit(observation([index], "窓口へ連絡してください。")), 1)
    assert result.guide_requirement_reviews[0].item_indexes == [-1]
    assert not grounded.requirement_quote_matches(result.guide_requirement_reviews[0], checked, ["窓口へ連絡してください。"])


def test_distinct_claims_from_one_quote_keep_their_own_reference():
    quote = "対象を送信します。送信結果を確認します。"
    items = [entry("対象を送信します。", quote=quote), entry("送信結果を確認します。", quote=quote)]
    checked, result = grounded.deduplicate_audited_items(items, audit(observation([1], items[1].item.text)), 1)
    assert len(checked) == 2
    assert result.guide_requirement_reviews[0].item_indexes == [1]
    assert grounded.requirement_quote_matches(result.guide_requirement_reviews[0], checked, ["別の案内です。/notice"])
