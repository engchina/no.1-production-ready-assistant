"""RAG 診断情報のテスト。"""

import pytest

from app.config import Settings
from app.rag.diagnostics import build_search_diagnostics, rag_config_fingerprint
from app.schemas.search import SearchDiagnostics, SearchRequest


def test_search_diagnostics_exposes_execution_shape_without_secrets() -> None:
    """検索 diagnostics は回答の経路と設定の fingerprint を示し、secret 値を含めない。"""
    settings = Settings(oracle_password="super-secret-password")
    request = SearchRequest(
        query="承認条件",
        top_k=7,
        filters={"status": "indexed", "file_name": "policy"},
        knowledge_base_ids=["kb-1", "kb-2"],
    )

    diagnostics = build_search_diagnostics(
        request,
        settings=settings,
        retrieval_strategy_adapter="grounded",
        answer={"confidence": "high"},
    )

    assert diagnostics.retrieval_strategy == "hybrid"
    assert diagnostics.retrieval_strategy_adapter == "grounded"
    # 常に通る安全チェックの段は外した（#37）。診断にその項目は無い。
    assert not any(key.startswith("guardrail") for key in diagnostics.model_dump())
    assert diagnostics.answer == {"confidence": "high"}
    assert diagnostics.filter_keys == ["file_name", "knowledge_base_id", "status"]
    assert diagnostics.knowledge_base_count == 2
    assert len(diagnostics.config_fingerprint) == 64
    assert "super-secret-password" not in diagnostics.model_dump_json()


def test_search_diagnostics_has_no_removed_standard_fields() -> None:
    """旧 standard の回答エンジンの診断(検索の内訳・context の件数など)は持たない(#595)。"""
    fields = set(SearchDiagnostics.model_fields)
    for removed in (
        "mode",
        "retrieval_breakdown",
        "retrieval_candidates",
        "generation_profile",
        "context_expanded_count",
        "agent_memory_retrieved_count",
        "stream_stage_timings",
        "crag_evidence_grade",
    ):
        assert removed not in fields
    # 保存済みの評価結果などに残る古い診断も読める(未知の項目は読み捨てる)。
    legacy = SearchDiagnostics.model_validate(
        {"mode": "hybrid", "retrieval_strategy": "hybrid", "retrieval_breakdown": {}}
    )
    assert legacy.retrieval_strategy == "hybrid"


def test_rag_config_fingerprint_changes_when_rag_parameters_change() -> None:
    """fingerprint は RAG の非機密設定変更を反映する。"""
    first = rag_config_fingerprint(Settings(rag_chunk_size=800))
    second = rag_config_fingerprint(Settings(rag_chunk_size=1200))

    assert first != second


def test_rag_config_fingerprint_changes_when_oracle_vector_accuracy_changes() -> None:
    """fingerprint は Oracle approximate search 精度の変更も反映する。"""
    # 設定の accuracy がそのまま効く balanced で比べる（既定の高精度は 98 固定。#272）。
    first = rag_config_fingerprint(
        Settings(oracle_vector_target_accuracy=95, rag_vector_index_profile="balanced")
    )
    second = rag_config_fingerprint(
        Settings(oracle_vector_target_accuracy=90, rag_vector_index_profile="balanced")
    )

    assert first != second


def test_rag_config_fingerprint_changes_when_rrf_k_changes() -> None:
    """fingerprint は hybrid RRF 定数の変更も反映する。"""
    first = rag_config_fingerprint(Settings(rag_rrf_k=60))
    second = rag_config_fingerprint(Settings(rag_rrf_k=10))

    assert first != second


def test_rag_config_fingerprint_changes_when_context_group_max_chunks_changes() -> None:
    """fingerprint は根拠の group から足す sibling 数の変更も反映する。"""
    first = rag_config_fingerprint(Settings(rag_context_group_max_chunks=4))
    second = rag_config_fingerprint(Settings(rag_context_group_max_chunks=2))

    assert first != second


@pytest.mark.parametrize(
    "update",
    [
        {"rag_query_strategy": "rag_fusion"},
        {"rag_answer_flow": "crag"},
        {"rag_neighbor_child_count": 5},
        {"rag_rerank_enabled": False},
        {"rag_screen_linking_enabled": True},
        {"rag_request_coverage_retrieval_enabled": True},
    ],
    ids=[
        "query_strategy",
        "answer_flow",
        "neighbor",
        "rerank",
        "screen_linking",
        "request_coverage",
    ],
)
def test_rag_config_fingerprint_changes_when_answer_settings_change(
    update: dict[str, object],
) -> None:
    """fingerprint は回答の検索と生成の全体既定の変更も反映する。"""
    base = Settings()

    assert rag_config_fingerprint(base) != rag_config_fingerprint(base.model_copy(update=update))


def test_rag_config_fingerprint_changes_with_vector_index_profile() -> None:
    """fingerprint は検索インデックス profile の違い(検索時 accuracy)を反映する。"""
    fingerprints = {
        rag_config_fingerprint(Settings(rag_vector_index_profile=profile))
        for profile in ("balanced", "accurate", "fast")
    }

    assert len(fingerprints) == 3
