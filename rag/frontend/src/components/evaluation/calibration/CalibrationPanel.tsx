import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  RowTitleButton,
  SelectField,
  StatusBadge,
  useConfirm,
} from "@production-ready/ui";
import { FlaskConical } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/StateViews";
import {
  ApiError,
  type CalibrationSummary,
  type LabelSetDetail,
} from "@/lib/api";
import {
  useCalibrations,
  useRefreshCalibrationResults,
  useStartCalibration,
} from "@/lib/calibration-queries";
import { formatDateTime } from "@/lib/format";
import { type I18nKey, t } from "@/lib/i18n";
import { useCancelEvaluationJob, useEvaluationJob } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { useWorkspaceState } from "@/lib/workspace-state";

import { isEvaluationJobActive, isEvaluationJobId } from "../evaluation-job";
import { EvaluationJobPanel } from "../EvaluationJobPanel";
import {
  aspectLabel,
  calibrationResultRows,
  formatKappa,
  formatRate,
  shortSha,
  splitLabel,
  type CalibrationResultRow,
} from "./calibration-format";

const REPEAT_OPTIONS = ["1", "2", "3"] as const;
type Repeats = (typeof REPEAT_OPTIONS)[number];

/**
 * 校正（判定の校正の管理の権限）。最終の判定（裁定・全員一致）で RAG の回答の評価器を動かし、観点 × 区分の
 * κ・判定率・誤合格の率と合否（閾値は suite と同じ）を出す。job で動き、品質評価の job と同じ実行状況を出す。
 */
