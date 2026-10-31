# NL2SQL Manual Integration

`nl2sql_manual_integration.py` is a local smoke test for the real Oracle runtime and
OCI Enterprise AI Direct (`enterprise_ai_direct`, the only SQL generation engine). It
uses the shared `platform/.env` (`PLATFORM_*`) and `backend/.env` (`NL2SQL_*`) through
the existing FastAPI settings (#211) and does not print secret values.

Safe preview-only check (`--engines` defaults to `enterprise_ai_direct`):

```bash
uv run python scripts/nl2sql_manual_integration.py
```

Oracle diagnostics has an outer timeout guard. If the driver/network does not
return quickly, the script stops before previewing SQL:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --diagnostics-timeout 8
```

Diagnostics-only mode exits after the first diagnostics step. It does not
preview SQL, execute jobs, or mutate Oracle state:

```bash
uv run python scripts/nl2sql_manual_integration.py --diagnostics-only --json-report reports/nl2sql-diagnostics.json
```

Enterprise AI Direct-required check. This fails fast when
`PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT`, `PLATFORM_OCI_ENTERPRISE_AI_API_KEY`, or an Enterprise AI
model is missing, and also fails if `enterprise_ai_direct` falls back to
deterministic mode:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-enterprise-ai --execute
```

Production persistence-required check. This fails when NL2SQL state is still
process-local memory instead of the Oracle JSON state table:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence
```

When the default profile still points at local mock tables, pass one or more
real Oracle tables. The script creates/updates a `manual_integration` profile
for the smoke test:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --refresh-catalog --allowed-table YOUR_TABLE
```

Full smoke including generated SQL execution:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-enterprise-ai --execute
```

One-command smoke. This runs preview, generated SQL execution, and supporting
feature checks in sequence:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence --full-smoke --allowed-table YOUR_TABLE
```

Production release gate alias. This expands to full smoke and requires Oracle
runtime, Oracle-backed NL2SQL state persistence, and OCI Enterprise AI Direct:

```bash
uv run python scripts/nl2sql_manual_integration.py --release-gate --allowed-table YOUR_TABLE
uv run python scripts/nl2sql_manual_integration.py --release-gate --allowed-table YOUR_TABLE --json-report reports/nl2sql-release-gate.json
```

`--json-report` writes a machine-readable artifact with `ok`, `exit_code`,
engine/profile/table inputs, summary counts, and every step result for CI or
operations dashboards. The report also includes `started_at`, `finished_at`,
and `elapsed_ms`.

Real-schema smoke after catalog refresh. Replace the table/question with a known
table from the configured Oracle schema:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-enterprise-ai --refresh-catalog --execute --allowed-table YOUR_TABLE --question "YOUR_TABLE から主要な列を一覧して"
```

Support checks for the absorbed No.1-SQL-Assist features (comment suggestions,
COMMENT ON SQL generation, feedback vector index plan). This does not run DDL
against business tables:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --check-supporting-features
```

Legacy absorption checks (classifier, comments, annotations, feedback vector
search). `--execute-comments` / `--execute-annotations` mutate Oracle and require
`--allowed-table` with disposable `NL2SQL_*` tables:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --check-legacy-absorption --allowed-table NL2SQL_DISPOSABLE_TABLE
```

Live feedback vector index smoke. This seeds demo feedback items and rebuilds
the Oracle AI Database vector table/index using OCI GenAI embeddings:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-feedback-embedding --seed-demo-learning --execute-feedback-index
```

`--execute` runs the generated `SELECT` SQL and writes a history item through the
normal NL2SQL job path. `--require-oracle` enforces the Oracle runtime;
`--require-enterprise-ai` enforces real OCI Enterprise AI Direct runtime for
`enterprise_ai_direct`; `--require-feedback-embedding` enforces OCI GenAI
feedback embedding readiness for live vector learning checks;
`--require-oracle-persistence` enforces Oracle-backed NL2SQL state persistence.
Each diagnostics / preview / job line prints non-secret readiness and engine
metadata (runtime, provider, mode, model).
`--full-smoke` writes job/history records and executes generated SELECT SQL when
the runtime is configured. `--release-gate` is the production gate alias for
`--full-smoke` plus Oracle runtime, Oracle persistence, and Enterprise AI Direct
requirements. It does not require live feedback embedding; run the feedback vector
smoke separately when OCI GenAI embedding is part of the release gate.
