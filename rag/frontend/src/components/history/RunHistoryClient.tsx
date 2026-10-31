"use client";

/**
 * 実行履歴（`/history`・`/history/<run_id>`。#1890・#1955）。チャットと RAG 検索の実行を一覧し、全幅の詳細で
 * 回答・根拠・資料との照合・処理の経過を確かめる。
 *
 * - 型は 3 製品で共通（UX 契約 run-history.md）。一覧は `RunHistoryToolbar`（検索 → 状態 → 種類 → 期間 → 実行者）
 *   → `RunHistoryTable` → `OffsetPagination`、詳細は `RunDetailLayout`（概要 → 処理の経過 → 回答 → 根拠 →
 *   フィードバック → 技術的な詳細）を縦に 1 枚で並べる。RAG が持つのはデータの取得と節の中身（回答・根拠）だけ。
 * - 詳細は URL の run_id を唯一の情報源にし、戻ると一覧の検索・絞り込み・ページ・開いた行を保つ（workspace-state.md）。
 * - 見え方は backend が決める（system_admin は全員。それ以外は自分が実行した実行と、自分の会話で
 *   他の人〔system_admin〕が実行した実行。#1889・#1954）。「実行者」の列と絞り込みは、他の人の実行が
 *   見える利用者（system_admin と、他の人が実行した行を持つ利用者）だけに出す。
 */

import {
  AnswerNotice,
  Banner,
  ProcessFailureNotice,
  ButtonLink,
  OpenConversationAction,
  OpenRunInputAction,
  type DataTableColumn,
  DEFAULT_PAGE_SIZE,
  DEFAULT_RUN_PERIOD,
  DataTable,
  InfoTip,
  OffsetPagination,
  PageBody,
  PageHeader,
  RUN_EXECUTOR_FILTERS,
  RUN_PERIODS,
  RunDetailLayout,
  RunDetailSection,
  RunDetailSkeleton,
  Skeleton,
  type RunExecutorFilter,
  type RunFact,
  type RunHistoryItem,
  type RunPeriod,
  type RunStatusFilter,
  RunHistoryPeriodEmptyState,
  RunHistoryTable,
  RunHistoryToolbar,
  RunSummary as RunSummaryCard,
  RunTimelineSection,
  SearchField,
  StatusBadge,
  TableSkeleton,
  TimedLoadingState,
  ClearActionButton,
  Card,
  formatRunDateTime,
  useActionPending,
  useTraceDetailsLabels,
} from "@production-ready/ui";
import { ExternalLink, RefreshCw } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "@/components/security/AuthProvider";
import { ApiErrorState, EmptyState } from "@/components/StateViews";
import { AnswerDetailsPanel } from "@/components/search/AnswerDetailsPanel";
import { AnswerText } from "@/components/search/AnswerText";
import { AnswerCitations, CitationList } from "@/components/search/CitationList";
import { api, type RunDetail, type RunFeedback, type RunSummary } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { isPageSize, listCountProps } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { failedStageLabel, processFailureLabels, runFailure, storageFailureNotice } from "@/lib/process-failure";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { useListTotal, useRunHistory, useRunHistoryDetail } from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { useRagTraceProgress } from "@/lib/trace-progress";
import { useWorkspaceState } from "@/lib/workspace-state";

import {
  RUN_STATUS_OPTIONS,
  RUN_SURFACE_FILTERS,
  runConversationOwner,
  runExecutorLabel,
  runExecutorParam,
  runListUsage,
  runOutcomeView,
  runProfileLabel,
  runSearchHref,
  runStartedAt,
  runStatusLabels,
  runStatusParam,
  runSurfaceLabel,
  runTimeline,
  runUsageView,
  runVerificationView,
  toRunStatus,
  type RunSurfaceFilter,
} from "./run-history-view";

const QUERY_MAX_LENGTH = 200;

