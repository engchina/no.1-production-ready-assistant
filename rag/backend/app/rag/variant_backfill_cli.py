"""KB 構築 variant の migration / backfill 検証 artifact を生成する CLI。

この CLI は安全側の運用補助に限定する。既存 Oracle data へ書き込む SQL は生成せず、
staging / production で migration 適用後に確認すべき read-only check と runbook を固定する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pr_backend_core.logging import configure_cli_logging

BACKFILL_ARTIFACT_VERSION = "20260629_001"
STATUS_ENUM = (
    "not_requested",
    "planned_only",
    "materialized",
    "needs_reingest",
    "error",
)

Severity = Literal["blocker", "warning", "info"]


@dataclass(frozen=True)
class BackfillCheck:
    """Oracle migration/backfill 後に実行する read-only validation check。"""

    name: str
    severity: Severity
    expected: str
    sql: str


@dataclass(frozen=True)
class BackfillPhase:
    """運用 runbook の段階。"""

    phase_id: str
    title: str
    objective: str
    commands: tuple[str, ...]
    acceptance: tuple[str, ...]


def variant_backfill_checks() -> tuple[BackfillCheck, ...]:
    """V3 variant artifact migration/backfill 用の read-only checks を返す。"""
    return (
        BackfillCheck(
            name="required_variant_tables_missing",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM (
    SELECT 'RAG_CHUNK_SETS' AS table_name FROM dual
    UNION ALL SELECT 'RAG_DOCUMENT_EXTRACTIONS' FROM dual
    UNION ALL SELECT 'RAG_ARTIFACT_LAYERS' FROM dual
) required
WHERE NOT EXISTS (
    SELECT 1
    FROM user_tables t
    WHERE t.table_name = required.table_name
)
""".strip(),
        ),
        BackfillCheck(
            name="required_variant_columns_missing",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM (
    SELECT 'RAG_CHUNKS' AS table_name, 'CHUNK_SET_ID' AS column_name FROM dual
    UNION ALL SELECT 'RAG_CHUNK_SETS', 'EXTRACTION_RECIPE_ID' FROM dual
    UNION ALL SELECT 'RAG_DOCUMENT_EXTRACTIONS', 'EXTRACTION_RECIPE_ID' FROM dual
    UNION ALL SELECT 'RAG_DOCUMENT_EXTRACTIONS', 'STATUS' FROM dual
    UNION ALL SELECT 'RAG_ARTIFACT_LAYERS', 'LAYER_KIND' FROM dual
    UNION ALL SELECT 'RAG_ARTIFACT_LAYERS', 'PARENT_CHUNK_SET_ID' FROM dual
    UNION ALL SELECT 'RAG_CHUNK_SETS', 'IS_SERVING' FROM dual
) required
WHERE NOT EXISTS (
    SELECT 1
    FROM user_tab_columns c
    WHERE c.table_name = required.table_name
      AND c.column_name = required.column_name
)
""".strip(),
        ),
        BackfillCheck(
            name="indexed_chunks_without_chunk_set",
            severity="blocker",
            expected="issue_count = 0 before enabling variant-filtered serving",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunks c
JOIN rag_documents d
  ON d.document_id = c.document_id
WHERE d.status = 'INDEXED'
  AND c.chunk_set_id IS NULL
""".strip(),
        ),
        BackfillCheck(
            name="chunk_sets_missing_extraction_recipe",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunk_sets cs
WHERE cs.extraction_recipe_id IS NULL
""".strip(),
        ),
        BackfillCheck(
            name="chunk_sets_missing_extraction_artifact",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunk_sets cs
WHERE cs.extraction_recipe_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM rag_document_extractions de
      WHERE de.document_id = cs.document_id
        AND de.extraction_recipe_id = cs.extraction_recipe_id
  )
""".strip(),
        ),
        BackfillCheck(
            name="indexed_chunk_sets_without_materialized_extraction",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunk_sets cs
LEFT JOIN rag_document_extractions de
  ON de.document_id = cs.document_id
 AND de.extraction_recipe_id = cs.extraction_recipe_id
WHERE cs.status = 'INDEXED'
  AND (de.status IS NULL OR de.status <> 'materialized')
""".strip(),
        ),
        BackfillCheck(
            name="kb_memberships_without_serving_chunk_set",
            severity="blocker",
            expected="issue_count = 0 for indexed documents assigned to active KBs",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_document_knowledge_bases dkb
JOIN rag_documents d
  ON d.document_id = dkb.document_id
JOIN rag_knowledge_bases kb
  ON kb.knowledge_base_id = dkb.knowledge_base_id
WHERE d.status = 'INDEXED'
  AND kb.status = 'ACTIVE'
  AND NOT EXISTS (
      SELECT 1
      FROM rag_chunk_sets cs
      WHERE cs.document_id = dkb.document_id
        AND cs.is_serving = 1
        AND cs.status = 'INDEXED'
  )
""".strip(),
        ),
        BackfillCheck(
            name="serving_chunk_sets_not_indexed",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunk_sets cs
WHERE cs.is_serving = 1
  AND cs.status <> 'INDEXED'
""".strip(),
        ),
        BackfillCheck(
            name="chunks_referencing_missing_chunk_set",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunks c
WHERE c.chunk_set_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM rag_chunk_sets cs
      WHERE cs.chunk_set_id = c.chunk_set_id
  )
""".strip(),
        ),
        BackfillCheck(
            name="indexed_chunk_sets_without_chunks",
            severity="blocker",
            expected="issue_count = 0",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_chunk_sets cs
WHERE cs.status = 'INDEXED'
  AND NOT EXISTS (
      SELECT 1
      FROM rag_chunks c
      WHERE c.chunk_set_id = cs.chunk_set_id
  )
""".strip(),
        ),
        BackfillCheck(
            name="requested_layers_needing_action",
            severity="blocker",
            expected="issue_count = 0 before claiming layer readiness",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_artifact_layers l
WHERE l.requested = 1
  AND l.status IN ('needs_reingest', 'error')
""".strip(),
        ),
        BackfillCheck(
            name="requested_layers_planned_only",
            severity="warning",
            expected="issue_count may be > 0 only for explicitly planned GraphRAG/navigation work",
            sql="""
SELECT COUNT(*) AS issue_count
FROM rag_artifact_layers l
WHERE l.requested = 1
  AND l.status = 'planned_only'
""".strip(),
        ),
    )


