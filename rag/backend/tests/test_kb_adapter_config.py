"""KB 単位構築設定(kb_adapter_config)の単体テスト。"""

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.main import app
from app.rag.kb_adapter_config import (
    KbAdapterConfigError,
    KnowledgeBaseAdapterConfig,
    KnowledgeBaseIngestionConfig,
    KnowledgeBaseQueryConfig,
    apply_adapter_config_or_global,
    compose_query_settings,
    dump_adapter_config,
    parse_adapter_config,
    resolve_effective_adapter_config,
    resolve_effective_settings,
)
from app.schemas.document import DocumentProcessingConfig, DocumentProcessingConfigInput
from tests.support import AsgiTestClient


def _config(**raw: object) -> KnowledgeBaseAdapterConfig:
    return KnowledgeBaseAdapterConfig.model_validate(raw)


def test_empty_config_returns_global_settings_unchanged() -> None:
    """上書きが無ければグローバル Settings をそのまま返す(同一オブジェクト)。"""
    settings = get_settings()
    config = KnowledgeBaseAdapterConfig()
    assert config.is_empty()
    assert resolve_effective_settings(settings, config, scope="ingestion") is settings
    assert resolve_effective_settings(settings, config, scope="query") is settings


def test_ingestion_scope_overlays_only_ingestion_fields() -> None:
    """ingestion scope は Parser/Chunking だけ上書きし、query 系には触れない。"""
    settings = get_settings()
    config = _config(
        ingestion={"chunking_strategy": "markdown_heading", "chunk_size": 1200},
        query={"query_strategy": "rag_fusion"},
    )

    effective = resolve_effective_settings(settings, config, scope="ingestion")

    assert effective.rag_chunking_strategy == "markdown_heading"
    assert effective.rag_chunk_size == 1200
    # query 系はグローバルのまま。
    assert effective.rag_query_strategy == settings.rag_query_strategy


def test_kb_query_scope_is_legacy_noop() -> None:
    """KB query scope は legacy として読めるが Settings へは反映しない。"""
    settings = get_settings()
    config = _config(
        ingestion={"chunk_size": 1200},
        query={"answer_flow": "standard_rag", "guardrail_policy": "strict"},
    )

    effective = resolve_effective_settings(settings, config, scope="query")

    assert effective is settings
    assert config.settings_overrides("query") == {}


def test_settings_overrides_uses_allowlist_only() -> None:
    """Settings へ渡す上書きは scope の allowlist フィールドに限定される。"""
    config = _config(
        ingestion={"parser_adapter_backend": "docling", "chunk_overlap": 60},
    )
    overrides = config.settings_overrides("ingestion")
    assert overrides == {
        "rag_parser_adapter_backend": "docling",
        # backend 選択時は対応 feature flag も有効化する。
        "rag_parser_docling_enabled": True,
        "rag_chunk_overlap": 60,
    }
    assert config.settings_overrides("query") == {}


@pytest.mark.usefixtures("mineru_selectable")
def test_external_parser_backend_enables_feature_flag() -> None:
    """外部 parser backend を選ぶと対応 feature flag が有効になる。"""
    for backend, flag_field in (
        ("unstructured", "rag_parser_unstructured_enabled"),
        ("mineru", "rag_parser_mineru_enabled"),
        ("dots_ocr", "rag_parser_dots_ocr_enabled"),
    ):
        # グローバル既定が無効でも backend 選択で有効化されることを確かめる。
        settings = get_settings().model_copy(update={flag_field: False})
        config = _config(ingestion={"parser_adapter_backend": backend})

        overrides = config.settings_overrides("ingestion")
        assert overrides[flag_field] is True

        effective = resolve_effective_settings(settings, config, scope="ingestion")
        assert effective.rag_parser_adapter_backend == backend
        assert getattr(effective, flag_field) is True


def test_explicit_false_flag_is_not_overridden_by_backend_selection() -> None:
    """KB が flag を明示的に無効化していれば backend 選択でも上書きしない。"""
    config = _config(
        ingestion={
            "parser_adapter_backend": "unstructured",
            "parser_unstructured_enabled": False,
        },
    )
    overrides = config.settings_overrides("ingestion")
    assert overrides["rag_parser_unstructured_enabled"] is False


