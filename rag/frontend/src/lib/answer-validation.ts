/** 実行した資料照合の状態を表示に写す。主張の支持を新たに判定しない。 */
import type { StatusVariant } from "@production-ready/ui";
import { t } from "@/lib/i18n";

export type ValidationExecution = "disabled" | "not_run" | "error" | "timed_out" | "passed" | "failed" | "partial" | "unknown";

const EXECUTIONS: readonly ValidationExecution[] = ["disabled", "not_run", "error", "timed_out", "passed", "failed", "partial", "unknown"];

export function validationExecutionBadge(execution?: string | null): { variant: StatusVariant; label: string } | null {
  if (!execution || !EXECUTIONS.includes(execution as ValidationExecution)) return null;
  const variant = execution === "passed" ? "success" : execution === "failed" || execution === "error" ? "danger"
    : execution === "partial" || execution === "timed_out" ? "warning" : "neutral";
  return { variant, label: t(`search.answerDetails.validation.${execution as ValidationExecution}`) };
}

export function parseAnswerValidation(value: unknown): { execution: ValidationExecution; status: string; reason: string } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const status = typeof raw.status === "string" ? raw.status : "";
  const reason = typeof raw.reason === "string" ? raw.reason : "";
  const execution: ValidationExecution = status === "disabled" || (status === "not_run" && reason === "audit_disabled") ? "disabled"
    : ["completed", "ok", "validated"].includes(status) ? raw.valid === true ? "passed" : raw.valid === false ? "failed" : "unknown"
      : status === "no_claims" ? "not_run" : status === "timeout" ? "timed_out"
        : ["not_run", "error", "timed_out", "partial"].includes(status) ? status as ValidationExecution : "unknown";
  return { execution, status, reason };
}