def variant_backfill_phases() -> tuple[BackfillPhase, ...]:
    """V3 artifact migration/backfill の推奨運用段階を返す。"""
    return (
        BackfillPhase(
            phase_id="01_prepare_artifacts",
            title="成果物を生成してレビューする",
            objective=(
                "Oracle schema migration と検証 SQL を、適用前に hash 付き artifact として"
                "固定する。"
            ),
            commands=(
                "uv run python -m app.rag.oracle_schema --migration "
                "--output ../artifacts/oracle-schema-migration.sql "
                "--manifest-output ../artifacts/oracle-schema-migration.manifest.json",
                "uv run python -m app.rag.variant_backfill_cli --format sql --checks-only "
                "--output ../artifacts/variant-backfill-checks.sql",
                "uv run python -m app.rag.variant_backfill_cli --format json "
                "--output ../artifacts/variant-backfill.manifest.json",
            ),
            acceptance=(
                "migration manifest の sha256 をレビュー済み artifact として保存している。",
                "検証 SQL に書き込み文が含まれていない。",
                (
                    "RAG repo の公開面はナレッジ構築 / 検索"
                    "・回答プロファイル / 取込・検索・回答設定に閉じている。"
                ),
            ),
        ),
        BackfillPhase(
            phase_id="02_apply_schema_migration",
            title="schema migration を適用する",
            objective=(
                "`rag_document_extractions` / `rag_artifact_layers` / `chunk_set_id` を"
                "本番 schema へ反映する。"
            ),
            commands=(
                "sqlcl @../artifacts/oracle-schema-migration.sql",
                "uv run python -m app.rag.variant_backfill_cli --format sql --checks-only "
                "--output ../artifacts/variant-backfill-checks.sql",
            ),
            acceptance=(
                "`required_variant_tables_missing` が 0。",
                "`required_variant_columns_missing` が 0。",
                "既存検索を止める必要がある場合は maintenance window 内に収まっている。",
            ),
        ),
        BackfillPhase(
            phase_id="03_backfill_existing_documents",
            title="既存文書を構築 artifact へ紐付ける",
            objective=(
                "既存 chunk を安全側の単一 chunk_set に紐付け、extraction artifact と "
                "文書単位の serving chunk_set を確定する。parser/preprocess 差分がある文書は"
                "再取込対象へ分ける。"
            ),
            commands=(
                "既存文書の source_sha256 / effective 構築設定から extraction_recipe_id と "
                "chunk_set_id を算出する。",
                "同一 extraction_recipe は extraction artifact を 1 件だけ "
                "materialized として記録する。",
                "文書ごとに rag_chunk_sets.is_serving=1 を 1 つだけ確定する。",
            ),
            acceptance=(
                "`indexed_chunks_without_chunk_set` が 0。",
                "`chunk_sets_missing_extraction_artifact` が 0。",
                "`kb_memberships_without_serving_chunk_set` が 0。",
                "再抽出できない parser/preprocess 差分は `needs_reingest` として見える。",
            ),
        ),
        BackfillPhase(
            phase_id="04_validate_serving",
            title="検索配信を検証する",
            objective=(
                "Search Answer Profile 検索が KB の serving chunk_set だけを使い、"
                "古い KB query 設定を使わないことを確認する。"
            ),
            commands=(
                "uv run pytest tests/test_search_api.py tests/test_knowledge_bases_api.py "
                "tests/test_search_answer_profiles_api.py -q",
                "staging で代表 Search Answer Profile の検索 diagnostics を保存する。",
            ),
            acceptance=(
                "`serving_chunk_sets_not_indexed` が 0。",
                "`chunks_referencing_missing_chunk_set` が 0。",
                "検索 diagnostics の設定解決順が request > Search Answer Profile > "
                "global defaults になっている。",
            ),
        ),
        BackfillPhase(
            phase_id="05_record_layer_readiness",
            title="派生 layer の状態を記録する",
            objective=(
                "metadata / graph / navigation を完了と誤表示せず、"
                "実体化済み・計画のみ・再取込必要を区別する。"
            ),
            commands=(
                "GET /api/documents/{id}/chunk-sets の layer_statuses を sampling する。",
                "GraphRAG / navigation builder が未接続の環境では planned_only を"
                "受け入れ条件に明記する。",
            ),
            acceptance=(
                "`requested_layers_needing_action` が 0。",
                "`requested_layers_planned_only` が 0 でない場合、その件数と対象 layer が"
                "運用メモに残っている。",
                "UI は planned_only を完了表示にしていない。",
            ),
        ),
    )


