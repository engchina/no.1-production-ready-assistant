import { useWorkspaceState } from "@/components/WorkspaceState";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import { RefreshCw } from "lucide-react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import {
  ApiErrorBanner,
  Button,
  ButtonLink,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  OffsetPagination,
  PageBody,
  PageHeader,
  ProcessingIndicator,
  RunHistoryPeriodEmptyState,
  RunHistoryTable,
  RunHistoryToolbar,
  StatusBadge,
  TableSkeleton,
  TimedLoadingState,
  toast,
  type RunHistoryItem,
} from "@production-ready/ui";
import { useAuth } from "@/features/security/AuthProvider";
import { canOpenRoute } from "@/features/security/route-permissions";

import { ApiError, apiGet, isAbortError } from "@/lib/api";
import { appPath } from "@/lib/base-path";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { listCountLabels, listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { APP_ROUTES } from "@/lib/routes";
import { useRequestScope } from "@/lib/useRequestScope";
import { useWorkspacePaging } from "@/lib/use-workspace-paging";
import { DbManagementSearchField } from "../components/DbObjectManagementShared";
import { HistoryRunDetail } from "../components/HistoryRunDetail";
import { feedbackRatingLabel } from "../feedbackLabels";
import {
  DEFAULT_HISTORY_FILTERS,
  DEFAULT_HISTORY_KIND,
  HISTORY_KIND_FILTERS,
  HISTORY_KINDS,
  HISTORY_RUN_STATUSES,
  isCancelledHistory,
  hasActiveHistoryFilters,
  hasActiveNonPeriodHistoryFilters,
  isHistoryListNarrowed,
  historyDetailPath,
  historyExecutor,
  historyKindDisplay,
  historyKindLabel,
  historyListUsage,
  historyRequestUrl,
  historyRunStatus,
  normalizeHistoryFilters,
  type HistoryFilters,
  type HistoryKindFilter,
} from "../historyManagementState";
import { profileRecordDisplayLabel } from "../profileDisplay";
import type { HistoryData, HistoryItem } from "../types";

/*
 * 実行履歴（#1955。UX 契約 run-history.md）。一覧（`RunHistoryToolbar` → `RunHistoryTable` → `OffsetPagination`）と、
 * `?id=<run_id>` の全幅の詳細（`HistoryRunDetail`）。詳細を開いている間も一覧は消さずに隠し、「一覧へ戻る」・
 * ブラウザの戻るで、絞り込み・ページ・スクロールの位置・開いていた行（`aria-current`）のまま戻る。
 */

// 種類の選択肢は Agent の実行履歴と同じ並び（品質評価以外〔既定〕→ すべて → 各種類〔品質評価は最後〕。#1986）。
const KIND_FILTER_OPTIONS = (): { value: HistoryKindFilter; label: string }[] => [
  { value: DEFAULT_HISTORY_KIND, label: t("history.filter.kind.notEvaluation") },
  { value: "all", label: t("history.filter.all") },
  ...HISTORY_KINDS.map((kind) => ({ value: kind, label: historyKindLabel(kind) })),
];
const isHistoryKindFilter = (value: string): value is HistoryKindFilter =>
  (HISTORY_KIND_FILTERS as readonly string[]).includes(value);

function toRunItem(item: HistoryItem): RunHistoryItem {
  return {
    id: item.id,
    title: item.question,
    target: profileRecordDisplayLabel(item) || null,
    href: appPath(historyDetailPath(APP_ROUTES.history, item.id)),
    status: historyRunStatus(item),
    kind: historyKindDisplay(item),
    executor: historyExecutor(item),
    startedAt: item.created_at,
    durationMs: item.elapsed_ms ?? null,
    // 実行ごとのモデルの利用量（合計の token と計測の状態。RAG と同じ規則・文言。#3543）。
    usage: historyListUsage(item),
  };
}

/** 主の領域（`AppShell` の `main`）。一覧と詳細を切り替えるとき、一覧のスクロールの位置を残す。 */
function mainScroller() {
  return typeof document === "undefined" ? null : document.getElementById("pr-main");
}

export function HistoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const openId = searchParams.get("id")?.trim() ?? "";
  // 最後に開いた行（一覧へ戻ったとき `aria-current` と選択の面を付ける）。
  const [lastOpenedId, setLastOpenedId] = useWorkspaceState("selectedId", "");
  const listScrollTop = useRef(0);
  const [previousOpenId, setPreviousOpenId] = useState(openId);
  if (previousOpenId !== openId) {
    setPreviousOpenId(openId);
    // URL で直接開いた・ブラウザで進んだ詳細も、戻ったときの選択の行にする。
    if (openId && openId !== lastOpenedId) setLastOpenedId(openId);
  }
  // 詳細は先頭から、一覧へ戻ったら開く前のスクロールの位置から（一覧は隠しているだけで作り直さない）。
  const mountedOpenId = useRef(openId);
  useLayoutEffect(() => {
    if (mountedOpenId.current === openId) return;
    mountedOpenId.current = openId;
    const main = mainScroller();
    if (main) main.scrollTop = openId ? 0 : listScrollTop.current;
  }, [openId]);

  const openRun = (item: HistoryItem) => {
    listScrollTop.current = mainScroller()?.scrollTop ?? 0;
    setLastOpenedId(item.id);
    const next = new URLSearchParams(searchParams);
    next.set("id", item.id);
    // 一覧から開いたことを残し、「一覧へ戻る」はブラウザの戻ると同じにする（履歴を積み増さない）。
    setSearchParams(next, { state: { fromHistoryList: true } });
  };
  const backToList = () => {
    if ((location.state as { fromHistoryList?: boolean } | null)?.fromHistoryList) {
      navigate(-1);
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("id");
    setSearchParams(next, { replace: true });
  };

  return (
    <>
      <div hidden={Boolean(openId)} data-testid="history-list-view">
        <HistoryList active={!openId} selectedRunId={lastOpenedId || null} onOpen={openRun} />
      </div>
      {openId ? <HistoryRunDetail key={openId} historyId={openId} onBack={backToList} /> : null}
    </>
  );
}

function HistoryList({
  active,
  selectedRunId,
  onOpen,
}: {
  active: boolean;
  selectedRunId: string | null;
  onOpen: (item: HistoryItem) => void;
}) {
  const { hasPermission } = useAuth();
  // 空の状態の「SQL 生成を開く」は、SQL 生成の画面を開ける利用者だけ（#912）。
  const canOpenQuery = canOpenRoute(APP_ROUTES.query, hasPermission);
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  // 「最新情報を取得」が始めた読込か（スピナーは押したボタンだけが出す。#819）。条件・ページの変更は一覧の処理中の表示。
  const [loadOrigin, setLoadOrigin] = useState<"header" | "retry" | null>("header");
  const [filtersSeen, setFiltersSeen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [storedFilters, setStoredFilters] = useWorkspaceState<HistoryFilters>("filters", DEFAULT_HISTORY_FILTERS);
  const filters = normalizeHistoryFilters(storedFilters);
  // ページ番号と表示件数は作業状態に残し、再読込・画面の移動のあとも同じページに戻る（#1764 / #1891）。
  const pages = useWorkspacePaging("listPaging");
  const { offset: pageOffset, pageSize } = pages;
  // 表示している一覧の取得条件（取得に成功したときだけ入る）。
  const [loadedRequest, setLoadedRequest] = useState("");
  const [total, setTotal] = useState(0);
  const [unfilteredTotal, setUnfilteredTotal] = useState(0);
  const [hiddenEvaluationTotal, setHiddenEvaluationTotal] = useState(0);
  const [othersVisible, setOthersVisible] = useState(false);
  const loadSequence = useRef(0);
  const requestSignature = JSON.stringify([filters, pageOffset, pageSize]);
  const { abortAll, run: runScopedRequest } = useRequestScope();

  // 今の条件のページを取り直す。state の更新は応答の callback の中だけで行う（effect からも呼ぶため）。
  const fetchHistory = (announce: boolean) => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    return runScopedRequest(async (signal) => {
      const data = await apiGet<HistoryData>(historyRequestUrl({ offset: pageOffset, limit: pageSize, filters }), {
        signal,
      });
      if (signal.aborted || sequence !== loadSequence.current) return;
      if (pageOffset > 0 && data.items.length === 0 && data.total > 0) {
        // 復元したページが無くなった（履歴が減った）。空のページを出さず、最後のページを取り直す。
        pages.setPage(Math.max(1, Math.ceil(data.total / pageSize)));
        return;
      }
      setItems(data.items);
      setLoadedRequest(requestSignature);
      setTotal(data.total);
      setUnfilteredTotal(data.unfiltered_total ?? data.total);
      setHiddenEvaluationTotal(data.hidden_evaluation_total ?? 0);
      setOthersVisible(Boolean(data.others_visible));
      setError(null);
    })
      .then(() => {
        if (announce && sequence === loadSequence.current) toast.success(t("common.action.refreshed"));
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || sequence !== loadSequence.current) return;
        if (pageOffset > 0 && err instanceof ApiError && err.status === 422) {
          // 復元したページの位置を読めない（範囲の外の件数など）。先頭のページから取り直す。
          pages.reset();
          return;
        }
        setError(err);
      })
      .finally(() => {
        if (sequence === loadSequence.current) setLoading(false);
      });
  };

  const load = async (announce: boolean, origin: "header" | "retry") => {
    setLoading(true);
    setLoadOrigin(origin);
    await fetchHistory(announce);
  };

  // 条件・ページが変わったレンダーで読み込み中の表示にし（effect で setState しない）、取得は effect で行う。
  const fetchHistoryRef = useRef(fetchHistory);
  useLayoutEffect(() => {
    fetchHistoryRef.current = fetchHistory;
  });
  const requestChanged = useValuesChanged([requestSignature]);
  if (requestChanged) {
    if (filtersSeen) setLoadOrigin(null);
    else setFiltersSeen(true);
    setLoading(true);
  }
  useEffect(() => {
    void fetchHistoryRef.current(false);
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [requestSignature, abortAll]);

  const filtered = hasActiveHistoryFilters(filters);
  // 件数の「N 件（全 M 件）」は既定の過去 7 日でも出す（7 日より前の実行があることを伝える。#3386）。
  const narrowed = isHistoryListNarrowed(filters);
  // 期間だけで絞り、その中に実行が無くそれより前にはある（期間を広げる操作を出す。#3386）。
  const periodOnlyEmpty = !hasActiveNonPeriodHistoryFilters(filters) && filters.period !== "all" && unfilteredTotal > 0;
  // スケルトンで覆うのは、まだ 1 度も一覧を取得できていない初回の読み込みだけ。条件の変更後は前の一覧と
  // 絞り込みの欄を出したまま処理中を示す（入力中の検索欄のフォーカスを失わせない。#739）。
  const initialLoading = loading && !loadedRequest;
  // 今の条件の結果をまだ取得できていない失敗（前の条件の一覧を今の条件の結果として出さない。#912）。
  const listUnavailable = !loading && error !== null && loadedRequest !== requestSignature;
  // 既定で隠している品質評価の実行だけがあるときは「まだ実行がありません」にしない（絞り込みで出せるよう、
  // ツールバーを出したまま表の空の状態で隠していることを伝える。#1986）。
  const evaluationOnly = !filtered && hiddenEvaluationTotal > 0;
  const trulyEmpty =
    !loading && !error && loadedRequest !== "" && unfilteredTotal === 0 && items.length === 0 && !evaluationOnly;
  const rows = listUnavailable ? [] : items;

  // 条件を変えたら 1 ページ目へ戻す（ページの位置は条件ごとに意味が変わる）。
  const updateFilters = (patch: Partial<HistoryFilters>) => {
    const next = { ...filters, ...patch };
    if (JSON.stringify(next) === JSON.stringify(filters)) return;
    pages.reset();
    setStoredFilters(next);
  };
  const clearFilters = () => {
    pages.reset();
    setStoredFilters(DEFAULT_HISTORY_FILTERS);
  };

  const retryButton = (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      icon={RefreshCw}
      loading={loading && loadOrigin === "retry"}
      disabled={loading && loadOrigin !== "retry"}
      onClick={() => void load(false, "retry")}
    >
      {t("common.retry")}
    </Button>
  );

  return (
    <>
      <PageHeader
        wide
        title={t("nav.history")}
        subtitle={t("history.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => void load(true, "header"),
            loading: active && loading && loadOrigin === "header",
            disabled: loading && loadOrigin !== "header",
          },
        ]}
      />
      <PageBody wide>
        <Card className="min-w-0" data-testid="history-list-card">
          <CardHeader>
            <CardTitle>{t("history.list.title")}</CardTitle>
          </CardHeader>
          <CardContent className="grid min-w-0 gap-3">
            {initialLoading ? (
              <TimedLoadingState
                label={t("history.list.loading")}
                operationKey="history-list-load"
                placement="panel"
                testId="history-list-loading"
                framed={false}
                // 初回の読込は PageHeader の「最新情報を取得」の loading がスピナーを出す（同じ処理のスピナーは 1 つ。#416）。
                activityIcon="none"
              >
                <TableSkeleton columns={4} testId="history-list-skeleton" />
              </TimedLoadingState>
            ) : trulyEmpty ? (
              <div data-testid="history-empty">
                <EmptyState
                  title={t("history.empty.title")}
                  hint={t("history.empty.hint")}
                  action={
                    canOpenQuery ? (
                      <ButtonLink to={APP_ROUTES.query} linkComponent={Link} variant="secondary" size="sm">
                        {t("history.empty.action")}
                      </ButtonLink>
                    ) : undefined
                  }
                />
              </div>
            ) : (
              <>
                <RunHistoryToolbar
                  testId="history-toolbar"
                  idPrefix="history-filter"
                  search={
                    <DbManagementSearchField
                      label={t("history.search.label")}
                      placeholder={t("history.search.placeholder")}
                      value={filters.search}
                      onChange={(search) => updateFilters({ search })}
                      testId="history-search"
                    />
                  }
                  searchActive={Boolean(filters.search.trim())}
                  status={{
                    value: filters.status,
                    options: HISTORY_RUN_STATUSES,
                    onChange: (status) => updateFilters({ status }),
                  }}
                  // 種類の既定は「品質評価以外」（Agent の品質評価の Run が MCP で呼んだ実行を既定で隠す。#1986）。
                  kind={{
                    value: filters.kind,
                    defaultValue: DEFAULT_HISTORY_KIND,
                    options: KIND_FILTER_OPTIONS(),
                    onChange: (kind) => updateFilters({ kind: isHistoryKindFilter(kind) ? kind : DEFAULT_HISTORY_KIND }),
                  }}
                  period={{ value: filters.period, onChange: (period) => updateFilters({ period }) }}
                  // 実行者は他の人の実行が見える利用者だけ（#1954）。絞り込み中は戻せるように出したままにする。
                  executor={
                    othersVisible || filters.executor !== "all"
                      ? { value: filters.executor, onChange: (executor) => updateFilters({ executor }) }
                      : undefined
                  }
                  onClearFilters={clearFilters}
                  count={
                    listUnavailable
                      ? undefined
                      : {
                          total: unfilteredTotal,
                          matched: narrowed ? total : undefined,
                          labels: listCountLabels(),
                          testId: "history-list-count",
                        }
                  }
                  labels={{ all: t("history.filter.all"), toolbar: listToolbarLabels() }}
                />
                {loading && !initialLoading ? (
                  <ProcessingIndicator
                    active
                    label={t("common.processing.refreshing")}
                    operationKey="history-refresh"
                    placement="workspace"
                    testId="history-workspace-processing"
                    // 条件の変更による取り直しはボタンが回らないため、この表示がスピナーを出す（#819）。
                    activityIcon={loadOrigin ? "none" : "spinner"}
                  />
                ) : null}
                {error !== null && !loading ? (
                  <ApiErrorBanner
                    error={error}
                    fallback={t("history.error.load")}
                    action={retryButton}
                    testId="history-list-error"
                  />
                ) : null}
                <RunHistoryTable
                  testId="history-table"
                  rows={rows}
                  toItem={toRunItem}
                  onOpen={onOpen}
                  showExecutor={othersVisible}
                  // 実行ごとのモデルの利用量（合計の token。#3543）。見出しは RAG・Agent と同じ「token」。
                  showUsage
                  selectedRunId={selectedRunId}
                  // 再実行は一覧の行にも置かない（3 製品の実行の詳細・一覧を RAG にそろえる。#2343）。
                  extraColumns={[
                    {
                      key: "rating",
                      header: t("history.column.rating"),
                      className: "whitespace-nowrap",
                      render: (item) =>
                        // 停止の行（#3348）は評価できない。
                        isCancelledHistory(item) ? (
                          <span className="text-fg-muted">—</span>
                        ) : item.feedback_rating ? (
                          <StatusBadge
                            variant={item.feedback_rating === "good" ? "success" : "warning"}
                            label={feedbackRatingLabel(item.feedback_rating)}
                          />
                        ) : (
                          <span className="text-fg-muted">{feedbackRatingLabel(null)}</span>
                        ),
                    },
                    {
                      key: "rows",
                      header: t("history.column.rows"),
                      align: "right",
                      className: "whitespace-nowrap tnum text-fg-muted",
                      render: (item) =>
                        item.generation_only || !item.safety_is_safe || isCancelledHistory(item)
                          ? t("history.notExecuted")
                          : formatNumber(item.result_row_count),
                    },
                  ]}
                  labels={{
                    table: t("history.list.title"),
                    columns: { usage: t("history.column.usage") },
                    self: t("history.self"),
                    openDetail: (title) => t("history.openDetail", { title }),
                    rowActions: (title) => t("history.rowActions", { title }),
                  }}
                  empty={
                    listUnavailable ? undefined : periodOnlyEmpty && filters.period !== "all" ? (
                      <RunHistoryPeriodEmptyState
                        period={filters.period}
                        onShowAll={() => updateFilters({ period: "all" })}
                        testId="history-period-empty"
                      />
                    ) : evaluationOnly ? (
                      // 品質評価の実行だけがある: 既定で隠していることと出し方を伝える（Agent と同じ。#1986）。
                      <EmptyState
                        title={t("history.empty.title")}
                        hint={t("history.empty.evaluationHidden", { count: formatNumber(hiddenEvaluationTotal) })}
                      />
                    ) : (
                      <EmptyState
                        title={t("history.noResults.title")}
                        hint={t("history.noResults.hint")}
                        action={
                          filtered ? (
                            <Button type="button" variant="secondary" size="sm" onClick={clearFilters}>
                              {t("history.action.clearFilters")}
                            </Button>
                          ) : undefined
                        }
                      />
                    )
                  }
                />
                {/* ページ送りは表の直下の共通の OffsetPagination（1 ページなら出さない。#1764 / #1891）。 */}
                {listUnavailable ? null : (
                  <OffsetPagination
                    offset={pageOffset}
                    limit={pageSize}
                    total={total}
                    count={rows.length}
                    onPageChange={pages.setPage}
                    onPageSizeChange={pages.setPageSize}
                    disabled={loading}
                    labels={paginationLabels()}
                    ariaLabel={t("history.pagination.label")}
                    testId="history-pagination"
                  />
                )}
              </>
            )}
          </CardContent>
        </Card>
      </PageBody>
    </>
  );
}
