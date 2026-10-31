import { useId, useState } from "react";
import { CheckCircle2, CircleAlert, Clock3 } from "lucide-react";

import { useWorkspaceActive } from "@/components/WorkspaceState";
import { formatDateTime } from "@/lib/format";

import { t } from "@/lib/i18n";
import { useOperationTiming, type UseOperationTimingOptions, surfaceTintFor } from "@production-ready/ui";

export type ExecutionActivityStatus = "running" | "success" | "error";
type ExecutionActivityTone = "info" | "success" | "danger";

export interface ExecutionActivityPanelProps
  extends Pick<
    UseOperationTimingOptions,
    "operationKey" | "startedAt" | "finishedAt" | "elapsedMs"
  > {
  inputSignature?: string;
  status: ExecutionActivityStatus;
  label: string;
  testId?: string;
}

const statusLabelKey: Record<ExecutionActivityStatus, string> = {
  running: "executionActivity.status.running",
  success: "executionActivity.status.success",
  error: "executionActivity.status.error",
};

const toneClass: Record<ExecutionActivityTone, string> = {
  info: "border-info-border bg-info-subtle text-info-fg",
  success: "border-success-border bg-success-subtle text-success-fg",
  danger: "border-danger-border bg-danger-subtle text-danger-fg",
};

function statusTone(status: ExecutionActivityStatus): ExecutionActivityTone {
  if (status === "success") return "success";
  if (status === "error") return "danger";
  return "info";
}

function ActivityIcon({ status }: { status: ExecutionActivityStatus }) {
  if (status === "success") return <CheckCircle2 size={16} aria-hidden="true" />;
  if (status === "error") return <CircleAlert size={16} aria-hidden="true" />;
  return <Clock3 size={16} aria-hidden="true" />;
}

export function ExecutionActivityPanel({
  status,
  label,
  operationKey,
  startedAt,
  finishedAt,
  elapsedMs,
  testId,
  inputSignature,
}: ExecutionActivityPanelProps) {
  const titleId = useId();
  const pageActive = useWorkspaceActive();
  const [previousRun, setPreviousRun] = useState<unknown>(null);
  // 実行が変わった時点の入力を state で持つ（render 中に ref を読み書きしない）。
  const [stored, setStored] = useState({ operationKey, inputSignature });
  let submitted = stored;
  if (stored.operationKey !== operationKey) {
    submitted = { operationKey, inputSignature };
    setStored(submitted);
  }
  const inputChanged = inputSignature !== undefined && inputSignature !== submitted.inputSignature;
  // 非表示の間に来た実行を「前回の結果」として覚える（effect ではなく render 中に同期する）。
  if (!pageActive && previousRun !== operationKey) setPreviousRun(operationKey);
  const historical = status !== "running" && (previousRun === operationKey || inputChanged);

  const active = status === "running";
  const timing = useOperationTiming({
    active,
    operationKey,
    startedAt,
    finishedAt,
    elapsedMs,
  });
  const statusClass = toneClass[statusTone(status)];
  const timerLabel = active ? t("common.processing.elapsed") : t("common.processing.duration");

  return (
    <section
      role="status"
      aria-atomic="true"
      aria-busy={active ? "true" : undefined}
      aria-labelledby={titleId}
      className="grid min-w-0 gap-3 rounded-md border border-border bg-surface-sunken px-3 py-3 text-sm shadow-sm"
      data-testid={testId}
      data-execution-activity-status={status}
    >
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-2">
          <span
            data-surface-tint={surfaceTintFor(statusClass)}
            className={`mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border ${statusClass}`}
            aria-hidden="true"
            // 見出しと説明の 2 行のまとまりの横の印の箱（文字の 1 行目にそろえる規則の例外。#2371）。
            data-icon-align-skip=""
          >
            <ActivityIcon status={status} />
          </span>
          <div className="min-w-0">
            <h3 id={titleId} className="text-sm font-semibold text-fg">
              {t(historical ? "workspace.previousResult" : "executionActivity.title")}
            </h3>
            <p className="mt-0.5 break-words text-sm text-fg">{label}</p>
            {!active && finishedAt ? <p className="mt-1 text-xs text-fg-muted">{t("workspace.executedAt", { date: formatDateTime(new Date(finishedAt).toISOString()) })}</p> : null}
            {inputChanged && !active ? <p className="mt-1 text-sm text-fg-muted">{t("workspace.inputChanged")}</p> : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
          <span
            data-surface-tint={surfaceTintFor(statusClass)}
            className={`inline-flex min-h-7 items-center rounded-md border px-2 py-1 text-xs font-semibold ${statusClass}`}
          >
            {t(statusLabelKey[status])}
          </span>
          <span
            className="inline-flex min-h-7 items-center gap-1.5 whitespace-nowrap rounded-md border border-border bg-surface px-2 py-1 text-xs font-medium text-fg-muted"
            role="timer"
            aria-live="off"
            aria-label={`${timerLabel} ${timing.elapsedClock}`}
            data-testid={testId ? `${testId}-timer` : undefined}
          >
            <Clock3 size={14} aria-hidden="true" />
            <span>{timerLabel}</span>
            <span className="min-w-[3.25rem] text-right font-sans tabular-nums text-fg">
              {timing.elapsedClock}
            </span>
          </span>
        </div>
      </div>
    </section>
  );
}
