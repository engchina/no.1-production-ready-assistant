import type { ReactNode } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  ListToolbar,
  PageBody,
  PageHeader,
  FilterSelect,
  Skeleton,
  TabPanel,
  TableSkeleton,
  Tabs,
  type DataTableColumn,
  useActionPending,
  displayTimeZone,
  formatDisplayCalendarDate,
} from "@production-ready/ui";

import { listTotal } from "@/components/ListFilters";
import { PagedDataTable, QueryState } from "@/components/ListViews";
import { ReportSourceNote } from "@/components/ReportSourceNote";
import { agentApi, REPORT_PERIOD_DAYS, type UsagePeriodDays, type UsageReport, type UsageTotals } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { useWorkspaceState, type WorkspaceValidator } from "@/lib/workspace-state";

// 利用状況（#772）。業務 Agent の Run が使ったモデルの量を、期間・業務 Agent・利用者・モデル・日ごとに見る。
// 金額には換算しない（モデルの単価は契約で変わるため）。期間は 365 日まで。Oracle の構成は保存した
// Run の履歴（AGENT_RUN_FACTS）を SQL で集計する（#794）。

const PERIODS: readonly UsagePeriodDays[] = REPORT_PERIOD_DAYS;
const DEFAULT_PERIOD: UsagePeriodDays = 30;
const VIEWS = ["agent", "user", "model", "day"] as const;
type UsageView = (typeof VIEWS)[number];

const isPeriod: WorkspaceValidator<UsagePeriodDays> = (value): value is UsagePeriodDays =>
  PERIODS.includes(value as UsagePeriodDays);
const isView: WorkspaceValidator<UsageView> = (value): value is UsageView => VIEWS.includes(value as UsageView);

export function UsagePage() {
  const [days, setDays] = useWorkspaceState("usage", "days", DEFAULT_PERIOD, isPeriod);
  const [view, setView] = useWorkspaceState("usage", "view", "agent" as UsageView, isView);
  // 集計の日の区切りはシステムの設定の時間帯（ブラウザの時間帯ではない。#3265）。
  const timezone = displayTimeZone();
  const report = useQuery({
    queryKey: ["usage", days, timezone],
    queryFn: () => agentApi.getUsageReport(days, timezone),
    // 期間を変えている間は前の集計を出したまま取り直す（Skeleton に戻さない）。
    placeholderData: keepPreviousData,
  });
  // 「最新情報を取得」は押した取り直しの間だけ回す（定期の取り直し・他の操作の後の invalidate・条件の切り替えでは回さない。#819）。
  const manualRefresh = useActionPending();

  return (
    <>
      <PageHeader
        wide
        title={t("nav.usage")}
        subtitle={t("page.usage.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () => void manualRefresh.track(() => report.refetch()),
          },
        ]}
      />
      <PageBody wide>
        {/* ページ全体の期間（一覧の外の単独の絞り込み。ラベルは欄の中。#1933） */}
        <FilterSelect<string>
          id="usage-period"
          label={t("usage.period")}
          value={String(days)}
          defaultValue={String(DEFAULT_PERIOD)}
          options={PERIODS.map((period) => ({ value: String(period), label: t(`usage.period.${period}` as I18nKey) }))}
          onValueChange={(value) => setDays(Number(value) as UsagePeriodDays)}
        />
        <QueryState query={report} loadingLabel={t("loading.usage")} testId="usage-loading" skeleton={<UsageSkeleton />}>
          {report.data ? <UsageContent report={report.data} view={view} onViewChange={setView} /> : null}
        </QueryState>
      </PageBody>
    </>
  );
}

function UsageSkeleton() {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="grid grid-cols-2 gap-4 pt-4 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-16" />
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-4">
          <TableSkeleton columns={6} />
        </CardContent>
      </Card>
    </div>
  );
}