interface RunHistoryView {
  q: string;
  status: RunStatusFilter;
  surface: RunSurfaceFilter;
  period: RunPeriod;
  /** 実行者の絞り込み（#1954。既定はすべて）。 */
  executor: RunExecutorFilter;
  offset: number;
  /** 1 ページの件数（表示件数。#1891）。 */
  limit: number;
  /** 最後に詳細を開いた実行（戻ったときに行に印を付ける）。 */
  selected: string | null;
}

const INITIAL_VIEW: RunHistoryView = {
  q: "",
  status: "all",
  surface: "all",
  // 期間の既定は 3 製品で共通の過去 7 日（#3386）。明示に選んだ「すべて」は作業状態に残す。
  period: DEFAULT_RUN_PERIOD,
  executor: "all",
  offset: 0,
  limit: DEFAULT_PAGE_SIZE,
  selected: null,
};

const STATUS_FILTERS: readonly string[] = ["all", ...RUN_STATUS_OPTIONS];

function isRunHistoryView(value: unknown): value is RunHistoryView {
  const view = value as RunHistoryView;
  return (
    typeof view === "object" &&
    view !== null &&
    typeof view.q === "string" &&
    STATUS_FILTERS.includes(view.status) &&
    (RUN_SURFACE_FILTERS as readonly string[]).includes(view.surface) &&
    (RUN_PERIODS as readonly string[]).includes(view.period) &&
    (RUN_EXECUTOR_FILTERS as readonly string[]).includes(view.executor) &&
    Number.isInteger(view.offset) &&
    view.offset >= 0 &&
    isPageSize(view.limit) &&
    (view.selected === null || typeof view.selected === "string")
  );
}

export function runDetailPath(runId: string): string {
  return `${APP_ROUTES.history}/${encodeURIComponent(runId)}`;
}

/** 一覧の行 → 共通の行の形。 */
function toItem(run: RunSummary): RunHistoryItem {
  return {
    id: run.run_id,
    title: run.question || t("history.question.withheld"),
    target: runProfileLabel(run),
    href: runDetailPath(run.run_id),
    status: toRunStatus(run.status),
    kind: runSurfaceLabel(run.surface),
    executor: { name: runExecutorLabel(run), isSelf: run.executed_by_me },
    startedAt: runStartedAt(run),
    durationMs: run.elapsed_ms,
    usage: runListUsage(run),
  };
}

