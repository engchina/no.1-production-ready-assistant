import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, PlayCircle, RefreshCw, X } from "lucide-react";
import {
  Banner,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DEFAULT_RUN_PERIOD,
  EmptyState,
  FormActionBar,
  FormStatus,
  FormSkeleton,
  OffsetPagination,
  PAGE_SIZE_OPTIONS,
  RUN_EXECUTOR_FILTERS,
  RUN_PERIODS,
  RUN_STATUSES,
  RunDetailSkeleton,
  RunHistoryPeriodEmptyState,
  RunHistoryTable,
  RunHistoryToolbar,
  TableSkeleton,
  PageHeader,
  InfoTip,
  Switch,
  toast,
  useConfirm,
  type EntityAction,
  type RunExecutorFilter as CommonRunExecutorFilter,
  type RunHistoryItem,
  type RunPeriod,
  type RunStatusFilter as CommonRunStatusFilter,
  PageBody,
  isSubmitEnter,
  runPeriodSince,
  SelectField,
  TextareaField,
  useActionPending,
} from "@production-ready/ui";
import {
  ApiError,
  agentApi,
  type RunListFilters,
  type RunState,
  type RunSummary,
  type RunsPage as RunsPageData,
} from "@/lib/api";
import { MissingEditorTarget } from "@/components/EntityLayout";
import { agentPaginationLabels, listToolbarLabels, QueryState, usePersistedServerPaging } from "@/components/ListViews";
import { ListSearchField, listCount, NoMatchState, useListSearch } from "@/components/ListFilters";
import { t } from "@/lib/i18n";
import { useEditorRoute } from "@/lib/editor-route";
import { useCapabilities } from "@/lib/permissions";
import { runSourceLabel } from "@/lib/run-source";
import {
  AGENT_RUN_KINDS,
  AGENT_RUN_STATUS_OPTIONS,
  DEFAULT_AGENT_RUN_KIND,
  type AgentRunKindFilter,
  agentRunHistoryLabels,
  backendRunExecutor,
  backendRunKind,
  backendRunStatus,
  commonRunStatus,
  runStatusLabel,
} from "@/lib/run-history";
import { useEditorLeaveGuard } from "@/lib/leave-guard";
import { isNullableString, isOneOf, isString, useWorkspaceState } from "@/lib/workspace-state";
import { RunDetail, runCapabilities, runSummaryCapabilities } from "@/pages/runs/RunDetail";
import { type RunCommandKind, useRunEventSource } from "@/pages/runs/run-event-stream";
import { focusField } from "@/pages/shared/page-helpers";
import { isOwnDecision } from "@/pages/shared/approval-decision";
import { useAuth } from "@/components/security/AuthProvider";
import { useViewSwitchFocus } from "@/pages/shared/view-switch-focus";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";

const DEFAULT_RUN_GOAL = t("run.form.goalDefault");

/** 実行履歴の絞り込みの値（共通の型。run-history.md「絞り込み」。#1955）。作業状態に残す。 */
const isRunStatusFilter = isOneOf<CommonRunStatusFilter>(["all", ...RUN_STATUSES]);
const isRunKindFilter = isOneOf<AgentRunKindFilter>([DEFAULT_AGENT_RUN_KIND, "all", ...AGENT_RUN_KINDS]);
const isRunPeriod = isOneOf<RunPeriod>(RUN_PERIODS);
const isRunExecutorFilter = isOneOf<CommonRunExecutorFilter>(RUN_EXECUTOR_FILTERS);

/** 期間の始まり（分の単位に丸める。描画のたびに一覧の取得の key が変わらないように）。 */
function periodSince(period: RunPeriod): string | undefined {
  const minute = 60_000;
  return runPeriodSince(period, new Date(Math.floor(Date.now() / minute) * minute)) ?? undefined;
}

/** 一覧の行（要約）と詳細（RunState）のどちらからでも、行の操作に要る事実を読む（#1897）。 */
type RunRow = RunState | RunSummary;

function runRowFacts(run: RunRow): {
  canCancel: boolean;
  canResume: boolean;
  pendingApproval?: { id: string; toolName: string };
} {
  if ("approvals" in run) {
    const pending = run.approvals.find((approval) => approval.status === "pending");
    return {
      ...runCapabilities(run),
      pendingApproval: pending ? { id: pending.id, toolName: pending.tool_call.name } : undefined,
    };
  }
  const pending = run.pending_approval;
  return {
    ...runSummaryCapabilities(run),
    pendingApproval: pending ? { id: pending.id, toolName: pending.tool_name } : undefined,
  };
}

