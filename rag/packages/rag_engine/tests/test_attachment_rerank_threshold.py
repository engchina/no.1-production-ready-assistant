"""選択済みの会話の添付を rerank の下限で落とさない（#3211）。"""

from dataclasses import replace

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.generation.answer_records import _records_by_rerank_ranks
from rag_engine.models.llm import RerankTextRank
from rag_engine.retrieval.attachment_evidence import ATTACHMENT_EVIDENCE_KEY
from tests.test_document_selection import child, parent


def _candidates():
    records = [
        child("a", 1, "資料 A の本文", "a.txt", "pa"),
        child("b", 2, "資料 B の本文", "b.txt", "pb"),
        child("attachment", 3, "項目,値\nA,17", "input.txt", "pc"),
    ]
    records[-1].metadata[ATTACHMENT_EVIDENCE_KEY] = True
    parents = [
        parent(f"p{key}", 1, text, source)
        for key, text, source in (("a", "資料 A の本文", "a.txt"), ("b", "資料 B の本文", "b.txt"), ("c", "項目,値\nA,17", "input.txt"))
    ]
    return records, parents


@pytest.mark.parametrize("threshold", [0.0, 0.1], ids=["below-top-k", "below-min-score"])
def test_low_scored_attachment_reaches_context(threshold):
    settings = replace(get_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=threshold)
    records, parents = _candidates()
    ranks = [RerankTextRank(0, 0.9), RerankTextRank(1, 0.8), RerankTextRank(2, 0.01)]
    ranked = _records_by_rerank_ranks(records, ranks, settings)
    assert [r.id for r in ranked] == ["a", "b", "attachment"]
    selected, _, _ = a.select_documents(ranked, settings, top_k=2)
    anchors = a.cap_anchors_per_document(selected, settings, top_k=2)
    bundle = a._bundle_context(
        "添付の項目 A の値を資料と照合する",
        anchors,
        [*records, *parents],
        settings,
        top_k=2,
        neighbor_child_count=0,
        max_records=2,
        max_chars=1000,
    )
    assert [r.id for r in bundle.records] == ["pa", "pb", "pc"]
    assert "A,17" in bundle.text


@pytest.mark.parametrize("flag", [None, False, "true"], ids=["no-flag", "false", "string"])
def test_low_score_without_the_explicit_attachment_flag_is_still_filtered(flag):
    settings = replace(get_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=0.1)
    records, _ = _candidates()
    records[-1].metadata[ATTACHMENT_EVIDENCE_KEY] = flag
    ranked = _records_by_rerank_ranks(records, [RerankTextRank(0, 0.9), RerankTextRank(1, 0.8), RerankTextRank(2, 0.01)], settings)
    assert [r.id for r in ranked] == ["a", "b"]


def test_attachment_keeps_its_real_score_and_original_candidate_index():
    settings = replace(get_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=0.1)
    records, _ = _candidates()
    ranked = _records_by_rerank_ranks(
        records, [RerankTextRank(2, 0.01), RerankTextRank(2, 0.8), RerankTextRank(99, 1.0)], settings, candidate_indices=[5, 7, 9]
    )
    attached = ranked[0]
    assert attached.id == "attachment"
    assert attached.metadata["rerank"]["relevance_score"] == 0.01
    assert attached.metadata["rerank"]["candidate_index"] == 9
    assert attached.metadata["rerank"]["attachment_preserved_below_min_score"] is True
    assert sum(r.id == "attachment" for r in ranked) == 1


def test_attachment_still_obeys_the_context_character_limit():
    settings = replace(get_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=0.1)
    records, parents = _candidates()
    parents[-1] = replace(parents[-1], text="添付の長い本文" * 100)
    ranked = _records_by_rerank_ranks(records, [RerankTextRank(0, 0.9), RerankTextRank(1, 0.8), RerankTextRank(2, 0.01)], settings)
    bundle = a._bundle_context(
        "添付", ranked, [*records, *parents], settings, top_k=2, neighbor_child_count=0, max_records=2, max_chars=100
    )
    assert [r.id for r in bundle.records] == ["pa", "pb"]
    assert {"id": "pc", "reason": "context_character_budget", "context_reason": "conversation_attachment"} in bundle.excluded_evidence


def test_retrieval_trace_records_attachment_retention_and_character_exclusion(monkeypatch):
    settings = replace(get_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=0.1)
    records, parents = _candidates()
    ranked = _records_by_rerank_ranks(records, [RerankTextRank(0, 0.9), RerankTextRank(1, 0.8), RerankTextRank(2, 0.01)], settings)
    stages = []
    monkeypatch.setattr(a, "record_retrieval_stage", stages.append)
    monkeypatch.setattr(a, "_rerank_records", lambda *args, **kwargs: ranked)
    a.rerank_records("添付", records, settings, enabled=True, business_domains=(), screen_terms=())
    output = next(stage for stage in stages if stage["stage"] == "rerank_output")
    assert output["attachment_candidate_count"] == output["attachment_selected_count"] == 1
    assert output["attachment_preserved_below_min_score_count"] == 1
    parents[-1] = replace(parents[-1], text="長い本文" * 100)
    a._bundle_context("添付", ranked, [*records, *parents], settings, top_k=2, neighbor_child_count=0, max_records=2, max_chars=100)
    context = next(stage for stage in stages if stage["stage"] == "answer_context")
    assert context["attachment_anchor_count"] == 1
    assert context["attachment_context_count"] == 0
    assert any(item["reason"] == "context_character_budget" for item in context["excluded"])
