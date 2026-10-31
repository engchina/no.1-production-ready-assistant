"""同一表記の再順位付けを省き、別条件の検索文を保持する。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rag_engine.generation.answering import _best_rerank_ranks
from rag_engine.models.llm import RerankTextRank
from rag_engine.retrieval.query_identity import normalized_query_key


@pytest.mark.parametrize("variant", ["ABC 設定", "ＡＢＣ　設定", " ABC\t\n設定 "])
def test_equivalent_query_forms_share_identity(variant):
    assert normalized_query_key(variant) == "ABC 設定"


@pytest.mark.parametrize("different", ["ABC設定", "abc 設定", "ABC 設定 追加条件", "ABC 変更"])
def test_distinct_question_boundaries_are_preserved(different):
    assert normalized_query_key(different) != normalized_query_key("ABC 設定")


def test_rerank_deduplicates_before_limit_and_preserves_raw_queries_and_scores():
    original = "ＡＢＣ  設定"
    other = "ABC 設定 追加条件"
    calls = []

    def rank(query, documents, settings, top_n):
        calls.append((query, list(documents), top_n))
        return [RerankTextRank(0, 0.9 if query == original else 0.3), RerankTextRank(1, 0.2 if query == original else 0.95)]

    with patch("rag_engine.generation.answering.rerank_text_with_scores", side_effect=rank):
        result = _best_rerank_ranks(
            original,
            ["ABC 設定", "ABC\t設定", "別の観点"] * 7 + [other, "別の観点\n"],
            ["A", "B"],
            None,
            SimpleNamespace(add=lambda text: None),
        )
    assert calls == [(original, ["A", "B"], 2), ("別の観点", ["A", "B"], 2), (other, ["A", "B"], 2)]
    assert [(r.index, r.relevance_score, r.question_relevance_score) for r in result] == [(1, 0.95, 0.2), (0, 0.9, 0.9)]


def test_original_and_max_comparison_uses_all_frozen_candidates_without_extra_calls():
    from rag_engine.resources.rerank_diagnostics import current_rerank_receipt

    rows = []
    contexts = []

    def rank(query, documents, settings, top_n):
        contexts.append(current_rerank_receipt().to_dict())
        return (
            [RerankTextRank(0, 0.9), RerankTextRank(1, 0.2)] if query == "元の質問" else [RerankTextRank(1, 0.95), RerankTextRank(0, 0.3)]
        )

    with (
        patch("rag_engine.generation.answering.rerank_text_with_scores", side_effect=rank),
        patch("rag_engine.generation.answering.record_retrieval_stage", side_effect=rows.append),
    ):
        ranks = _best_rerank_ranks(
            "元の質問",
            ["別の観点"],
            ["本文A", "本文B"],
            None,
            SimpleNamespace(add=lambda text: None),
            rerank_phase="guide_step",
            candidate_ids=["A", "B"],
        )
    assert len(contexts) == 2
    assert [row["question_index"] for row in contexts] == [0, 1]
    assert [row["query_role"] for row in contexts] == ["original", "supplement"]
    assert {row["phase"] for row in contexts} == {"guide_step"}
    assert len({row["batch_id"] for row in contexts}) == 1
    assert all(row["candidate_count"] == 2 and len(row["candidate_hash"]) == 64 for row in contexts)
    assert len({row["candidate_hash"] for row in contexts}) == 1
    assert len({row["normalized_query_hash"] for row in contexts}) == 2
    assert all(row["sdk_attempt_count"] is None for row in contexts)
    comparison = rows[-1]
    assert comparison["stage"] == "rerank_score_comparison"
    assert comparison["candidate_ids"] == ["A", "B"]
    assert comparison["original_ranks"] == [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.2}]
    assert comparison["merged_ranks"] == [{"index": 1, "score": 0.95}, {"index": 0, "score": 0.9}]
    assert [rank.index for rank in ranks] == [1, 0]
    assert all(row["logical_status"] == "completed" for row in comparison["queries"])
    assert all(row["status"] == "completed" for row in comparison["queries"])
    assert all(row["cache_state"] == "unknown" and row["sdk_attempt_count"] is None for row in comparison["queries"])
    assert current_rerank_receipt() is None


def test_extra_query_failure_keeps_original_comparison_and_records_failed_logical_call():
    rows = []
    with (
        patch(
            "rag_engine.generation.answering.rerank_text_with_scores", side_effect=[[RerankTextRank(0, 0.7)], RuntimeError("unavailable")]
        ),
        patch("rag_engine.generation.answering.record_retrieval_stage", side_effect=rows.append),
        patch("rag_engine.generation.answering.report_degraded"),
    ):
        ranks = _best_rerank_ranks("元の質問", ["別の観点"], ["A"], None, SimpleNamespace(add=lambda text: None))
    assert ranks[0].relevance_score == 0.7
    assert rows[-1]["phase"] == "other"
    assert [row["logical_status"] for row in rows[-1]["queries"]] == ["completed", "error"]
    assert [row["status"] for row in rows[-1]["queries"]] == ["completed", "error"]


def test_export_retains_original_question_and_max_scores_separately():
    from rag_engine.generation.answer_payload import _rerank_scores_from_evidence
    from tests.test_document_selection import child

    record = child("A", 1, "合成本文", "合成資料", "parent", 0.95, question_score=0.2)
    exported = _rerank_scores_from_evidence([record], ())
    assert exported[0]["relevance_score"] == 0.95
    assert exported[0]["question_relevance_score"] == 0.2


def test_original_only_preserves_provider_order_and_duplicate_index_handling():
    original = [RerankTextRank(1, 0.91), RerankTextRank(99, 0.1), RerankTextRank(1, 0.9)]
    with patch("rag_engine.generation.answering.rerank_text_with_scores", return_value=original) as provider:
        ranks = _best_rerank_ranks("元の質問", [" 元の質問 "], ["A", "B"], None, SimpleNamespace(add=lambda text: None))
    assert ranks == original
    assert provider.call_count == 1


def test_distinct_supplement_budget_is_unchanged():
    with patch("rag_engine.generation.answering.rerank_text_with_scores", return_value=[RerankTextRank(0, 0.7)]) as provider:
        _best_rerank_ranks("元の質問", [f"別の観点{i}" for i in range(9)], ["A"], None, SimpleNamespace(add=lambda text: None))
    assert provider.call_count == 7
