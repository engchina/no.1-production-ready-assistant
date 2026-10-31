import type { AllowedObjects, JobData, Nl2SqlResult, PreviewData, SafetyReport } from "./types";

function previewFallbackSafety(preview: PreviewData): SafetyReport {
  return {
    is_safe: preview.is_safe,
    is_select_only: preview.is_safe,
    row_limit_applied: preview.row_limit,
    blocked_reason: preview.is_safe ? "" : preview.note,
    warnings: [],
    referenced_tables: [],
    referenced_columns: [],
  };
}

/**
 * プレビュー同期応答(`/api/nl2sql/preview`)を「SQL 生成の処理状況」タイムラインへ流し込むための
 * 擬似 JobData。実行はしないため execute/format は skipped、生成〜安全確認までを done とする。
 * `useNl2SqlJobPolling` は経由せず、表示専用として Workbench のローカル state に保持する。
 */
export function previewToJob(preview: PreviewData, question: string): JobData {
  const result: Nl2SqlResult = {
    engine: preview.engine,
    engine_meta: preview.engine_meta,
    fallback_reason: preview.fallback_reason,
    original_question: question,
    rewritten_question: preview.rewritten_question,
    generated_sql: preview.sql,
    executable_sql: preview.executable_sql,
    explanation: preview.note,
    safety: preview.safety ?? previewFallbackSafety(preview),
    recommendations: preview.recommendations,
    repaired_sql: preview.repaired_sql,
    optimization_hints: preview.optimization_hints,
    results: { columns: [], rows: [], total: 0 },
    timing: preview.timing ?? { created_at: "", elapsed_ms: 0, stage_timings: [] },
  };
  return {
    job_id: "preview",
    status: "done",
    created_at: "",
    elapsed_ms: preview.timing?.elapsed_ms ?? null,
    result,
    error_message: null,
    timing: preview.timing ?? null,
    steps: [
      { stage: "prepare_context", status: "done" },
      { stage: "generate_sql", status: "done" },
      // Select AI / Select AI Agent の SQL は安全性の確認を省略する（#1523）。
      { stage: "safety_check", status: result.safety.safety_checked === false ? "skipped" : "done" },
      { stage: "execute_sql", status: "skipped" },
      { stage: "format_results", status: "skipped" },
    ],
  };
}

export function previewExecutePayload(
  sql: string,
  allowedObjects: AllowedObjects
) {
  return sqlExecutePayload(sql, allowedObjects);
}

export function sqlExecutePayload(
  sql: string,
  allowedObjects: AllowedObjects,
  rowLimit?: number | null
) {
  const payload: {
    sql: string;
    allowed_objects: AllowedObjects;
    row_limit?: number;
  } = {
    sql: sql.trim(),
    allowed_objects: allowedObjects,
  };
  if (typeof rowLimit === "number") {
    payload.row_limit = rowLimit;
  }
  return payload;
}