/** 一覧（`/history`）。 */
export function RunHistoryClient() {
  const navigate = useNavigate();
  const seesAllUsers = useAuth().user?.is_system_admin === true;
  // 検索語・絞り込み・ページ・開いた行は、詳細へ移って戻っても再読込しても残す（workspace-state.md）。
  const [view, setView] = useWorkspaceState("history.view", INITIAL_VIEW, isRunHistoryView);
  const query = useRunHistory({
    q: view.q || undefined,
    surface: view.surface === "all" ? undefined : view.surface,
    status: runStatusParam(view.status),
    executor: runExecutorParam(view.executor),
    period: view.period,
    limit: view.limit,
    offset: view.offset,
  });
  const manualRefresh = useActionPending();
  const page = query.data;
  const items = page?.items ?? [];
  // 期間のほかの検索語・絞り込みがあるか。
  const otherFiltersActive =
    Boolean(view.q) || view.status !== "all" || view.surface !== "all" || view.executor !== "all";
  // 一覧が全件より狭いか（件数の「N 件（全 M 件）」）。既定の過去 7 日も期間で絞っているので数える（#3386）。
  const filtered = otherFiltersActive || view.period !== "all";
  // 件数の「N 件（全 M 件）」の M（条件を外した、見える範囲の全件）。絞り込み中だけ 1 件ぶんの取得で数える（#1891）。
  const allTotal = useListTotal(
    "run-history",
    {},
    async () => (await api.listRuns({ limit: 1, offset: 0 })).total,
    filtered
  );
  // 一般の利用者に、他の人が実行した行（自分の会話で system_admin が実行した行）があるか（#1954）。
  // あるときだけ「実行者」の列と絞り込みを出す（system_admin は常に出す）。
  const othersTotal = useListTotal(
    "run-history",
    { executor: "others" },
    async () => (await api.listRuns({ limit: 1, offset: 0, executor: "others" })).total,
    !seesAllUsers
  );
  const showExecutor = seesAllUsers || view.executor !== "all" || (othersTotal.data ?? 0) > 0;
  // 条件を変えたら 1 ページ目へ戻す（変わらない Enter・blur ではページを保つ）。
  const update = (patch: Partial<Pick<RunHistoryView, "q" | "status" | "surface" | "period" | "executor">>) =>
    setView((current) => {
      const next = { ...current, ...patch };
      const changed =
        next.q !== current.q ||
        next.status !== current.status ||
        next.surface !== current.surface ||
        next.period !== current.period ||
        next.executor !== current.executor;
      return changed ? { ...next, offset: 0 } : current;
    });
  const clearFilters = () =>
    setView((current) => ({ ...INITIAL_VIEW, limit: current.limit, selected: current.selected }));
  const open = (run: RunSummary) => {
    setView((current) => ({ ...current, selected: run.run_id }));
    navigate(runDetailPath(run.run_id));
  };
  const statusLabels = runStatusLabels();

  return (
    <div>
      <PageHeader
        wide
        title={t("nav.history")}
        subtitle={t("history.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("history.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () => void manualRefresh.track(() => query.refetch()),
          },
        ]}
      />
      <PageBody wide className="grid grid-cols-1 gap-5">
        <p className="text-sm text-fg-muted" data-testid="run-history-scope">
          {seesAllUsers ? t("history.scope.all") : t("history.scope.own")}{" "}
          {t("history.retention")}
        </p>
        {/* 一覧のツールバー: 検索 → 状態 → 種類 → 期間 → 実行者（run-history.md「一覧」。#1933 の FilterSelect）。 */}
        <RunHistoryToolbar
          search={
            <SearchField
              id="run-history-search"
              label={t("history.search.label")}
              labelHidden
              value={view.q}
              onSearch={(value) => update({ q: value })}
              clearLabel={t("common.clearSearch")}
              resultCountLabel={
                page ? t("common.searchResultCount", { count: formatNumber(page.total) }) : ""
              }
              maxLength={QUERY_MAX_LENGTH}
              placeholder={t("history.search.placeholder")}
            />
          }
          searchActive={Boolean(view.q)}
          status={{
            value: view.status,
            onChange: (value) => update({ status: value }),
            options: RUN_STATUS_OPTIONS,
          }}
          kind={{
            value: view.surface,
            onChange: (value) => update({ surface: value as RunSurfaceFilter }),
            options: RUN_SURFACE_FILTERS.map((value) => ({
              value,
              label: value === "all" ? t("history.filter.all") : runSurfaceLabel(value),
            })),
          }}
          period={{ value: view.period, onChange: (value) => update({ period: value }) }}
          executor={showExecutor ? { value: view.executor, onChange: (value) => update({ executor: value }) } : undefined}
          onClearFilters={clearFilters}
          count={listCountProps({ matchedTotal: page?.total, filtered, allTotal: allTotal.data })}
          labels={{ status: statusLabels }}
          testId="run-history-toolbar"
        />

        {query.isError ? (
          <ApiErrorState
            error={query.error}
            fallback={t("history.loadError")}
            onRetry={() => void query.refetch()}
          />
        ) : query.isPending ? (
          <TimedLoadingState
            label={t("history.loading")}
            operationKey="run-history-load"
            testId="run-history-loading"
          >
            <TableSkeleton columns={4} />
          </TimedLoadingState>
        ) : (page?.total ?? 0) > 0 ? (
          <div className="grid gap-2">
            <RunHistoryTable<RunSummary>
              growToViewport
              rows={items}
              toItem={toItem}
              onOpen={open}
              showExecutor={showExecutor}
              // 実行ごとのモデルの利用量（合計の token。#3466）。見出しは Agent と同じ「token」。
              showUsage
              extraColumns={runExtraColumns()}
              selectedRunId={view.selected}
              // 件数が減って保存したページが範囲外になったら OffsetPagination が最後のページへ戻す。
              loading={items.length === 0}
              labels={{
                table: t("history.list.aria"),
                columns: { usage: t("history.col.usage") },
                status: statusLabels,
              }}
              testId="run-history"
            />
            <OffsetPagination
              offset={view.offset}
              limit={view.limit}
              total={page?.total ?? 0}
              count={items.length}
              onPageChange={(_page, nextOffset) =>
                setView((current) => ({ ...current, offset: nextOffset }))
              }
              onPageSizeChange={(nextLimit) =>
                setView((current) => ({ ...current, limit: nextLimit, offset: 0 }))
              }
              labels={paginationLabels()}
              ariaLabel={t("history.paginationLabel")}
              testId="run-history-pagination"
            />
          </div>
        ) : (
          <Card>
            {!otherFiltersActive && view.period !== "all" && allTotal.data !== 0 ? (
              // 期間（既定は過去 7 日）だけで絞り、その中に実行が無くそれより前にはある: 期間を広げる操作を出す（#3386）。
              <RunHistoryPeriodEmptyState
                period={view.period}
                onShowAll={() => update({ period: "all" })}
                testId="run-history-period-empty"
              />
            ) : filtered && allTotal.data !== 0 ? (
              <EmptyState
                title={t("history.noMatch.title")}
                hint={t("history.noMatch.hint")}
                action={<ClearActionButton label={t("history.clearFilters")} onClick={clearFilters} />}
              />
            ) : (
              <EmptyState
                title={t("history.empty.title")}
                hint={seesAllUsers ? t("history.empty.hint") : t("history.empty.ownHint")}
              />
            )}
          </Card>
        )}
      </PageBody>
    </div>
  );
}