def test_local_backend_does_not_enable_adapter_flags() -> None:
    """local は特定 adapter flag を有効化しない。"""
    config = _config(ingestion={"parser_adapter_backend": "local"})
    overrides = config.settings_overrides("ingestion")
    assert "rag_parser_docling_enabled" not in overrides
    assert "rag_parser_unstructured_enabled" not in overrides


def test_resolve_raises_on_chunk_inconsistency() -> None:
    """overlay 後に chunk_overlap >= chunk_size となる設定は拒否する。"""
    settings = get_settings()
    config = _config(ingestion={"chunk_size": 200, "chunk_overlap": 500})
    with pytest.raises(KbAdapterConfigError):
        resolve_effective_settings(settings, config, scope="ingestion")


def test_apply_or_global_falls_back_on_inconsistency() -> None:
    """堅牢版は矛盾時にグローバルへ縮退し applied=False を返す。"""
    settings = get_settings()
    config = _config(ingestion={"chunk_size": 200, "chunk_overlap": 500})
    merged, applied = apply_adapter_config_or_global(settings, config, scope="ingestion")
    assert merged is settings
    assert applied is False


def test_apply_or_global_ignores_kb_query_override() -> None:
    """KB query の legacy 上書きは applied=False でグローバルのまま返す。"""
    settings = get_settings()
    config = _config(query={"vector_index_profile": "accurate"})
    merged, applied = apply_adapter_config_or_global(settings, config, scope="query")
    assert applied is False
    assert merged is settings


def test_parse_is_tolerant_of_legacy_and_unknown_keys() -> None:
    """旧 free-form retrieval_config や未知キーは捨てて空設定へ縮退する。"""
    assert parse_adapter_config(None).is_empty()
    assert parse_adapter_config({}).is_empty()
    assert parse_adapter_config({"top_k": 20, "rerank": True}).is_empty()
    # 不正な値が混じっていても空へ縮退して例外を出さない。
    assert parse_adapter_config({"ingestion": "broken"}).is_empty()


def test_dump_parse_round_trip() -> None:
    """dump → parse で正規の KB 構築フィールドだけが保たれる。"""
    config = _config(
        ingestion={"chunking_strategy": "page_level", "parser_adapter_backend": "docling"},
        query={"guardrail_policy": "strict"},
    )
    restored = parse_adapter_config(dump_adapter_config(config))
    assert restored.ingestion == config.ingestion
    assert restored.query == KnowledgeBaseQueryConfig()


@pytest.mark.parametrize(
    "removed", ["marker", "unlimited_ocr", "glm_ocr", "oci_genai_vision", "enterprise_ai_vlm"]
)
def test_removed_parser_engine_in_saved_config_inherits_global_default(removed: str) -> None:
    """削除したエンジン(#270)が保存済み KB 構築設定に残っていても壊れず、global 既定を継承する。

    backend の選択と旧 feature flag は捨て、他の上書き(分割など)は保つ。再保存で旧値は消える。
    """
    raw = {
        "version": 2,
        "ingestion": {
            "parser_adapter_backend": removed,
            f"parser_{removed}_enabled": True,
            "chunking_strategy": "page_level",
        },
    }
    config = parse_adapter_config(raw)

    assert config.ingestion.parser_adapter_backend is None
    assert config.ingestion.chunking_strategy == "page_level"
    overrides = config.settings_overrides("ingestion")
    assert "rag_parser_adapter_backend" not in overrides
    assert not [key for key in overrides if removed in key]
    settings = get_settings()
    effective = resolve_effective_settings(settings, config, scope="ingestion")
    assert effective.rag_parser_adapter_backend == settings.rag_parser_adapter_backend
    dumped = dump_adapter_config(config)
    assert removed not in str(dumped)


def test_saved_roadmap_parser_engine_inherits_global_default() -> None:
    """保存済みのレシピに準備中(ロードマップ)のエンジンが残っても global 既定を継承する(#3374)。

    画面・取込を壊さず、取込の経路は準備中のエンジンを選ばない。他の上書きは保つ。
    """
    saved = DocumentProcessingConfig.model_validate(
        {"parser_adapter_backend": "oci_document_understanding", "chunking_strategy": "page_level"}
    )

    assert saved.parser_adapter_backend is None
    assert saved.chunking_strategy == "page_level"
    config = KnowledgeBaseAdapterConfig(ingestion=saved)
    assert "rag_parser_adapter_backend" not in config.settings_overrides("ingestion")


