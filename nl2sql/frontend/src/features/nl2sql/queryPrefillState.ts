import type { HistoryItem, Nl2SqlEngine } from "./types";

// URL prefill で受け付けるのは画面(EngineSelector)で選べる明示エンジンだけ。
const ENGINES: readonly Nl2SqlEngine[] = ["select_ai", "select_ai_agent", "enterprise_ai_direct"];

export interface QueryPrefill {
  question: string;
  engine: Nl2SqlEngine | null;
  profileId: string;
}

export function parseNl2SqlEngine(value: string | null): Nl2SqlEngine | null {
  return ENGINES.includes(value as Nl2SqlEngine) ? (value as Nl2SqlEngine) : null;
}

export function prefillFromSearchParams(params: URLSearchParams): QueryPrefill {
  return {
    question: params.get("question") ?? "",
    engine: parseNl2SqlEngine(params.get("engine")),
    profileId: params.get("profile_id") ?? "",
  };
}

/**
 * SQL 生成の画面を、実行の質問・業務プロファイル・生成の方式を入れて開く URL（実行の詳細の「SQL 生成で開く」。
 * #3309）。画面は値を一度だけ入れて URL から外し、SQL の生成と実行は利用者が押す。質問は書き換えない。
 * 画面で選べない方式は入れない（画面の業務プロファイルの既定に任せる）。
 */
export function querySqlGenerationUrl(
  item: Pick<HistoryItem, "question" | "engine" | "profile_id">,
  basePath = "/query"
): string {
  const params = new URLSearchParams();
  params.set("question", item.question);
  const engine = parseNl2SqlEngine(item.engine);
  if (engine) params.set("engine", engine);
  if (item.profile_id) params.set("profile_id", item.profile_id);
  return `${basePath}?${params.toString()}`;
}