/** 詳細の実行が無い・読めない（別の実行へ暗黙に置換せず、一覧へ戻す案内を出す）。 */
function isRunGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

export function RunsPage() {
  const editor = useEditorRoute();
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const capabilities = useCapabilities();
  const { user } = useAuth();
  // 実行履歴の絞り込み（質問・実行 ID・業務 Agent の検索と、状態・種類・期間・実行者。#808・#1955）とページは
  // 作業状態に残し、サーバー側で絞り込んでページングする（全 Run を読まない。#1897）。
  const [runQuery, setRunQuery] = useListSearch("runs");
  const [statusFilter, setStatusFilter] = useWorkspaceState(
    "listFilter",
    "runs",
    "all" as CommonRunStatusFilter,
    isRunStatusFilter
  );
  const [kindFilter, setKindFilter] = useWorkspaceState(
    "listFilter",
    "runsKind",
    DEFAULT_AGENT_RUN_KIND as AgentRunKindFilter,
    isRunKindFilter
  );
  // 期間の既定は 3 製品で共通の過去 7 日（#3386）。明示に選んだ「すべて」は作業状態に残す。
  const [periodFilter, setPeriodFilter] = useWorkspaceState(
    "listFilter",
    "runsPeriod",
    DEFAULT_RUN_PERIOD as RunPeriod,
    isRunPeriod
  );
  const [executorFilter, setExecutorFilter] = useWorkspaceState(
    "listFilter",
    "runsExecutor",
    "all" as CommonRunExecutorFilter,
    isRunExecutorFilter
  );
  const runPaging = usePersistedServerPaging("runs");
  const setRunPage = runPaging.setPage;
  const runFilters: RunListFilters = {
    status: backendRunStatus(statusFilter),
    kind: backendRunKind(kindFilter),
    since: periodSince(periodFilter),
    executor: backendRunExecutor(executorFilter),
    q: runQuery.trim() || undefined,
    limit: runPaging.limit,
    offset: runPaging.offset,
  };
  const isList = editor.target.kind === "list";
  const targetRunId = editor.target.kind === "edit" ? editor.target.id : null;
  const runs = useQuery({
    queryKey: ["runs", "list", runFilters],
    queryFn: () => agentApi.listRuns(runFilters),
    // 詳細を開いている間も読んでおき（定期の取り直しはしない）、一覧へ戻ったらすぐ選んだ行へフォーカスを戻す。
    refetchInterval: isList ? 5000 : false,
    // ページ・絞り込みを変えている間は今のページを出したまま取り直す（表を Skeleton に戻さない）。
    placeholderData: keepPreviousData,
  });
  // 詳細は URL の実行 1 件だけを読む（一覧と同じく 5 秒ごとに取り直し、Event の受信でも取り直す）。
  const runDetail = useQuery({
    queryKey: ["runs", "detail", targetRunId],
    queryFn: () => agentApi.getRun(targetRunId ?? ""),
    enabled: targetRunId !== null,
    refetchInterval: 5000,
    retry: (count, error) => !isRunGone(error) && count < 3,
  });
  // 「最新情報を取得」は押した取り直しの間だけ回す（定期の取り直し・他の操作の後の invalidate・条件の切り替えでは回さない。#819）。
  const manualRefresh = useActionPending();
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: agentApi.listAgents,
  });
  // 組み込み Runtime が実行できるか（モデル未設定なら Run は失敗するため、作成の前に知らせる。#754）。
  // 作成できる利用者だけが読む（閲覧だけの利用者には API の権限が無く、403 で権限なしの画面へ移るため。#1113）。
  const runtimeStatus = useQuery({
    queryKey: ["runtime-status"],
    queryFn: agentApi.getRuntimeStatus,
    enabled: capabilities.operateRuns,
  });
  const createRun = useMutation({
    mutationFn: agentApi.createRun,
    onSuccess: (run) => {
      toast.success(t("run.createdToast"));
      setSelectedRunId(run.id);
      queryClient.setQueryData<RunState>(["runs", "detail", run.id], run);
      editor.openItem(run.id, { replace: true });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const refreshRunQueries = () => {
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
  };
  // 行メニュー・詳細の操作で、結果を出す固定の面が無いため、失敗は danger の Toast で返す（messaging.md §1）。
  const cancelRun = useMutation({
    mutationFn: agentApi.cancelRun,
    onSuccess: refreshRunQueries,
    onError: (error) => toast.error(t("run.cancelFailed"), { description: error.message }),
  });
  const resumeRun = useMutation({
    mutationFn: agentApi.resumeRun,
    onSuccess: refreshRunQueries,
    onError: (error) => toast.error(t("run.resumeFailed"), { description: error.message }),
  });
  const decideApproval = useMutation({
    mutationFn: ({ approvalId, approved }: { runId: string; approvalId: string; approved: boolean }) =>
      // 決定者はログイン中の利用者から server が決める（#215）。
      agentApi.decideApproval(approvalId, { approved }),
    // 判断の結果は返却値の承認の状態で確かめる。先に取り消された・他の操作者が判断した承認は、backend が
    // 状態を変えずに 200 で返すため、成功と案内しない（承認の画面と同じ。#877 / #919）。
    onSuccess: (updatedRun, { approvalId, approved }) => {
      // 押した判断が自分の判断として残ったときだけ成功と案内する（#1119）。
      if (isOwnDecision(updatedRun, approvalId, approved, user?.login_user_id)) {
        toast.success(approved ? t("approval.decided") : t("approval.rejected"));
      } else {
        toast.info(t("approval.changedDuringReview"));
      }
      // 返却値を先に反映し、再取得を待つ間の二重判断を防ぐ。
      queryClient.setQueryData<RunState>(["runs", "detail", updatedRun.id], updatedRun);
      queryClient.setQueriesData<RunsPageData>({ queryKey: ["runs", "list"] }, (current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === updatedRun.id
                  ? { ...item, status: updatedRun.status, pending_approval_count: 0, pending_approval: null }
                  : item
              ),
            }
          : current
      );
      refreshRunQueries();
    },
    onError: (error) => toast.error(t("run.decideFailed"), { description: error.message }),
  });
  // 作業状態（目標の下書き・選択中の Run・購読方式）はこのタブの sessionStorage に残す（#87）。
  // Agent は実行条件なので残さず、戻るたびに選び直す（実行の意思は確認し直す）。
  const [goal, setGoal, goalSaved] = useWorkspaceState("runs", "goal", DEFAULT_RUN_GOAL, isString);
  const [agentId, setAgentId] = useState("default");
  const [goalError, setGoalError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useWorkspaceState(
    "runs",
    "selectedRunId",
    null as string | null,
    isNullableString
  );
  // 一時保存に失敗した下書きは、離脱の前に破棄を確認する。
  const { confirmClose } = useEditorLeaveGuard(
    editor.target.kind === "new" && !goalSaved && goal !== DEFAULT_RUN_GOAL,
    createRun.isPending
  );

  const agentNames = useMemo(
    () => new Map((agents.data?.agents ?? []).map((agent) => [agent.id, agent.name])),
    [agents.data?.agents]
  );
  const agentNameOf = (agentId: string) => agentNames.get(agentId) ?? agentId;
  // 検索語・絞り込みを変えたら 1 ページ目へ戻す（5 秒ごとの取り直しではページを戻さない）。
  const changeRunQuery = (value: string) => {
    setRunQuery(value);
    setRunPage(1);
  };
  function changeFilter<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setRunPage(1);
    };
  }
  // 期間のほかの検索語・絞り込みがあるか。
  const otherFiltersActive =
    Boolean(runQuery.trim()) ||
    statusFilter !== "all" ||
    kindFilter !== DEFAULT_AGENT_RUN_KIND ||
    executorFilter !== "all";
  // 既定（期間は過去 7 日）と違う絞り込みがあるか（「条件をクリア」の文言）。
  const filtersActive =
    statusFilter !== "all" ||
    kindFilter !== DEFAULT_AGENT_RUN_KIND ||
    periodFilter !== DEFAULT_RUN_PERIOD ||
    executorFilter !== "all";
  // 一覧が全件より狭いか（件数の「N 件（全 M 件）」）。既定の過去 7 日も期間で絞っているので数える（#3386）。
  const listNarrowed = otherFiltersActive || periodFilter !== "all";
  const clearRunFilters = () => {
    setRunQuery("");
    setStatusFilter("all");
    setKindFilter(DEFAULT_AGENT_RUN_KIND);
    setPeriodFilter(DEFAULT_RUN_PERIOD);
    setExecutorFilter("all");
    setRunPage(1);
  };
  // 詳細の対象は URL の ID。別の実行へ暗黙に置換しない（#875）。
  const viewKind = editor.target.kind;
  const selectedRun = runDetail.data?.id === targetRunId ? runDetail.data : undefined;
  // 利用できるエージェントは backend が絞り込む。既定の Agent を使えない利用者は、使える最初の Agent を選ぶ（#215）。
  // 下書きで実行するのは Agent 管理（admin）だけ（#770）。下書きでなければ公開した版のある Agent だけ選べる。
  const [runDraft, setRunDraft] = useState(false);
  const draftRun = capabilities.admin && runDraft;
  const runnableAgents = (agents.data?.agents ?? []).filter(
    (agent) => agent.enabled && (draftRun || agent.published_version !== null)
  );
  const selectedAgentId =
    runnableAgents.some((agent) => agent.id === agentId) || !runnableAgents.length ? agentId : runnableAgents[0].id;

  useEffect(() => {
    if (targetRunId) {
      setSelectedRunId(targetRunId);
    }
  }, [targetRunId, setSelectedRunId]);
  // URL の切替はページ移動として見出しへ、一覧へ戻ると選んだ行へフォーカスを戻す（開いた直後は動かさない。#1122）。
  useViewSwitchFocus(
    `${viewKind}:${targetRunId ?? ""}`,
    viewKind === "list" && selectedRunId ? `a[data-run-id="${CSS.escape(selectedRunId)}"]` : null
  );

  async function backToList() {
    if (!createRun.isPending && (await confirmClose())) editor.backToList();
  }

  const refreshRuntimeEvents = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
  }, [queryClient]);
  // イベントの push は SSE、操作は REST（#2260）。
  const sseState = useRunEventSource(selectedRun, true, refreshRuntimeEvents);

  function onAgentChange(value: string) {
    setAgentId(value);
  }

  function submitRun() {
    // ゴールは backend（RunCreateRequest）でも必須（#540）。未入力は欄の下に出し、画面の並び順で最初の欄へ移す。
    const nextGoalError = goal.trim() ? null : t("run.goalRequired");
    setGoalError(nextGoalError);
    if (nextGoalError) {
      focusField("run-goal");
      return;
    }
    createRun.mutate({
      goal,
      agent_id: selectedAgentId,
      ...(draftRun ? { draft: true } : {}),
    });
  }

  async function cancelLatestRun(run: RunRow) {
    const confirmed = await confirm({
      title: t("run.cancelTitle"),
      description: run.id,
      confirmLabel: t("run.cancelConfirm"),
      cancelLabel: t("common.cancel"),
      tone: "danger",
    });
    if (!confirmed) return;
    cancelRun.mutate(run.id);
  }

  function resumeLatestRun(run: RunRow) {
    resumeRun.mutate(run.id);
  }

  async function decideRunApproval(run: RunRow, approval: { id: string; toolName: string }, approved: boolean) {
    const ok = await confirm({
      title: approved ? t("run.approveTitle") : t("run.rejectTitle"),
      description: approval.toolName,
      confirmLabel: approved ? t("common.approve") : t("common.reject"),
      cancelLabel: t("common.cancel"),
      tone: approved ? "info" : "danger",
    });
    if (!ok) return;
    // 確認中に状態が変わっていたら（他の操作者の判断・実行の取消）、古い状態で送らない（#919）。
    // 一覧は要約だけを持つため、その実行 1 件を読み直して確かめる（#1897）。
    let latestRun: RunState;
    try {
      latestRun = await queryClient.fetchQuery({
        queryKey: ["runs", "detail", run.id],
        queryFn: () => agentApi.getRun(run.id),
        staleTime: 0,
      });
    } catch (error) {
      toast.error(t("run.decideFailed"), { description: error instanceof Error ? error.message : String(error) });
      return;
    }
    const latest = latestRun.approvals.find((item) => item.id === approval.id);
    if (!latest || latest.status !== "pending") {
      toast.info(t("approval.changedDuringReview"));
      void queryClient.invalidateQueries({ queryKey: ["runs", "list"] });
      return;
    }
    decideApproval.mutate({ runId: run.id, approvalId: latest.id, approved });
  }

  // 操作ごとに pending を分け、押した操作だけを loading、他は disabled にする（messaging.md §3.7）。
  const pendingAction: RunCommandKind | null = cancelRun.isPending
    ? "cancel"
    : resumeRun.isPending
      ? "resume"
      : decideApproval.isPending
        ? decideApproval.variables?.approved
          ? "approve"
          : "reject"
        : null;
  // 一覧の行と詳細で同じ定義を使う（UX 契約 buttons.md §5.1）。取消・却下は確認してから送る。
  const runActions = (run: RunRow): EntityAction[] => {
    const { canCancel, canResume, pendingApproval } = runRowFacts(run);
    // 承認・再開は Run を実行した人と system_admin だけ（会話の持ち主でも、system_admin が実行した Run は操作しない。
    // backend も 404。#1928・#1954）。無い応答は操作できるとみなす（backend が最後に判定する）。
    const decidable = run.can_decide !== false;
    // 実行中の操作が対象の Run のものか（別の Run の行のボタンを回さない）。variables は完了後も前の値が
    // 残るため、処理中の mutation の variables だけを読む（#819）。
    const pendingRunId = cancelRun.isPending
      ? cancelRun.variables
      : resumeRun.isPending
        ? resumeRun.variables
        : decideApproval.isPending
          ? decideApproval.variables?.runId
          : undefined;
    const isPendingFor = (kind: RunCommandKind) =>
      pendingAction === kind && pendingRunId === run.id;
    const busy = pendingAction !== null;
    return [
      {
        id: "approve",
        label: t("common.approve"),
        icon: Check,
        // 承認・却下は承認の判断の権限（approver）が必要（#215）。
        visible: capabilities.decideApprovals && decidable && pendingApproval !== undefined,
        disabled: busy,
        loading: isPendingFor("approve"),
        onSelect: () => (pendingApproval ? decideRunApproval(run, pendingApproval, true) : undefined),
      },
      {
        id: "resume",
        label: t("run.resume"),
        icon: PlayCircle,
        // 取消・再開は Run の実行・操作の権限（operator）が必要（#215）。再実行は実行の詳細・一覧に置かない
        // （3 製品の実行の詳細は「会話を開く」にそろえる。#2343）。
        visible: capabilities.operateRuns && decidable && canResume,
        disabled: busy,
        loading: isPendingFor("resume"),
        onSelect: () => resumeLatestRun(run),
      },
      {
        id: "reject",
        label: t("common.reject"),
        icon: X,
        tone: "danger",
        visible: capabilities.decideApprovals && decidable && pendingApproval !== undefined,
        disabled: busy,
        loading: isPendingFor("reject"),
        onSelect: () => (pendingApproval ? decideRunApproval(run, pendingApproval, false) : undefined),
      },
      {
        id: "cancel",
        label: t("run.cancel"),
        icon: X,
        tone: "danger",
        visible: capabilities.operateRuns && canCancel,
        disabled: busy,
        loading: isPendingFor("cancel"),
        onSelect: () => cancelLatestRun(run),
      },
    ];
  };

  return (
    <>
      <PageHeader
        wide
        title={isList ? t("nav.runs") : editor.target.kind === "new" ? t("run.form.submit") : t("run.detail")}
        subtitle={
          isList
            ? t("page.runs.subtitle")
            : editor.target.kind === "new"
              ? t("run.form.runtimeHint")
              : t("run.detailDescription")
        }
        back={
          !isList
            ? {
                label: t("common.backToList"),
                onClick: () => void backToList(),
                disabled: createRun.isPending,
              }
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
                await (isList ? runs.refetch() : targetRunId ? runDetail.refetch() : agents.refetch());
              }),
          },
          ...(isList && capabilities.operateRuns
            ? [
                {
                  id: "new-run",
                  kind: "primary" as const,
                  label: t("run.form.submit"),
                  icon: PlayCircle,
                  onClick: editor.openNew,
                },
              ]
            : []),
        ]}
      />
      <PageBody wide>
        <NonPersistentStorageNotice />
        {editor.target.kind === "new" ? (
          capabilities.operateRuns ? (
            <QueryState query={agents} loadingLabel={t("loading.agents")} skeleton={<FormSkeleton fields={2} />}>
              <QueryState
                query={runtimeStatus}
                loadingLabel={t("loading.runtimes")}
                skeleton={<FormSkeleton fields={2} />}
              >
                <Card className="min-w-0">
                  <CardHeader>
                    <CardTitle>{t("run.form.submit")}</CardTitle>
                    <CardDescription>{t("run.form.runtimeHint")}</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {/* grid の外に単独で置く選択欄は値の長さの幅（業務 Agent の名前は lg。#613）。 */}
                    <SelectField
                      id="run-agent"
                      label={t("run.form.agent")}
                      width="lg"
                      value={selectedAgentId}
                      options={runnableAgents.map((agent) => ({
                        value: agent.id,
                        label: agent.name,
                      }))}
                      onValueChange={onAgentChange}
                    />
                    {capabilities.admin ? (
                      // 補足の説明は常設せず、ラベルの横の info アイコンから出す（#901）。アイコンのボタンは
                      // label の外に置く（label の中だと、押したときにスイッチの切り替えと紛れる）。
                      <div className="flex items-center gap-1">
                        <label className="flex items-center gap-2 text-sm text-fg">
                          <Switch
                            checked={runDraft}
                            aria-label={t("run.form.draft")}
                            onCheckedChange={setRunDraft}
                            data-testid="run-draft"
                          />
                          {t("run.form.draft")}
                        </label>
                        <InfoTip
                          label={t("run.form.draftInfoLabel")}
                          content={t("run.form.draftHint")}
                          contentTestId="run-draft-hint"
                          data-testid="run-draft-info"
                        />
                      </div>
                    ) : null}
                    <TextareaField
                      id="run-goal"
                      label={t("run.form.goal")}
                      width="full"
                      required
                      error={goalError ?? undefined}
                      value={goal}
                      onValueChange={(value) => {
                        setGoal(value);
                        setGoalError(null);
                      }}
                      // 質問・目標の入力欄は 3 製品で 3.5 行（#2008）。
                      rows={3.5}
                      helper={t("run.form.submitShortcut")}
                      // 目標は重い問い合わせ（page-archetypes.md #535）。複数行なので Ctrl/⌘+Enter で明示的に実行する。
                      onKeyDown={(event) => {
                        if ((event.ctrlKey || event.metaKey) && isSubmitEnter(event)) {
                          event.preventDefault();
                          if (!createRun.isPending && runnableAgents.length && runtimeStatus.data?.ready) submitRun();
                        }
                      }}
                    />
                    {/* 組み込み Runtime が実行できないとき（モデル未設定など）は、理由と直す場所を知らせる。 */}
                    {runtimeStatus.data && !runtimeStatus.data.ready ? (
                      <Banner severity="warning" title={t("runtime.builtin.notReadyTitle")}>
                        <p>{runtimeStatus.data.message ?? t("runtime.builtin.notReadyDefault")}</p>
                      </Banner>
                    ) : null}
                    {!runnableAgents.length ? <Banner severity="warning">{t("run.noRunnableAgents")}</Banner> : null}
                    {!goalSaved ? <Banner severity="warning">{t("workspace.draftNotSaved")}</Banner> : null}
                    {/* 工程を進める主操作は区切り線の下の lg、失敗はその操作の行に出す（buttons.md §5.2.1、messaging.md §3.3）。 */}
                    <FormActionBar
                      ariaLabel={t("run.form.actions")}
                      primaryActions={[
                        {
                          id: "create-run",
                          label: t("run.form.submit"),
                          icon: PlayCircle,
                          loading: createRun.isPending,
                          disabled: !runnableAgents.length || !runtimeStatus.data?.ready,
                          onClick: submitRun,
                          testId: "run-create-submit",
                        },
                      ]}
                      status={
                        createRun.error ? (
                          <FormStatus
                            tone="danger"
                            message={t("run.form.failed", {
                              reason: createRun.error.message,
                            })}
                          />
                        ) : null
                      }
                    />
                  </CardContent>
                </Card>
              </QueryState>
            </QueryState>
          ) : (
            <Banner severity="warning">{t("run.createForbidden")}</Banner>
          )
        ) : isList ? (
          <QueryState query={runs} loadingLabel={t("loading.runs")} skeleton={<RunHistorySkeleton />}>
            {runs.data ? (
              <RunHistoryList
                page={runs.data}
                onPageChange={setRunPage}
                onPageSizeChange={runPaging.setPageSize}
                toolbar={
                  <RunHistoryToolbar
                    idPrefix="run-history"
                    testId="run-list-toolbar"
                    search={
                      <ListSearchField
                        id="run-search"
                        label={t("run.search")}
                        value={runQuery}
                        onSearch={changeRunQuery}
                      />
                    }
                    searchActive={Boolean(runQuery.trim())}
                    status={{
                      value: statusFilter,
                      options: AGENT_RUN_STATUS_OPTIONS,
                      onChange: changeFilter(setStatusFilter),
                    }}
                    // 種類の既定は「品質評価以外」（品質評価の Run を既定で隠す。#1983）。
                    kind={{
                      value: kindFilter,
                      defaultValue: DEFAULT_AGENT_RUN_KIND,
                      options: [
                        { value: DEFAULT_AGENT_RUN_KIND, label: t("run.filter.kind.notEvaluation") },
                        { value: "all", label: t("run.filter.all") },
                        ...AGENT_RUN_KINDS.map((kind) => ({ value: kind, label: runSourceLabel(kind) })),
                      ],
                      onChange: (value) =>
                        changeFilter(setKindFilter)(isRunKindFilter(value) ? value : DEFAULT_AGENT_RUN_KIND),
                    }}
                    period={{ value: periodFilter, onChange: changeFilter(setPeriodFilter) }}
                    // 実行者は他の人の実行が見える利用者だけ（system_admin・自分の会話で他の人が実行した Run を持つ利用者。#1954）。
                    executor={
                      runs.data.has_other_executors || executorFilter !== "all"
                        ? { value: executorFilter, onChange: changeFilter(setExecutorFilter) }
                        : undefined
                    }
                    onClearFilters={clearRunFilters}
                    count={listCount(runs.data.unfiltered_total, runs.data.total, listNarrowed)}
                    labels={{
                      all: t("run.filter.all"),
                      names: {
                        status: t("common.status"),
                        kind: t("run.column.kind"),
                        period: t("run.filter.period"),
                        executor: t("run.column.executor"),
                      },
                      status: { waiting: t("run.status.waiting") },
                      period: {
                        all: t("run.filter.all"),
                        "24h": t("run.period.24h"),
                        "7d": t("run.period.7d"),
                        "30d": t("run.period.30d"),
                      },
                      executor: {
                        all: t("run.filter.executor.all"),
                        self: t("run.filter.executor.mine"),
                        others: t("run.filter.executor.others"),
                      },
                      toolbar: listToolbarLabels(),
                    }}
                  />
                }
                empty={
                  runs.data.unfiltered_total && !otherFiltersActive && periodFilter !== "all" ? (
                    // 期間（既定は過去 7 日）だけで絞り、その中に実行が無くそれより前にはある: 期間を広げる操作を出す（#3386）。
                    <RunHistoryPeriodEmptyState
                      period={periodFilter}
                      onShowAll={() => changeFilter(setPeriodFilter)("all")}
                      testId="runs-period-empty"
                    />
                  ) : runs.data.unfiltered_total ? (
                    <NoMatchState
                      title={t("run.noMatch")}
                      clearLabel={filtersActive ? t("common.clearFilters") : t("common.clearSearch")}
                      onClear={clearRunFilters}
                    />
                  ) : (
                    <EmptyState
                      title={t("run.empty.title")}
                      hint={
                        // 品質評価の Run だけがあるときは、既定で隠していることと出し方を伝える（#1983）。
                        kindFilter === DEFAULT_AGENT_RUN_KIND && runs.data.evaluation_total
                          ? t("run.empty.evaluationHidden", { count: runs.data.evaluation_total })
                          : capabilities.operateRuns
                            ? t("run.empty.hint")
                            : undefined
                      }
                    />
                  )
                }
                agentNameOf={agentNameOf}
                showExecutor={Boolean(runs.data.has_other_executors)}
                executorNameOf={(run) =>
                  run.executor_display_name ||
                  (run.created_by_user_uuid && runs.data?.creator_display_names?.[run.created_by_user_uuid]) ||
                  (run.executed_by_me !== false ? user?.display_name : undefined) ||
                  t("run.userUnknown")
                }
                selectedRunId={selectedRunId}
                actionsFor={runActions}
                hrefFor={editor.itemHref}
                onSelect={(runId) => {
                  setSelectedRunId(runId);
                  editor.openItem(runId);
                }}
              />
            ) : null}
          </QueryState>
        ) : runDetail.error && isRunGone(runDetail.error) ? (
          <MissingEditorTarget
            id={editor.target.kind === "edit" ? editor.target.id : ""}
            onBack={() => void backToList()}
          />
        ) : (
          <QueryState query={runDetail} loadingLabel={t("loading.runs")} skeleton={<RunDetailSkeleton />}>
            {selectedRun ? (
              <RunDetail
                key={selectedRun.id}
                run={selectedRun}
                agentName={agentNameOf(selectedRun.agent_id)}
                actions={runActions(selectedRun)}
                sseState={sseState}
                capabilities={capabilities}
              />
            ) : null}
          </QueryState>
        )}
      </PageBody>
    </>
  );
}