def render_validation_sql(checks: Sequence[BackfillCheck] | None = None) -> str:
    """read-only validation SQL artifact を返す。"""
    resolved_checks = tuple(checks or variant_backfill_checks())
    sections = [
        "-- RAG variant backfill validation checks",
        f"-- artifact_version: {BACKFILL_ARTIFACT_VERSION}",
        "-- This artifact intentionally contains read-only SELECT statements only.",
    ]
    for check in resolved_checks:
        sections.extend(
            [
                "",
                f"-- check: {check.name}",
                f"-- severity: {check.severity}",
                f"-- expected: {check.expected}",
                check.sql.rstrip() + ";",
            ]
        )
    return "\n".join(sections) + "\n"


def render_markdown_runbook(
    *,
    checks_only: bool = False,
    checks: Sequence[BackfillCheck] | None = None,
    phases: Sequence[BackfillPhase] | None = None,
) -> str:
    """運用 runbook を Markdown で返す。"""
    resolved_checks = tuple(checks or variant_backfill_checks())
    resolved_phases = tuple(phases or variant_backfill_phases())
    lines = [
        "# RAG 構築 variant migration / backfill runbook",
        "",
        f"- artifact version: `{BACKFILL_ARTIFACT_VERSION}`",
        "- 対象: `rag_chunk_sets`(文書単位 `is_serving`)/ `rag_document_extractions` / "
        "`rag_artifact_layers`",
        "- 方針: この runbook と検証 SQL は read-only。"
        "実データの回填はレビュー済み手順で実行する。",
        "",
    ]
    if not checks_only:
        lines.append("## 手順")
        lines.append("")
        for phase in resolved_phases:
            lines.extend(
                [
                    f"### {phase.phase_id} {phase.title}",
                    "",
                    phase.objective,
                    "",
                    "Commands / actions:",
                    "",
                ]
            )
            lines.extend(f"- `{command}`" for command in phase.commands)
            lines.extend(["", "Acceptance:", ""])
            lines.extend(f"- {item}" for item in phase.acceptance)
            lines.append("")
    lines.extend(
        [
            "## 検証 SQL",
            "",
            "| check | severity | expected |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| `{check.name}` | `{check.severity}` | {check.expected} |" for check in resolved_checks
    )
    lines.extend(
        [
            "",
            "SQL artifact:",
            "",
            "```bash",
            "uv run python -m app.rag.variant_backfill_cli --format sql --checks-only "
            "--output ../artifacts/variant-backfill-checks.sql",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def variant_backfill_manifest(
    *,
    checks_only: bool = False,
    checks: Sequence[BackfillCheck] | None = None,
    phases: Sequence[BackfillPhase] | None = None,
) -> dict[str, Any]:
    """runbook/check artifact の deterministic manifest を返す。"""
    resolved_checks = tuple(checks or variant_backfill_checks())
    resolved_phases = () if checks_only else tuple(phases or variant_backfill_phases())
    validation_sql = render_validation_sql(resolved_checks)
    runbook = render_markdown_runbook(
        checks_only=checks_only,
        checks=resolved_checks,
        phases=resolved_phases,
    )
    return {
        "artifact_type": "variant_backfill_runbook",
        "artifact_version": BACKFILL_ARTIFACT_VERSION,
        "status_enum": list(STATUS_ENUM),
        "checks_only": checks_only,
        "validation_sql_sha256": _sha256(validation_sql),
        "runbook_sha256": _sha256(runbook),
        "checks": [
            {
                "name": check.name,
                "severity": check.severity,
                "expected": check.expected,
                "sql_sha256": _sha256(check.sql),
            }
            for check in resolved_checks
        ],
        "phases": [
            {
                "phase_id": phase.phase_id,
                "title": phase.title,
                "command_count": len(phase.commands),
                "acceptance_count": len(phase.acceptance),
            }
            for phase in resolved_phases
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    parser = _build_parser()
    args = parser.parse_args(argv)
    output_format = args.format
    checks_only = bool(args.checks_only)

    if output_format == "sql":
        content = render_validation_sql()
    elif output_format == "json":
        content = (
            json.dumps(
                variant_backfill_manifest(checks_only=checks_only),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    else:
        content = render_markdown_runbook(checks_only=checks_only)

    _write_text(content, args.output)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-variant-backfill",
        description=(
            "RAG 構築 variant migration/backfill の read-only runbook と検証 SQL を生成します。"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "sql"),
        default="markdown",
        help="出力形式。既定は markdown runbook。",
    )
    parser.add_argument(
        "--checks-only",
        action="store_true",
        help="runbook の手順を省き、validation checks だけを出力します。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="artifact の保存先。未指定なら stdout に出力します。",
    )
    return parser


def _write_text(content: str, output_path: Path | None) -> None:
    if output_path is None:
        print(content, end="")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
