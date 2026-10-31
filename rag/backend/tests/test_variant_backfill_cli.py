"""RAG 構築 variant backfill runbook CLI のテスト。"""

import json
import re
from pathlib import Path

from app.rag import variant_backfill_cli


def test_variant_backfill_markdown_describes_v3_artifact_tables() -> None:
    """runbook は V3 artifact の運用対象と検証観点を明示する。"""
    markdown = variant_backfill_cli.render_markdown_runbook()

    assert "RAG 構築 variant migration / backfill runbook" in markdown
    assert "rag_document_extractions" in markdown
    assert "rag_artifact_layers" in markdown
    # 3 層モデル: per-KB binding 表は退役し、配信は文書単位 is_serving に一本化。
    assert "rag_kb_chunk_set_bindings" not in markdown
    assert "is_serving" in markdown
    assert "needs_reingest" in markdown
    assert "Search Answer Profile" in markdown
    assert "Select AI" not in markdown
    assert "NL2SQL" not in markdown


def test_variant_backfill_manifest_is_deterministic() -> None:
    """manifest は時刻を含まず、checks / phases の hash を安定して返す。"""
    manifest = variant_backfill_cli.variant_backfill_manifest()

    assert manifest == variant_backfill_cli.variant_backfill_manifest()
    assert "generated_at" not in manifest
    assert manifest["artifact_type"] == "variant_backfill_runbook"
    assert manifest["artifact_version"] == "20260629_001"
    assert manifest["status_enum"] == [
        "not_requested",
        "planned_only",
        "materialized",
        "needs_reingest",
        "error",
    ]
    check_names = [check["name"] for check in manifest["checks"]]
    assert "required_variant_tables_missing" in check_names
    assert "indexed_chunks_without_chunk_set" in check_names
    assert "kb_memberships_without_serving_chunk_set" in check_names
    assert "requested_layers_planned_only" in check_names
    assert [phase["phase_id"] for phase in manifest["phases"]] == [
        "01_prepare_artifacts",
        "02_apply_schema_migration",
        "03_backfill_existing_documents",
        "04_validate_serving",
        "05_record_layer_readiness",
    ]


def test_variant_backfill_validation_sql_is_read_only() -> None:
    """検証 SQL は production data を変更する文を含めない。"""
    sql = variant_backfill_cli.render_validation_sql()

    assert "-- artifact_version: 20260629_001" in sql
    assert "-- check: required_variant_tables_missing" in sql
    assert "-- check: requested_layers_needing_action" in sql
    assert "SELECT COUNT(*) AS issue_count" in sql
    assert "rag_document_extractions" in sql
    assert "rag_artifact_layers" in sql

    mutation_match = re.search(
        r"\b(ALTER|CREATE|DELETE|DROP|INSERT|MERGE|TRUNCATE|UPDATE)\b",
        sql.upper(),
    )
    assert mutation_match is None


def test_variant_backfill_cli_writes_markdown_json_and_sql(tmp_path: Path) -> None:
    """CLI は runbook / manifest / validation SQL を artifact として保存できる。"""
    markdown_output = tmp_path / "artifacts" / "variant-backfill.md"
    json_output = tmp_path / "artifacts" / "variant-backfill.json"
    sql_output = tmp_path / "artifacts" / "variant-backfill.sql"

    assert (
        variant_backfill_cli.main(["--format", "markdown", "--output", str(markdown_output)]) == 0
    )
    assert (
        variant_backfill_cli.main(
            ["--format", "json", "--checks-only", "--output", str(json_output)]
        )
        == 0
    )
    assert (
        variant_backfill_cli.main(["--format", "sql", "--checks-only", "--output", str(sql_output)])
        == 0
    )

    assert "検証 SQL" in markdown_output.read_text(encoding="utf-8")
    manifest = json.loads(json_output.read_text(encoding="utf-8"))
    assert manifest["checks_only"] is True
    assert manifest["phases"] == []
    sql = sql_output.read_text(encoding="utf-8")
    assert "SELECT COUNT(*) AS issue_count" in sql
    assert "UPDATE" not in sql.upper()
