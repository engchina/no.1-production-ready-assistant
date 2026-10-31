import { toTabularData, type TabularData } from "@production-ready/ui";

import type { Artifact, RunState, RunStep } from "./api";
import { t } from "./i18n";
import { runToolTitles } from "./run-history";

/**
 * Run の中の表のデータ（#1158）。ツールの結果・成果物が表の形なら、共通の結果の表の部品で出す。
 * 表の形の判定は `@production-ready/ui` の `toTabularData`（製品に依存しない）。ここは Run の
 * どこから取り出すか（完了したツールの結果・成果物）だけを持つ。
 */

export interface ToolResultTable {
  stepId: string;
  toolName: string;
  data: TabularData;
  /** 業務データの照会（NL2SQL）の結果なら、生成した SQL と安全検査・実行の状態（#2346）。 */
  sql: ToolResultSql | null;
}

export interface ToolResultSql {
  text: string;
  safe: boolean;
  executed: boolean;
}

/**
 * NL2SQL の照会の結果（`nl2sql_query` / `nl2sql_get_job` の出力）の SQL。実行した SQL（`executable_sql`）を優先し、
 * 無ければ生成した SQL。SQL の無い結果は null。
 */
export function toolResultSql(output: Record<string, unknown> | null | undefined): ToolResultSql | null {
  if (!output) return null;
  const sql = [output.executable_sql, output.generated_sql].find(
    (value): value is string => typeof value === "string" && value.trim() !== ""
  );
  if (!sql) return null;
  return { text: sql, safe: output.is_safe !== false, executed: output.status === "done" };
}

/** ツールの結果の表（完了したツールだけ。表の形でない結果は含めない）。 */
export function stepResultTable(step: RunStep): TabularData | null {
  if (step.status !== "completed" || !step.tool_result?.success) return null;
  return toTabularData(step.tool_result.output ?? null);
}

/**
 * Run のツールの結果の表（呼んだ順）。同じジョブ（`job_id`）を何度も取り直した結果（NL2SQL の
 * `nl2sql_query` → `nl2sql_get_job`）は、最後の結果だけを残す。
 */
export function runToolResultTables(run: RunState): ToolResultTable[] {
  const tables: ToolResultTable[] = [];
  const titles = runToolTitles(run);
  const indexByJob = new Map<string, number>();
  for (const step of run.steps) {
    const data = stepResultTable(step);
    if (!data || !step.tool_call) continue;
    const table = {
      stepId: step.id,
      toolName: titles[step.tool_call.name] || step.tool_call.name,
      data,
      sql: toolResultSql(step.tool_result?.output ?? null),
    };
    const jobId = step.tool_result?.output?.job_id;
    if (typeof jobId === "string" && jobId) {
      const previous = indexByJob.get(jobId);
      if (previous !== undefined) {
        tables[previous] = table;
        continue;
      }
      indexByJob.set(jobId, tables.length);
    }
    tables.push(table);
  }
  return tables;
}

/** 成果物の内容が表の形なら表（回答・RAG の根拠は表にしない）。 */
export function artifactTable(artifact: Artifact): TabularData | null {
  if (artifact.kind === "answer" || artifact.kind === "rag_evidence") return null;
  return toTabularData(artifact.content);
}

/** 業務 Agent の出力の項目の成果物（#2549）。 */
export const OUTPUT_ARTIFACT_KIND = "structured_output";

/**
 * Run が記録した出力の項目の表（無ければ null）。真偽の列は「はい / いいえ」で出す（表の値は文字・数値として出す）。
 */
export function runOutputTable(run: Pick<RunState, "artifacts">): TabularData | null {
  const artifact = [...run.artifacts].reverse().find((item) => item.kind === OUTPUT_ARTIFACT_KIND);
  const content = artifact?.content as { columns?: { name: string; type?: string }[]; rows?: Record<string, unknown>[] } | undefined;
  if (!content || !Array.isArray(content.columns) || !Array.isArray(content.rows)) return null;
  const booleans = new Set(content.columns.filter((column) => column.type === "boolean").map((column) => column.name));
  const rows = content.rows.map((row) =>
    Object.fromEntries(
      Object.entries(row).map(([key, value]) => [
        key,
        booleans.has(key) && typeof value === "boolean" ? (value ? t("chat.output.yes") : t("chat.output.no")) : value,
      ])
    )
  );
  return toTabularData({ ...content, rows });
}

/**
 * 出力の項目を差し戻しても記録しなかった Run か（#2549。backend の `tool_grounding.output_retry`）。画面は記録の無いことを
 * 回答の下に示す。
 */
export function outputMissing(run: Pick<RunState, "artifacts" | "metadata" | "status">): boolean {
  if (run.status !== "completed") return false;
  const record = (run.metadata as Record<string, unknown> | undefined)?.tool_grounding;
  const retried = typeof record === "object" && record !== null && (record as Record<string, unknown>).output_retry === true;
  return retried && !run.artifacts.some((item) => item.kind === OUTPUT_ARTIFACT_KIND);
}
