// 実行履歴（#1955）の一覧の取得条件と、行 → 共通の実行履歴の形への写し。画面の部品（`RunHistoryTable` など）は
// `@production-ready/ui` が持ち、ここは NL2SQL の行をどう写すかだけを書く（UX 契約 run-history.md「状態の写し方」）。
//
// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import {
  DEFAULT_RUN_PERIOD,
  runPeriodSince,
  RUN_EXECUTOR_FILTERS,
  RUN_PERIODS,
  runListUsage,
  runUsageView,
  type RunCancellation,
  type RunExecutorFilter,
  type RunPeriod,
  type RunStatus,
  type RunStatusFilter,
  type RunUsageLabels,
  type RunUsageView,
} from "@production-ready/ui";

import { t } from "../../lib/i18n";
import { APP_ROUTES } from "../../lib/routes";
import { querySqlGenerationUrl } from "./queryPrefillState";
import type { HistoryItem } from "./types";

/**
 * NL2SQL が写せる共通の状態（完了 = 安全性の判定を通過、ブロック = 実行を止めた、停止 = 利用者が止めた。#3348）。
 * 失敗は記録しない。
 */
export const HISTORY_RUN_STATUSES: readonly RunStatus[] = ["succeeded", "blocked", "cancelled"] as const;

/** 行の種類（チャットの往復 / SQL 生成 / MCP / 品質評価。backend の `HistoryItem.kind`。#1986）。 */
export type HistoryKind = "chat" | "query" | "mcp" | "evaluation";
/** 一つずつ選ぶ種類の並び（Agent の「画面 → … → MCP → 品質評価」と同じく品質評価を最後に）。 */
export const HISTORY_KINDS: readonly HistoryKind[] = ["chat", "query", "mcp", "evaluation"] as const;

/**
 * 種類の絞り込みの既定。Agent の品質評価の Run が MCP で呼んだ実行（評価のケースごとの行）は利用者の実行を
 * 埋もれさせるので既定では隠し、「すべて」か「品質評価」を選んだときに出す（Agent の実行履歴と同じ。#1986）。
 */
export const DEFAULT_HISTORY_KIND = "not_evaluation";

/** 種類の絞り込み。`all` は「すべて」、既定は `not_evaluation`（品質評価以外）。 */
export type HistoryKindFilter = "all" | typeof DEFAULT_HISTORY_KIND | HistoryKind;
export const HISTORY_KIND_FILTERS: readonly HistoryKindFilter[] = [DEFAULT_HISTORY_KIND, "all", ...HISTORY_KINDS] as const;

/** 一覧の絞り込み（作業状態に残す）。値の読み直しは `normalizeHistoryFilters` を通す。 */
export interface HistoryFilters {
  search: string;
  status: RunStatusFilter;
  kind: HistoryKindFilter;
  period: RunPeriod;
  executor: RunExecutorFilter;
}

export const DEFAULT_HISTORY_FILTERS: HistoryFilters = {
  search: "",
  status: "all",
  kind: DEFAULT_HISTORY_KIND,
  // 期間の既定は 3 製品で共通の過去 7 日（#3386）。明示に選んだ「すべて」は作業状態に残す。
  period: DEFAULT_RUN_PERIOD,
  executor: "all",
};

/**
 * 保存していた値が壊れていても（古い版の `mine` など）、既定（種類は「品質評価以外」、期間は過去 7 日、ほかは
 * 「すべて」）として読む。
 */
export function normalizeHistoryFilters(value: unknown): HistoryFilters {
  const record = value && typeof value === "object" ? (value as Partial<Record<keyof HistoryFilters, unknown>>) : {};
  const pick = <T extends string>(raw: unknown, allowed: readonly T[], fallback: T): T =>
    typeof raw === "string" && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
  return {
    search: typeof record.search === "string" ? record.search : "",
    status: pick<RunStatusFilter>(record.status, ["all", ...HISTORY_RUN_STATUSES], "all"),
    kind: pick(record.kind, HISTORY_KIND_FILTERS, DEFAULT_HISTORY_KIND),
    period: pick(record.period, RUN_PERIODS, DEFAULT_RUN_PERIOD),
    executor: pick(record.executor, RUN_EXECUTOR_FILTERS, "all"),
  };
}

/** 期間のほかの検索語・絞り込みのどれかが既定と違う。 */
export function hasActiveNonPeriodHistoryFilters(filters: HistoryFilters) {
  return (
    Boolean(filters.search.trim()) ||
    filters.status !== "all" ||
    filters.kind !== DEFAULT_HISTORY_KIND ||
    filters.executor !== "all"
  );
}

