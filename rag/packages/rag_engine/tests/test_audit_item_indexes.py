"""監査後に重複を除いても、段落の参照を保つ（#3075）。"""

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem


def entry(text="対象を送信します。", *, quote=None):
    return grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, citations=[GroundedCitation(evidence_id="", quote=quote or text)]),
        span={"text": quote or text, "evidence_id": "a"},
    )


def audit(index):
    return GroundedAudit(
        reviews=[{"index": index, "support": "supported", "applicability": "matched", "reason": ""}],
        summary_supported=True,
        goal_alignment="aligned",
    )


def test_item_reviews_follow_the_remaining_items():
    items = [entry(), entry(), entry("保存します。")]
    checked, result = grounded.deduplicate_audited_items(items, audit(2))
    assert len(checked) == 2
    assert checked[1] is items[2]
    assert result.reviews[0].index == 1


def test_an_exact_duplicate_can_reference_the_retained_description():
    items = [entry(), entry(), entry("保存します。")]
    _checked, result = grounded.deduplicate_audited_items(items, audit(1))
    assert result.reviews[0].index == 0


def test_invalid_indexes_do_not_become_item_references():
    items = [entry(), entry(), entry("保存します。")]
    _checked, result = grounded.deduplicate_audited_items(items, audit(99))
    assert result.reviews[0].index == -1


def test_distinct_claims_from_one_quote_keep_their_own_reference():
    quote = "対象を送信します。送信結果を確認します。"
    items = [entry("対象を送信します。", quote=quote), entry("送信結果を確認します。", quote=quote)]
    checked, result = grounded.deduplicate_audited_items(items, audit(1))
    assert len(checked) == 2
    assert result.reviews[0].index == 1
