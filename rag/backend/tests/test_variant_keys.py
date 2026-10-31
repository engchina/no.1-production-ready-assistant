"""variant 層別 keying(variant_keys)の決定論ユニットテスト。"""

import pytest
from rag_parser_core.language import bind_pack, builtin_pack, current_pack, resolve

from app.config import Settings, get_settings
from app.rag.variant_keys import (
    compute_chunk_set_id,
    compute_document_recipe_extraction_id,
    compute_extraction_recipe_id,
    compute_graph_layer_id,
    compute_layer_ids,
    compute_metadata_layer_id,
    compute_nav_layer_id,
    extraction_recipe_subset,
)

SRC = "a" * 64
SRC2 = "b" * 64


def test_chunk_set_id_is_deterministic() -> None:
    """同一原本 + 同一設定なら chunk_set_id は安定。"""
    settings = get_settings()
    assert compute_chunk_set_id(SRC, settings) == compute_chunk_set_id(SRC, settings)


def test_chunk_set_id_changes_with_chunk_axis() -> None:
    """chunk 軸(chunk_size)が違えば別 chunk 集合になる。"""
    base = get_settings()
    other = base.model_copy(update={"rag_chunk_size": base.rag_chunk_size + 256})
    assert compute_chunk_set_id(SRC, base) != compute_chunk_set_id(SRC, other)


def test_chunk_set_id_changes_with_context_header_axis() -> None:
    """索引対象テキストが変わるため context header の有無は別 chunk_set。"""
    enabled = get_settings().model_copy(update={"rag_chunk_context_header_enabled": True})
    disabled = enabled.model_copy(update={"rag_chunk_context_header_enabled": False})

    assert compute_chunk_set_id(SRC, enabled) != compute_chunk_set_id(SRC, disabled)


def test_extraction_recipe_id_is_deterministic() -> None:
    """同一原本 + 同一設定なら extraction_recipe_id は安定。prefix は er_。"""
    settings = get_settings()
    recipe_id = compute_extraction_recipe_id(SRC, settings)
    assert recipe_id == compute_extraction_recipe_id(SRC, settings)
    assert recipe_id.startswith("er_")


def test_extraction_recipe_id_keeps_removed_asr_axis_in_hash() -> None:
    """音声の文字起こしの設定を外しても(#3369)、既存の extraction_recipe_id は変わらない。"""
    from app.rag.variant_keys import KEY_VERSION, _digest, extraction_recipe_subset

    settings = get_settings()
    subset = extraction_recipe_subset(settings)
    assert "rag_parser_asr_enabled" not in subset
    # 削除の前の hash の入力(既定値 True を含む)と同じ ID になる。
    expected = _digest(
        "er",
        {
            "v": KEY_VERSION,
            "src": SRC,
            "language_pack_ingest_hash": current_pack().ingest_hash,
            **subset,
            "rag_parser_asr_enabled": True,
        },
    )
    assert compute_extraction_recipe_id(SRC, settings) == expected


def test_document_recipe_extraction_id_is_isolated_by_recipe_and_revision() -> None:
    """同じ抽出設定でも recipe/revision が違えば成果物を共有しない。"""
    base = compute_extraction_recipe_id(SRC, get_settings())
    first = compute_document_recipe_extraction_id(base, "recipe-1", 2)

    assert first == compute_document_recipe_extraction_id(base, "recipe-1", 2)
    assert first != compute_document_recipe_extraction_id(base, "recipe-2", 2)
    assert first != compute_document_recipe_extraction_id(base, "recipe-1", 3)


def test_extraction_recipe_id_ignores_chunk_axis() -> None:
    """chunking だけ違う場合は同じ extraction を再利用できる。"""
    base = get_settings()
    other = base.model_copy(update={"rag_chunk_size": base.rag_chunk_size + 256})

    assert compute_extraction_recipe_id(SRC, base) == compute_extraction_recipe_id(SRC, other)
    assert compute_chunk_set_id(SRC, base) != compute_chunk_set_id(SRC, other)


def test_extraction_recipe_id_changes_with_parser_axis() -> None:
    """Parser 軸が違えば保存済み extraction は共有しない。"""
    base = get_settings().model_copy(update={"rag_parser_adapter_backend": "local"})
    other = get_settings().model_copy(update={"rag_parser_adapter_backend": "docling"})

    assert compute_extraction_recipe_id(SRC, base) != compute_extraction_recipe_id(SRC, other)
    assert compute_chunk_set_id(SRC, base) != compute_chunk_set_id(SRC, other)