/** 検索語か絞り込みのどれかが既定（期間は過去 7 日）と違う（「条件をクリア」）。 */
export function hasActiveHistoryFilters(filters: HistoryFilters) {
  return hasActiveNonPeriodHistoryFilters(filters) || filters.period !== DEFAULT_RUN_PERIOD;
}

/** 一覧が全件より狭いか（件数の「N 件（全 M 件）」）。既定の過去 7 日も期間で絞っているので数える（#3386）。 */
export function isHistoryListNarrowed(filters: HistoryFilters) {
  return hasActiveNonPeriodHistoryFilters(filters) || filters.period !== "all";
}

/** backend の「実行者」の値（`mine` / `others`。#1954）。 */
const EXECUTOR_PARAM: Record<RunExecutorFilter, string | null> = { all: null, self: "mine", others: "others" };

/**
 * `GET /api/nl2sql/history` の URL。1 ページは表示件数（既定 25 件）、新しい順（サーバーが全件で並べる）。
 * 期間は今の時刻から `since`（ISO 8601）に直して渡す（`runPeriodSince`）。
 */
export function historyRequestUrl(
  { offset, limit, filters }: { offset: number; limit: number; filters: HistoryFilters },
  now: Date = new Date()
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const search = filters.search.trim();
  if (search) params.set("q", search);
  if (filters.status !== "all") params.set("status", filters.status);
  if (filters.kind !== "all") params.set("kind", filters.kind);
  const since = runPeriodSince(filters.period, now);
  if (since) params.set("since", since);
  const executor = EXECUTOR_PARAM[filters.executor];
  if (executor) params.set("executor", executor);
  return `/api/nl2sql/history?${params.toString()}`;
}

/** 行の状態（共通の 7 つのうち NL2SQL が写せる 2 つ）。 */
export function historyRunStatus(item: HistoryItem): RunStatus {
  if (isCancelledHistory(item)) return "cancelled";
  return item.safety_is_safe ? "succeeded" : "blocked";
}

/** 停止で終わった実行の行か（SQL・実行結果・評価を持たない。#3348）。 */
export function isCancelledHistory(item: Pick<HistoryItem, "run_status">): boolean {
  return item.run_status === "cancelled";
}

/**
 * 停止の行の「取消」（共通の `RunSummary` の `cancellation`。#3348）。表示名を引けない利用者は「削除済みのユーザー」、
 * 取り消した人の記録が無い取消（認証無効の期間）は人を出さずに入口だけを出す。
 */
export function historyCancellation(item: HistoryItem): RunCancellation | null {
  const value = isCancelledHistory(item) ? item.cancellation : null;
  if (!value) return null;
  return {
    via: value.via === "agent_run" ? "agentRun" : value.via,
    actor: value.actor_recorded
      ? { name: value.actor_display_name.trim() || t("history.actor.missing"), isSelf: value.by_me }
      : null,
    apiKeyName: value.api_key_name || null,
  };
}

/** 行の種類。古い応答（`kind` の無い行）はきっかけと会話の持ち主の有無で決める（backend と同じ）。 */
export function historyKind(item: HistoryItem): HistoryKind {
  if (item.kind && (HISTORY_KINDS as readonly string[]).includes(item.kind)) return item.kind;
  if (item.run_source === "mcp" || item.run_source === "evaluation") return item.run_source;
  return item.conversation_owner_display_name != null ? "chat" : "query";
}

/** 種類の名前（絞り込みの選択肢）。 */
export function historyKindLabel(kind: HistoryKind) {
  return t(`history.kind.${kind}`);
}

/**
 * 行の種類の表示。MCP は呼び出し元を添える（外部の MCP クライアントは API キーの名前、Agent の Run は「Agent」。
 * Agent の実行履歴の「MCP（〈キーの名前〉）」と同じ形。#1986）。
 */
export function historyKindDisplay(item: HistoryItem) {
  const kind = historyKind(item);
  if (kind !== "mcp") return historyKindLabel(kind);
  const keyName = item.api_key_name?.trim();
  if (keyName) return t("history.kind.mcpCaller", { name: keyName });
  if (item.agent_run_id?.trim()) return t("history.kind.mcpCaller", { name: t("history.kind.agent") });
  return historyKindLabel(kind);
}

/** 実行した人の表示名（空ならログイン ID）。UUID は出さず、引けない利用者は削除済みの文言（#1696・#1954）。 */
export function historyExecutorName(item: HistoryItem) {
  if (!item.actor_user_uuid?.trim()) return t("history.actor.unrecorded");
  return (
    item.executor_display_name?.trim() ||
    item.actor_display_name?.trim() ||
    item.actor_login_user_id?.trim() ||
    t("history.actor.missing")
  );
}

