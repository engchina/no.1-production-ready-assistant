/**
 * Agent の Run の失敗を、3 製品共通の失敗のカード（`ProcessFailureNotice`）の形に写す（#2342）。
 *
 * 1 文目は止まった段（処理の経過の最後の失敗した段）、本文は Run の失敗のイベントの文（backend の利用者向けの文）と
 * 次の操作、「詳細」は止まった段・エラーコード（イベントの `error_code`）・実行 ID・時刻。チャットと実行の詳細で
 * 同じ形にし、RAG・NL2SQL とも同じ並びと文にする。
 */
import { failedStepName, type ProcessFailure, type ProcessFailureLabels } from "@production-ready/ui";

import type { RunState } from "./api";
import { t } from "./i18n";

/** 共通の失敗の表示の文言（Agent は実行を追う鍵が実行 ID）。 */
export function processFailureLabels(): Partial<ProcessFailureLabels> {
  return {
    details: t("failure.details"),
    failedStep: t("failure.failedStep"),
    errorType: t("failure.errorType"),
    errorCode: t("failure.errorCode"),
    traceId: t("failure.runId"),
    occurredAt: t("failure.occurredAt"),
  };
}

interface StepLike {
  label: string;
  status: string;
  /** 状態で変わらない名詞の名前（チャットの段階。#2342）。 */
  name?: string;
}

/**
 * 失敗した Run の失敗のカードの値（失敗していない Run は null）。`where` はチャット（もう一度送信）か実行の詳細
 * （チャットでもう一度質問）か。`steps` は処理の経過の段（チャットの段階・実行の詳細の段）。
 */
export function runFailure(
  run: Pick<RunState, "id" | "status" | "events">,
  steps: readonly StepLike[],
  where: "chat" | "detail"
): ProcessFailure | null {
  if (run.status !== "failed") return null;
  const event = [...run.events].reverse().find((item) => item.type.endsWith("failed"));
  const code = event?.payload?.error_code;
  const step = failedStepName(steps);
  return {
    summary: step ? t("failure.summaryAt", { step }) : t("failure.summary"),
    cause: event?.message || t("chat.failedUnknown"),
    nextAction: where === "chat" ? t("failure.next.chat") : t("failure.next.detail"),
    failedStep: step,
    errorCode: typeof code === "string" ? code : null,
    // 実行の詳細は概要に実行 ID があるので重ねない。
    traceId: where === "chat" ? run.id : null,
    occurredAt: event?.created_at ?? null,
  };
}