export function CalibrationPanel({ detail }: { detail: LabelSetDetail }) {
  const labelSetId = detail.summary.label_set_id;
  const calibrations = useCalibrations(labelSetId, true);
  const start = useStartCalibration(labelSetId);
  const refresh = useRefreshCalibrationResults();
  const cancelMutation = useCancelEvaluationJob();
  const confirm = useConfirm();
  const [repeats, setRepeats] = useState<Repeats>("1");
  const [startError, setStartError] = useState<string | null>(null);
  const [jobId, setJobId] = useWorkspaceState<string>(
    "evaluation.calibration.jobId",
    "",
    isEvaluationJobId,
  );
  const jobQuery = useEvaluationJob(jobId || null);
  const job = jobQuery.data?.kind === "calibration" ? jobQuery.data : null;
  const observed = useRef<string | null>(null);
  const items = calibrations.data?.items ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const shown =
    items.find((item) => item.calibration_id === selectedId) ??
    items[0] ??
    null;
  const unresolved = detail.summary.row_count - detail.summary.resolved_rows;

  useEffect(() => {
    if (!job) return;
    const previous = observed.current;
    observed.current = job.status;
    if (previous === "RUNNING" && job.status !== "RUNNING") {
      refresh(labelSetId);
      if (job.status === "SUCCEEDED")
        toast.success(t("evaluation.calibration.run.succeeded"));
      if (job.status === "FAILED")
        toast.error(t("evaluation.calibration.run.failed"));
    }
  }, [job, labelSetId, refresh]);

  const run = () => {
    setStartError(null);
    start.mutate(Number(repeats), {
      onSuccess: (data) => {
        observed.current = data.job.status;
        setJobId(data.job.job_id);
        setSelectedId(data.calibration.calibration_id);
        toast.info(t("evaluation.calibration.run.started"));
      },
      onError: (error) =>
        setStartError(
          error instanceof ApiError
            ? error.message
            : t("evaluation.calibration.run.error"),
        ),
    });
  };

  const cancel = async () => {
    if (!job) return;
    const confirmed = await confirm({
      title: t("evaluation.calibration.run.cancelTitle"),
      description: t("evaluation.calibration.run.cancelDescription"),
      confirmLabel: t("evaluation.job.cancelConfirm.confirm"),
      tone: "danger",
    });
    if (confirmed) cancelMutation.mutate(job.job_id);
  };

  return (
    <div className="grid min-w-0 gap-4" data-testid="calibration-run">
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FlaskConical size={16} className="text-accent-fg" aria-hidden />
            {t("evaluation.calibration.run.title")}
          </CardTitle>
          <CardDescription>
            {t("evaluation.calibration.run.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          <ul
            className="flex min-w-0 flex-wrap gap-1.5"
            data-testid="calibration-thresholds"
          >
            {(["items", "coverage", "kappa", "falsePass"] as const).map(
              (key) => (
                <li
                  key={key}
                  className="inline-flex min-h-6 items-center rounded-md bg-surface px-2 text-xs font-medium text-fg ring-1 ring-border"
                >
                  {t(`evaluation.calibration.threshold.${key}` as I18nKey)}
                </li>
              ),
            )}
          </ul>
          <SelectField<Repeats>
            id="calibration-repeats"
            label={t("evaluation.calibration.run.repeats")}
            helper={t("evaluation.calibration.run.repeatsHelp")}
            value={repeats}
            options={REPEAT_OPTIONS.map((value) => ({
              value,
              label: t("evaluation.calibration.run.repeatsOption", {
                count: value,
              }),
            }))}
            onValueChange={setRepeats}
            width="sm"
          />
          {unresolved > 0 ? (
            <Banner severity="info">
              {t("evaluation.calibration.run.unresolved", {
                count: unresolved,
              })}
            </Banner>
          ) : null}
          <div>
            <Button
              type="button"
              icon={FlaskConical}
              loading={start.isPending}
              disabled={isEvaluationJobActive(job) || unresolved > 0}
              onClick={run}
            >
              {t("evaluation.calibration.run.action")}
            </Button>
          </div>
          {startError ? <Banner severity="danger">{startError}</Banner> : null}
        </CardContent>
      </Card>
      {job ? (
        <EvaluationJobPanel
          job={job}
          title={t("evaluation.calibration.run.jobTitle")}
          testPrefix="calibration-job"
          currentLabel={null}
          cancelling={cancelMutation.isPending}
          onCancel={() => void cancel()}
        />
      ) : null}
      <CalibrationResult summary={shown} />
      {items.length > 1 ? (
        <CalibrationHistory
          items={items}
          selectedId={shown?.calibration_id ?? null}
          onSelect={setSelectedId}
        />
      ) : null}
    </div>
  );
}

function statusBadge(summary: CalibrationSummary) {
  if (summary.status !== "SUCCEEDED") {
    return (
      <StatusBadge
        variant={
          summary.status === "FAILED"
            ? "danger"
            : summary.status === "RUNNING"
              ? "info"
              : "neutral"
        }
        label={t(`evaluation.job.status.${summary.status}` as I18nKey)}
      />
    );
  }
  return (
    <StatusBadge
      variant={summary.passed ? "success" : "warning"}
      label={
        summary.passed
          ? t("evaluation.calibration.result.passed")
          : t("evaluation.calibration.result.failed")
      }
    />
  );
}

function CalibrationResult({
  summary,
}: {
  summary: CalibrationSummary | null;
}) {
  if (!summary) {
    return (
      <Card className="min-w-0">
        <CardContent className="pt-5">
          <EmptyState
            title={t("evaluation.calibration.result.empty")}
            hint={t("evaluation.calibration.result.emptyHint")}
          />
        </CardContent>
      </Card>
    );
  }
  const rows = calibrationResultRows(summary);
  const failures = summary.aspects.filter((aspect) => aspect.failures.length);
  return (
    <Card className="min-w-0" data-testid="calibration-result">
      <CardHeader>
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <CardTitle>{t("evaluation.calibration.result.title")}</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            {summary.current ? null : (
              <StatusBadge
                variant="warning"
                label={t("evaluation.calibration.result.stale")}
              />
            )}
            {statusBadge(summary)}
          </div>
        </div>
        <CardDescription>
          {t("evaluation.calibration.result.description")}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-4">
        <dl
          className="grid min-w-0 gap-x-6 gap-y-2 text-sm sm:grid-cols-2 xl:grid-cols-4"
          data-testid="calibration-provenance"
        >
          <Provenance
            label={t("evaluation.calibration.result.model")}
            value={summary.judge_model_id ?? "—"}
          />
          <Provenance
            label={t("evaluation.calibration.result.rubricVersion")}
            value={summary.judge_rubric_version ?? "—"}
          />
          <Provenance
            label={t("evaluation.calibration.result.promptSha")}
            value={shortSha(summary.prompt_sha256)}
            title={summary.prompt_sha256 ?? undefined}
            mono
          />
          <Provenance
            label={t("evaluation.calibration.result.when")}
            value={t("evaluation.calibration.result.whenValue", {
              time: formatDateTime(summary.finished_at ?? summary.created_at),
              repeats: summary.repeats,
            })}
          />
        </dl>
        {summary.status === "FAILED" && summary.error_message ? (
          <Banner severity="danger">{summary.error_message}</Banner>
        ) : null}
        {rows.length ? (
          <DataTable<CalibrationResultRow>
            testId="calibration-result-table"
            ariaLabel={t("evaluation.calibration.result.title")}
            rows={rows}
            getRowKey={(row) => row.key}
            dense
            columns={[
              {
                key: "aspect",
                header: t("evaluation.calibration.progress.aspect"),
                rowHeader: true,
                className: "whitespace-nowrap",
                render: (row) => (row.first ? aspectLabel(row.aspect) : ""),
              },
              {
                key: "split",
                header: t("evaluation.calibration.result.split"),
                className: "whitespace-nowrap",
                render: (row) => splitLabel(row.split),
              },
              {
                key: "gate",
                header: t("evaluation.calibration.result.gate"),
                className: "whitespace-nowrap",
                render: (row) =>
                  row.split === "holdout" ? (
                    <StatusBadge
                      variant={row.passed ? "success" : "warning"}
                      label={
                        row.passed
                          ? t("evaluation.calibration.result.aspectPassed")
                          : t("evaluation.calibration.result.aspectFailed")
                      }
                    />
                  ) : (
                    <span className="text-xs text-fg-muted">
                      {t("evaluation.calibration.result.reference")}
                    </span>
                  ),
              },
              {
                key: "kappa",
                header: "κ",
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) => formatKappa(row.kappa),
              },
              {
                key: "falsePass",
                header: t("evaluation.calibration.result.falsePass"),
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) => formatRate(row.falsePass),
              },
              {
                key: "coverage",
                header: t("evaluation.calibration.result.coverage"),
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) => formatRate(row.coverage),
              },
              {
                key: "total",
                header: t("evaluation.calibration.result.total"),
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) => row.total,
              },
              {
                key: "falseFail",
                header: t("evaluation.calibration.result.falseFail"),
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) => formatRate(row.falseFail),
              },
              {
                key: "unstable",
                header: t("evaluation.calibration.result.unstable"),
                className: "tnum whitespace-nowrap",
                align: "right",
                render: (row) =>
                  row.split === "holdout" ? formatRate(row.unstableRate) : "",
              },
            ]}
          />
        ) : null}
        {failures.length ? (
          <div
            className="grid min-w-0 gap-1 text-sm"
            data-testid="calibration-failures"
          >
            <p className="font-medium text-fg">
              {t("evaluation.calibration.result.failures")}
            </p>
            <ul className="grid gap-0.5 text-fg-muted">
              {failures.map((aspect) => (
                <li key={aspect.aspect} className="break-words">
                  {aspectLabel(aspect.aspect)}: {aspect.failures.join(" / ")}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Provenance({
  label,
  value,
  title,
  mono = false,
}: {
  label: string;
  value: string;
  title?: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd
        className={
          mono ? "truncate font-mono text-xs text-fg" : "break-words text-fg"
        }
        title={title}
      >
        {value}
      </dd>
    </div>
  );
}

function CalibrationHistory({
  items,
  selectedId,
  onSelect,
}: {
  items: readonly CalibrationSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{t("evaluation.calibration.history.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable<CalibrationSummary>
          testId="calibration-history"
          ariaLabel={t("evaluation.calibration.history.title")}
          rows={items}
          getRowKey={(row) => row.calibration_id}
          selectedRowKey={selectedId}
          onRowClick={(row) => onSelect(row.calibration_id)}
          dense
          columns={[
            {
              key: "when",
              header: t("evaluation.calibration.result.when"),
              rowHeader: true,
              className: "tnum whitespace-nowrap",
              render: (row) => (
                <RowTitleButton
                  title={formatDateTime(row.finished_at ?? row.created_at)}
                  current={selectedId === row.calibration_id}
                  onClick={() => onSelect(row.calibration_id)}
                />
              ),
            },
            {
              key: "model",
              header: t("evaluation.calibration.result.model"),
              render: (row) => row.judge_model_id ?? "—",
            },
            {
              key: "status",
              header: t("evaluation.calibration.result.gate"),
              className: "whitespace-nowrap",
              render: statusBadge,
            },
          ]}
        />
      </CardContent>
    </Card>
  );
}
