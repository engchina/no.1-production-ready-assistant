"""rag_poc から移したときの名前を RAG の標準の名前へ改名する migration（#599）。

旧名は、改名の前に保存した値・表を書き換えるための参照として migration にだけ残る。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.rag.oracle_schema import (
    STORED_NAME_COLUMNS_599,
    STORED_NAME_RENAMES_599,
    apply_stored_name_renames_599,
    oracle_schema_migration_sections,
    oracle_schema_migration_sql,
)
from app.rag.system_schema import MANAGED_TABLES, RETIRED_MANAGED_OBJECTS, SystemSchemaManager
from tests.test_system_schema_manager import _FakeDatabase

_OLD = "docrag"
_PROMPTS_MIGRATION = "20260930_008_answer_prompts_table"
_STORED_NAMES_MIGRATION = "20260930_009_stored_engine_names"


def _migration_sql(name: str) -> str:
    return next(
        section.sql for section in oracle_schema_migration_sections() if section.name == name
    )


def _compact(value: object) -> str:
    """JSON_SERIALIZE と同じく空白を入れない JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_rename_pairs_only_touch_old_names_and_do_not_shadow_each_other() -> None:
    """旧名だけを置き換え、先に当てる短い旧名が後の長い旧名を壊さない。"""
    olds = [old for old, _new in STORED_NAME_RENAMES_599]
    assert all(_OLD in old for old in olds)
    assert all(_OLD not in new for _old, new in STORED_NAME_RENAMES_599)
    assert len(set(olds)) == len(olds)
    for index, earlier in enumerate(olds):
        for later in olds[index + 1 :]:
            assert earlier not in later, (earlier, later)