/** 実行者（`executed_by_me` の無い古い応答は自分の行とみなす）。 */
export function historyExecutor(item: HistoryItem) {
  return { name: historyExecutorName(item), isSelf: item.executed_by_me !== false };
}

/**
 * 会話の持ち主（実行者と違うときだけ。#1954）。会話の無い実行（SQL 生成）は null。
 * 自分が実行して自分が持ち主の行と、表示名が実行者と同じ行は出さない。
 */
export function historyOwner(item: HistoryItem) {
  if (item.conversation_owner_display_name == null) return null;
  const executor = historyExecutor(item);
  const ownerIsSelf = item.owned_by_me === true;
  if (executor.isSelf && ownerIsSelf) return null;
  const name = item.conversation_owner_display_name || t("history.actor.missing");
  if (!executor.isSelf && !ownerIsSelf && name === executor.name) return null;
  return { name, isSelf: ownerIsSelf };
}

/** 詳細の URL（`?id=<run_id>`。新しいタブでも開ける）。 */
export function historyDetailPath(basePath: string, id: string) {
  return `${basePath}?${new URLSearchParams({ id }).toString()}`;
}

/**
 * 実行の詳細の「会話を開く」の URL（3 製品で同じ規則。#2343）。チャットの往復（`kind` が chat）で会話の ID があり、
 * 会話を見られる人（system_admin か会話の持ち主）がチャットを開けるときだけ。SQL 生成・MCP・品質評価の実行は
 * 開く元の会話が無いので null（操作を出さない）。開いた会話は backend が見え方を確かめる（見えなければ 404）。
 */
export function historyConversationHref(
  item: HistoryItem,
  { seesAllUsers, canOpenChat }: { seesAllUsers: boolean; canOpenChat: boolean }
): string | null {
  if (!canOpenChat || historyKind(item) !== "chat" || !item.session_id) return null;
  if (!seesAllUsers && item.owned_by_me !== true) return null;
  return `${APP_ROUTES.chat}?${new URLSearchParams({ profile_id: item.profile_id, conversation_id: item.session_id }).toString()}`;
}

/**
 * 実行の詳細の「SQL 生成で開く」の URL（#3309。run-history.md「詳細」）。SQL 生成の画面の実行（`kind` が query）を、
 * 質問・業務プロファイル・生成の方式を入れて SQL 生成の画面に開く（SQL の生成と実行はしない）。出すのは実行した本人で、
 * SQL 生成の画面の権限があるときだけ（system_admin でも他の人の実行には出さない。#3244）。チャット・MCP・品質評価の
 * 実行は null（チャットは「会話を開く」）。
 */
export function historySqlGenerationHref(
  item: HistoryItem,
  { canOpenQuery }: { canOpenQuery: boolean }
): string | null {
  if (!canOpenQuery || historyKind(item) !== "query" || item.executed_by_me !== true) return null;
  if (!item.question.trim()) return null;
  return querySqlGenerationUrl(item, APP_ROUTES.query);
}

/** 利用量の文言（NL2SQL の i18n。RAG と同じ文言。表示の規則は共通の `runUsageView` / `runListUsage`。#3543）。 */
function historyUsageLabels(): RunUsageLabels {
  return {
    unmeasured: t("history.detail.usageUnmeasured"),
    partial: t("history.detail.usagePartial"),
    modelCalls: (values) => t("history.detail.modelCallsValue", values),
    modelCallsPartial: (values) => t("history.detail.modelCallsPartial", values),
  };
}

/** 利用量を計測できない呼び出しの出どころの表示名（知らない値は ID のまま）。 */
function historyUsageSourceLabel(source: string): string {
  return source;
}

/**
 * 詳細の概要の「モデル」「token」「モデルの呼び出し」と「未計測の呼び出し」（RAG と同じ形。#3543）。記録の無い行は
 * 「未計測」（0 にしない）、計測できない呼び出しと混ざれば「一部計測」。
 */
export function historyUsageView(item: Pick<HistoryItem, "usage">): RunUsageView {
  return runUsageView(item.usage, { labels: historyUsageLabels(), sourceLabel: historyUsageSourceLabel });
}

/** 一覧の「token」の列（合計と計測の状態。規則は詳細と同じ。#3543）。 */
export function historyListUsage(
  item: Pick<HistoryItem, "total_tokens" | "usage_measurement">
): { tokens: number | null; note: string | null } {
  return runListUsage(item.total_tokens, item.usage_measurement, historyUsageLabels());
}