@pytest.mark.parametrize(
    ("backend", "field", "value"),
    [
        ("mineru", "rag_parser_mineru_tier", "standard"),
        ("dots_ocr", "rag_parser_dots_ocr_model", "dots-v2"),
        ("dots_ocr", "rag_parser_dots_ocr_dpi", 400),
    ],
)
def test_extraction_recipe_id_changes_with_output_affecting_parser_setting(
    backend: str,
    field: str,
    value: object,
) -> None:
    base = get_settings().model_copy(update={"rag_parser_adapter_backend": backend})
    changed = base.model_copy(update={field: value})
    base_id = compute_extraction_recipe_id(SRC, base)
    changed_id = compute_extraction_recipe_id(SRC, changed)

    assert base_id != changed_id
    assert compute_document_recipe_extraction_id(
        base_id, "recipe-1", 1
    ) != compute_document_recipe_extraction_id(changed_id, "recipe-1", 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rag_parser_mineru_tier", "standard"),
        ("rag_parser_dots_ocr_dpi", 400),
        ("rag_parser_dots_ocr_model", "dots-v2"),
    ],
)
def test_extraction_recipe_id_ignores_unselected_parser_setting(
    field: str,
    value: object,
) -> None:
    base = get_settings().model_copy(update={"rag_parser_adapter_backend": "unstructured"})
    changed = base.model_copy(update={field: value})

    assert compute_extraction_recipe_id(SRC, base) == compute_extraction_recipe_id(SRC, changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rag_parser_dots_ocr_api_host", "https://new.example.com"),
        ("rag_parser_dots_ocr_api_key", "new-secret"),
        ("rag_parser_dots_ocr_pdf_workers", 12),
    ],
)
def test_extraction_recipe_id_ignores_parser_deployment_setting(
    field: str,
    value: object,
) -> None:
    base = get_settings()
    changed = base.model_copy(update={field: value})

    assert compute_extraction_recipe_id(SRC, base) == compute_extraction_recipe_id(SRC, changed)


def test_extraction_recipe_diagnostics_match_hashed_non_secret_settings() -> None:
    settings = get_settings().model_copy(
        update={
            "rag_parser_adapter_backend": "dots_ocr",
            "rag_parser_dots_ocr_model": "dots-diagnostic",
            "rag_parser_dots_ocr_api_key": "never-persist-this",
        }
    )

    subset = extraction_recipe_subset(settings)

    assert subset["rag_parser_dots_ocr_model"] == "dots-diagnostic"
    assert "rag_parser_dots_ocr_api_key" not in subset
    assert "rag_parser_dots_ocr_api_host" not in subset
    assert "rag_parser_dots_ocr_pdf_workers" not in subset
    assert "rag_parser_mineru_tier" not in subset


def test_extraction_recipe_id_changes_with_preprocess_axis_and_source() -> None:
    """前処理軸・原本が違えば別 extraction recipe。"""
    base = get_settings()
    pre = base.model_copy(update={"rag_preprocess_profile": "office_to_pdf"})
    assert compute_extraction_recipe_id(SRC, base) != compute_extraction_recipe_id(SRC, pre)
    assert compute_extraction_recipe_id(SRC, base) != compute_extraction_recipe_id(SRC2, base)


def test_chunk_set_id_changes_with_source() -> None:
    """原本が違えば chunk 集合も別。"""
    settings = get_settings()
    assert compute_chunk_set_id(SRC, settings) != compute_chunk_set_id(SRC2, settings)


def test_graph_axis_shares_chunk_set_but_splits_graph_layer() -> None:
    """graph_profile だけ違うとき chunk_set は共有、graph 層だけ別になる。"""
    off = get_settings().model_copy(update={"rag_graph_profile": "off"})
    entities = get_settings().model_copy(update={"rag_graph_profile": "entities"})

    cs_off = compute_chunk_set_id(SRC, off)
    cs_entities = compute_chunk_set_id(SRC, entities)
    assert cs_off == cs_entities
    assert compute_graph_layer_id(cs_off, off) != compute_graph_layer_id(cs_entities, entities)