def test_stored_settings_and_chunk_metadata_are_renamed() -> None:
    """処理設定・検索・回答プロファイルの設定・chunk の
    metadata・解析結果・回答の診断の旧名を書き換える。"""
    processing_config = {
        "chunking_strategy": f"{_OLD}_small_to_big",
        f"{_OLD}_child_target_chars": 600,
        f"{_OLD}_table_child_target_chars": 3000,
        f"{_OLD}_parent_target_chars": 6000,
        f"{_OLD}_parent_max_pages": 2,
        f"{_OLD}_parent_max_children": 12,
    }
    profile_config = {
        "query": {
            f"{_OLD}_query_strategy": "hyde",
            f"{_OLD}_answer_flow": "crag",
            f"{_OLD}_neighbor_child_count": 3,
            f"{_OLD}_rerank_enabled": False,
            f"{_OLD}_screen_linking_enabled": True,
        }
    }
    settings_overrides = {f"rag_{_OLD}_child_target_chars": 600, "rag_chunk_size": 800}
    chunk_metadata = {
        "source_parser": f"docling_{_OLD}",
        "chunk_strategy": f"{_OLD}_small_to_big",
        f"{_OLD}_chunk_id": "c1",
        "chunk_group_kind": f"{_OLD}_parent",
        f"{_OLD}_parent_text": "親",
        f"{_OLD}_search_text": "検索",
        f"{_OLD}_metadata_json": json.dumps({"flags": [f"{_OLD}_instruction_callout"]}),
        f"{_OLD}_source_record_refs_json": "[]",
        f"{_OLD}_source_seq_ranges_json": "[]",
        f"{_OLD}_chunk_seq": 1,
        f"{_OLD}_first_page_context_json": "{}",
        "chunk_strategy_fallback_reason": f"{_OLD}_layout_missing",
    }
    extraction: dict[str, object] = {"parser_artifacts": {f"{_OLD}_layout": {"records": []}}}
    fingerprint = {f"{_OLD}_chunk_contract": "abc", "field_schema_hash": "def"}
    evaluation = {
        "diagnostics": {
            "retrieval_strategy": _OLD,
            "retrieval_strategy_adapter": f"{_OLD}_grounded",
            _OLD: {"confidence": "high"},
        },
        "error_stage": f"{_OLD}_answer",
        "stages": [f"{_OLD}_history_rewrite", f"{_OLD}_retrieval_only"],
        "citations": [{"metadata": {f"{_OLD}_role": "anchor", f"{_OLD}_model_used": True}}],
        "record": {"answer_engine": _OLD},
    }

    assert json.loads(apply_stored_name_renames_599(_compact(processing_config))) == {
        "chunking_strategy": "small_to_big",
        "chunk_child_target_chars": 600,
        "chunk_table_child_target_chars": 3000,
        "chunk_parent_target_chars": 6000,
        "chunk_parent_max_pages": 2,
        "chunk_parent_max_children": 12,
    }
    assert json.loads(apply_stored_name_renames_599(_compact(profile_config))) == {
        "query": {
            "query_strategy": "hyde",
            "answer_flow": "crag",
            "neighbor_child_count": 3,
            "rerank_enabled": False,
            "screen_linking_enabled": True,
        }
    }
    assert json.loads(apply_stored_name_renames_599(_compact(settings_overrides))) == {
        "rag_chunk_child_target_chars": 600,
        "rag_chunk_size": 800,
    }
    renamed_chunk = json.loads(apply_stored_name_renames_599(_compact(chunk_metadata)))
    assert renamed_chunk == {
        "source_parser": "docling_layout",
        "chunk_strategy": "small_to_big",
        "engine_chunk_id": "c1",
        "chunk_group_kind": "small_to_big_parent",
        "parent_text": "親",
        "engine_search_text": "検索",
        "engine_metadata_json": json.dumps({"flags": ["instruction_callout"]}),
        "source_record_refs_json": "[]",
        "source_seq_ranges_json": "[]",
        "engine_chunk_seq": 1,
        "first_page_context_json": "{}",
        "chunk_strategy_fallback_reason": "layout_missing",
    }
    assert json.loads(apply_stored_name_renames_599(_compact(extraction))) == {
        "parser_artifacts": {"layout_records": {"records": []}}
    }
    assert json.loads(apply_stored_name_renames_599(_compact(fingerprint))) == {
        "chunk_metadata_contract": "abc",
        "field_schema_hash": "def",
    }
    assert json.loads(apply_stored_name_renames_599(_compact(evaluation))) == {
        "diagnostics": {
            "retrieval_strategy": "hybrid",
            "retrieval_strategy_adapter": "grounded",
            "answer": {"confidence": "high"},
        },
        "error_stage": "answer",
        "stages": ["history_rewrite", "retrieval_only"],
        "citations": [{"metadata": {"evidence_role": "anchor", "evidence_model_used": True}}],
        "record": {"answer_engine": "grounded"},
    }
    # 2 回当てても変わらない（migration の再実行は何も変えない）。
    once = apply_stored_name_renames_599(_compact(evaluation))
    assert apply_stored_name_renames_599(once) == once
    assert _OLD not in once


def test_renamed_values_match_the_code() -> None:
    """書き換えた後の名前が、コードの読む名前と一致する。"""
    from app.config import SMALL_TO_BIG_SETTING_FIELDS, Settings
    from app.rag.chunking_small_to_big import (
        ENGINE_SEARCH_TEXT_KEY,
        FIRST_PAGE_CONTEXT_KEY,
        LAYOUT_ARTIFACT,
        LAYOUT_SOURCE_PARSER,
        SMALL_TO_BIG_STRATEGY,
    )
    from app.rag.kb_adapter_config import KnowledgeBaseIngestionConfig, KnowledgeBaseQueryConfig
    from app.rag.layer_fingerprint import CHUNK_METADATA_CONTRACT_INPUT

    news = {new.strip('"') for _old, new in STORED_NAME_RENAMES_599}
    assert {
        SMALL_TO_BIG_STRATEGY,
        ENGINE_SEARCH_TEXT_KEY,
        FIRST_PAGE_CONTEXT_KEY,
        LAYOUT_ARTIFACT,
        LAYOUT_SOURCE_PARSER,
        CHUNK_METADATA_CONTRACT_INPUT,
        # 以前の縮退の理由(#300)。新しい chunk には付けず、画面が以前の chunk の案内に読む(#3377)。
        "layout_missing",
    } <= news
    assert set(SMALL_TO_BIG_SETTING_FIELDS) <= news
    # 後で消した設定（書き換えの SQL は適用済みの版のまま残す）。回答の画像の添付は回答の
    # モデルの能力で決め、設定を持たない(#3444)。
    removed = {"rag_answer_vision_enabled", "rag_answer_profile"}
    assert all(name in Settings.model_fields for name in news - removed if name.startswith("rag_"))
    assert not removed & set(Settings.model_fields)
    assert {name for name in news if name in KnowledgeBaseIngestionConfig.model_fields} == {
        name.removeprefix("rag_") for name in SMALL_TO_BIG_SETTING_FIELDS
    }
    assert {name for name in news if name in KnowledgeBaseQueryConfig.model_fields} == {
        "query_strategy",
        "answer_flow",
        "neighbor_child_count",
        "rerank_enabled",
        "screen_linking_enabled",
    }


