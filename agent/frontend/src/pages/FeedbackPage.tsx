import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  EmptyState,
  INFORMATION_TABLE_ROW_CLASS,
  ListDetailLayout,
  ListDetailPager,
  ListToolbar,
  OffsetPagination,
  PageBody,
  PageHeader,
  PAGE_SIZE_OPTIONS,
  RowTitleButton,
  FilterSelect,
  Skeleton,
  StatusBadge,
  TableSkeleton,
  TabPanel,
  Tabs,
  type DataTableColumn,
  adjacentListItems,
  useActionPending,
  useListDetailSplit,
  useUrlSelection,
} from "@production-ready/ui";

import { AnswerBody } from "@/components/chat/ResultTables";
import { AGENT_LIST_DETAIL_STORAGE_PREFIX } from "@/components/EntityLayout";
import { listCount } from "@/components/ListFilters";
import {
  agentPaginationLabels,
  listScrollLabel,
  listToolbarLabels,
  QueryState,
  usePersistedServerPaging,
} from "@/components/ListViews";
import {
  agentApi,
  FEEDBACK_REASONS,
  REPORT_PERIOD_DAYS,
  SIGNAL_KINDS,
  type FeedbackItem,
  type FeedbackPeriodDays,
  type FeedbackRating,
  type FeedbackReason,
  type FeedbackReport,
  type FeedbackSummary,
  type RunFeedback,
  type RunState,
  type SignalKind,
} from "@/lib/api";
import { AnswerFeedback } from "@/components/chat/AnswerFeedback";
import { AddToEvaluationCase, useCanEditEvaluationSets } from "@/components/evaluation/AddToEvaluationCase";
import { ReportSourceNote } from "@/components/ReportSourceNote";
import { useCapabilities } from "@/lib/permissions";
import { formatDateTime, formatNumber } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { isOneOf, isString, useWorkspaceState, type WorkspaceValidator } from "@/lib/workspace-state";
import { countDelta, formatRate, Metric, rateDelta } from "@/pages/feedbackMetrics";
import { signalLabel, SignalsContent, SignalsSkeleton } from "@/pages/FeedbackSignals";

// フィードバック（#774）。2 つの見方を `Tabs` で切り替える（#1822）。
// - 確かめるべき実行（既定）: 評価の付いていない実行も、回答の検証・回答の対応・ツール・承認・実行の状態の
//   暗黙のシグナルから集める（`FeedbackSignals.tsx`）。明示の評価は回答のごく一部にしか付かないため既定にする。
// - 利用者の評価: チャットの回答への利用者の評価を集計し、役に立たなかった回答と理由を確かめる。
//   画面の形は RAG のフィードバック（集計 → 理由 → 一覧 → 詳細）にそろえる。
// 期間・業務 Agent は 2 つの見方で共通。期間は 365 日まで。Oracle の構成は保存した Run の履歴を集計し、
// 一覧はサーバー側でページングする（#794）。

const PERIODS: readonly FeedbackPeriodDays[] = REPORT_PERIOD_DAYS;
const DEFAULT_PERIOD: FeedbackPeriodDays = 30;
const RATINGS: readonly FeedbackRating[] = ["helpful", "not_helpful"];
const VIEWS = ["signals", "ratings"] as const;
type FeedbackView = (typeof VIEWS)[number];

const isPeriod: WorkspaceValidator<FeedbackPeriodDays> = (value): value is FeedbackPeriodDays =>
  PERIODS.includes(value as FeedbackPeriodDays);
const isRatingFilter: WorkspaceValidator<string> = (value): value is string =>
  value === "" || RATINGS.includes(value as FeedbackRating);
const isReasonFilter: WorkspaceValidator<string> = (value): value is string =>
  value === "" || FEEDBACK_REASONS.includes(value as FeedbackReason);
const isSignalFilter: WorkspaceValidator<string> = (value): value is string =>
  value === "" || SIGNAL_KINDS.includes(value as SignalKind);

export function reasonLabel(reason: FeedbackReason): string {
  return t(`feedback.reason.${reason}` as I18nKey);
}