function RunHistoryList({
  page,
  onPageChange,
  onPageSizeChange,
  toolbar,
  empty,
  selectedRunId,
  onSelect,
  actionsFor,
  agentNameOf,
  showExecutor,
  executorNameOf,
  hrefFor,
}: {
  /** サーバーが返した 1 ページ（#1897）。 */
  page: RunsPageData;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  /** 表の上の検索・絞り込み（RunHistoryToolbar）。 */
  toolbar: ReactNode;
  empty: ReactNode;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  actionsFor: (run: RunSummary) => EntityAction[];
  /** 業務 Agent の ID を名前にする（一覧に無い・読めないときは ID のまま）。 */
  agentNameOf: (agentId: string) => string;
  /** 「実行者」の列を出す（他の人の実行が見える利用者。#1954・#1955）。 */
  showExecutor: boolean;
  /** Run を実行した利用者の表示名（#1889・#1954）。 */
  executorNameOf: (run: RunSummary) => string;
  hrefFor: (id: string) => string;
}) {
  // 製品の行 → 共通の行（run-history.md「一覧」）。状態は共通の 7 つ、文言は Agent の言い方（承認待ちなど）。
  const toItem = (run: RunSummary): RunHistoryItem => ({
    id: run.id,
    title: run.goal,
    target: agentNameOf(run.agent_id),
    href: hrefFor(run.id),
    status: commonRunStatus(run.status),
    statusLabel: runStatusLabel(run.status),
    kind: runSourceLabel(run.source, run.api_key_name),
    executor: { name: executorNameOf(run), isSelf: run.executed_by_me !== false },
    startedAt: run.created_at,
    durationMs: run.duration_ms ?? null,
    usage: run.total_tokens != null ? { tokens: run.total_tokens } : null,
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{t("run.history")}</CardTitle>
        <CardDescription>{t("run.historyDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {toolbar}
        {/* 5 秒ごとの再取得で行が変わっても、ページは作業状態に残して戻さない（戻すのは絞り込みを変えたときだけ）。 */}
        <div className="grid min-w-0 gap-2">
          <RunHistoryTable
            growToViewport
            rows={page.items}
            toItem={toItem}
            onOpen={(run) => onSelect(run.id)}
            showExecutor={showExecutor}
            showUsage
            actionsFor={actionsFor}
            selectedRunId={selectedRunId}
            labels={agentRunHistoryLabels()}
            empty={empty}
            // 行は run-row-<id>、行の操作は run-row-actions-<id>（e2e が使う。#1897）。
            testId="run"
          />
          <OffsetPagination
            offset={page.offset}
            limit={page.limit}
            total={page.total}
            count={page.items.length}
            onPageChange={onPageChange}
            onPageSizeChange={onPageSizeChange}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            labels={agentPaginationLabels()}
            testId="run-history-pagination"
          />
        </div>
      </CardContent>
    </Card>
  );
}

/** 実行履歴のカード（見出し + 表）の形。 */
function RunHistorySkeleton() {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{t("run.history")}</CardTitle>
        <CardDescription>{t("run.historyDescription")}</CardDescription>
      </CardHeader>
      <CardContent>
        <TableSkeleton columns={4} />
      </CardContent>
    </Card>
  );
}