/** RAG の列（回答の対応・資料との照合）。所要時間の右に置き、狭い画面では出さない（詳細の概要で見る）。 */
function runExtraColumns(): DataTableColumn<RunSummary>[] {
  return [
    {
      key: "outcome",
      header: t("history.col.outcome"),
      className: "whitespace-nowrap",
      render: (run) => <StatusBadge {...runOutcomeView(run)} />,
    },
    {
      key: "verification",
      header: (
        <span className="inline-flex items-center gap-1">
          {t("history.col.verification")}
          <InfoTip label={t("history.verification.helpLabel")} content={t("history.verification.help")} />
        </span>
      ),
      className: "whitespace-nowrap",
      render: (run) => {
        const view = runVerificationView(run.verification, run.validation_execution);
        return view ? <StatusBadge {...view} /> : <span className="text-fg-muted">{t("history.notRecorded")}</span>;
      },
    },
  ];
}

/** 詳細（`/history/<run_id>`）。 */
export function RunHistoryDetailClient({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const detail = useRunHistoryDetail(runId);
  const manualRefresh = useActionPending();

  return (
    <div>
      <PageHeader
        wide
        title={t("history.detail.title")}
        subtitle={t("history.detail.subtitle")}
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: t("nav.history") }),
          onClick: () => navigate(APP_ROUTES.history),
          testId: "run-history-back",
        }}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("history.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () => void manualRefresh.track(() => detail.refetch()),
          },
        ]}
      />
      <PageBody wide className="grid grid-cols-1 gap-5">
        {detail.isError ? (
          <ApiErrorState
            error={detail.error}
            fallback={t("history.detail.loadError")}
            onRetry={() => void detail.refetch()}
          />
        ) : detail.isPending ? (
          <TimedLoadingState
            label={t("history.detail.loading")}
            operationKey="run-history-detail-load"
            testId="run-history-detail-loading"
          >
            <RunDetailSkeleton />
          </TimedLoadingState>
        ) : (
          <RunDetailView run={detail.data} />
        )}
      </PageBody>
    </div>
  );
}

