/**
 * 実行履歴（#1890・#1955）の表示の対応表（backend の値 → 共通の部品の値と文言）。画面とテストが同じ規則を使う。
 *
 * - 状態は共通の 7 つ（`@production-ready/ui` の `RunStatus`）に写す（run-history.md「状態の写し方」）。
 *   RAG は `success` → 完了、`blocked`（安全チェック）→ ブロック（文言は「安全チェックで停止」）、`error` → 失敗。
 * - 処理の経過は共通の span の表の snapshot（実行の trace の API）を、チャット・RAG 検索と同じ hook で木にする
 *   （#3226）。span の無い古い形式の記録は行を作らず、古い形式の案内を出す。
 */
import {
  formatRunDuration,
  chatProgressTimelineStep,
  runListUsage as sharedRunListUsage,
  runUsageView as sharedRunUsageView,
  type RunUsageView,
  type ChatProgressStep,
  type RunExecutorFilter as SharedRunExecutorFilter,
  type RunStatus as SharedRunStatus,
  type RunStatusFilter as SharedRunStatusFilter,
  type RunStatusLabels,
  type RunTimelineStep,
  type StatusVariant,
} from "@production-ready/ui";

import { outcomeBadge, type AnswerOutcome } from "@/lib/answer-diagnostics";
import { validationExecutionBadge } from "@/lib/answer-validation";
import type {
  RunDetail,
  RunExecutorFilter,
  RunStatus,
  RunSummary,
  RunSurface,
  RunVerification,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { searchPrefillHref } from "@/components/search/search-prefill";

export interface BadgeView {
  variant: StatusVariant;
  label: string;
}

/** RAG の実行が取りうる共通の状態（一覧の「状態」の絞り込みの選択肢）。 */
export const RUN_STATUS_OPTIONS = ["succeeded", "blocked", "failed"] as const satisfies readonly SharedRunStatus[];

const TO_SHARED_STATUS: Record<RunStatus, SharedRunStatus> = {
  success: "succeeded",
  blocked: "blocked",
  error: "failed",
};

const FROM_SHARED_STATUS: Partial<Record<SharedRunStatus, RunStatus>> = {
  succeeded: "success",
  blocked: "blocked",
  failed: "error",
};

/** RAG の状態 → 共通の状態。 */
export function toRunStatus(status: RunStatus): SharedRunStatus {
  return TO_SHARED_STATUS[status] ?? "failed";
}

/** 「状態」の絞り込み → API の `status`（すべては付けない）。 */
export function runStatusParam(filter: SharedRunStatusFilter): RunStatus | undefined {
  return filter === "all" ? undefined : FROM_SHARED_STATUS[filter];
}

/** 「実行者」の絞り込み（共通の self / others）→ API の `executor`（RAG は mine / others。#1954）。 */
export function runExecutorParam(filter: SharedRunExecutorFilter): RunExecutorFilter {
  return filter === "self" ? "mine" : filter;
}

/** 状態の文言の RAG の上書き（ブロックは安全チェックで止めた実行）。 */
export function runStatusLabels(): Partial<RunStatusLabels> {
  return {
    succeeded: t("history.status.success"),
    blocked: t("history.status.blocked"),
    failed: t("history.status.error"),
  };
}

/** 種類の絞り込みの値（すべて / チャット / RAG 検索）。 */
export const RUN_SURFACE_FILTERS = ["all", "chat", "search"] as const;
export type RunSurfaceFilter = (typeof RUN_SURFACE_FILTERS)[number];

export function runSurfaceLabel(surface: RunSurface): string {
  return surface === "chat" ? t("history.surface.chat") : t("history.surface.search");
}

/**
 * 回答の対応のバッジ。回答を作らなかった RAG 検索は「回答なし（検索のみ）」、止めた・失敗した実行は
 * 「回答なし」。回答した実行は対応（answered は「回答」、ほかは回答の詳細と同じ文言）。
 */
export function runOutcomeView(run: Pick<RunSummary, "answer_generated" | "status" | "answer_outcome">): BadgeView {
  if (!run.answer_generated) {
    return { variant: "neutral", label: t("history.outcome.searchOnly") };
  }
  if (run.status !== "success" || !run.answer_outcome) {
    return { variant: "neutral", label: t("history.outcome.none") };
  }
  const badge = outcomeBadge(run.answer_outcome as AnswerOutcome);
  if (badge) return { variant: badge.variant as StatusVariant, label: t(badge.key) };
  return { variant: "success", label: t("history.outcome.answered") };
}

/** 資料との照合のバッジ。回答を作らなかった・止めた・失敗した実行は null（「—」）。 */
export function runVerificationView(verification: RunVerification | null, execution?: string | null): BadgeView | null {
  const explicit = validationExecutionBadge(execution);
  if (explicit) return explicit;
  switch (verification) {
    case "verified":
      return { variant: "success", label: t("history.verification.verified") };
    case "partial":
      return { variant: "warning", label: t("history.verification.partial") };
    case "unverified":
      return { variant: "neutral", label: t("history.verification.unverified") };
    default:
      return null;
  }
}

/** 検索・回答プロファイルの表示名（ID は出さない）。 */
export function runProfileLabel(run: Pick<RunSummary, "search_answer_profile_id" | "search_answer_profile_name">): string {
  if (!run.search_answer_profile_id) return t("history.profile.none");
  return run.search_answer_profile_name || t("history.profile.missing");
}

/** 実行した利用者（実行者）の表示名（引けない利用者は「削除済みのユーザー」。UUID は出さない）。 */
export function runExecutorLabel(run: Pick<RunSummary, "executor_display_name">): string {
  return run.executor_display_name?.trim() || t("history.user.missing");
}

/** 会話の持ち主の表示名。会話の無い実行は null（詳細に出さない）。 */
export function runConversationOwnerLabel(run: { conversation_owner_display_name: string | null }): string | null {
  if (run.conversation_owner_display_name == null) return null;
  return run.conversation_owner_display_name.trim() || t("history.user.missing");
}

/**
 * 詳細の「会話の持ち主」（実行者と違うときだけ。#1954）。会話の無い実行と、自分が実行した自分の会話は null。
 * 見ている利用者から見て、実行者と持ち主のどちらか一方だけが自分なら別の人。どちらも自分でなければ名前で比べる。
 */
export function runConversationOwner(
  run: Pick<RunDetail, "conversation_owner_display_name" | "owned_by_me" | "executed_by_me" | "executor_display_name">
): { name: string; isSelf: boolean } | null {
  const name = runConversationOwnerLabel(run);
  if (name == null) return null;
  const ownedByMe = run.owned_by_me === true;
  if (ownedByMe && run.executed_by_me) return null;
  if (!ownedByMe && !run.executed_by_me && name === runExecutorLabel(run)) return null;
  return { name, isSelf: ownedByMe };
}

/**
 * 開始日時（ISO 8601）。記録の時刻（`created_at`）は実行が終わったときなので、所要時間を引いて開始にする。
 * 所要時間の記録が無ければ記録の時刻のまま。
 */
/**
 * 実行の詳細の「RAG 検索で開く」の URL（#3309。run-history.md「詳細」）。RAG 検索の実行を、質問・検索・回答
 * プロファイル・回答の生成の有無・記録の絞り込み条件を入れて RAG 検索の画面に開く（検索はしない）。
 * 出すのは実行した本人で、RAG 検索の画面の権限があるときだけ（system_admin でも他の人の実行には出さない。#3244）。
 * 質問を保存していない実行（安全チェックで止めた実行）は入れる質問が無いので null。
 */
export function runSearchHref(
  run: Pick<
    RunDetail,
    "surface" | "executed_by_me" | "question" | "search_answer_profile_id" | "answer_generated" | "search_conditions"
  >,
  { canOpenSearch }: { canOpenSearch: boolean }
): string | null {
  if (!canOpenSearch || run.surface !== "search" || run.executed_by_me !== true || !run.question) return null;
  return searchPrefillHref({
    question: run.question,
    searchAnswerProfileId: run.search_answer_profile_id,
    generateAnswer: run.answer_generated,
    filters: run.search_conditions?.filters ?? {},
  });
}

export function runStartedAt(run: Pick<RunSummary, "created_at" | "elapsed_ms">): string {
  const finished = Date.parse(run.created_at);
  if (run.elapsed_ms == null || !Number.isFinite(run.elapsed_ms) || Number.isNaN(finished)) return run.created_at;
  return new Date(finished - run.elapsed_ms).toISOString();
}

/** 処理の経過（実行の trace の span の木。#3226）。行と trace id と、span の無い古い形式の記録か。 */
export interface RunTimelineView {
  steps: RunTimelineStep[];
  traceId?: string;
  /** span の無い古い形式の記録（段だけのイベント・記録なし）。処理の経過の位置に案内を出す。 */
  legacy: boolean;
}

/**
 * 処理の経過の行（実行の trace の API の snapshot。チャット・RAG 検索の処理の経過と同じ hook で作った段。#3226）。
 * 古い形式の記録（span の無い記録）は行にせず、古い形式として返す（段の再構成はしない）。
 */
export function runTimeline(snapshot: {
  steps: readonly ChatProgressStep[];
  traceId?: string;
  legacy: boolean;
}): RunTimelineView {
  if (snapshot.legacy) return { steps: [], legacy: true };
  return { steps: snapshot.steps.map(chatProgressTimelineStep), traceId: snapshot.traceId, legacy: false };
}

/** 所要時間（ミリ秒）の表記（共通の書式。「1.2 秒」「2 分 5 秒」、記録なしは「—」）。 */
export function formatRunElapsed(elapsedMs: number | null | undefined): string {
  return formatRunDuration(elapsedMs, t("history.notRecorded"));
}

/** 利用量の文言（RAG の i18n。表示の規則は 3 製品で共通の `runUsageView` / `runListUsage`。#3419・#3543）。 */
function usageLabels() {
  return {
    unmeasured: t("history.detail.usageUnmeasured"),
    partial: t("history.detail.usagePartial"),
    modelCalls: (values: { requests: string; input: string; output: string }) =>
      t("history.detail.modelCallsValue", values),
    modelCallsPartial: (values: { requests: string; measured: string; input: string; output: string }) =>
      t("history.detail.modelCallsPartial", values),
  };
}

/**
 * 概要の「モデル」「token」「モデルの呼び出し」（Agent の実行の詳細と同じ形。#3419）。モデルは実行のときに記録した具体的な
 * ID だけを出し、今の設定から推測しない。規則（未計測・一部計測・実測の 0）は共通の `runUsageView`。
 */
export function runUsageView(run: Pick<RunDetail, "model_id" | "usage">): RunUsageView {
  return sharedRunUsageView(run.usage, { fallbackModel: run.model_id, labels: usageLabels() });
}

/**
 * 一覧の「token」の列（#3466）。一覧の API は利用量の全体ではなく合計と計測の状態だけを返す。規則は詳細の概要と同じ
 * （共通の `runListUsage`）。
 */
export function runListUsage(
  run: Pick<RunSummary, "total_tokens" | "usage_measurement">
): { tokens: number | null; note: string | null } {
  return sharedRunListUsage(run.total_tokens, run.usage_measurement, usageLabels());
}
