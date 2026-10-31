"""検索・回答プロファイル(Search Answer Profile)設定解決の単体テスト(DB 非依存)。"""

from app.config import get_settings
from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
from app.rag.search_answer_profile_config import (
    PROFILE_GLOBAL_QUERY_FIELDS,
    SearchAnswerProfileConfig,
    dump_search_answer_profile_config,
    parse_search_answer_profile_config,
    resolve_search_answer_profile_settings,
)


def test_empty_config_keeps_global_settings() -> None:
    """空設定はグローバルをそのまま返し、上書きは効かない。"""
    settings = get_settings()
    merged, applied = resolve_search_answer_profile_settings(settings, SearchAnswerProfileConfig())
    assert applied is False
    assert merged is settings


def test_query_overrides_apply() -> None:
    """query 設定(回答の検索と生成・安全チェック)はグローバルへ上書きされる。"""
    settings = get_settings()
    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1", "kb-2"],
        query=KnowledgeBaseQueryConfig(max_anchors_per_document=2),
    )
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert applied is True
    assert merged.rag_max_anchors_per_document == 2
    # グローバルは破壊しない。
    assert settings.rag_query_strategy == "simple_retrieval"


def test_legacy_vector_index_is_read_but_not_applied_or_saved() -> None:
    """旧 vector index 値は読めるが、共有索引設定を上書きせず次回保存で除外する。"""
    settings = get_settings()
    config = parse_search_answer_profile_config(
        {
            "query": {
                "max_anchors_per_document": 2,
                "vector_index_profile": "accurate",
            }
        }
    )

    assert config.query.vector_index_profile == "accurate"
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert applied is True
    assert merged.rag_max_anchors_per_document == 2
    assert merged.rag_vector_index_profile == settings.rag_vector_index_profile
    dumped_query = dump_search_answer_profile_config(config)["query"]
    assert isinstance(dumped_query, dict)
    assert "vector_index_profile" not in dumped_query


def test_saved_evaluation_suite_is_ignored_on_load_and_dropped_on_save() -> None:
    """検索・回答プロファイルは品質評価を上書きしない。保存済みの値は読み込み時に無視し、次回保存で消える(#301)。"""
    settings = get_settings()
    config = parse_search_answer_profile_config(
        {
            "query": {
                "max_anchors_per_document": 3,
                "evaluation_suite": "strict_ci",
            }
        }
    )

    # 他の上書きは生きたまま、評価スイートだけを捨てる(設定全体を空へ縮退させない)。
    assert config.query.max_anchors_per_document == 3
    assert "evaluation_suite" not in KnowledgeBaseQueryConfig.model_fields
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert applied is True
    assert merged.rag_evaluation_suite == settings.rag_evaluation_suite
    dumped_query = dump_search_answer_profile_config(config)["query"]
    assert isinstance(dumped_query, dict)
    assert "evaluation_suite" not in dumped_query
    assert dumped_query["max_anchors_per_document"] == 3


def test_saved_standard_options_are_ignored_on_load_and_dropped_on_save() -> None:
    """旧 standard の回答エンジンの値(#595 で削除)は読み込み時に捨て、次回保存で消える。

    検索モード・検索オプション・根拠確認・回答スタイルと、persona(system prompt / 既定言語)。
    他の上書きは生きたまま残す(設定全体を空へ縮退させない)。
    """
    settings = get_settings()
    config = parse_search_answer_profile_config(
        {
            "knowledge_base_ids": ["kb-1"],
            "system_prompt": "あなたは経理規程アシスタントです。",
            "default_language": "日本語",
            "query": {
                "retrieval_strategy": "business_context_strict",
                "retrieval_query_expansion": False,
                "retrieval_query_expansion_llm": True,
                "retrieval_gap_stop": True,
                "retrieval_corrective": True,
                "retrieval_business_fit_weighting": True,
                "post_retrieval_pipeline": "lean",
                "generation_profile": "structured_json",
                "max_anchors_per_document": 5,
            },
        }
    )

    assert config.normalized_knowledge_base_ids() == ["kb-1"]
    assert config.query.max_anchors_per_document == 5
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert applied is True
    assert merged.rag_max_anchors_per_document == 5
    dumped = dump_search_answer_profile_config(config)
    assert "system_prompt" not in dumped
    assert "default_language" not in dumped
    dumped_query = dumped["query"]
    assert isinstance(dumped_query, dict)
    for removed in (
        "retrieval_strategy",
        "retrieval_query_expansion",
        "retrieval_query_expansion_llm",
        "retrieval_gap_stop",
        "retrieval_corrective",
        "retrieval_business_fit_weighting",
        "post_retrieval_pipeline",
        "generation_profile",
    ):
        assert removed not in dumped_query
    assert dumped_query["max_anchors_per_document"] == 5


def test_dump_parse_roundtrip() -> None:
    """dump -> parse で設定が保たれる。"""
    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1", " kb-1 ", "kb-2"],
        query=KnowledgeBaseQueryConfig(max_anchors_per_document=3),
    )
    restored = parse_search_answer_profile_config(dump_search_answer_profile_config(config))
    assert restored.query.max_anchors_per_document == 3
    # 正規化で重複・空白は取り除かれる。
    assert restored.normalized_knowledge_base_ids() == ["kb-1", "kb-2"]


def test_default_config_does_not_override_settings() -> None:
    """上書きの無い設定は global の Settings をそのまま使う。"""
    settings = get_settings()
    merged, applied = resolve_search_answer_profile_settings(settings, SearchAnswerProfileConfig())
    assert applied is False
    assert merged == settings


