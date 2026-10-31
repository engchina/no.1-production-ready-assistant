import { t, type I18nKey } from "@/lib/i18n";

/** Run のきっかけ（backend の `run_facts.run_source` と同じ判定。#1874）。 */
export type RunSource = "user" | "automation" | "evaluation" | "mcp" | "replay";

const RUN_SOURCES: readonly RunSource[] = ["user", "automation", "evaluation", "mcp", "replay"];

/** Run の metadata からきっかけを決める（品質評価 → 自動実行 → MCP → 再実行 → 画面の順）。 */
export function runSourceOf(metadata: Record<string, unknown> | undefined): RunSource {
  const value = metadata ?? {};
  if (value.evaluation_job_id) return "evaluation";
  if (value.automation_id) return "automation";
  if (value.source === "mcp") return "mcp";
  if (value.replayed_from_run_id) return "replay";
  return "user";
}

/** 表示の名前。MCP の Run は API キーの名前を添える（例: 「MCP（会社の PC の Claude Code）」）。 */
export function runSourceLabel(source: string | undefined, apiKeyName?: string | null): string {
  const known = RUN_SOURCES.includes(source as RunSource) ? (source as RunSource) : "user";
  if (known === "mcp" && apiKeyName) return t("run.source.mcpKey", { name: apiKeyName });
  return t(`run.source.${known}` as I18nKey);
}

/** Run（metadata）のきっかけの表示の名前。 */
export function runSourceLabelOf(metadata: Record<string, unknown> | undefined): string {
  const name = metadata?.api_key_name;
  return runSourceLabel(runSourceOf(metadata), typeof name === "string" ? name : null);
}