function ratingLabel(rating: FeedbackRating): string {
  return t(`feedback.rating.${rating}` as I18nKey);
}

export function FeedbackPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [savedView, setView] = useWorkspaceState("feedback", "view", "signals" as FeedbackView, isOneOf(VIEWS));
  const selectedView: FeedbackView | null = searchParams.has("signal") ? "signals" : searchParams.has("feedback") ? "ratings" : null;
  const view = selectedView ?? savedView;
  // 詳細 URL を開いたときだけ見方を保存する。タブの操作で保存値を変えた途中に、
  // 遷移前の URL の見方を書き戻すと最初の切り替えが取り消される。
  useEffect(() => {
    if (selectedView) setView(selectedView);
  }, [selectedView, setView]);
  const [days, setDays] = useWorkspaceState("feedback", "days", DEFAULT_PERIOD, isPeriod);
  const [agentId, setAgentId] = useWorkspaceState("feedback", "agentId", "", isString);
  const [rating, setRating] = useWorkspaceState("feedback", "rating", "", isRatingFilter);
  const [reason, setReason] = useWorkspaceState("feedback", "reason", "", isReasonFilter);
  const [signalKind, setSignalKind] = useWorkspaceState("feedback", "signalKind", "", isSignalFilter);
  // 一覧のページ（作業状態。絞り込みを変えたら 1 ページ目へ戻す）。
  // 表示件数も作業状態に残す（#1891）。
  const ratingPaging = usePersistedServerPaging("feedback");
  const signalPaging = usePersistedServerPaging("feedbackSignals");
  const setPage = ratingPaging.setPage;
  const setSignalPage = signalPaging.setPage;
  // 一覧 + 詳細の選択は URL が唯一の情報源。利用者の評価は `?feedback=`、
  // 確かめるべき実行は `?signal=`（どちらも Run の ID）。先頭の行を自動で選ばない。1 ペインで一覧から
  // 詳細へ入るときは履歴に積み、「一覧へ戻る」とブラウザの「戻る」で一覧へ戻る。
  const split = useListDetailSplit();
  const ratingSelection = useUrlSelection({ param: "feedback", searchParams, setSearchParams, singlePane: !split });
  const signalSelection = useUrlSelection({ param: "signal", searchParams, setSearchParams, singlePane: true });
  const viewSelection = view === "signals" ? signalSelection : ratingSelection;
  const queryClient = useQueryClient();

  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents });
  const filters = useMemo(
    () => ({
      days,
      agentId: agentId || undefined,
      rating: (rating || undefined) as FeedbackRating | undefined,
      reason: (reason || undefined) as FeedbackReason | undefined,
      offset: ratingPaging.offset,
      limit: ratingPaging.limit,
    }),
    [days, agentId, rating, reason, ratingPaging.offset, ratingPaging.limit]
  );
  const signalFilters = useMemo(
    () => ({
      days,
      agentId: agentId || undefined,
      kind: (signalKind || undefined) as SignalKind | undefined,
      offset: signalPaging.offset,
      limit: signalPaging.limit,
    }),
    [days, agentId, signalKind, signalPaging.offset, signalPaging.limit]
  );
  // 見ている見方だけを取得する。
  const report = useQuery({
    queryKey: ["feedback", "ratings", filters],
    queryFn: () => agentApi.getFeedbackReport(filters),
    enabled: view === "ratings",
    // 絞り込みを変えている間は前の結果を出したまま取り直す（Skeleton に戻さない）。
    placeholderData: keepPreviousData,
  });
  const signals = useQuery({
    queryKey: ["feedback", "signals", signalFilters],
    queryFn: () => agentApi.getFeedbackSignals(signalFilters),
    enabled: view === "signals",
    placeholderData: keepPreviousData,
  });
  // 「最新情報を取得」は押した取り直しの間だけ回す（定期の取り直し・他の操作の後の invalidate・条件の切り替えでは回さない。#819）。
  const manualRefresh = useActionPending();
  const filtered = Boolean(rating || reason);

  // 期間・業務 Agent は 2 つの見方で共通。変えたら両方の一覧を 1 ページ目から読む（UX 契約 workspace-state.md）。
  function changeSharedFilter<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setPage(1);
      setSignalPage(1);
    };
  }

  function changeRatingFilter(set: (value: string) => void) {
    return (value: string) => {
      set(value);
      setPage(1);
    };
  }

  function clearListFilters() {
    setRating("");
    setReason("");
    setPage(1);
  }

  function changeSignalKind(value: string) {
    setSignalKind(value);
    setSignalPage(1);
  }

  // 「条件をクリア」（ツールバー。#1933）: 見ている見方の絞り込みと、共通の期間・業務 Agent を既定に戻す。
  const sharedActiveCount = Number(days !== DEFAULT_PERIOD) + Number(Boolean(agentId));
  function clearSharedFilters() {
    setDays(DEFAULT_PERIOD);
    setAgentId("");
    setPage(1);
    setSignalPage(1);
  }

  // 2 つの見方の絞り込みの先頭（タブの中は 1 つだけ描くので、id は同じでよい）。ツールバーの中なので、
  // ラベルは欄の中（「期間: 直近 30 日」。page-archetypes.md「一覧の絞り込みの部品」。#1933）。
  const sharedFilters = (
    <>
      <FilterSelect<string>
        id="feedback-period"
        label={t("feedback.filter.period")}
        value={String(days)}
        defaultValue={String(DEFAULT_PERIOD)}
        options={PERIODS.map((period) => ({ value: String(period), label: t(`feedback.period.${period}` as I18nKey) }))}
        onValueChange={(value) => changeSharedFilter(setDays)(Number(value) as FeedbackPeriodDays)}
      />
      <FilterSelect<string>
        id="feedback-agent"
        width="md"
        label={t("feedback.filter.agent")}
        value={agentId}
        options={[
          { value: "", label: t("feedback.filter.all") },
          ...(agents.data?.agents ?? []).map((agent) => ({ value: agent.id, label: agent.name })),
        ]}
        onValueChange={changeSharedFilter(setAgentId)}
      />
    </>
  );

  return (
    <>
      <PageHeader
        wide
        title={t("nav.feedback")}
        subtitle={t("page.feedback.subtitle")}
        // 確かめるべき実行は全幅の詳細、利用者の評価は xl 未満の詳細で「一覧へ戻る」。
        back={
          (view === "signals" || !split) && viewSelection.selectedId
            ? { label: t("common.backToList"), onClick: viewSelection.clear, testId: "feedback-detail-back" }
            : undefined
        }
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () =>
              void manualRefresh.track(async () => {
                await (view === "signals" ? signals.refetch() : report.refetch());
              }),
          },
        ]}
        tabs={
          <Tabs
            idPrefix="feedback"
            ariaLabel={t("feedback.view.label")}
            value={view}
            onChange={(id) => {
              const nextView = id as FeedbackView;
              setView(nextView);
              const nextParams = new URLSearchParams(searchParams);
              nextParams.delete(nextView === "signals" ? "feedback" : "signal");
              setSearchParams(nextParams, { replace: true });
            }}
            items={[
              { id: "signals", label: t("feedback.view.signals") },
              { id: "ratings", label: t("feedback.view.ratings") },
            ]}
          />
        }
      />
      <PageBody wide>
        <TabPanel idPrefix="feedback" id="signals" value={view} className="space-y-4">
          <ListToolbar
            filters={
              <>
                {sharedFilters}
                <FilterSelect<string>
                  id="feedback-signal"
                  width="md"
                  label={t("feedback.filter.signal")}
                  value={signalKind}
                  options={[
                    { value: "", label: t("feedback.filter.all") },
                    ...SIGNAL_KINDS.map((item) => ({ value: item, label: signalLabel(item) })),
                  ]}
                  onValueChange={changeSignalKind}
                />
              </>
            }
            filtersLayout="sheet"
            activeFilterCount={sharedActiveCount + Number(Boolean(signalKind))}
            onClearFilters={() => {
              clearSharedFilters();
              setSignalKind("");
            }}
            labels={listToolbarLabels()}
            testId="signals-filters"
            className={signalSelection.selectedId ? "hidden" : undefined}
          />
          <QueryState
            query={signals}
            loadingLabel={t("loading.feedbackSignals")}
            testId="signals-loading"
            skeleton={<SignalsSkeleton />}
          >
            {signals.data ? (
              <SignalsContent
                report={signals.data}
                filtered={Boolean(signalKind)}
                onClearFilters={() => changeSignalKind("")}
                selectedId={signalSelection.selectedId}
                onSelect={signalSelection.select}
                onPageChange={setSignalPage}
                onPageSizeChange={signalPaging.setPageSize}
              />
            ) : null}
          </QueryState>
        </TabPanel>
        <TabPanel idPrefix="feedback" id="ratings" value={view} className="space-y-4">
          <ListToolbar
            filters={
              <>
                {sharedFilters}
                <FilterSelect<string>
                  id="feedback-rating"
                  label={t("feedback.filter.rating")}
                  value={rating}
                  options={[
                    { value: "", label: t("feedback.filter.all") },
                    ...RATINGS.map((item) => ({ value: item, label: ratingLabel(item) })),
                  ]}
                  onValueChange={changeRatingFilter(setRating)}
                />
                <FilterSelect<string>
                  id="feedback-reason"
                  width="md"
                  label={t("feedback.filter.reason")}
                  value={reason}
                  options={[
                    { value: "", label: t("feedback.filter.all") },
                    ...FEEDBACK_REASONS.map((item) => ({ value: item, label: reasonLabel(item) })),
                  ]}
                  onValueChange={changeRatingFilter(setReason)}
                />
              </>
            }
            filtersLayout="sheet"
            activeFilterCount={sharedActiveCount + Number(Boolean(rating)) + Number(Boolean(reason))}
            onClearFilters={() => {
              clearSharedFilters();
              setRating("");
              setReason("");
            }}
            labels={listToolbarLabels()}
            testId="feedback-filters"
          />
          <QueryState
            query={report}
            loadingLabel={t("loading.feedback")}
            testId="feedback-loading"
            skeleton={<FeedbackSkeleton />}
          >
            {report.data ? (
              <FeedbackContent
                report={report.data}
                filtered={filtered}
                onClearFilters={clearListFilters}
                selectedId={ratingSelection.selectedId}
                onSelect={ratingSelection.select}
                onReviewed={(run) => {
                  void queryClient.invalidateQueries({ queryKey: ["feedback"] });
                  // 評価ケースの下書きは管理者のコメントを使う（#810）。
                  void queryClient.invalidateQueries({ queryKey: ["evaluation-case-draft", run.id] });
                }}
                onPageChange={setPage}
                onPageSizeChange={ratingPaging.setPageSize}
              />
            ) : null}
          </QueryState>
        </TabPanel>
      </PageBody>
    </>
  );
}