def test_saved_serving_mode_is_ignored_and_not_saved_again() -> None:
    """配信モード(serving_mode)は削除した(#1331)。保存済みの値は読み捨て、次回保存で書かない。"""
    config = parse_search_answer_profile_config(
        {"version": 1, "knowledge_base_ids": ["kb-1"], "serving_mode": "single"}
    )
    assert config.normalized_knowledge_base_ids() == ["kb-1"]
    assert "serving_mode" not in dump_search_answer_profile_config(config)
    assert not hasattr(get_settings(), "rag_serving_mode")


def test_parse_tolerates_broken_payload() -> None:
    """壊れた永続値は空設定へ縮退する。"""
    restored = parse_search_answer_profile_config({"query": "not-a-dict"})
    assert restored.normalized_knowledge_base_ids() == []
    assert restored.query == KnowledgeBaseQueryConfig()


def test_removed_guardrail_policy_cannot_override_or_be_saved() -> None:
    settings = get_settings()
    config = SearchAnswerProfileConfig(query=KnowledgeBaseQueryConfig(guardrail_policy="regulated"))
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert not hasattr(merged, "rag_guardrails_input_enabled")
    assert applied is False
    assert "guardrail_policy" not in config.model_dump()["query"]


def test_support_guide_switch_round_trip_preserves_internal_matching_settings() -> None:
    settings = get_settings().model_copy(update={"rag_support_guide_enabled": True})
    old = {
        "support_guide_semantic_match_enabled": True,
        "support_guide_close_match_clarification_enabled": True,
    }
    for enabled in [None, False, True]:
        config = parse_search_answer_profile_config(
            {"query": {**old, "support_guide_enabled": enabled}}
        )
        reloaded = parse_search_answer_profile_config(dump_search_answer_profile_config(config))
        resolved, _ = resolve_search_answer_profile_settings(settings, reloaded)
        assert resolved.rag_support_guide_enabled is (enabled is True)
        assert resolved.rag_support_guide_semantic_match_enabled is True
        assert resolved.rag_support_guide_close_match_clarification_enabled is True
        assert (
            resolved.rag_support_guide_semantic_threshold
            == settings.rag_support_guide_semantic_threshold
        )
        assert (
            resolved.rag_support_guide_close_match_margin
            == settings.rag_support_guide_close_match_margin
        )
    missing, _ = resolve_search_answer_profile_settings(
        settings, parse_search_answer_profile_config({"query": old})
    )
    assert missing.rag_support_guide_enabled is False
    assert settings.rag_support_guide_enabled is True


def test_global_search_options_ignore_legacy_profile_values_and_drop_them_on_save() -> None:
    settings = get_settings().model_copy(
        update={
            "rag_query_strategy": "simple_retrieval",
            "rag_answer_flow": "crag",
            "rag_neighbor_child_count": 3,
            "rag_rerank_enabled": True,
            "rag_auto_field_filter_enabled": False,
            "rag_graph_expansion_enabled": False,
            "rag_screen_linking_enabled": False,
            "rag_request_coverage_retrieval_enabled": True,
            "rag_guide_step_retrieval_enabled": True,
            "rag_entity_expansion_enabled": True,
            "rag_entity_expansion_max_chunks": 6,
            "rag_section_expansion_enabled": True,
            "rag_section_expansion_max_levels": 4,
            "rag_section_expansion_input_token_percent": 50,
            "rag_graph_expansion_max_chunks": 5,
            "rag_guide_step_max_queries": 2,
            "rag_guide_step_deadline_seconds": 20.0,
        }
    )
    old = {
        "query_strategy": "hyde",
        "answer_flow": "standard_rag",
        "neighbor_child_count": 0,
        "rerank_enabled": False,
        "auto_field_filter_enabled": True,
        "graph_expansion_enabled": True,
        "screen_linking_enabled": True,
        "request_coverage_retrieval_enabled": False,
        "guide_step_retrieval_enabled": False,
        "entity_expansion_enabled": False,
        "entity_expansion_max_chunks": 3,
        "section_expansion_enabled": False,
        "section_expansion_max_levels": 2,
        "section_expansion_input_token_percent": 40,
        # 関係情報の拡張の上限と業務ガイドの手順ごとの検索の上限・期限も全体設定だけ(#3523)。
        "graph_expansion_max_chunks": 3,
        "guide_step_max_queries": 7,
        "guide_step_deadline_seconds": 37.5,
    }
    assert set(old) == set(PROFILE_GLOBAL_QUERY_FIELDS)
    config = parse_search_answer_profile_config(
        {
            "knowledge_base_ids": ["kb-1"],
            "query": {
                **old,
                "max_anchors_per_document": 2,
                "support_guide_enabled": True,
            },
        }
    )
    merged, applied = resolve_search_answer_profile_settings(settings, config)
    assert applied is True
    for key in old:
        assert getattr(merged, f"rag_{key}") == getattr(settings, f"rag_{key}")
    assert merged.rag_max_anchors_per_document == 2
    # 全体の値が効く(プロファイルの保存値は重ねない)。
    assert merged.rag_graph_expansion_max_chunks == 5
    assert merged.rag_guide_step_max_queries == 2
    assert merged.rag_guide_step_deadline_seconds == 20.0
    saved = dump_search_answer_profile_config(config)
    assert saved["knowledge_base_ids"] == ["kb-1"]
    query = saved["query"]
    assert isinstance(query, dict)
    assert not set(old) & set(query)
    assert query["support_guide_enabled"] is True
    assert query["max_anchors_per_document"] == 2
