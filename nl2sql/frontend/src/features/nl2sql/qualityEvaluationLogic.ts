import type {
  QualityEvaluationEngine,
  QualityEvaluationEngineCapability,
  QualityEvaluationJobSummary,
  QualityEvaluationStatus,
} from "./types.ts";

export type QualityEvaluationValidationCode =
  | "profile_required"
  | "file_required"
  | "file_extension"
  | "file_size"
  | "engine_required"
  | "repeat_range";

export interface QualityEvaluationValidationInput {
  profileId: string;
  file: { name: string; size: number } | null;
  engines: QualityEvaluationEngine[];
  repeatCount: number;
  maxFileBytes: number;
  capabilities: QualityEvaluationEngineCapability[];
}

export function validateQualityEvaluationInput(
  input: QualityEvaluationValidationInput
): Partial<Record<"profile" | "file" | "engines" | "repeat", QualityEvaluationValidationCode>> {
  const errors: Partial<
    Record<"profile" | "file" | "engines" | "repeat", QualityEvaluationValidationCode>
  > = {};
  if (!input.profileId) errors.profile = "profile_required";
  if (!input.file) errors.file = "file_required";
  else if (!input.file.name.toLowerCase().endsWith(".xlsx")) errors.file = "file_extension";
  else if (input.file.size > input.maxFileBytes) errors.file = "file_size";
  const readiness = new Map(input.capabilities.map((item) => [item.engine, item.available]));
  if (input.engines.length === 0 || input.engines.some((engine) => !readiness.get(engine))) {
    errors.engines = "engine_required";
  }
  if (!Number.isInteger(input.repeatCount) || input.repeatCount < 1 || input.repeatCount > 10) {
    errors.repeat = "repeat_range";
  }
  return errors;
}

export function toggleQualityEvaluationEngine(
  engines: QualityEvaluationEngine[],
  engine: QualityEvaluationEngine
): QualityEvaluationEngine[] {
  return engines.includes(engine)
    ? engines.filter((item) => item !== engine)
    : [...engines, engine];
}

export function qualityEvaluationAttemptCount(
  caseCount: number,
  engineCount: number,
  repeatCount: number
) {
  return Math.max(0, caseCount) * Math.max(0, engineCount) * Math.max(0, repeatCount);
}

export function qualityEvaluationPollingInterval(status?: QualityEvaluationStatus) {
  return status === "pending" || status === "running" ? 1_500 : false;
}

function timestampMillis(value?: string | null): number | null {
  if (!value) return null;
  const millis = Date.parse(value);
  return Number.isNaN(millis) ? null : millis;
}

export function qualityEvaluationLastHeartbeatMs(
  job: Pick<QualityEvaluationJobSummary, "heartbeat_at">
) {
  return timestampMillis(job.heartbeat_at);
}

export function qualityEvaluationLeaseExpired(
  job: Pick<QualityEvaluationJobSummary, "status" | "lease_expires_at">,
  nowMs = Date.now()
) {
  if (job.status !== "running") return false;
  const leaseExpiresAt = timestampMillis(job.lease_expires_at);
  return leaseExpiresAt === null || leaseExpiresAt <= nowMs;
}

export function qualityEvaluationAttemptTimedOut(
  job: Pick<
    QualityEvaluationJobSummary,
    | "status"
    | "current_attempt_started_at"
    | "current_attempt_deadline_at"
    | "attempt_timeout_seconds"
  >,
  nowMs = Date.now()
) {
  if (job.status !== "running") return false;
  const deadlineAt = timestampMillis(job.current_attempt_deadline_at);
  if (deadlineAt !== null) return deadlineAt <= nowMs;
  const startedAt = timestampMillis(job.current_attempt_started_at);
  if (startedAt === null) return false;
  const timeoutSeconds = Math.max(1, Number(job.attempt_timeout_seconds || 0));
  return startedAt + timeoutSeconds * 1000 <= nowMs;
}