function FeedbackSkeleton() {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="grid grid-cols-2 gap-4 pt-4 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, index) => (
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

function FeedbackContent({
  report,
  filtered,
  onClearFilters,
  selectedId,
  onSelect,
  onReviewed,
  onPageChange,
  onPageSizeChange,
}: {
  report: FeedbackReport;
  filtered: boolean;
  onClearFilters: () => void;
  selectedId: string | null;
  onSelect: (runId: string) => void;
  onReviewed: (run: RunState) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}) {
  const { summary, previous } = report;
  // 選んだ評価が今のページに無い（絞り込み・ページ送りで外れた）ときは、最後に見た内容を出したままにし、
  // 別の行へ置き換えない（page-archetypes.md §1 B「選択」）。
  const found = report.items.find((item) => item.run_id === selectedId) ?? null;
  const [lastSeen, setLastSeen] = useState<FeedbackItem | null>(found);
  if (found && found !== lastSeen) setLastSeen(found);
  const selected = found ?? (lastSeen && lastSeen.run_id === selectedId ? lastSeen : null);
  const { previous: previousItem, next: nextItem } = adjacentListItems(report.items, selectedId, (item) => item.run_id);
  if (summary.total === 0 && summary.admin_reviewed === 0 && !selectedId) {
    return (
      <Card>
        <CardContent className="pt-4">
          <EmptyState title={t("feedback.empty.title")} hint={t("feedback.empty.hint")} />
          <ReportSourceNote source={report.source} />
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("feedback.summary.title")}</CardTitle>
          <CardDescription>{t("feedback.summary.description")}</CardDescription>
          <ReportSourceNote source={report.source} />
        </CardHeader>
        <CardContent className="grid gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <div className="grid grid-cols-2 gap-x-5 gap-y-4 self-start sm:grid-cols-4 lg:grid-cols-2" data-testid="feedback-summary">
            <Metric label={t("feedback.summary.total")} value={formatNumber(summary.total)} delta={countDelta(summary.total, previous.total)} />
            <Metric
              label={t("feedback.summary.helpfulRate")}
              value={formatRate(summary.helpful_rate)}
              delta={rateDelta(summary.helpful_rate, previous.helpful_rate)}
            />
            <Metric label={t("feedback.summary.helpful")} value={formatNumber(summary.helpful)} delta={countDelta(summary.helpful, previous.helpful)} />
            <Metric
              label={t("feedback.summary.notHelpful")}
              value={formatNumber(summary.not_helpful)}
              delta={countDelta(summary.not_helpful, previous.not_helpful)}
            />
          </div>
          <ReasonCounts summary={summary} />
          <p className="text-xs text-fg-muted lg:col-span-2" data-testid="feedback-admin-summary">
            {t("feedback.summary.admin", {
              count: formatNumber(summary.admin_reviewed),
              notHelpful: formatNumber(summary.admin_not_helpful),
            })}
          </p>
        </CardContent>
      </Card>

      {report.total === 0 && !selectedId ? (
        <Card className="min-w-0">
          <CardContent className="pt-4">
            <EmptyState
              title={t("feedback.noMatch.title")}
              action={
                filtered ? (
                  <Button variant="secondary" size="sm" onClick={onClearFilters}>
                    {t("feedback.noMatch.clear")}
                  </Button>
                ) : undefined
              }
            />
          </CardContent>
        </Card>
      ) : (
        // B 型の一覧 + 詳細（共有 ListDetailLayout。#2283）。xl 以上は「狭い一覧 | 広い詳細」、xl 未満は 1 ペイン。
        <ListDetailLayout
          id="feedback-ratings"
          listLabel={t("feedback.list.title")}
          detailLabel={t("feedback.detail.title")}
          storagePrefix={AGENT_LIST_DETAIL_STORAGE_PREFIX}
          listHeader={
            <div className="grid gap-2">
              <h2 className="text-sm font-semibold text-fg">{t("feedback.list.title")}</h2>
              {/* 件数: 絞り込み（評価・理由）中は「N 件（全 M 件）」。M は期間・業務 Agent の評価の全件（#1891）。 */}
              <ListToolbar count={listCount(summary.rated_runs, report.total, filtered)} testId="feedback-list-toolbar" />
            </div>
          }
          list={
            report.items.length ? (
              <FeedbackList items={report.items} selectedId={selectedId} onSelect={onSelect} />
            ) : (
              <div className="p-3">
                <EmptyState title={t("feedback.noMatch.title")} />
              </div>
            )
          }
          listFooter={
            <OffsetPagination
              offset={report.offset}
              limit={report.limit}
              total={report.total}
              count={report.items.length}
              onPageChange={(next) => onPageChange(next)}
              onPageSizeChange={onPageSizeChange}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              labels={agentPaginationLabels()}
              ariaLabel={t("feedback.pagerLabel")}
              testId="feedback-pagination"
            />
          }
          hasSelection={selectedId !== null}
          detail={
            selected ? (
              <FeedbackDetail item={selected} onReviewed={onReviewed} />
            ) : (
              <EmptyState title={t("feedback.detail.notInListTitle")} hint={t("feedback.detail.notInListHint")} />
            )
          }
          emptyDetail={<EmptyState title={t("feedback.detail.emptyTitle")} hint={t("feedback.detail.emptyHint")} />}
          detailToolbar={
            selectedId ? (
              <ListDetailPager
                onPrevious={previousItem ? () => onSelect(previousItem.run_id) : null}
                onNext={nextItem ? () => onSelect(nextItem.run_id) : null}
                labels={{ previous: t("listDetail.previous"), next: t("listDetail.next") }}
                testId="feedback-detail-pager"
              />
            ) : undefined
          }
          labels={{ resize: t("listDetail.resize"), resizeHint: t("listDetail.resizeHint") }}
        />
      )}
    </div>
  );
}

function ReasonCounts({ summary }: { summary: FeedbackSummary }) {
  return (
    <section aria-labelledby="feedback-reasons-heading" className="min-w-0" data-testid="feedback-reasons">
      <h3 id="feedback-reasons-heading" className="mb-3 text-sm font-semibold text-fg">
        {t("feedback.reasons.title")}
      </h3>
      {summary.reason_counts.length ? (
        <div className="space-y-2">
          {summary.reason_counts.map((item) => {
            const ratio = summary.not_helpful ? item.count / summary.not_helpful : 0;
            return (
              <div key={item.reason} className="grid grid-cols-[minmax(8rem,1fr)_minmax(4rem,1fr)_auto] items-center gap-2 text-xs">
                <span className="truncate text-fg" title={reasonLabel(item.reason)}>
                  {reasonLabel(item.reason)}
                </span>
                <span data-surface-tint="neutral" className="h-1.5 overflow-hidden rounded-full bg-surface-hover" aria-hidden>
                  <span className="block h-full rounded-full bg-danger-emphasis" style={{ width: `${Math.round(ratio * 100)}%` }} />
                </span>
                <span className="w-20 text-right tabular-nums text-fg-muted">
                  {formatNumber(item.count)} / {formatRate(ratio)}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="text-sm text-fg-muted">{t("feedback.reasons.empty")}</p>
      )}
    </section>
  );
}

/**
 * 一覧の列（ListDetailLayout の狭い列。#2283）の行。質問（2 行まで）と補足 1 行（本人の評価・管理者の評価・評価日時）
 * だけにし、理由・業務 Agent・利用者は詳細で見る（page-archetypes.md §1 B「一覧の行」）。
 */
function FeedbackList({
  items,
  selectedId,
  onSelect,
}: {
  items: FeedbackItem[];
  selectedId: string | null;
  onSelect: (runId: string) => void;
}) {
  const columns: DataTableColumn<FeedbackItem>[] = [
    {
      key: "question",
      header: t("feedback.column.question"),
      rowHeader: true,
      className: "py-2.5 align-top",
      render: (item) => (
        <RowTitleButton
          title={item.question}
          maxLines={2}
          subtitle={
            <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
              {item.feedback ? <RatingBadge rating={item.feedback.rating} /> : null}
              {item.admin_review ? (
                <StatusBadge
                  variant={item.admin_review.rating === "helpful" ? "success" : "danger"}
                  label={t("feedback.list.adminBadge", { rating: ratingLabel(item.admin_review.rating) })}
                />
              ) : null}
              <span className="tabular-nums">{formatDateTime(item.updated_at)}</span>
            </span>
          }
          current={item.run_id === selectedId}
          aria-label={t("feedback.list.selectNamed", { name: item.question })}
          onClick={() => onSelect(item.run_id)}
        />
      ),
    },
  ];
  return (
    <DataTable<FeedbackItem>
      rows={items}
      columns={columns}
      getRowKey={(item) => item.run_id}
      onRowClick={(item) => onSelect(item.run_id)}
      selectedRowKey={selectedId}
      rowProps={(item) => ({ className: INFORMATION_TABLE_ROW_CLASS, "data-testid": `feedback-row-${item.run_id}` })}
      ariaLabel={t("feedback.list.label")}
      scrollAriaLabel={listScrollLabel(t("feedback.list.label"))}
      stickyHeader
      // 一覧の列の高さの中で縦にスクロールし、表頭を固定する。列の枠と二重にしない。
      className="min-h-0 rounded-none border-0"
      tableClassName="w-full table-fixed"
    />
  );
}

function RatingBadge({ rating }: { rating: FeedbackRating }) {
  return (
    <StatusBadge variant={rating === "helpful" ? "success" : "danger"} label={ratingLabel(rating)} />
  );
}

/** 役に立たなかった回答（本人の 👎 か管理者の評価）。評価ケースに追加できる（#810）。 */
function notHelpful(item: FeedbackItem): boolean {
  return item.feedback?.rating === "not_helpful" || item.admin_review?.rating === "not_helpful";
}

function FeedbackDetail({ item, onReviewed }: { item: FeedbackItem; onReviewed: (run: RunState) => void }) {
  const capabilities = useCapabilities();
  const canAddCase = useCanEditEvaluationSets();
  return (
    <div className="space-y-4 text-sm" data-testid="feedback-detail">
      <h2 className="text-base font-semibold text-fg">{t("feedback.detail.title")}</h2>
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
        <dt className="text-fg-muted">{t("feedback.column.agent")}</dt>
        <dd className="break-words text-fg">{item.agent_name || t("feedback.agentDeleted")}</dd>
        <dt className="text-fg-muted">{t("feedback.column.user")}</dt>
        <dd className="break-words text-fg">{item.display_name || t("feedback.userUnknown")}</dd>
        <dt className="text-fg-muted">{t("feedback.column.ratedAt")}</dt>
        <dd className="tabular-nums text-fg">{formatDateTime(item.updated_at)}</dd>
        <dt className="text-fg-muted">{t("feedback.column.reason")}</dt>
        <dd className="break-words text-fg">{item.feedback?.reason ? reasonLabel(item.feedback.reason) : "—"}</dd>
        <dt className="text-fg-muted">{t("feedback.detail.run")}</dt>
        <dd className="break-all font-mono text-fg">{item.run_id}</dd>
      </dl>
      <section className="space-y-1">
        <h3 className="text-xs font-semibold text-fg-muted">{t("feedback.detail.question")}</h3>
        <p data-surface-tint="accent" className="whitespace-pre-wrap break-words rounded-md bg-accent-subtle px-3 py-2 text-fg">{item.question}</p>
      </section>
      <section className="space-y-1">
        <h3 className="text-xs font-semibold text-fg-muted">{t("feedback.detail.answer")}</h3>
        <div className="rounded-md border border-border p-3">
          <AnswerBody text={item.answer} className="text-sm text-fg" />
        </div>
      </section>
      <RatingSection title={t("feedback.detail.ownerRating")} rating={item.feedback} testId="feedback-detail-owner" />
      <RatingSection
        title={t("feedback.detail.adminReview")}
        rating={item.admin_review}
        by={item.reviewer_display_name}
        testId="feedback-detail-admin"
      />
      {/* 管理者の評価は Agent 管理の権限だけ（本人の評価とは別に残す。#774）。 */}
      {capabilities.admin ? (
        <AnswerFeedback runId={item.run_id} current={item.admin_review} mode="admin" onSaved={onReviewed} />
      ) : null}
      {/* 役に立たなかった回答を品質評価の評価ケースにする（品質評価の権限。#810）。 */}
      {canAddCase && notHelpful(item) ? (
        <AddToEvaluationCase key={item.run_id} runId={item.run_id} testId="feedback-add-case" />
      ) : null}
    </div>
  );
}

function RatingSection({
  title,
  rating,
  by,
  testId,
}: {
  title: string;
  rating: RunFeedback | null;
  by?: string;
  testId: string;
}) {
  return (
    <section className="space-y-1" data-testid={testId}>
      <h3 className="text-xs font-semibold text-fg-muted">{title}</h3>
      {rating ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <RatingBadge rating={rating.rating} />
            {rating.reason ? <span className="text-fg">{reasonLabel(rating.reason)}</span> : null}
            {by ? <span className="text-xs text-fg-muted">{by}</span> : null}
          </div>
          {rating.comment ? <p className="whitespace-pre-wrap break-words text-fg">{rating.comment}</p> : null}
        </>
      ) : (
        <p className="text-fg-muted">{t("feedback.detail.notRated")}</p>
      )}
    </section>
  );
}
