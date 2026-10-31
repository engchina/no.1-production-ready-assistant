import { useQuery } from "@tanstack/react-query";
import {
  ApiErrorState,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  ListToolbar,
  SelectField,
  StatusBadge,
  TableSkeleton,
  TimedLoadingState,
  type DataTableColumn,
  type StatusVariant,
} from "@production-ready/ui";

import { listTotal } from "@/components/ListFilters";
import { PagedDataTable } from "@/components/ListViews";
import { evaluatedVersionLabel } from "@/components/evaluation/EvaluationVersionField";
import {
  agentApi,
  type EvaluationComparisonChange,
  type EvaluationComparisonOutcome,
  type EvaluationComparisonRow,
  type EvaluationJobItem,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { isString, useWorkspaceState } from "@/lib/workspace-state";

/** 比べる評価の候補の件数（最近の評価の API の上限）。 */
const COMPARE_OPTIONS_LIMIT = 100;

function jobLabel(item: EvaluationJobItem): string {
  return t("evaluation.compare.option", {
    started: formatDateTime(item.created_at),
    set: item.set_name || "—",
    agent: item.agent_name || item.agent_id,
    version: evaluatedVersionLabel(item.agent_version),
  });
}

function formatRate(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

const CHANGE_VARIANT: Record<EvaluationComparisonChange, StatusVariant> = {
  not_comparable: "warning",
  improved: "success",
  regressed: "danger",
  unchanged: "neutral",
  base_only: "neutral",
  target_only: "neutral",
};

function outcomeVariant(outcome: EvaluationComparisonOutcome): StatusVariant {
  if (outcome.status === "correct") return "success";
  if (outcome.status === "incorrect" || outcome.status === "run_failed") return "danger";
  if (outcome.status === "cancelled") return "neutral";
  return "warning";
}

function Outcome({ outcome }: { outcome: EvaluationComparisonOutcome | null }) {
  if (!outcome) return <span className="text-xs text-fg-muted">{t("evaluation.compare.noCase")}</span>;
  return (
    <span className="flex flex-col items-start gap-1">
      <StatusBadge variant={outcomeVariant(outcome)} label={t(`evaluation.status.${outcome.status}` as I18nKey)} />
      <span className="text-xs tabular-nums text-fg-muted">
        {t("evaluation.compare.score", { score: outcome.score === null ? "—" : outcome.score.toFixed(2) })}
      </span>
      <span className="break-all font-mono text-xs text-fg-muted">
        {outcome.tool_calls.length ? outcome.tool_calls.join(", ") : t("evaluation.tools.none")}
      </span>
    </span>
  );
}

/**
 * 評価を比べる（#1845）。2 つの評価（業務 Agent・版・評価セットをまたいでよい）を、同じ質問の
 * ケースどうしで並べ、合否の変化（改善・悪化・変化なし）を文と色で出す。選んだ 2 つは作業状態に残す。
 */
export function EvaluationCompare() {
  const [baseId, setBaseId] = useWorkspaceState("evaluation", "compareBase", "", isString);
  const [targetId, setTargetId] = useWorkspaceState("evaluation", "compareTarget", "", isString);
  const jobs = useQuery({
    queryKey: ["evaluations", "compare-options"],
    queryFn: () => agentApi.listEvaluations({ offset: 0, limit: COMPARE_OPTIONS_LIMIT }),
  });
  const finished = (jobs.data?.items ?? []).filter((item) => item.status !== "queued" && item.status !== "running");
  const options = finished.map((item) => ({ value: item.id, label: jobLabel(item) }));
  const base = finished.some((item) => item.id === baseId) ? baseId : "";
  const target = finished.some((item) => item.id === targetId) ? targetId : "";
  const ready = Boolean(base && target && base !== target);
  const comparison = useQuery({
    queryKey: ["evaluation-compare", base, target],
    queryFn: () => agentApi.compareEvaluations(base, target),
    enabled: ready,
    retry: false,
  });

  const columns: DataTableColumn<EvaluationComparisonRow>[] = [
    {
      key: "case",
      header: t("evaluation.column.case"),
      rowHeader: true,
      className: "min-w-56 max-w-96",
      render: (row) => (
        <span className="flex min-w-0 flex-col">
          <span className="break-words font-medium text-fg">{row.question}</span>
          <span className="break-all text-xs text-fg-muted">
            {[row.base?.case_id, row.target?.case_id].filter((id, index, ids) => id && ids.indexOf(id) === index).join(" / ")}
          </span>
        </span>
      ),
    },
    // 変化は狭い画面でも見えるよう、ケースのすぐ右に置く。
    {
      key: "change",
      header: t("evaluation.compare.change"),
      render: (row) => (
        <StatusBadge variant={CHANGE_VARIANT[row.change]} label={t(`evaluation.compare.change.${row.change}` as I18nKey)} />
      ),
    },
    { key: "base", header: t("evaluation.compare.base"), className: "max-w-64", render: (row) => <Outcome outcome={row.base} /> },
    { key: "target", header: t("evaluation.compare.target"), className: "max-w-64", render: (row) => <Outcome outcome={row.target} /> },
  ];

  return (
    <Card className="min-w-0" data-testid="evaluation-compare">
      <CardHeader>
        <CardTitle>{t("evaluation.compare.title")}</CardTitle>
        <CardDescription>{t("evaluation.compare.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {jobs.isLoading ? (
          <TimedLoadingState label={t("loading.evaluations")} testId="evaluation-compare-options-loading">
            <TableSkeleton columns={4} />
          </TimedLoadingState>
        ) : jobs.error ? (
          <ApiErrorState error={jobs.error} fallback={t("common.error.load")} retryLabel={t("common.retry")} onRetry={() => void jobs.refetch()} />
        ) : finished.length < 2 ? (
          <EmptyState title={t("evaluation.compare.notEnough")} hint={t("evaluation.compare.notEnoughHint")} />
        ) : (
          <>
            <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
              <SelectField<string>
                id="evaluation-compare-base"
                data-testid="evaluation-compare-base"
                label={t("evaluation.compare.base")}
                width="lg"
                value={base}
                placeholder={t("evaluation.compare.choose")}
                options={options}
                onValueChange={setBaseId}
              />
              <SelectField<string>
                id="evaluation-compare-target"
                data-testid="evaluation-compare-target"
                label={t("evaluation.compare.target")}
                width="lg"
                value={target}
                placeholder={t("evaluation.compare.choose")}
                options={options.filter((option) => option.value !== base)}
                onValueChange={setTargetId}
              />
            </div>
            {!ready ? (
              <p className="text-xs text-fg-muted">{t("evaluation.compare.chooseHint")}</p>
            ) : comparison.isLoading ? (
              <TimedLoadingState label={t("loading.evaluationCompare")} testId="evaluation-compare-loading">
                <TableSkeleton columns={4} />
              </TimedLoadingState>
            ) : comparison.error ? (
              <ApiErrorState
                error={comparison.error}
                fallback={t("common.error.load")}
                retryLabel={t("common.retry")}
                onRetry={() => void comparison.refetch()}
              />
            ) : comparison.data ? (
              <div className="grid min-w-0 gap-3">
                <p className="text-sm text-fg" data-testid="evaluation-compare-summary">
                  {t("evaluation.compare.summary", {
                    base: formatRate(comparison.data.base.summary.pass_rate),
                    target: formatRate(comparison.data.target.summary.pass_rate),
                    improved: comparison.data.improved,
                    regressed: comparison.data.regressed,
                    unchanged: comparison.data.unchanged,
                  })}
                  {comparison.data.base_only || comparison.data.target_only
                    ? t("evaluation.compare.unmatched", {
                        base: comparison.data.base_only,
                        target: comparison.data.target_only,
                      })
                    : ""}
                </p>
                <ListToolbar count={listTotal(comparison.data.rows.length)} testId="evaluation-compare-toolbar" />
                <PagedDataTable<EvaluationComparisonRow>
                  pageKey="evaluationCompare"
                  rows={comparison.data.rows}
                  columns={columns}
                  getRowKey={(row) => `${row.base?.case_id ?? ""}|${row.target?.case_id ?? ""}|${row.question}`}
                  ariaLabel={t("evaluation.compare.label")}
                  tableClassName="w-full min-w-[54rem]"
                  resetKey={`${base}|${target}`}
                />
              </div>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