def test_field_extraction_axis_splits_chunks_and_metadata_layer() -> None:
    """項目の検索用 Chunk が増えるため chunk_set と metadata 層を分ける。"""
    off = get_settings().model_copy(update={"rag_field_extraction_enabled": False})
    on = get_settings().model_copy(update={"rag_field_extraction_enabled": True})

    cs = compute_chunk_set_id(SRC, off)
    assert cs != compute_chunk_set_id(SRC, on)
    assert compute_metadata_layer_id(cs, off) != compute_metadata_layer_id(cs, on)


def test_nav_axis_splits_chunks_and_nav_layer() -> None:
    """要約の検索用 Chunk が増えるため chunk_set と nav 層を分ける。"""
    off = get_settings().model_copy(update={"rag_navigation_summary_enabled": False})
    on = get_settings().model_copy(update={"rag_navigation_summary_enabled": True})

    cs = compute_chunk_set_id(SRC, off)
    assert cs != compute_chunk_set_id(SRC, on)
    assert compute_nav_layer_id(cs, off) != compute_nav_layer_id(cs, on)


def test_layer_ids_are_consistent_bundle() -> None:
    """compute_layer_ids は個別計算と一致し、5 層キーを返す。"""
    settings = get_settings()
    bundle = compute_layer_ids(SRC, settings)
    er = compute_extraction_recipe_id(SRC, settings)
    cs = compute_chunk_set_id(SRC, settings)

    assert bundle["extraction_recipe_id"] == er
    assert bundle["chunk_set_id"] == cs
    assert bundle["metadata_layer_id"] == compute_metadata_layer_id(cs, settings)
    assert bundle["graph_layer_id"] == compute_graph_layer_id(cs, settings)
    assert bundle["nav_layer_id"] == compute_nav_layer_id(cs, settings)
    assert set(bundle) == {
        "extraction_recipe_id",
        "chunk_set_id",
        "metadata_layer_id",
        "graph_layer_id",
        "nav_layer_id",
    }


def test_ids_carry_layer_prefixes() -> None:
    """各層 ID は層を識別する prefix を持つ。"""
    bundle = compute_layer_ids(SRC, get_settings())
    assert bundle["extraction_recipe_id"].startswith("er_")
    assert bundle["chunk_set_id"].startswith("cs_")
    assert bundle["metadata_layer_id"].startswith("md_")
    assert bundle["graph_layer_id"].startswith("gr_")
    assert bundle["nav_layer_id"].startswith("nv_")


