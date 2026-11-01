import type { HistoryItem } from "./types";

export interface QueryPrefill {
  question: string;
  profileId: string;
}

export function prefillFromSearchParams(params: URLSearchParams): QueryPrefill {
  return {
    question: params.get("question") ?? "",
    profileId: params.get("profile_id") ?? "",
  };
}

/**
 * SQL 生成の画面を、実行の質問・業務プロファイルを入れて開く URL（実行の詳細の「SQL 生成で開く」。
 * #3309）。画面は値を一度だけ入れて URL から外し、SQL の生成と実行は利用者が押す。質問は書き換えない。
 */
export function querySqlGenerationUrl(
  item: Pick<HistoryItem, "question" | "profile_id">,
  basePath = "/query"
): string {
  const params = new URLSearchParams();
  params.set("question", item.question);
  if (item.profile_id) params.set("profile_id", item.profile_id);
  return `${basePath}?${params.toString()}`;
}