def test_recipe_input_rejects_roadmap_parser_engine_with_reason() -> None:
    """処理レシピの保存の入力で準備中のエンジンを選ぶと、理由の分かる検証のエラーにする(#3374)。"""
    with pytest.raises(ValidationError, match="準備中（ロードマップ）"):
        DocumentProcessingConfigInput.model_validate(
            {"parser_adapter_backend": "oci_document_understanding"}
        )
    # 使える解析エンジンと継承(None)はそのまま受ける。
    assert (
        DocumentProcessingConfigInput.model_validate(
            {"parser_adapter_backend": "docling"}
        ).parser_adapter_backend
        == "docling"
    )
    assert DocumentProcessingConfigInput.model_validate({}).parser_adapter_backend is None


def test_recipe_put_rejects_roadmap_parser_engine_before_saving() -> None:
    """``PUT /api/documents/{id}/recipes/{recipe_id}`` は保存する前に 422 にする(#3374)。"""
    resp = AsgiTestClient(app).put(
        "/api/documents/doc-a/recipes/recipe-a",
        json={"parser_adapter_backend": "oci_document_understanding"},
    )

    assert resp.status_code == 422
    assert "準備中（ロードマップ）" in resp.text


def test_ingestion_scope_overlays_advanced_axes() -> None:
    """取込側の高度軸(graph / field / vision / nav)も KB 上書きで effective に反映される。"""
    settings = get_settings()
    config = _config(
        ingestion={
            "graph_profile": "entities",
            "field_extraction_enabled": True,
            "vision_enabled": True,
            "navigation_summary_enabled": True,
        },
    )

    overrides = config.settings_overrides("ingestion")
    assert overrides == {
        "rag_graph_profile": "entities",
        "rag_field_extraction_enabled": True,
        "rag_vision_enabled": True,
        "rag_navigation_summary_enabled": True,
    }

    effective = resolve_effective_settings(settings, config, scope="ingestion")
    assert effective.rag_graph_profile == "entities"
    assert effective.rag_field_extraction_enabled is True
    assert effective.rag_vision_enabled is True
    assert effective.rag_navigation_summary_enabled is True
    # query 系は不変。
    assert effective.rag_query_strategy == settings.rag_query_strategy


def test_resolve_effective_adapter_config_fills_inherited_with_global() -> None:
    """構築設定の継承(None)フィールドはグローバル既定で埋められる。"""
    settings = get_settings()
    config = _config(
        ingestion={"chunking_strategy": "page_level"},
        query={"vector_index_profile": "accurate"},
    )

    effective = resolve_effective_adapter_config(settings, config)

    # 上書きフィールドは override 値。
    assert effective.ingestion.chunking_strategy == "page_level"
    # 継承フィールドはグローバル既定で解決され、None にはならない。
    assert effective.ingestion.preprocess_profile == settings.rag_preprocess_profile
    assert effective.ingestion.parser_adapter_backend == settings.rag_parser_adapter_backend
    # query は KB では legacy ignored なので解決済み表示にも出さない。
    assert effective.query == KnowledgeBaseQueryConfig()


def test_resolve_effective_adapter_config_all_global_when_empty() -> None:
    """空設定では全フィールドがグローバル既定で解決される(None が無い)。"""
    settings = get_settings()
    effective = resolve_effective_adapter_config(settings, KnowledgeBaseAdapterConfig())

    assert effective.ingestion.chunking_strategy == settings.rag_chunking_strategy
    assert effective.ingestion.chunk_size == settings.rag_chunk_size
    assert effective.query == KnowledgeBaseQueryConfig()
    # 解決済みは表示専用なので継承判定の元データ(config)は変更しない。
    assert KnowledgeBaseAdapterConfig().is_empty()


def test_compose_query_settings_empty_returns_global() -> None:
    """overlay が空(全継承)ならグローバルをそのまま返し applied=False。"""
    settings = get_settings()
    merged, applied = compose_query_settings(settings, [])
    assert merged is settings
    assert applied is False

    merged2, applied2 = compose_query_settings(
        settings, [KnowledgeBaseQueryConfig(), KnowledgeBaseQueryConfig()]
    )
    assert merged2 is settings
    assert applied2 is False