function UsageContent({
  report,
  view,
  onViewChange,
}: {
  report: UsageReport;
  view: UsageView;
  onViewChange: (view: UsageView) => void;
}) {
  const { totals, previous } = report;
  const unrecorded = totals.runs - totals.runs_with_usage;
  // 更新前の backend が返す欠測の行もモデル件数に含めない。記録済みの0 token は残す。
  const models = report.by_model.filter((row) => row.runs_with_usage > 0);
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("usage.summary.title")}</CardTitle>
          <CardDescription>
            {t("usage.summary.description", { since: formatDay(report.since), timezone: report.timezone })}
          </CardDescription>
          <ReportSourceNote source={report.source} />
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 統計のタイルはカードの幅を等分する（6 個なので 2 / 3 列。design-system README §4）。 */}
          <div className="grid grid-cols-2 gap-x-5 gap-y-4 xl:grid-cols-3" data-testid="usage-summary">
            <Metric label={t("usage.metric.totalTokens")} value={totals.total_tokens} previous={previous.total_tokens} />
            <Metric label={t("usage.metric.inputTokens")} value={totals.input_tokens} previous={previous.input_tokens} />
            <Metric label={t("usage.metric.outputTokens")} value={totals.output_tokens} previous={previous.output_tokens} />
            <Metric label={t("usage.metric.averageTokens")} value={averageTokens(totals)} previous={averageTokens(previous)} />
            <Metric label={t("usage.metric.requests")} value={totals.requests} previous={previous.requests} />
            <Metric label={t("usage.metric.runs")} value={totals.runs} previous={previous.runs} />
          </div>
          {unrecorded > 0 ? (
            <p className="text-xs text-fg-muted" data-testid="usage-unrecorded">
              {t("usage.unrecorded", { count: formatNumber(unrecorded) })}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>{t("usage.breakdown.title")}</CardTitle>
          <CardDescription>{t("usage.breakdown.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Tabs
            idPrefix="usage"
            ariaLabel={t("usage.breakdown.title")}
            value={view}
            onChange={(id) => onViewChange(id as UsageView)}
            items={[
              { id: "agent", label: t("usage.view.agent"), count: report.by_agent.length },
              { id: "user", label: t("usage.view.user"), count: report.by_user.length },
              { id: "model", label: t("usage.view.model"), count: models.length },
              { id: "day", label: t("usage.view.day") },
            ]}
          />
          {totals.runs === 0 ? (
            // 選んだタブの aria-controls が指す tabpanel を、0 件のときも置く（WCAG 4.1.2。axe の aria-valid-attr-value。#1671）。
            <TabPanel idPrefix="usage" id={view} value={view}>
              <EmptyState title={t("usage.empty.title")} hint={t("usage.empty.description")} />
            </TabPanel>
          ) : (
            <>
              <TabPanel idPrefix="usage" id="agent" value={view}>
                <BreakdownTable
                  rows={report.by_agent}
                  total={totals.total_tokens}
                  rowKey={(row) => row.agent_id}
                  label={t("usage.table.agent")}
                  nameHeader={t("usage.column.agent")}
                  resetKey={`agent:${report.days}`}
                  name={(row) => (
                    <>
                      <span className="block break-words font-medium text-fg [overflow-wrap:anywhere]">
                        {row.agent_name || t("usage.agentDeleted")}
                      </span>
                      <span className="block break-all font-mono text-xs text-fg-muted">{row.agent_id}</span>
                    </>
                  )}
                />
              </TabPanel>
              <TabPanel idPrefix="usage" id="user" value={view}>
                <BreakdownTable
                  rows={report.by_user}
                  total={totals.total_tokens}
                  rowKey={(row) => row.user_uuid ?? "none"}
                  label={t("usage.table.user")}
                  nameHeader={t("usage.column.user")}
                  resetKey={`user:${report.days}`}
                  name={(row) => (
                    <span className="break-words font-medium text-fg [overflow-wrap:anywhere]">
                      {row.user_uuid ? row.display_name || t("usage.userUnknown") : t("usage.userNone")}
                    </span>
                  )}
                />
              </TabPanel>
              <TabPanel idPrefix="usage" id="model" value={view} className="space-y-3">
                <p className="text-xs text-fg-muted">{t("usage.model.description")}</p>
                {models.length === 0 ? (
                  <EmptyState title={t("usage.model.empty.title")} hint={t("usage.model.empty.description")} />
                ) : (
                  <BreakdownTable
                    rows={models}
                    total={totals.total_tokens}
                    rowKey={(row) => row.model || "none"}
                    label={t("usage.table.model")}
                    nameHeader={t("usage.column.model")}
                    resetKey={`model:${report.days}`}
                    name={(row) =>
                      row.model ? (
                        <span className="break-all font-mono text-xs text-fg">{row.model}</span>
                      ) : (
                        <span className="text-fg-muted">{t("usage.modelNone")}</span>
                      )
                    }
                  />
                )}
              </TabPanel>
              <TabPanel idPrefix="usage" id="day" value={view}>
                <BreakdownTable
                  // 新しい日を上に出す（API は古い順）。
                  rows={[...report.by_day].reverse()}
                  total={totals.total_tokens}
                  rowKey={(row) => row.day}
                  label={t("usage.table.day")}
                  nameHeader={t("usage.column.day")}
                  resetKey={`day:${report.days}`}
                  name={(row) => <span className="tabular-nums text-fg">{formatDay(row.day)}</span>}
                />
              </TabPanel>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Metric({ label, value, previous }: { label: string; value: number; previous: number }) {
  return (
    <div className="min-w-0 border-l-2 border-accent-emphasis pl-3">
      <p className="truncate text-xs text-fg-muted" title={label}>
        {label}
      </p>
      <p className="mt-0.5 text-xl font-semibold tabular-nums text-fg">{formatNumber(value)}</p>
      <p className="mt-0.5 text-xs tabular-nums text-fg-muted">{deltaLabel(value, previous)}</p>
    </div>
  );
}

/** 利用量を記録した Run 1 件あたりの合計 token（四捨五入）。 */
function averageTokens(totals: UsageTotals): number {
  return totals.runs_with_usage > 0 ? Math.round(totals.total_tokens / totals.runs_with_usage) : 0;
}

function deltaLabel(value: number, previous: number): string {
  const delta = value - previous;
  if (delta === 0) return t("usage.delta.same");
  return t("usage.delta", { value: `${delta > 0 ? "+" : "−"}${formatNumber(Math.abs(delta))}` });
}

function BreakdownTable<T extends UsageTotals>({
  rows,
  total,
  rowKey,
  label,
  nameHeader,
  name,
  resetKey,
}: {
  rows: T[];
  total: number;
  rowKey: (row: T) => string;
  label: string;
  nameHeader: string;
  name: (row: T) => ReactNode;
  resetKey: string;
}) {
  const columns: DataTableColumn<T>[] = [
    { key: "name", header: nameHeader, rowHeader: true, className: "min-w-40", render: name },
    { key: "runs", header: t("usage.metric.runs"), align: "right", render: (row) => numberCell(row.runs) },
    { key: "requests", header: t("usage.metric.requests"), align: "right", render: (row) => numberCell(row.requests) },
    {
      key: "input_tokens",
      header: t("usage.metric.inputTokens"),
      align: "right",
      render: (row) => numberCell(row.input_tokens),
    },
    {
      key: "output_tokens",
      header: t("usage.metric.outputTokens"),
      align: "right",
      render: (row) => numberCell(row.output_tokens),
    },
    {
      key: "total_tokens",
      header: t("usage.metric.totalTokens"),
      align: "right",
      render: (row) => <span className="font-medium tabular-nums text-fg">{formatNumber(row.total_tokens)}</span>,
    },
    {
      key: "share",
      header: t("usage.column.share"),
      className: "w-40",
      render: (row) => <ShareBar value={row.total_tokens} total={total} />,
    },
  ];
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar count={listTotal(rows.length)} testId="usage-list-toolbar" />
      <PagedDataTable<T>
        rows={rows}
        columns={columns}
        getRowKey={rowKey}
        ariaLabel={label}
        tableClassName="w-full min-w-[54rem]"
        resetKey={resetKey}
      />
    </div>
  );
}

function numberCell(value: number) {
  return <span className="tabular-nums text-fg">{formatNumber(value)}</span>;
}

/** 期間の合計 token に対する割合（数字を併記し、棒だけに頼らない）。 */
function ShareBar({ value, total }: { value: number; total: number }) {
  const ratio = total > 0 ? value / total : 0;
  const percent = Math.round(ratio * 1000) / 10;
  return (
    <div className="grid grid-cols-[minmax(3rem,1fr)_auto] items-center gap-2 text-xs">
      <span data-surface-tint="neutral" className="h-1.5 overflow-hidden rounded-full bg-surface-hover" aria-hidden>
        <span className="block h-full rounded-full bg-accent-emphasis" style={{ width: `${Math.min(100, percent)}%` }} />
      </span>
      <span className="w-12 text-right tabular-nums text-fg-muted">{`${percent}%`}</span>
    </div>
  );
}

const DAY_FORMAT = { year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" } as const;

/** `YYYY-MM-DD`（その日）か ISO の日時（システムの設定の時間帯）を「YYYY/MM/DD(曜)」へ。 */
function formatDay(value: string): string {
  return formatDisplayCalendarDate(value, DAY_FORMAT, value);
}
