import type { ReactNode } from "react";
import {
  Check,
  CheckCircle2,
  Clock3,
  Route,
  TriangleAlert,
  X,
} from "lucide-react";

import {
  Button,
  Spinner,
  StatusBadge,
  useOperationTiming,
  DisclosureChevron,
  surfaceTintFor,
} from "@production-ready/ui";
import { t } from "@/lib/i18n";
import type { OperationTimestamp } from "@/lib/operationTiming";
import { activitySpinnerTarget } from "./workflowProgressSpinner";

type StatusBadgeVariant = "neutral" | "info" | "success" | "warning" | "danger";

export type WorkflowProgressStepStatus = "pending" | "running" | "done" | "error" | "skipped";
export type WorkflowProgressTone = "active" | "success" | "danger" | "neutral";

export interface WorkflowProgressStep {
  id: string;
  label: ReactNode;
  description?: ReactNode;
  content?: ReactNode;
  status: WorkflowProgressStepStatus;
  statusLabel: string;
  elapsedLabel?: string;
  open?: boolean;
  testId?: string;
  dataStatus?: string;
}

export interface WorkflowProgressCollapsible {
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  collapseLabel: string;
  expandLabel: string;
  toggleTestId?: string;
}

export interface WorkflowProgressStripProps {
  active: boolean;
  operationKey: string | number | null;
  startedAt?: OperationTimestamp;
  finishedAt?: OperationTimestamp;
  elapsedMs?: number | null;
  title: ReactNode;
  titleId: string;
  message: ReactNode;
  statusLabel: string;
  statusVariant: StatusBadgeVariant;
  tone: WorkflowProgressTone;
  steps: WorkflowProgressStep[];
  stepsAriaLabel: string;
  testId: string;
  dataJobStatus: string;
  role?: "alert" | "status";
  meta?: ReactNode;
  actions?: ReactNode;
  headerExtra?: ReactNode;
  footer?: ReactNode;
  collapsible?: WorkflowProgressCollapsible;
  /**
   * 帯の中のスピナー。帯の中のボタン（「中止」など）が loading を出している間は "none" にする
   * （同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
   */
  activityIcon?: "spinner" | "none";
}

function toneBorderClass(tone: WorkflowProgressTone) {
  if (tone === "danger") return "border-l-danger-fg";
  if (tone === "success") return "border-l-success-fg";
  if (tone === "neutral") return "border-l-fg-muted";
  return "border-l-accent-fg";
}

function toneIconClass(tone: WorkflowProgressTone) {
  if (tone === "danger") return "bg-danger-subtle text-danger-fg";
  if (tone === "success") return "bg-success-subtle text-success-fg";
  if (tone === "neutral") return "bg-surface-hover text-fg-muted";
  return "bg-accent-subtle text-accent-fg";
}

function toneIcon(tone: WorkflowProgressTone) {
  if (tone === "success") return <CheckCircle2 size={20} />;
  if (tone === "danger") return <TriangleAlert size={20} />;
  if (tone === "neutral") return <Clock3 size={20} />;
  return <Route size={20} />;
}

function stepTextClass(status: WorkflowProgressStepStatus) {
  if (status === "running") return "text-accent-fg";
  if (status === "done") return "text-success-fg";
  if (status === "error") return "text-danger-fg";
  return "text-fg-muted";
}

function stepCircleClass(status: WorkflowProgressStepStatus) {
  if (status === "running") {
    return "border-accent-emphasis bg-accent-emphasis text-fg-on-accent";
  }
  if (status === "done") return "border-success-emphasis bg-success-emphasis text-fg-on-emphasis";
  if (status === "error") return "border-danger-emphasis bg-danger-emphasis text-fg-on-emphasis";
  if (status === "skipped") return "border-border bg-surface-hover text-fg-muted";
  return "border-border bg-surface text-fg-muted";
}

