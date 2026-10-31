/**
 * 品質評価の job（#390）の表示のための判定。画面の部品から切り離して単体テストする。
 */

import type { StatusVariant } from "@production-ready/ui";

import type { EvaluationCaseResult, EvaluationJob, EvaluationJobStatus } from "@/lib/api";
import { answerStageLabel } from "@/lib/answer-progress";
import { t, type I18nKey } from "@/lib/i18n";

import { evaluationCaseRowLabel } from "./evaluation-input";

/** job の状態 → StatusBadge の variant（色だけに頼らず、バッジのアイコンと文言も付く）。 */
export const EVALUATION_JOB_STATUS_VARIANT: Record<EvaluationJobStatus, StatusVariant> = {
  RUNNING: "info",
  SUCCEEDED: "success",
  FAILED: "danger",
  CANCELLED: "neutral",
};

export function evaluationJobStatusLabel(status: EvaluationJobStatus): string {
  return t(`evaluation.job.status.${status}` as I18nKey);
}

export function isEvaluationJobActive(job: Pick<EvaluationJob, "status"> | null | undefined) {
  return job?.status === "RUNNING";
}

/** 終わったケースの割合（0〜100 の整数）。件数が 0 のときは 0。 */
export function evaluationJobPercent(job: Pick<EvaluationJob, "completed_cases" | "total_cases">) {
  if (job.total_cases <= 0) return 0;
  const ratio = Math.min(1, Math.max(0, job.completed_cases / job.total_cases));
  return Math.floor(ratio * 100);
}

/** 進捗の件数の文言（「3 / 10 件（30%）」）。 */
export function evaluationJobCountLabel(
  job: Pick<EvaluationJob, "completed_cases" | "total_cases">
): string {
  return t("evaluation.job.count", {
    completed: job.completed_cases,
    total: job.total_cases,
    percent: evaluationJobPercent(job),
  });
}

/** 実行中のケースの文言（Excel から作ったケースは「5 行目」。#2065）。ケースが無ければ null。 */
export function evaluationJobCurrentCaseLabel(
  job: Pick<EvaluationJob, "status" | "current_case_id">
): string | null {
  if (job.status !== "RUNNING" || !job.current_case_id) return null;
  return t("evaluation.job.currentCase", { case: evaluationCaseRowLabel(job.current_case_id) });
}

/** job 全体の時間の上限の文言（分。1 分未満は切り上げ）。 */
export function evaluationJobTimeLimitLabel(job: Pick<EvaluationJob, "time_limit_seconds">) {
  return t("evaluation.job.timeLimit", {
    minutes: Math.max(1, Math.ceil(job.time_limit_seconds / 60)),
  });
}

/** workspace に保存した job id の検証（空文字は「job なし」）。 */
export function isEvaluationJobId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{0,64}$/.test(value);
}

export interface EvaluationCaseErrorSummary {
  /** 時間切れになった工程の表示名（時間切れ以外は null）。 */
  stageLabel: string | null;
  message: string;
}

/** 失敗したケース（status=error）の理由と工程（#383）。成功したケースは null。 */
export function evaluationCaseErrorSummary(
  result: Pick<EvaluationCaseResult, "status" | "error_stage" | "error_message" | "error_type">
): EvaluationCaseErrorSummary | null {
  if (result.status !== "error") return null;
  return {
    stageLabel: result.error_stage ? answerStageLabel(result.error_stage) : null,
    message: result.error_message || result.error_type || t("evaluation.case.errorUnknown"),
  };
}

export interface EvaluationJobFailure {
  /** 原因と対処（利用者向けの文）。 */
  message: string;
  /** 例外の種別（技術的な詳細。「詳細」に畳む）。無ければ null。 */
  errorType: string | null;
}

/**
 * job の失敗の文から、backend が末尾に付けた「エラー種別: …」（例外のクラス名）を分ける。
 * 本文は原因と対処だけにし、エラー種別は「詳細」に出す（UX 契約 messaging.md §10.3）。
 */
export function splitEvaluationJobFailure(message: string | null | undefined): EvaluationJobFailure {
  const text = (message ?? "").trim();
  const match = /\s*エラー種別:\s*([\w.]+)\s*$/.exec(text);
  if (!match) return { message: text, errorType: null };
  return { message: text.slice(0, match.index).trim(), errorType: match[1] ?? null };
}