function RunDetailView({ run }: { run: RunDetail }) {
  const { user, hasPermission } = useAuth();
  const seesAllUsers = user?.is_system_admin === true;
  // 引用箇所のプレビューは文書を読む権限（backend の `_DOCUMENT_VIEW`）がある利用者だけ。
  const canPreview = [
    MENU_PERMISSIONS.upload,
    MENU_PERMISSIONS.fileList,
    MENU_PERMISSIONS.search,
    MENU_PERMISSIONS.chat,
    MENU_PERMISSIONS.knowledgeBases,
  ].some((permission) => hasPermission(permission));
  // 会話を開けるのは会話の持ち主と system_admin だけ（会話の API の見え方。#1889・#1928）。
  // 持ち主の会話で system_admin が実行した行も、持ち主は会話を開ける（#1954）。
  const canOpenConversation = seesAllUsers || run.owned_by_me === true;
  const chatHref =
    canOpenConversation &&
    run.conversation_id &&
    run.search_answer_profile_id &&
    hasPermission(MENU_PERMISSIONS.chat)
      ? `${APP_ROUTES.chat}?${new URLSearchParams({
          search_answer_profile_id: run.search_answer_profile_id,
          conversation_id: run.conversation_id,
        }).toString()}`
      : null;
  // 「RAG 検索で開く」（#3309）: 本人の RAG 検索の実行を、質問と条件を入れて RAG 検索の画面に開く（検索はしない）。
  const searchHref = runSearchHref(run, { canOpenSearch: hasPermission(MENU_PERMISSIONS.search) });
  const verification = runVerificationView(run.verification, run.validation_execution);
  const statusLabels = runStatusLabels();
  const traceLabels = useTraceDetailsLabels();
  const succeeded = run.status === "success";
  // 保存した根拠のうち、今の権限で見られない根拠の数（#3252）。
  const withheld = run.withheld_evidence_count ?? 0;
  // 失敗は 3 製品共通の失敗のカード（チャットと同じ 1 文目・次の操作。#2342）。
  const failure = runFailure(run);

  // モデル・token・モデルの呼び出し（実行のときに記録した値。Agent の実行の詳細と同じ形。#3419）。
  const usageView = runUsageView(run);
  const extraFacts: RunFact[] = [
    { id: "modelCalls", label: t("history.detail.modelCalls"), value: usageView.modelCalls },
    { id: "citations", label: t("history.detail.citations"), value: formatNumber(run.citation_count) },
  ];
  if (run.status === "error") {
    extraFacts.push({
      id: "errorStage",
      label: t("history.detail.errorStage"),
      value: run.error_stage ? failedStageLabel(run.error_stage) : t("history.notRecorded"),
    });
  }

  const storageNotice = storageFailureNotice(run.storage_failures, run.run_id);

  // 処理の経過は共通の span の表の snapshot（実行の trace の API。#3226）。終わった実行なので 1 回だけ読む。
  const trace = useRagTraceProgress({
    key: run.run_id,
    active: false,
    fetchSnapshot: (signal) => api.getRunTrace(run.run_id, signal),
  });
  const timeline = runTimeline(trace);
  // 処理の詳細（#2325）: 回答の記録の詳細と技術的な識別子を、処理の経過のタイムラインの後ろに 1 か所で畳む。
  // チャット・RAG 検索の処理の経過の「処理の詳細」と同じ名前・同じ部品（`RunProcessDetails`）・同じ位置。
  const processDetails = <AnswerDetailsPanel diagnostics={run.answer_diagnostics} recordedModels={run.usage?.models ?? (run.model_id ? [run.model_id] : [])} />;

  return (
    <RunDetailLayout
      testId="run-history-detail"
      summary={
        <RunSummaryCard
          testId="run-history-summary"
          status={toRunStatus(run.status)}
          labels={{ summary: t("history.detail.summary"), status: statusLabels }}
          badges={
            <>
              <StatusBadge {...runOutcomeView(run)} />
              {verification ? <StatusBadge {...verification} /> : null}
            </>
          }
          question={run.question || t("history.question.withheld")}
          startedAt={runStartedAt(run)}
          finishedAt={run.elapsed_ms != null ? run.created_at : null}
          durationMs={run.elapsed_ms}
          kind={runSurfaceLabel(run.surface)}
          executor={{ name: runExecutorLabel(run), isSelf: run.executed_by_me }}
          owner={runConversationOwner(run)}
          target={{ label: t("history.detail.profile"), value: runProfileLabel(run) }}
          model={usageView.model}
          usage={usageView.usage}
          runId={run.run_id}
          extraFacts={extraFacts}
          alert={
            run.status === "blocked" ? (
              <Banner severity="warning">{t("history.result.blocked")}</Banner>
            ) : failure ? (
              <ProcessFailureNotice failure={failure} labels={processFailureLabels()} testId="run-history-failure" />
            ) : storageNotice ? (
              // 回答は作ったが一部を保存できなかった実行（#2342）。
              <ProcessFailureNotice
                failure={storageNotice}
                severity="warning"
                labels={processFailureLabels()}
                testId="run-history-storage-failure"
              />
            ) : null
          }
          actions={
            // 「会話を開く」（チャット。#2343）と「RAG 検索で開く」（RAG 検索。#3309）は 3 製品共通の部品で、
            // 実行のきっかけでどちらか 1 つだけが出る。
            <>
              <OpenConversationAction
                href={chatHref}
                linkComponent={Link}
                labels={{ label: t("history.detail.openChat"), ariaLabel: t("history.detail.openChatLabel") }}
                testId="run-history-open-chat"
              />
              <OpenRunInputAction
                href={searchHref}
                linkComponent={Link}
                labels={{ label: t("history.detail.openSearch"), ariaLabel: t("history.detail.openSearchLabel") }}
                testId="run-history-open-search"
              />
            </>
          }
        />
      }
      answer={
        run.answer_generated && succeeded ? (
          <RunDetailSection title={t("history.result.answer")} testId="run-history-answer">
            {run.answer_available && run.answer ? (
              <div className="space-y-3">
                <AnswerText
                  text={run.answer}
                  citations={canPreview ? run.citations : []}
                  diagnostics={run.answer_diagnostics}
                />
                {/* 回答に付けた注意書き（そのときの文。#2405）。 */}
                <AnswerNotice notice={run.answer_notice} label={t("answer.notice.label")} />
              </div>
            ) : (
              <p className="text-sm text-fg-muted" data-testid="run-history-answer-missing">
                {t("history.result.answerMissing")}
              </p>
            )}
          </RunDetailSection>
        ) : null
      }
      timeline={
        <RunTimelineSection
          traceId={timeline.traceId}
          title={t("history.process.title")}
          steps={timeline.steps}
          totalDurationMs={run.elapsed_ms}
          empty={
            trace.loaded || trace.failed ? (
              <p className="text-sm text-fg-muted" data-testid="run-history-timeline-empty">
                {timeline.legacy ? traceLabels.legacy : t("history.process.empty")}
              </p>
            ) : (
              <Skeleton className="h-5 w-full" data-testid="run-history-timeline-loading" />
            )
          }
          processDetailsProps={{ metadata: { conversationId: run.conversation_id ?? null, messageId: run.message_id ?? null }, title: t("answer.process.details"), testId: "run-history-technical", summaryTestId: "run-history-process-details" }}
          processDetails={processDetails}
          // 表に書けなかった span がある（#3226。3 製品共通の案内を段の一覧の後ろに出す）。
          partial={trace.partial}
          testId="run-history-timeline"
        />
      }
      evidence={
        succeeded ? (
          <RunDetailSection
            title={
              // 回答を作った実行は回答の根拠（件数と内訳はチャットと同じ折りたたみの見出し。#2328）、検索だけの実行は
              // 検索結果の件数（RAG 検索と同じ。#2040）。
              run.answer_generated ? t("history.result.evidence") : `${t("search.results")}（${run.citations.length}）`
            }
            testId="run-history-evidence"
          >
            {(run.citations.length || withheld) && run.answer_generated ? (
              // チャットと同じ部品（「AI に渡した N 件 ／ 回答で引用 M 件」の折りたたみ・一定の高さの中のスクロール）。
              // 今の権限で見られない根拠は数だけ（#3252）。
              <AnswerCitations
                citations={run.citations}
                idPrefix={`run-${run.run_id}`}
                listTestId="run-history-citations"
                cardProps={{ preview: canPreview }}
                withheldCount={withheld}
              />
            ) : run.citations.length || withheld ? (
              <CitationList
                citations={run.citations}
                idPrefix={`run-${run.run_id}`}
                listTestId="run-history-citations"
                cardProps={{ preview: canPreview }}
                withheldCount={withheld}
              />
            ) : (
              <p className="text-sm text-fg-muted">{t("history.result.noCitations")}</p>
            )}
          </RunDetailSection>
        ) : null
      }
      feedback={
        run.feedback.length ? (
          <RunFeedbackSection feedback={run.feedback} canOpen={hasPermission(MENU_PERMISSIONS.feedback)} />
        ) : null
      }
    />
  );
}

