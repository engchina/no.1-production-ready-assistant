"""ファイル準備 csv_to_json / excel_to_json の改名と、固定分割符の既定の変更の移行（#2457）。

- DB の保存値（文書レシピ・取込ジョブ・その記録）は system schema の migration が移す。
- global の設定（backend/.env・parser-settings.json）は settings の migration の CLI が移す。
- 構造化レコードの JSONL を作るファイル準備に Docling / Unstructured を選ぶと、取込の前に止める。
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import dotenv_values

from app.config import Settings, get_settings
from app.rag.oracle_schema import (
    STORED_PREPROCESS_COLUMNS_2457,
    STORED_PREPROCESS_RENAMES_2457,
    apply_stored_preprocess_renames_2457,
    oracle_schema_migration_sections,
)
from app.rag.parser_source_guard import parser_source_block
from app.rag.source_profile import build_source_profile
from app.rag.structured_records_settings_migration import (
    apply_settings_migration,
    main,
    plan_settings_migration,
)
from app.schemas.document import DocumentProcessingConfig

_MIGRATION = "20261019_003_structured_records_jsonl_profiles"


def _migration_sql() -> str:
    return next(
        section.sql for section in oracle_schema_migration_sections() if section.name == _MIGRATION
    )


def _compact(value: object) -> str:
    """JSON_SERIALIZE と同じく空白を入れない JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_rename_pairs_are_in_the_sql_in_order_and_are_idempotent() -> None:
    sql = _migration_sql()
    positions = [sql.index(f"''{old}'', ''{new}''") for old, new in STORED_PREPROCESS_RENAMES_2457]
    assert positions == sorted(positions)
    for table, column in STORED_PREPROCESS_COLUMNS_2457:
        assert f"'{table}.{column}'" in sql
    # 新しい名前は旧い名前を含まない（引用符まで含めて置き換える）ので、2 回目は何も変えない。
    renamed = _compact({"preprocess_profile": "csv_to_jsonl", "excel_to_jsonl_format": "v1"})
    assert apply_stored_preprocess_renames_2457(renamed) == renamed


def test_stored_recipe_and_job_values_are_renamed() -> None:
    recipe = {"preprocess_profile": "excel_to_json", "excel_options": {"mode": "table"}}
    migrated = json.loads(apply_stored_preprocess_renames_2457(_compact(recipe)))
    assert migrated["preprocess_profile"] == "excel_to_jsonl"
    # 改名した値は今の文書レシピとして読める。
    assert DocumentProcessingConfig.model_validate(migrated).preprocess_profile == "excel_to_jsonl"

    job = {"rag_preprocess_profile": "csv_to_json", "rag_parser_adapter_backend": "docling"}
    assert json.loads(apply_stored_preprocess_renames_2457(_compact(job))) == {
        "rag_preprocess_profile": "csv_to_jsonl",
        "rag_parser_adapter_backend": "docling",
    }
    subset = {"rag_preprocess_profile": "csv_to_json", "csv_to_json_format": "v1"}
    assert json.loads(apply_stored_preprocess_renames_2457(_compact(subset))) == {
        "rag_preprocess_profile": "csv_to_jsonl",
        "csv_to_jsonl_format": "v1",
    }
    # 他の値の一部は変えない。
    other = {"preprocess_profile": "office_to_pdf", "note": "csv_to_json_like"}
    assert apply_stored_preprocess_renames_2457(_compact(other)) == _compact(other)