function StepIcon({
  status,
  index,
  spinning,
}: {
  status: WorkflowProgressStepStatus;
  index: number;
  /** この工程でスピナーを回すか（帯全体で 1 つ。`activitySpinnerTarget`）。 */
  spinning: boolean;
}) {
  if (spinning) {
    return <Spinner size={14} />;
  }
  if (status === "done") return <Check size={14} aria-hidden="true" />;
  if (status === "error") return <X size={14} aria-hidden="true" />;
  if (status === "skipped") return <span aria-hidden="true">-</span>;
  return <span aria-hidden="true">{index + 1}</span>;
}

export function WorkflowProgressStrip({
  active,
  operationKey,
  startedAt,
  finishedAt,
  elapsedMs,
  title,
  titleId,
  message,
  statusLabel,
  statusVariant,
  tone,
  steps,
  stepsAriaLabel,
  testId,
  dataJobStatus,
  role,
  meta,
  actions,
  headerExtra,
  footer,
  collapsible,
  activityIcon = "spinner",
}: WorkflowProgressStripProps) {
  const timing = useOperationTiming({
    active,
    operationKey,
    startedAt,
    finishedAt,
    elapsedMs,
  });
  const timerKind = active ? t("common.processing.elapsed") : t("common.processing.duration");
  const collapsed = collapsible?.collapsed ?? false;
  const bodyId = `${titleId}-body`;
  const spinnerTarget = activitySpinnerTarget({
    active: active && activityIcon === "spinner",
    collapsed,
    steps,
  });
  const toggleLabel = collapsed ? collapsible?.expandLabel : collapsible?.collapseLabel;

  return (
    <section
      className={`overflow-hidden rounded-md border border-border border-l-4 bg-surface shadow-sm ${toneBorderClass(tone)}`}
      role={role}
      aria-labelledby={titleId}
      data-testid={testId}
      data-job-status={dataJobStatus}
    >
      <div
        className={`flex flex-col gap-3 bg-surface-sunken px-4 py-3 sm:flex-row sm:items-center sm:justify-between ${
          collapsed ? "" : "border-b border-border"
        }`}
      >
        <div className="flex min-w-0 items-start gap-3">
          {/* 見出しと説明の 2 行のまとまりの横の印の箱（文字の 1 行目にそろえる規則の例外。#2371）。 */}
          <span
            data-surface-tint={surfaceTintFor(toneIconClass(tone))}
            className={`mt-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md ${toneIconClass(tone)}`}
            aria-hidden="true"
            data-icon-align-skip=""
          >
            {spinnerTarget === "header" ? <Spinner size={20} /> : toneIcon(tone)}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id={titleId} className="font-semibold text-fg">
                {title}
              </h2>
              <StatusBadge variant={statusVariant} label={statusLabel} />
              {headerExtra}
            </div>
            <p className="mt-1 text-sm text-fg" aria-live="polite">
              {message}
            </p>
          </div>
        </div>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-x-4 gap-y-2 text-xs text-fg-muted sm:justify-end">
          {meta}
          <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-1.5 font-sans tabular-nums">
            <Clock3 size={12} className="shrink-0" aria-hidden="true" />
            <span>{timerKind}</span>
            <span
              className="min-w-0 break-all"
              role="timer"
              aria-live="off"
              aria-label={`${timerKind} ${timing.elapsedClock}`}
            >
              {timing.elapsedClock}
            </span>
          </span>
          {actions}
          {collapsible ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              iconOnly
              aria-label={toggleLabel}
              aria-expanded={!collapsed}
              aria-controls={bodyId}
              onClick={() => collapsible.onCollapsedChange(!collapsed)}
              data-testid={collapsible.toggleTestId}
            >
              <DisclosureChevron
                expanded={!collapsed}
                size={16}
              />
            </Button>
          ) : null}
        </div>
      </div>

      <div id={bodyId} hidden={collapsed}>
        <ol className="grid gap-0 px-4 py-2" aria-label={stepsAriaLabel}>
          {steps.map((step, index) => {
            const running = step.status === "running";
            const done = step.status === "done";
            return (
              <li
                key={step.id}
                className="relative grid min-w-0 grid-cols-[1.75rem_minmax(0,1fr)] gap-2 py-2 sm:gap-3"
                aria-current={running ? "step" : undefined}
                data-testid={step.testId}
                data-step-status={step.dataStatus ?? step.status}
              >
                {index < steps.length - 1 && (
                  // 線は丸の下端から（丸は見出しの 1 行目の中心の高さ。li の上の余白 0.5rem + 見出しの上の余白 + 1 行の半分 +
                  // 丸の半分 = 3rem）。
                  <span
                    className={`absolute bottom-[-0.5rem] left-[0.84375rem] top-12 w-px ${
                      done ? "bg-success-emphasis" : "bg-border"
                    }`}
                    aria-hidden="true"
                  />
                )}
                {/* 丸の中心を工程の見出しの 1 行目の中心にそろえる（見出しの上の余白の分だけ下げ、見出しと同じ文字の
                    大きさの行の高さの箱の中央に置く。#2371）。 */}
                <span className="flex justify-center pt-[calc(0.25rem+(2.75rem-var(--line-height-sm))/2)] text-sm">
                  <span className="pr-icon-line">
                    <span
                      data-surface-tint={surfaceTintFor(stepCircleClass(step.status))}
                      className={`relative z-10 inline-flex h-7 w-7 items-center justify-center rounded-full border text-xs font-bold ${stepCircleClass(step.status)}`}
                    >
                      <StepIcon status={step.status} index={index} spinning={spinnerTarget === index} />
                    </span>
                  </span>
                </span>
                {/*
                  工程の見出し（名前 + Chevron）と状態（右端、375px では次の行）を 1 行に並べる工程表の行。
                  見出しと Chevron だけを持つ Disclosure では表せないため手書きし、状態は DisclosureChevron で示す（#397）。
                  中の Disclosure（オントロジー構築の工程）は自分の状態で Chevron を回すので、入れ子でも向きは混ざらない。
                */}
                {/* 開いた枠は ring で描く（枠線の幅で見出しの位置をずらさない）。見出しは 1 行目に合わせて上から並べ、
                    1 行のときは最小の高さの中央に来るよう上下の余白を行の高さから決める（#2371）。 */}
                {/* oxlint-disable-next-line design-system/restricted-syntax -- 上の説明のとおり工程表の行。 */}
                <details
                  className="group/disclosure min-w-0 rounded-md px-1 py-1 ring-1 ring-transparent open:bg-surface-sunken open:ring-border sm:px-2"
                  open={step.open}
                >
                  <summary className="flex min-h-11 min-w-0 max-w-full cursor-pointer list-none flex-wrap items-start justify-between gap-2 overflow-hidden rounded-sm py-[calc((2.75rem-1lh)/2)] text-sm sm:gap-3 [&::-webkit-details-marker]:hidden">
                    <span className="flex min-w-0 max-w-full flex-1 basis-full items-start gap-2 font-semibold text-fg sm:basis-0">
                      <span className="min-w-0 break-words sm:truncate">{step.label}</span>
                      <span className="pr-icon-line text-fg-muted">
                        <DisclosureChevron expanded="group" size={14} />
                      </span>
                    </span>
                    <span
                      className={`min-w-0 max-w-full basis-full break-all text-right text-xs font-medium leading-5 sm:basis-auto sm:break-normal ${stepTextClass(step.status)}`}
                    >
                      {step.statusLabel}
                      {step.elapsedLabel ? ` · ${step.elapsedLabel}` : ""}
                    </span>
                  </summary>
                  {step.description ? (
                    <p className="mt-2 border-l border-border pl-3 text-xs leading-5 text-fg-muted">
                      {step.description}
                    </p>
                  ) : null}
                  {step.content}
                </details>
              </li>
            );
          })}
        </ol>

        {footer}
      </div>
    </section>
  );
}