function RunFeedbackSection({ feedback, canOpen }: { feedback: RunFeedback[]; canOpen: boolean }) {
  const columns: DataTableColumn<RunFeedback>[] = [
    {
      key: "target",
      header: t("history.feedback.col.target"),
      rowHeader: true,
      render: (item) =>
        item.target_type === "answer"
          ? t("history.feedback.target.answer")
          : t("history.feedback.target.citation"),
    },
    {
      key: "rating",
      header: t("history.feedback.col.rating"),
      render: (item) => (
        <StatusBadge
          variant={item.rating === "helpful" ? "success" : "warning"}
          label={
            item.rating === "helpful" ? t("history.feedback.helpful") : t("history.feedback.notHelpful")
          }
        />
      ),
    },
    {
      key: "createdAt",
      header: t("history.col.createdAt"),
      className: "tnum text-fg-muted",
      render: (item) => formatRunDateTime(item.created_at),
    },
  ];
  if (canOpen) {
    columns.push({
      key: "open",
      header: t("history.feedback.col.actions"),
      align: "right",
      render: (item) => (
        <ButtonLink
          to={`${APP_ROUTES.feedback}?${new URLSearchParams({ feedback: item.feedback_id, period: "all" }).toString()}`}
          linkComponent={Link}
          size="sm"
          variant="ghost"
          trailingIcon={ExternalLink}
          testId={`run-history-feedback-open-${item.feedback_id}`}
        >
          {t("history.feedback.open")}
        </ButtonLink>
      ),
    });
  }
  return (
    <RunDetailSection
      title={t("history.feedback.title")}
      description={t("history.feedback.description")}
      meta={<StatusBadge variant="neutral" label={t("history.feedback.count", { count: formatNumber(feedback.length) })} />}
      testId="run-history-feedback"
    >
      <DataTable
        columns={columns}
        rows={feedback}
        getRowKey={(item) => item.feedback_id}
        ariaLabel={t("history.feedback.title")}
        tableClassName="w-full min-w-[32rem] text-sm"
      />
    </RunDetailSection>
  );
}