def test_recipe_parser_and_delimiter_rules_only_touch_matching_recipes() -> None:
    """解析エンジンは Docling / Unstructured / 未指定、分割符は固定分割の未指定だけを書き換える。"""
    sql = _migration_sql()
    parser_update, delimiter_update = sql.split("EXECUTE IMMEDIATE")[-2:]
    assert "IN (''csv_to_jsonl'', ''excel_to_jsonl'')" in parser_update
    assert "NVL(JSON_VALUE(" in parser_update
    assert "''docling'') IN (''docling'', ''unstructured'')" in parser_update
    assert "chunking_strategy" not in parser_update
    assert "SET ''$.chunk_delimiter'' = ''\\n\\n''" in delimiter_update
    assert "''$.chunk_delimiter'') IS NULL" in delimiter_update
    assert "preprocess_profile" not in delimiter_update
    assert "DELETE" not in sql and "DROP" not in sql


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_migration_renames_env_and_switches_global_parser(tmp_path: Path) -> None:
    env_file = _write(
        tmp_path / ".env",
        "# 既存のコメント\n"
        "RAG_PREPROCESS_PROFILE=csv_to_json\n"
        "RAG_PREPROCESS_CSV_TO_JSON_SERVICE_URL=http://127.0.0.1:28012\n"
        "RAG_PREPROCESS_EXCEL_TO_JSON_SERVICE_URL=http://127.0.0.1:28013\n"
        "RAG_CHUNKING_STRATEGY=fixed_delimiter\n"
        "RAG_UNRELATED=keep\n",
    )
    parser_file = _write(
        tmp_path / "parser-settings.json",
        json.dumps({"adapter_backend": "unstructured", "unstructured_enabled": True}),
    )

    plan = plan_settings_migration(env_file, parser_file)
    assert not plan.empty
    # 予定を作るだけではファイルを変えない。
    assert dotenv_values(env_file)["RAG_PREPROCESS_PROFILE"] == "csv_to_json"

    apply_settings_migration(plan, env_file, parser_file)
    env = dotenv_values(env_file)
    assert env["RAG_PREPROCESS_PROFILE"] == "csv_to_jsonl"
    assert "RAG_PREPROCESS_CSV_TO_JSON_SERVICE_URL" not in env
    assert "RAG_PREPROCESS_EXCEL_TO_JSON_SERVICE_URL" not in env
    assert env["RAG_PREPROCESS_CSV_TO_JSONL_SERVICE_URL"] == "http://127.0.0.1:28012"
    assert env["RAG_PREPROCESS_EXCEL_TO_JSONL_SERVICE_URL"] == "http://127.0.0.1:28013"
    # 既定のまま使っていた固定分割の分割符は旧い既定を明示し、分割の結果を変えない。
    assert env["RAG_CHUNK_DELIMITER"] == "\\n\\n"
    assert env["RAG_UNRELATED"] == "keep"
    assert "# 既存のコメント" in env_file.read_text(encoding="utf-8")
    parser = json.loads(parser_file.read_text(encoding="utf-8"))
    assert parser == {
        "adapter_backend": "structured_records_jsonl",
        "unstructured_enabled": True,
        "structured_records_jsonl_enabled": True,
    }
    # 移した値で起動できる（旧い値・旧い変数名は読まない）。
    settings = Settings(_env_file=env_file)
    assert settings.rag_preprocess_profile == "csv_to_jsonl"
    assert settings.rag_chunk_delimiter == "\\n\\n"
    assert settings.rag_preprocess_csv_to_jsonl_service_url == "http://127.0.0.1:28012"

    # 2 回目は何も変えない（冪等）。
    assert plan_settings_migration(env_file, parser_file).empty


def test_settings_migration_without_parser_file_writes_backend_env(tmp_path: Path) -> None:
    env_file = _write(tmp_path / ".env", "RAG_PREPROCESS_PROFILE=excel_to_json\n")
    parser_file = tmp_path / "parser-settings.json"

    apply_settings_migration(plan_settings_migration(env_file, parser_file), env_file, parser_file)

    env = dotenv_values(env_file)
    assert env["RAG_PREPROCESS_PROFILE"] == "excel_to_jsonl"
    assert env["RAG_PARSER_ADAPTER_BACKEND"] == "structured_records_jsonl"
    assert env["RAG_PARSER_STRUCTURED_RECORDS_JSONL_ENABLED"] == "true"
    assert not parser_file.exists()
    # 固定分割を使っていなければ分割符は書かない（新しい既定のまま）。
    assert "RAG_CHUNK_DELIMITER" not in env


