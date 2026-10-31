"""RAG 実行時の非機密診断情報。"""

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from rag_pipeline_core.vector_index import resolve_vector_index

from app.config import Settings, get_settings
from app.schemas.common import JsonValue
from app.schemas.search import SearchDiagnostics, SearchRequest

# 回答の経路。grounded は根拠付き回答、retrieval_only は検索だけ
# (KB の検索テスト・レシピの検索比較。#593)、blocked は質問の安全チェックで止めたもの。
RetrievalPath = Literal["grounded", "retrieval_only", "blocked"]


def _effective_vector_target_accuracy(settings: Settings) -> int:
    """選択 profile 解決後の検索時 target accuracy(fingerprint 用)。

    実 SQL は ``resolve_vector_index_adapter`` 経由で同値を使うが、ここでは決定論・
    ネットワーク無しの pure core ロジックで揃える(サービス委譲と同一結果)。
    """
    return resolve_vector_index(
        settings.rag_vector_index_profile,
        settings.oracle_vector_target_accuracy,
    ).target_accuracy


def build_search_diagnostics(
    request: SearchRequest,
    *,
    settings: Settings | None = None,
    retrieval_strategy_adapter: RetrievalPath,
    guardrail_degraded: bool = False,
    answer: Mapping[str, JsonValue] | None = None,
) -> SearchDiagnostics:
    """検索実行の再現・調査に使う非機密メタデータを作る。"""
    resolved_settings = settings or get_settings()
    return SearchDiagnostics(
        retrieval_strategy="hybrid",
        retrieval_strategy_adapter=retrieval_strategy_adapter,
        guardrails_input_enabled=resolved_settings.rag_guardrails_input_enabled,
        guardrails_output_enabled=resolved_settings.rag_guardrails_output_enabled,
        guardrail_degraded=guardrail_degraded,
        filter_keys=sorted(request.filters),
        knowledge_base_count=len(request.knowledge_base_ids),
        config_fingerprint=rag_config_fingerprint(resolved_settings),
        answer=dict(answer) if answer is not None else None,
    )


def rag_config_fingerprint(settings: Settings | None = None) -> str:
    """検索・回答の結果に効く RAG 設定の非機密 fingerprint を返す。"""
    resolved_settings = settings or get_settings()
    payload = {
        "embedding_dim": resolved_settings.oci_genai_embedding_dim,
        "embedding_model": resolved_settings.oci_genai_embedding_model,
        "rerank_model": resolved_settings.oci_genai_rerank_model,
        "chunk_size": resolved_settings.rag_chunk_size,
        "chunk_overlap": resolved_settings.rag_chunk_overlap,
        "context_group_max_chunks": resolved_settings.rag_context_group_max_chunks,
        "min_similarity": resolved_settings.rag_min_similarity,
        "rrf_k": resolved_settings.rag_rrf_k,
        "vector_index_profile": resolved_settings.rag_vector_index_profile,
        "oracle_vector_target_accuracy": _effective_vector_target_accuracy(resolved_settings),
        "query_strategy": resolved_settings.rag_query_strategy,
        "answer_flow": resolved_settings.rag_answer_flow,
        "neighbor_child_count": resolved_settings.rag_neighbor_child_count,
        "rerank_enabled": resolved_settings.rag_rerank_enabled,
        "screen_linking_enabled": resolved_settings.rag_screen_linking_enabled,
        "request_coverage_retrieval_enabled": (
            resolved_settings.rag_request_coverage_retrieval_enabled
        ),
        # 業務ガイドの手順ごとの検索と追加の検索の枠(#2395)。
        "guide_step_retrieval_enabled": resolved_settings.rag_guide_step_retrieval_enabled,
        "guide_step_max_queries": resolved_settings.rag_guide_step_max_queries,
        "extra_retrieval_max_queries": resolved_settings.rag_extra_retrieval_max_queries,
        "reference_expansion_enabled": resolved_settings.rag_reference_expansion_enabled,
        "reference_expansion_max_chunks": resolved_settings.rag_reference_expansion_max_chunks,
        # 実体の 1 段の拡張(#1362)。検索方法の全体設定で選ぶ(#1388・#3053)。
        "entity_expansion_enabled": resolved_settings.rag_entity_expansion_enabled,
        "entity_expansion_max_chunks": resolved_settings.rag_entity_expansion_max_chunks,
        # 関係情報で検索の候補を足す(#2055)。
        "graph_expansion_enabled": resolved_settings.rag_graph_expansion_enabled,
        "graph_expansion_max_chunks": resolved_settings.rag_graph_expansion_max_chunks,
        # 章節の拡張(#1436)。検索方法の全体設定で選ぶ。
        "section_expansion_enabled": resolved_settings.rag_section_expansion_enabled,
        "section_expansion_max_levels": resolved_settings.rag_section_expansion_max_levels,
        "section_expansion_input_token_percent": (
            resolved_settings.rag_section_expansion_input_token_percent
        ),
        # 生成に渡す根拠の原文の文字予算(#3776)。
        "evidence_budget_chars": resolved_settings.rag_evidence_budget_chars,
        # rerank の後、起点に採る同じ文書の chunk の上限(#2167)。
        "max_anchors_per_document": resolved_settings.rag_max_anchors_per_document,
        "auto_field_filter_enabled": resolved_settings.rag_auto_field_filter_enabled,
        # 自動で足した検索条件で 0 件のとき外して検索し直す段の上限(#2401)。
        "filter_relaxation_max_steps": resolved_settings.rag_filter_relaxation_max_steps,
        # 回答の LLM の時間の短縮(#1499)。
        "strategy_reasoning_effort": resolved_settings.rag_strategy_reasoning_effort,
        "judge_reasoning_effort": resolved_settings.rag_judge_reasoning_effort,
        "generation_reasoning_effort": resolved_settings.rag_generation_reasoning_effort,
        # 監査の推論の量・CRAG の評価の入力の圧縮・読む範囲まで読んだ後の評価し直しの省略(#1528)。
        "audit_reasoning_effort": resolved_settings.rag_audit_reasoning_effort,
        "crag_compact_input": resolved_settings.rag_crag_compact_input,
        # CRAG の評価の候補の短い ID(#1864)。
        "crag_short_candidate_ids": resolved_settings.rag_crag_short_candidate_ids,
        # 監査の通った判定の reason を空にする・是正の組の回数の上限(#1912)。
        "answer_audit_compact_reasons": resolved_settings.rag_answer_audit_compact_reasons,
        "answer_max_correction_rounds": resolved_settings.rag_answer_max_correction_rounds,
        "answer_audit_mode": resolved_settings.rag_answer_audit_mode,
        "answer_evidence_refinement_mode": resolved_settings.rag_answer_evidence_refinement_mode,
        "section_skip_regrade_at_scope": resolved_settings.rag_section_skip_regrade_at_scope,
        "crag_skip_on_strong_signals": resolved_settings.rag_crag_skip_on_strong_signals,
        "crag_skip_min_rerank_score": resolved_settings.rag_crag_skip_min_rerank_score,
        "query_expansion_max_queries": resolved_settings.rag_query_expansion_max_queries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
