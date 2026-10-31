# NL2SQL Manual Integration

`nl2sql_manual_integration.py` is a local smoke test for real Oracle Select AI /
Select AI Agent wiring. It uses the shared `platform/.env` (`PLATFORM_*`) and
`backend/.env` (`NL2SQL_*`) through the existing FastAPI settings (#211) and does
not print secret values.

Safe preview-only check:

```bash
uv run python scripts/nl2sql_manual_integration.py
```

Oracle diagnostics has an outer timeout guard. If the driver/network does not
return quickly, the script stops before previewing Oracle engines:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --diagnostics-timeout 8
```

Diagnostics-only mode exits after the first diagnostics step. It does not
refresh assets, preview SQL, execute jobs, or mutate Oracle state:

```bash
uv run python scripts/nl2sql_manual_integration.py --diagnostics-only --json-report reports/nl2sql-diagnostics.json
```

Enterprise AI Direct-required check. This fails fast when
`PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT`, `PLATFORM_OCI_ENTERPRISE_AI_API_KEY`, or an Enterprise AI
model is missing, and also fails if `enterprise_ai_direct` falls back to
deterministic mode:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-enterprise-ai --engines enterprise_ai_direct --execute
```

Oracle-required check with asset refresh:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --refresh-catalog --refresh-assets
```

Production persistence-required check. This fails when NL2SQL state is still
process-local memory instead of the Oracle JSON state table:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence --engines select_ai
```

Cross-process asset metadata check. First refresh assets once, then run the
second command in a new process to prove Select AI profile / Agent team metadata
was restored from Oracle persistence rather than process memory:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence --refresh-assets --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence --require-refreshed-assets --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE
```

When the default profile still points at local mock tables, pass one or more
real Oracle tables. The script creates/updates a `manual_integration` profile
for the smoke test:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --refresh-catalog --refresh-assets --allowed-table YOUR_TABLE
```

Full smoke including generated SQL execution:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --refresh-assets --execute
```

One-command production smoke. This runs asset refresh, post-refresh diagnostics,
preview, generated SQL execution, supporting feature checks, and engine
comparison in sequence:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-oracle-persistence --full-smoke --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE
```

Production release gate alias. This expands to full smoke, requires Oracle
runtime, requires Oracle-backed NL2SQL state persistence, and verifies refreshed
Select AI / Agent assets after refresh:

```bash
uv run python scripts/nl2sql_manual_integration.py --release-gate --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE
uv run python scripts/nl2sql_manual_integration.py --release-gate --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE --json-report reports/nl2sql-release-gate.json
```

`--json-report` writes a machine-readable artifact with `ok`, `exit_code`,
engine/profile/table inputs, summary counts, and every step result for CI or
operations dashboards. The report also includes `started_at`, `finished_at`,
and `elapsed_ms`; import it from the Engine Operations page to review and export
a Markdown handoff report.

Engine comparison only:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --compare --execute --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE
```

Real-schema smoke after catalog refresh. Replace the table/question with a known
table from the configured Oracle schema:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --refresh-catalog --refresh-assets --execute --debug-raw-preview --engines select_ai_agent,select_ai --allowed-table YOUR_TABLE --question "YOUR_TABLE から主要な列を一覧して"
```

Support checks for the absorbed No.1-SQL-Assist features (comment suggestions,
COMMENT ON SQL generation, synthetic cases, deterministic evaluation, persisted
evaluation sets/run history, feedback vector index plan). This does not run DDL
against business tables; it creates and archives one temporary NL2SQL evaluation
set and records one deterministic evaluation run in the NL2SQL state store:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --check-supporting-features --synthetic-limit 4
```

Live feedback vector index smoke. This seeds demo feedback items and rebuilds
the Oracle AI Database vector table/index using OCI GenAI embeddings:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-feedback-embedding --seed-demo-learning --execute-feedback-index --engines enterprise_ai_direct
```

Raw response troubleshooting for Select AI / Select AI Agent preview. This prints
truncated DBMS_CLOUD_AI / DBMS_CLOUD_AI_AGENT return values only; it does not
print environment variables or secrets:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --debug-raw-preview
```

Cleanup manual Oracle assets after explicit confirmation:

```bash
uv run python scripts/nl2sql_manual_integration.py --cleanup-assets --confirm-cleanup --profile-id manual_integration
```

Confirmed cleanup:

```bash
uv run python scripts/nl2sql_manual_integration.py --require-oracle --cleanup-assets --confirm-cleanup --profile-id manual_integration
```

`--refresh-assets` creates/replaces Oracle Select AI profile and Select AI Agent
profile/tool/agent/task/team assets. `--execute` runs the generated `SELECT`
SQL and writes a history item through the normal NL2SQL job path.
`--require-oracle` enforces Oracle runtime for Select AI / Select AI Agent;
`--require-enterprise-ai` enforces real OCI Enterprise AI Direct runtime for
`enterprise_ai_direct`; `--require-feedback-embedding` enforces OCI GenAI
feedback embedding readiness for live vector learning checks;
`--require-oracle-persistence` enforces Oracle-backed NL2SQL state persistence;
`--require-refreshed-assets` enforces ready Select AI / Agent assets for the
selected engines.
Each diagnostics / preview / job line prints non-secret readiness and engine
metadata, including runtime, Direct model, Select AI profile, Agent team, and
conversation id when available.
When `--refresh-assets` is used, the script prints `diagnostics_after_refresh`
so the initial pre-refresh readiness warning can be distinguished from the
post-refresh state.
`--check-supporting-features` does not run DDL against business tables; it
validates metadata suggestions, COMMENT ON SQL generation, evaluation helper
flows, persisted evaluation set create/update/archive, evaluation run history,
and the Oracle AI Database feedback vector index plan.
`--debug-raw-preview` is for troubleshooting live Oracle package responses when
the normalized preview path falls back because no SQL could be extracted.
`--cleanup-assets` only prints the target assets unless `--confirm-cleanup` is
also passed.
`--full-smoke` is a mutating smoke mode: it refreshes Oracle assets for selected
engines, writes job/history/compare records, and executes generated SELECT SQL
when the runtime is configured.
`--release-gate` is the production gate alias for `--full-smoke` plus Oracle
runtime and Oracle persistence requirements. It does not require live feedback
embedding; run the feedback vector smoke separately when OCI GenAI embedding is
part of the release gate.
