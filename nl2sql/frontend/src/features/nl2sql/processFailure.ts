/**
 * NL2SQL のチャットのジョブの失敗を、3 製品共通の失敗の表示（`ProcessFailureNotice`）に写す（#2342）。
 *
 * 1 文目は止まった段（処理の経過の失敗した段の名前）、本文は backend の利用者向けの文（`error_message`）と次の操作、
 * 「詳細」は止まった段・エラーコード（`error_code`）・ジョブ ID・時刻・元のメッセージ（`error_detail`）。
 * RAG・Agent と同じ並びと文。並べ方は共通の部品が持ち、ここは値と文言だけを渡す。
 */
import type { ChatProgressStep, ProcessFailure, ProcessFailureLabels } from "@production-ready/ui";
import { DEFAULT_API_ERROR_DETAIL_LABELS, failedStepName } from "@production-ready/ui";

import { t } from "../../lib/i18n";

import type { JobData, SqlChatExecuteData } from "./types";

/** 共通の失敗の表示の文言（NL2SQL は実行を追う鍵がジョブ ID）。 */
export function processFailureLabels(): Partial<ProcessFailureLabels> {
  return {
    details: t("failure.details"),
    failedStep: t("failure.failedStep"),
    errorType: t("failure.errorType"),
    errorCode: t("failure.errorCode"),
    traceId: t("failure.jobId"),
    occurredAt: t("failure.occurredAt"),
  };
}

/** チャットのジョブの失敗（`error_message` のある失敗だけ。停止は失敗にしない）。 */
export function chatJobFailure(
  turn: Pick<JobData, "job_id" | "error_message" | "error_code" | "error_detail" | "finished_at">,
  steps: readonly ChatProgressStep[]
): ProcessFailure {
  // 処理の経過は span の木（#3226）。段の中の span（モデルの呼び出しなど）が失敗しても、止まった段は工程の段の名前
  // （最上位の行）で示す。最上位に失敗が無ければ、失敗した span の名前。
  const step = failedStepName(steps.filter((item) => !item.level)) ?? failedStepName(steps);
  return {
    summary: step ? t("failure.summaryAt", { step }) : t("failure.summary"),
    cause: turn.error_message,
    // 再試行の要否は失敗で違う（業務定義の再検証が要る失敗は再試行しても直らない）ので、backend の文
    // （`error_message`。1 文目と次の操作）に任せ、ここは管理者への連絡だけを添える。
    nextAction: t("failure.next"),
    failedStep: step,
    errorCode: turn.error_code,
    traceId: turn.job_id,
    occurredAt: turn.finished_at,
    extraDetails: turn.error_detail
      ? [{ label: DEFAULT_API_ERROR_DETAIL_LABELS.rawMessage, value: turn.error_detail }]
      : [],
  };
}

/**
 * 実行した後の失敗（「実行」「もう一度実行」で DB がエラーを返した）。結果の位置に結果の代わりに出す（#2342）。
 * 1 文目は「SQL を実行できませんでした。」、本文は backend の利用者向けの文、「詳細」はコード・時刻・元のメッセージ。
 */
export function executionFailure(
  data: Pick<SqlChatExecuteData, "error_message" | "error_code" | "error_detail" | "executed_at">
): ProcessFailure {
  return {
    summary: t("chat.execute.failed"),
    cause: data.error_message,
    // 次の操作は backend の文（`error_message`）が持つ（言い換えて実行し直す等）。重ねない。
    errorCode: data.error_code,
    occurredAt: data.executed_at,
    extraDetails: data.error_detail
      ? [{ label: DEFAULT_API_ERROR_DETAIL_LABELS.rawMessage, value: data.error_detail }]
      : [],
  };
}