def test_settings_migration_keeps_unrelated_settings(tmp_path: Path) -> None:
    env_file = _write(
        tmp_path / ".env",
        "RAG_PREPROCESS_PROFILE=office_to_pdf\n"
        "RAG_CHUNKING_STRATEGY=fixed_delimiter\n"
        "RAG_CHUNK_DELIMITER=---\n",
    )
    parser_file = _write(
        tmp_path / "parser-settings.json", json.dumps({"adapter_backend": "docling"})
    )
    assert plan_settings_migration(env_file, parser_file).empty
    assert main(["--env-file", str(env_file), "--parser-settings", str(parser_file)]) == 0


def test_main_dry_run_does_not_write(tmp_path: Path, capsys: object) -> None:
    env_file = _write(tmp_path / ".env", "RAG_PREPROCESS_PROFILE=csv_to_json\n")
    before = env_file.read_text(encoding="utf-8")
    assert main(["--env-file", str(env_file)]) == 0
    assert env_file.read_text(encoding="utf-8") == before
    assert main(["--env-file", str(env_file), "--apply"]) == 0
    assert dotenv_values(env_file)["RAG_PREPROCESS_PROFILE"] == "csv_to_jsonl"


def _settings(**update: object) -> Settings:
    return get_settings().model_copy(update=update)


def test_jsonl_preprocess_with_docling_is_blocked_before_ingestion() -> None:
    """構造化レコードの JSONL を作るファイル準備は、構造化レコード JSONL 以外では始めない。"""
    profile = build_source_profile(
        original_file_name="table.csv",
        sanitized_file_name="table.csv",
        content_type="text/csv",
        file_size_bytes=8,
        content_sha256="0" * 64,
        data=b"a,b\n1,2\n",
    )
    for backend in ("docling", "unstructured"):
        block = parser_source_block(
            _settings(rag_preprocess_profile="csv_to_jsonl", rag_parser_adapter_backend=backend),
            profile,
        )
        assert block is not None
        assert block.code == "structured_records_requires_structured_records_jsonl"
        assert block.suggested_backend == "structured_records_jsonl"
        assert "「構造化レコード JSONL」" in block.message
    assert (
        parser_source_block(
            _settings(
                rag_preprocess_profile="csv_to_jsonl",
                rag_parser_adapter_backend="structured_records_jsonl",
            ),
            profile,
        )
        is None
    )


def test_recipe_chunk_delimiter_overrides_global_and_is_compared_only_for_fixed_split() -> None:
    """文書レシピの分割符(#2457)は global を上書きし、固定分割のときだけ再処理の要否に効く。"""
    from app.api.routes.documents import _processing_config_drift_groups
    from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig, resolve_effective_settings

    config = KnowledgeBaseAdapterConfig(
        ingestion=DocumentProcessingConfig(
            chunking_strategy="fixed_delimiter", chunk_delimiter="  ---  "
        )
    )
    merged = resolve_effective_settings(get_settings(), config, scope="ingestion")
    assert merged.rag_chunk_delimiter == "---"

    # 分割符をレシピに持つ前の snapshot は旧い既定(\n\n)で分割したものとして比べる。
    old_snapshot = {"chunking_strategy": "fixed_delimiter"}
    same = {"chunking_strategy": "fixed_delimiter", "chunk_delimiter": "\\n\\n"}
    marker = {
        "chunking_strategy": "fixed_delimiter",
        "chunk_delimiter": "<<<JSONL_RECORD_BOUNDARY>>>",
    }
    assert "chunking_strategy" not in _processing_config_drift_groups(old_snapshot, same)
    assert "chunking_strategy" in _processing_config_drift_groups(old_snapshot, marker)
    # 固定分割以外では分割符の既定の変更を再処理の理由にしない。
    other = {
        "chunking_strategy": "structure_aware",
        "chunk_delimiter": "<<<JSONL_RECORD_BOUNDARY>>>",
    }
    assert _processing_config_drift_groups({"chunking_strategy": "structure_aware"}, other) == []
