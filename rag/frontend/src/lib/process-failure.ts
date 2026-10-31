/**
 * RAG の失敗を 3 製品共通の失敗の表示（`ProcessFailureNotice` の `ProcessFailure`）に写す（#2342）。
 *
 * - チャットの回答の失敗（SSE の `error`・保存済みの ERROR の回答）: 何が起きたか（止まった工程）→ 原因（backend の
 *   利用者向けの文）→ 次の操作（もう一度送信・トレース ID を添えて管理者へ）→「詳細」（止まった段・エラーの種類・
 *   エラーコード・トレース ID・時刻）。
 * - 作った回答を保存できなかった（`storage_failures`）: 回答は残したまま、warning で欠けたものと理由を出す。
 * - 実行の詳細の失敗: チャットと同じ 1 文目・次の操作、技術的な詳細は同じ項目。
 * 並べ方・「詳細」の項目は共通の部品が持ち、ここは値と文言（i18n）を渡すだけ。
 */
import type { ProcessFailure, ProcessFailureLabels } from "@production-ready/ui";

import { ANSWER_STAGE_LABEL, ANSWER_STEP_STAGE_PREFIX, answerStageLabel } from "./answer-progress";
import { answerStepDisplayName } from "./answer-process";
import type { RunDetail, StorageFailure } from "./api";
import { t } from "./i18n";

/** 共通の失敗の表示の文言（「詳細」の見出しと項目の名前）。 */
export function processFailureLabels(): Partial<ProcessFailureLabels> {
  return {
    details: t("failure.details"),
    failedStep: t("failure.failedStep"),
    errorType: t("failure.errorType"),
    errorCode: t("failure.errorCode"),
    traceId: t("failure.traceId"),
    occurredAt: t("failure.occurredAt"),
  };
}

/**
 * 止まった段の名前。パイプラインの工程（`answer_record_save` など）と `answer_step:` の工程は工程の名前、例外で
 * 終わった回答フローの工程は接頭辞の無い工程名で届くので、回答フローの工程の名前で引く。
 */
export function failedStageLabel(stage: string): string {
  if (stage.startsWith(ANSWER_STEP_STAGE_PREFIX) || Object.prototype.hasOwnProperty.call(ANSWER_STAGE_LABEL, stage)) {
    return answerStageLabel(stage);
  }
  return answerStepDisplayName(stage);
}

/** 失敗の 1 文目（止まった工程が分かれば工程の名前を入れる。チャットと実行の詳細で同じ文）。 */
function failureSummary(stage: string | null | undefined): string {
  return stage ? t("failure.summaryAt", { step: failedStageLabel(stage) }) : t("failure.summary");
}

/** チャットの回答の失敗の情報（SSE の `error` の項目。保存済みの ERROR の回答は本文と trace_id だけ）。 */
export interface ChatAnswerFailureInfo {
  message: string;
  traceId?: string | null;
  errorType?: string | null;
  errorCode?: string | null;
  stage?: string | null;
  occurredAt?: string | null;
}

export function chatAnswerFailure(info: ChatAnswerFailureInfo): ProcessFailure {
  return {
    summary: failureSummary(info.stage),
    cause: info.message,
    nextAction: t("failure.answer.next"),
    failedStep: info.stage ? failedStageLabel(info.stage) : null,
    errorType: info.errorType,
    errorCode: info.errorCode,
    traceId: info.traceId,
    occurredAt: info.occurredAt,
  };
}

/** 保存できなかったもの（回答の記録・会話の回答）ごとの、利用者への影響の文。 */
function storageImpact(step: StorageFailure["step"]): string {
  return step === "chat_message_save"
    ? t("failure.storage.chatMessageSave")
    : t("failure.storage.answerRecordSave");
}

/** 作った回答を保存できなかったときの warning（保存の失敗が無ければ null）。 */
export function storageFailureNotice(
  failures: readonly StorageFailure[] | undefined,
  traceId: string | null | undefined
): ProcessFailure | null {
  if (!failures?.length) return null;
  const [first] = failures;
  return {
    summary: t("failure.storage.summary"),
    cause: failures.map((failure) => storageImpact(failure.step)).join("\n"),
    nextAction: t("failure.storage.next"),
    failedStep: failures.map((failure) => answerStageLabel(failure.step)).join("・"),
    errorType: first.error_type,
    errorCode: failures
      .map((failure) => failure.error_code)
      .filter((code): code is string => Boolean(code))
      .join("・"),
    traceId,
  };
}

/** 実行の詳細の失敗（失敗した実行だけ）。トレース ID は概要の実行 ID と同じなので入れない。 */
export function runFailure(run: Pick<RunDetail, "status" | "error_stage" | "error_type" | "error_code">): ProcessFailure | null {
  if (run.status !== "error") return null;
  return {
    summary: failureSummary(run.error_stage),
    nextAction: t("failure.run.next"),
    failedStep: run.error_stage ? failedStageLabel(run.error_stage) : null,
    errorType: run.error_type,
    errorCode: run.error_code ?? null,
  };
}
