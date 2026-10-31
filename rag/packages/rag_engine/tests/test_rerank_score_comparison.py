"""同じ採点から二つの順位を比較し、既定の集約は変更しない。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rag_engine.generation.answering import _best_rerank_ranks
from rag_engine.models.llm import RerankTextRank


@pytest.mark.parametrize(
    "original,extra,supported,required,expected_original,expected_max",
    [
        # 全手順を説明する候補が、局所的な高得点の二候補に押し出される場合。
        ([0.9, 0.8, 0.7], [0.85, 0.98, 0.97], [{"S1", "S2", "S3"}, {"S1"}, {"S2"}], {"S1", "S2", "S3"}, 3, 2),
        # 補助検索文が別の必須観点を救う場合。
        ([0.9, 0.8, 0.1], [0.7, 0.2, 0.99], [{"S1"}, set(), {"S2"}], {"S1", "S2"}, 1, 2),
    ],
)
def test_frozen_scores_expose_both_coverage_tradeoffs_without_more_calls(
    original, extra, supported, required, expected_original, expected_max
):
    rows = []
    calls = []

    def provider(query, documents, settings, top_n):
        calls.append(query)
        scores = original if query == "全体の手順は？" else extra
        return [RerankTextRank(i, scores[i]) for i in sorted(range(len(scores)), key=lambda i: -scores[i])]

    with (
        patch("rag_engine.generation.answering.rerank_text_with_scores", side_effect=provider),
        patch("rag_engine.generation.answering.record_retrieval_stage", side_effect=rows.append),
    ):
        ranks = _best_rerank_ranks(
            "全体の手順は？",
            ["一部の確認方法は？"],
            ["合成候補A", "合成候補B", "合成候補C"],
            None,
            SimpleNamespace(add=lambda _: None),
            rerank_phase="main_search",
            candidate_ids=["A", "B", "C"],
        )

    comparison = rows[-1]

    def coverage(key):
        selected = comparison[key][:2]
        return len(set().union(*(supported[item["index"]] for item in selected)) & required)

    assert calls == ["全体の手順は？", "一部の確認方法は？"]
    assert comparison["aggregation"] == "max"
    assert comparison["candidate_ids"] == ["A", "B", "C"]
    assert len(comparison["original_ranks"]) == len(comparison["merged_ranks"]) == 3
    assert coverage("original_ranks") == expected_original
    assert coverage("merged_ranks") == expected_max
    assert [item.index for item in ranks] == [item["index"] for item in comparison["merged_ranks"]]
