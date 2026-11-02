/** 資料との照合の記録を、RAG・Agent 共通の 5 状態（`AnswerCheck`。#141）に写す。主張の支持を新たに判定しない。 */
import { hasAnswerPassageRecords, type AnswerCheck, type AnswerCheckState } from "@production-ready/ui";

import { answerPassages } from "@/lib/answer-passages";
import { t } from "@/lib/i18n";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

/**
 * 回答の診断（`diagnostics.answer`）または保存した会話のメッセージ（どちらも `answer_validation` と段落の印を持つ）から
 * 照合の状態を作る。照合が完了した回答は `valid` ではなく段落の印の有無で決める（standard の意味監査は未校正で `valid`
 * を持たないため。backend の `answer_check_state` と同じ規則）。照合の記録の無い過去の回答・知らない状態は null。
 */
export function ragAnswerCheck(source: unknown): AnswerCheck | null {
  const validation = record(record(source)?.answer_validation);
  const status = typeof validation?.status === "string" ? validation.status : "";
  const reason = validation?.reason;
  if (!status) return null;
  if (status === "disabled" || reason === "audit_disabled") return { state: "off" };
  if (reason === "empty_answer") return { state: "skipped", reason: t("answer.check.reason.emptyAnswer") };
  if (status === "error") return { state: "failed", reason: t("answer.check.reason.error") };
  if (status === "timed_out" || status === "timeout" || status === "partial") {
    return { state: "failed", reason: t("answer.check.reason.timedOut") };
  }
  if (status === "input_too_large") return { state: "failed", reason: t("answer.check.reason.inputTooLarge") };
  if (["completed", "ok", "validated", "no_claims"].includes(status)) {
    const passages = answerPassages(source);
    return passages && hasAnswerPassageRecords(passages) ? { state: "flagged", passages } : { state: "verified" };
  }
  return null;
}

/**
 * 実行履歴の照合の状態。`answer_check`（#141 以降の実行）が無い過去の実行は、記録した照合の実行状態
 * （`validation_execution`）から読む。完了したが合否を決めていない（unknown）実行は null（「記録なし」）。
 */
export function runAnswerCheck(answerCheck?: string | null, execution?: string | null): AnswerCheck | null {
  const states: readonly AnswerCheckState[] = ["verified", "flagged", "skipped", "off", "failed"];
  if (answerCheck && states.includes(answerCheck as AnswerCheckState)) return { state: answerCheck as AnswerCheckState };
  switch (execution) {
    case "passed":
      return { state: "verified" };
    case "failed":
      return { state: "flagged" };
    case "disabled":
      return { state: "off" };
    case "not_run":
      return { state: "skipped" };
    case "error":
    case "timed_out":
    case "partial":
      return { state: "failed" };
    default:
      return null;
  }
}