def test_chunk_set_id_keeps_ids_after_parent_child_removal() -> None:
    """親子階層の削除(#271)後も、既定の子サイズで作った既存 chunk_set_id は変わらない。"""
    import hashlib
    import json

    from app.rag.variant_keys import KEY_VERSION

    settings = get_settings().model_copy(update={"rag_chunking_strategy": "structure_aware"})
    legacy_payload = {
        "v": KEY_VERSION,
        "er": compute_extraction_recipe_id(SRC, settings),
        "rag_chunking_strategy": settings.rag_chunking_strategy,
        "rag_chunk_size": settings.rag_chunk_size,
        "rag_chunk_overlap": settings.rag_chunk_overlap,
        "rag_chunk_child_size": 320,
        "rag_chunk_min_chars": settings.rag_chunk_min_chars,
        # #2457 の前の固定分割符の既定(分割符を使わない方式の hash は旧い既定のまま)。
        "rag_chunk_delimiter": "\\n\\n",
        "rag_chunk_context_header_enabled": settings.rag_chunk_context_header_enabled,
    }
    canonical = json.dumps(
        legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    legacy_id = "cs_" + hashlib.sha1(canonical.encode(), usedforsecurity=False).hexdigest()[:16]

    assert compute_chunk_set_id(SRC, settings) == legacy_id


def test_chunk_set_id_keeps_ids_after_delimiter_default_change() -> None:
    """固定分割符の既定の変更(#2457)で、分割符を使わない方式の chunk_set_id は変わらない。"""
    base = get_settings().model_copy(update={"rag_chunking_strategy": "structure_aware"})
    new_default = base.model_copy(update={"rag_chunk_delimiter": "<<<JSONL_RECORD_BOUNDARY>>>"})
    old_default = base.model_copy(update={"rag_chunk_delimiter": "\\n\\n"})
    assert compute_chunk_set_id(SRC, new_default) == compute_chunk_set_id(SRC, old_default)

    fixed_new = new_default.model_copy(update={"rag_chunking_strategy": "fixed_delimiter"})
    fixed_old = old_default.model_copy(update={"rag_chunking_strategy": "fixed_delimiter"})
    # 固定分割では分割符が分割の結果を変えるので、ID も変わる。
    assert compute_chunk_set_id(SRC, fixed_new) != compute_chunk_set_id(SRC, fixed_old)


def test_chunk_set_id_uses_legacy_chunking_names_with_current_docling_text_contract() -> None:
    """分割方式の値と 5 項目の属性名の改名(#599)で、既存の chunk_set_id を変えない。

    期待値は旧い分割名と現在の Docling の本文の契約で計算した ID（#3014）。
    改名の hash 入力は維持し、解析の内容が変わった場合は別の抽出・Chunk 集合にする。
    """
    # 改名時の明示設定で比較し、その後の既定値変更から独立させる。
    base = Settings(
        _env_file=None, rag_chunk_context_header_enabled=True, rag_entity_index_enabled=True
    )
    small_to_big = base.model_copy(update={"rag_chunking_strategy": "small_to_big"})
    tuned = small_to_big.model_copy(
        update={"rag_chunk_child_target_chars": 600, "rag_chunk_parent_max_pages": 2}
    )
    structure = base.model_copy(update={"rag_chunking_strategy": "structure_aware"})

    assert compute_chunk_set_id(SRC, small_to_big) == "cs_47d010dd491c1d60"
    assert compute_chunk_set_id(SRC, tuned) == "cs_c95b73bc9632c686"
    assert compute_chunk_set_id(SRC, structure) == "cs_a506a0c75acdc2f1"


def test_chunk_set_id_uses_small_to_big_params_only_for_small_to_big() -> None:
    """親子階層（small-to-big）の 5 項目は、その方式のときだけ、既定から変えた値だけが
    ID に効く。
    """
    small_to_big = get_settings().model_copy(update={"rag_chunking_strategy": "small_to_big"})
    tuned = small_to_big.model_copy(update={"rag_chunk_child_target_chars": 600})
    assert compute_chunk_set_id(SRC, small_to_big) != compute_chunk_set_id(SRC, tuned)

    structure = small_to_big.model_copy(update={"rag_chunking_strategy": "structure_aware"})
    structure_tuned = structure.model_copy(update={"rag_chunk_child_target_chars": 600})
    assert compute_chunk_set_id(SRC, structure) == compute_chunk_set_id(SRC, structure_tuned)


def test_ingest_language_changes_invalidate_extraction_and_search_only_changes_do_not() -> None:
    settings = Settings(_env_file=None)
    with bind_pack(builtin_pack()):
        original = compute_extraction_recipe_id(SRC, settings)
    with bind_pack(resolve(builtin_pack(), {"question_cues": {"cause_cues": ["理屈"]}})):
        assert compute_extraction_recipe_id(SRC, settings) == original
    with bind_pack(resolve(builtin_pack(), {"labels": {"default_document_type": "合成資料"}})):
        assert compute_extraction_recipe_id(SRC, settings) != original


def test_unstructured_text_contract_invalidates_old_cached_extraction() -> None:
    from rag_parser_core.result import UNSTRUCTURED_TEXT_CONTRACT

    from app.rag.variant_keys import KEY_VERSION, _digest, extraction_recipe_subset

    settings = Settings(_env_file=None, rag_parser_adapter_backend="unstructured")
    legacy = extraction_recipe_subset(settings)
    assert legacy.pop("unstructured_text_contract") == UNSTRUCTURED_TEXT_CONTRACT
    old_id = _digest(
        "er",
        {
            "v": KEY_VERSION,
            "src": SRC,
            "language_pack_ingest_hash": current_pack().ingest_hash,
            **legacy,
            "rag_parser_asr_enabled": True,
        },
    )
    current_id = compute_extraction_recipe_id(SRC, settings)
    assert current_id != old_id
    assert current_id == compute_extraction_recipe_id(SRC, settings)
    assert "unstructured_text_contract" not in extraction_recipe_subset(
        settings.model_copy(update={"rag_parser_adapter_backend": "docling"})
    )