def test_stored_names_migration_rewrites_each_column_in_order() -> None:
    """JSON の列ごとに旧名を含む行だけを、同じ順の REPLACE で書き換え、行は消さない。"""
    sql = _migration_sql(_STORED_NAMES_MIGRATION)
    positions = [
        sql.index(f", ''{old.replace(chr(39), chr(39) * 2)}'', ''{new}'')")
        for old, new in STORED_NAME_RENAMES_599
    ]
    assert positions == sorted(positions)
    for table, column in STORED_NAME_COLUMNS_599:
        assert f"'{table}.{column}'" in sql
    assert "JSON_SERIALIZE(' || v_column || ' RETURNING CLOB)" in sql
    assert "LIKE ''%docrag%''" in sql
    assert "SET answer_engine = ''grounded''" in sql
    assert "error_stage IN (''docrag_answer'', ''docrag_history_rewrite'')" in sql
    for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in sql
    assert f"-- migration: {_STORED_NAMES_MIGRATION}" in oracle_schema_migration_sql()


def test_answer_prompts_migration_renames_or_copies_the_old_table() -> None:
    """旧表だけなら改名し、新しい表もあれば行を写す（行は消さない）。"""
    sql = _migration_sql(_PROMPTS_MIGRATION)
    assert "'ALTER TABLE rag_docrag_prompts RENAME TO rag_answer_prompts'" in sql
    assert "'MERGE INTO rag_answer_prompts target '" in sql
    assert "'WHEN NOT MATCHED THEN INSERT (prompt_key, content, updated_at) '" in sql
    for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in sql
    assert "RAG_ANSWER_PROMPTS" in MANAGED_TABLES
    assert "RAG_DOCRAG_PROMPTS" not in MANAGED_TABLES
    assert ("RAG_DOCRAG_PROMPTS", "TABLE") in RETIRED_MANAGED_OBJECTS


def test_update_moves_prompts_table_without_destructive_approval() -> None:
    """旧名のプロンプトの表が残る既存の DB は、承認なしの更新で新しい表へ移る（#599）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.objects[("RAG_DOCRAG_PROMPTS", "TABLE")] = datetime.now(UTC)
    database.migrations.pop(_PROMPTS_MIGRATION)
    database.migrations.pop(_STORED_NAMES_MIGRATION)

    status = manager.status()
    assert status["status"] == "outdated"
    assert {_PROMPTS_MIGRATION, _STORED_NAMES_MIGRATION} <= set(status["pending_versions"])
    assert status["pending_destructive_migrations"] == []
    assert {item["name"] for item in status["retired_objects"]} == {"RAG_DOCRAG_PROMPTS"}

    result = manager.initialize()

    assert result["status"] == "ready"
    assert {_PROMPTS_MIGRATION, _STORED_NAMES_MIGRATION} <= set(database.migrations)
    # 行を写す migration の後に、旧表を退役したオブジェクトとして消す（記録は大文字）。
    merge_at = next(
        index
        for index, statement in enumerate(database.executed)
        if "MERGE INTO RAG_ANSWER_PROMPTS" in statement
    )
    drop_at = next(
        index
        for index, statement in enumerate(database.executed)
        if statement.startswith("DROP TABLE RAG_DOCRAG_PROMPTS")
    )
    assert merge_at < drop_at
    assert ("RAG_DOCRAG_PROMPTS", "TABLE") not in database.objects
    assert ("RAG_ANSWER_PROMPTS", "TABLE") in database.objects