def test_compose_query_settings_higher_precedence_wins_per_field() -> None:
    """後の overlay(高優先)が同一フィールドを上書きし、別フィールドは両方効く。"""
    settings = get_settings()
    kb = KnowledgeBaseQueryConfig(rerank_enabled=False, query_strategy="hyde")
    view = KnowledgeBaseQueryConfig(query_strategy="rag_fusion")

    # 低優先=kb, 高優先=view の順で渡す。
    merged, applied = compose_query_settings(settings, [kb, view])

    assert applied is True
    # 同一フィールド(質問拡張戦略)は高優先 view が勝つ。
    assert merged.rag_query_strategy == "rag_fusion"
    # view が触れていない rerank は下位 overlay の値が残る(per-field merge の肝)。
    assert merged.rag_rerank_enabled is False


def test_compose_query_settings_null_does_not_wipe_lower_layer() -> None:
    """高優先 overlay の None フィールドは下位層の値を消さない。"""
    settings = get_settings()
    kb = KnowledgeBaseQueryConfig(rerank_enabled=False)
    view = KnowledgeBaseQueryConfig()  # 何も設定しない

    merged, _ = compose_query_settings(settings, [kb, view])
    assert merged.rag_rerank_enabled is False


def test_invalid_literal_value_is_rejected_at_validation() -> None:
    """存在しない戦略名は pydantic バリデーションで弾く。"""
    with pytest.raises(ValueError):
        KnowledgeBaseAdapterConfig.model_validate({"query": {"query_strategy": "does_not_exist"}})


def test_removed_parent_child_strategy_is_read_as_small_to_big() -> None:
    """保存済みの親子階層は親子階層（small-to-big）として読み、専用の子サイズは捨てる(#271)。"""
    from app.schemas.document import DocumentProcessingConfig

    config = parse_adapter_config(
        {
            "version": 2,
            "ingestion": {
                "chunking_strategy": "hierarchical_parent_child",
                "chunk_child_size": 320,
            },
        }
    )
    assert config.ingestion.chunking_strategy == "small_to_big"
    assert "chunk_child_size" not in config.ingestion.model_dump()
    dumped_ingestion = dump_adapter_config(config)["ingestion"]
    assert isinstance(dumped_ingestion, dict)
    assert dumped_ingestion["chunking_strategy"] == "small_to_big"

    # 文書レシピは extra="forbid" だが、削除した項目があっても読み込める。
    recipe = DocumentProcessingConfig.model_validate(
        {"chunking_strategy": "hierarchical_parent_child", "chunk_child_size": 800}
    )
    assert recipe.chunking_strategy == "small_to_big"
    assert "chunk_child_size" not in recipe.model_dump(exclude_none=True)


def test_small_to_big_params_overlay_ingestion_settings() -> None:
    """親子階層の 5 項目は文書レシピ(ingestion scope)で上書きできる。"""
    config = KnowledgeBaseAdapterConfig(
        ingestion=KnowledgeBaseIngestionConfig(
            chunking_strategy="small_to_big",
            chunk_child_target_chars=500,
            chunk_parent_max_pages=1,
        )
    )
    merged = resolve_effective_settings(get_settings(), config, scope="ingestion")
    assert merged.rag_chunking_strategy == "small_to_big"
    assert merged.rag_chunk_child_target_chars == 500
    assert merged.rag_chunk_parent_max_pages == 1
    assert merged.rag_chunk_parent_target_chars == get_settings().rag_chunk_parent_target_chars
    with pytest.raises(ValueError):
        KnowledgeBaseIngestionConfig(chunk_child_target_chars=2000)


@pytest.mark.parametrize("value", [0, 15.5, 60])
def test_query_overlay_step_deadline_is_bounded_and_does_not_mutate_defaults(
    value: float,
) -> None:
    settings = get_settings()
    original = settings.rag_guide_step_deadline_seconds
    query = KnowledgeBaseQueryConfig(guide_step_deadline_seconds=value)
    merged, applied = compose_query_settings(settings, [query])
    assert merged.rag_guide_step_deadline_seconds == value
    assert settings.rag_guide_step_deadline_seconds == original
    assert applied


@pytest.mark.parametrize("value", [-1, 61])
def test_query_overlay_step_deadline_rejects_out_of_range(value: float) -> None:
    with pytest.raises(ValueError):
        KnowledgeBaseQueryConfig(guide_step_deadline_seconds=value)
