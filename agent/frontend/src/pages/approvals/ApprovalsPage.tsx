import { useEffect } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CheckCheck, History, RefreshCw, X } from "lucide-react";
import {
  Banner,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  FormSkeleton,
  ListToolbar,
  ObjectActionBar,
  TableSkeleton,
  PageHeader,
  RowActionMenu,
  StatusBadge,
  FilterChipGroup,
  toast,
  useActionPending,
  useConfirm,
  type DataTableColumn,
  type EntityAction,
  PageBody,
  RowTitleButton,
  displayDateTimeFormat,
} from "@production-ready/ui";
import {
  ApiError,
  agentApi,
  type ApprovalDetail,
  type ApprovalListFilters,
  type ApprovalRequest,
  type ApprovalsPage as ApprovalsPageData,
  type ApprovalSummary,
  type RunState,
} from "@/lib/api";
import { MissingEditorTarget } from "@/components/EntityLayout";
import { listToolbarLabels, QueryState, ServerPagedDataTable, usePersistedServerPaging } from "@/components/ListViews";
import { ListSearchField, listCount, NoMatchState, useListSearch } from "@/components/ListFilters";
import { useEditorRoute } from "@/lib/editor-route";
import { t } from "@/lib/i18n";
import { useCapabilities } from "@/lib/permissions";
import { approvalStatusView, runStatusView } from "@/lib/status-labels";
import { isNullableString, isOneOf, useWorkspaceState } from "@/lib/workspace-state";
import { JsonPreview } from "@/pages/shared/page-helpers";
import { isOwnDecision } from "@/pages/shared/approval-decision";
import { actorDisplayName } from "@/pages/shared/user-names";
import { useAuth } from "@/components/security/AuthProvider";
import { useViewSwitchFocus } from "@/pages/shared/view-switch-focus";

/** 承認の操作に要る事実（一覧の行の要約と、詳細の承認のどちらからでも作る。#1897）。 */
type ApprovalTarget = { id: string; toolName: string; status: ApprovalRequest["status"]; goal: string };
const APPROVAL_FILTERS = ["pending", "decided", "all"] as const;
type ApprovalFilter = (typeof APPROVAL_FILTERS)[number];
const isApprovalFilter = isOneOf<ApprovalFilter>(APPROVAL_FILTERS);
const approvalDateFormat = displayDateTimeFormat({
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

/** 判断の日時はブラウザの時間帯に依存せず、システムの設定の時間帯（#3265）。古い記録の欠落は補完しない。 */
function approvalDate(value: string | null | undefined) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? approvalDateFormat.format(date) : t("approval.notRecorded");
}

function summaryTarget(row: ApprovalSummary): ApprovalTarget {
  return { id: row.id, toolName: row.tool_name, status: row.status, goal: row.run_goal };
}

/** 承認が無い・読めない（別の承認へ暗黙に移らず、一覧へ戻す案内を出す）。 */
function isApprovalGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

/** 判断の返却値（Run）の承認の状態を、承認の一覧の行に写す。 */
function withDecidedApprovals(current: ApprovalsPageData | undefined, run: RunState): ApprovalsPageData | undefined {
  if (!current) return current;
  const decided = new Map(run.approvals.map((approval) => [approval.id, approval]));
  return {
    ...current,
    items: current.items.map((row) => {
      const approval = decided.get(row.id);
      return approval
        ? { ...row, status: approval.status, decided_by: approval.decided_by, decided_at: approval.decided_at }
        : row;
    }),
  };
}

export function ApprovalsPage() {
  const editor = useEditorRoute();
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const capabilities = useCapabilities();
  const { user } = useAuth();
  const manualRefresh = useActionPending();
  const isList = editor.target.kind === "list";
  const targetId = editor.target.kind === "edit" ? editor.target.id : null;
  // 検索語・絞り込み・ページは作業状態に残し、サーバー側で絞り込んでページングする（全 Run を読まない。#1897）。
  const [approvalQuery, setApprovalQuery] = useListSearch("approvals");
  const [approvalFilter, setApprovalFilter] = useWorkspaceState(
    "listFilter",
    "approvals",
    "pending" as ApprovalFilter,
    isApprovalFilter
  );
  const approvalPaging = usePersistedServerPaging("approvals");
  const setApprovalPage = approvalPaging.setPage;
  const [selectedId, setSelectedId] = useWorkspaceState(
    "approvals",
    "selectedId",
    null as string | null,
    isNullableString
  );
  const filters: ApprovalListFilters = {
    status: approvalFilter === "all" ? undefined : approvalFilter,
    q: approvalQuery.trim() || undefined,
    limit: approvalPaging.limit,
    offset: approvalPaging.offset,
  };
  const approvals = useQuery({
    queryKey: ["approvals", "list", filters],
    queryFn: () => agentApi.listApprovals(filters),
    // 詳細を開いている間も読んでおき（定期の取り直しはしない）、一覧へ戻ったらすぐ選んだ行へフォーカスを戻す。
    refetchInterval: isList ? 5000 : false,
    // ページ・絞り込みを変えている間は今のページを出したまま取り直す（表を Skeleton に戻さない）。
    placeholderData: keepPreviousData,
  });
  // 詳細は承認の ID から、その承認と実行の要約だけを読む。判断して保留中の一覧から消えても詳細を保つ。
  const detail = useQuery({
    queryKey: ["approvals", "detail", targetId],
    queryFn: () => agentApi.getApproval(targetId ?? ""),
    enabled: targetId !== null,
    refetchInterval: 5000,
    retry: (count, error) => !isApprovalGone(error) && count < 3,
  });
  const decide = useMutation({
    mutationFn: ({
      approval,
      approved,
      alwaysAllow = false,
    }: {
      approval: ApprovalTarget;
      approved: boolean;
      alwaysAllow?: boolean;
    }) =>
      // 決定者はログイン中の利用者から server が決める（#215）。「今後は自動」はツール権限も変える（#1832）。
      agentApi.decideApproval(approval.id, alwaysAllow ? { approved, always_allow: true } : { approved }),
    onSuccess: (updatedRun, { approval, approved, alwaysAllow }) => {
      // 押した判断が自分の判断として残ったときだけ成功と案内する（ほかの操作者が先に判断した承認は、backend が
      // 状態を変えずに 200 で返す。#1119）。
      if (isOwnDecision(updatedRun, approval.id, approved, user?.login_user_id)) {
        if (alwaysAllow) {
          toast.success(t("approval.approvedAlways", { tool: approval.toolName }));
          void queryClient.invalidateQueries({ queryKey: ["settings", "tool-policy"] });
        } else {
          toast.success(approved ? t("approval.decided") : t("approval.rejected"));
        }
      } else {
        toast.info(t("approval.changedDuringReview"));
      }
      // 判断の返却値を先に反映し、再取得を待つ間の二重判断を防ぐ。対象の URL は変えない（#877）。
      const decided = updatedRun.approvals.find((item) => item.id === approval.id);
      if (decided) {
        queryClient.setQueryData<ApprovalDetail>(["approvals", "detail", approval.id], (current) =>
          current ? { ...current, approval: decided, run: { ...current.run, status: updatedRun.status } } : current
        );
      }
      queryClient.setQueriesData<ApprovalsPageData>({ queryKey: ["approvals", "list"] }, (current) =>
        withDecidedApprovals(current, updatedRun)
      );
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
    onError: (error) => {
      toast.error(error.message);
      // 他の操作者が先に判断した場合も、最新の状態を取り直す。
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
  // 検索語・絞り込みを変えたら 1 ページ目へ戻す。
  const changeApprovalQuery = (value: string) => {
    setApprovalQuery(value);
    setApprovalPage(1);
  };
  const changeApprovalFilter = (value: ApprovalFilter) => {
    setApprovalFilter(value);
    setApprovalPage(1);
  };
  const selected = detail.data?.approval.id === targetId ? detail.data : undefined;

  useEffect(() => {
    if (targetId) setSelectedId(targetId);
  }, [targetId, setSelectedId]);
  // 詳細を開くと見出しへ、一覧へ戻ると選んだ行へフォーカスを移す（開いた直後は動かさない。#1122）。
  useViewSwitchFocus(
    `${isList ? "list" : "detail"}:${targetId ?? ""}`,
    isList && selectedId ? `a[data-approval-id="${CSS.escape(selectedId)}"]` : null
  );

  function openApproval(id: string) {
    setSelectedId(id);
    editor.openItem(id);
  }

  async function decideApproval(approval: ApprovalTarget, approved: boolean, alwaysAllow = false) {
    if (decide.isPending || !capabilities.decideApprovals || approval.status !== "pending") return;
    const context = t("approval.confirmContext", {
      tool: approval.toolName,
      goal: approval.goal,
      id: approval.id,
    });
    // 「今後は自動」はポリシーが変わること（全体に効くこと・戻し方）を確認のダイアログで示す（#1832）。
    const ok = await confirm({
      title: alwaysAllow
        ? t("approval.approveAlwaysTitle", { tool: approval.toolName })
        : approved
          ? t("run.approveTitle")
          : t("run.rejectTitle"),
      description: alwaysAllow
        ? `${t("approval.approveAlwaysDescription", { tool: approval.toolName })} ${context}`
        : context,
      confirmLabel: alwaysAllow ? t("approval.approveAlways") : approved ? t("common.approve") : t("common.reject"),
      cancelLabel: t("common.cancel"),
      tone: alwaysAllow ? "warning" : approved ? "info" : "danger",
    });
    if (ok) {
      // 確認中に他の操作者が判断した・実行が取り消された場合は、古い状態で送らない（その承認 1 件を読み直す。#1897）。
      let latest: ApprovalDetail;
      try {
        latest = await queryClient.fetchQuery({
          queryKey: ["approvals", "detail", approval.id],
          queryFn: () => agentApi.getApproval(approval.id),
          staleTime: 0,
        });
      } catch (error) {
        toast.error(error instanceof Error ? error.message : String(error));
        return;
      }
      if (latest.approval.status !== "pending") {
        toast.info(t("approval.changedDuringReview"));
        void queryClient.invalidateQueries({ queryKey: ["approvals", "list"] });
        return;
      }
      decide.mutate({ approval, approved, alwaysAllow });
    }
  }

  // 一覧の行と詳細で同じ定義を使う。処理中は押した対象の操作だけを回す。
  const approvalActions = (approval: ApprovalTarget): EntityAction[] => [
    {
      id: "approve",
      label: t("common.approve"),
      icon: Check,
      visible: capabilities.decideApprovals && approval.status === "pending",
      disabled: decide.isPending,
      loading:
        decide.isPending &&
        decide.variables?.approval.id === approval.id &&
        decide.variables.approved &&
        !decide.variables.alwaysAllow,
      onSelect: () => decideApproval(approval, true),
    },
    {
      id: "reject",
      label: t("common.reject"),
      icon: X,
      tone: "danger",
      visible: capabilities.decideApprovals && approval.status === "pending",
      disabled: decide.isPending,
      loading: decide.isPending && decide.variables?.approval.id === approval.id && !decide.variables.approved,
      onSelect: () => decideApproval(approval, false),
    },
    {
      // 承認して、今後このツールは自動で実行（ツール権限を変えられる Agent 管理だけ。#1832）。承認・却下の
      // 後に置く（狭い画面では「その他の操作」に入り、承認と却下を直接押せるままにする）。
      id: "approve-always",
      label: t("approval.approveAlways"),
      icon: CheckCheck,
      visible: capabilities.decideApprovals && capabilities.admin && approval.status === "pending",
      disabled: decide.isPending,
      loading:
        decide.isPending && decide.variables?.approval.id === approval.id && Boolean(decide.variables.alwaysAllow),
      onSelect: () => decideApproval(approval, true, true),
    },
  ];

  const columns: DataTableColumn<ApprovalSummary>[] = [
    {
      key: "tool",
      header: t("common.tool"),
      rowHeader: true,
      render: (approval) => (
        <RowTitleButton
          title={approval.tool_name}
          subtitle={
            <>
              <span className="block line-clamp-2">{approval.run_goal}</span>
              <span className="block">{approvalDate(approval.created_at)}</span>
            </>
          }
          href={editor.itemHref(approval.id)}
          data-approval-id={approval.id}
          current={approval.id === selectedId}
          onClick={() => openApproval(approval.id)}
        />
      ),
    },
    {
      key: "status",
      header: t("common.status"),
      render: (approval) => <StatusBadge {...approvalStatusView(approval.status)} />,
    },
    {
      key: "actions",
      header: t("run.actions"),
      align: "right",
      render: (approval) => (
        <RowActionMenu
          actions={approvalActions(summaryTarget(approval))}
          ariaLabel={t("common.entityActions", { name: approval.tool_name })}
          testId={`approval-row-actions-${approval.id}`}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader
        wide
        title={isList ? t("nav.approvals") : t("approval.detail")}
        subtitle={isList ? t("page.approvals.subtitle") : t("approval.detailDescription")}
        back={!isList ? { label: t("common.backToList"), onClick: () => editor.backToList() } : undefined}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () =>
              void manualRefresh.track(async () => {
                await (isList ? approvals.refetch() : detail.refetch());
              }),
          },
        ]}
      />
      <PageBody wide>
        {isList ? (
          <QueryState query={approvals} loadingLabel={t("loading.approvals")} skeleton={<TableSkeleton columns={3} />}>
            {approvals.data ? (
              <Card className="min-w-0">
                <CardHeader>
                  <CardTitle>{t("approval.list")}</CardTitle>
                  <CardDescription>{t("approval.listDescription")}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <ListToolbar
                    search={
                      <ListSearchField
                        id="approval-search"
                        label={t("approval.search")}
                        value={approvalQuery}
                        onSearch={changeApprovalQuery}
                      />
                    }
                    filters={
                      <FilterChipGroup<ApprovalFilter>
                        label={t("approval.filter.label")}
                        value={approvalFilter}
                        options={APPROVAL_FILTERS.map((filter) => ({ value: filter, label: t(`approval.filter.${filter}`) }))}
                        onValueChange={changeApprovalFilter}
                      />
                    }
                    activeFilterCount={approvalFilter === "pending" ? 0 : 1}
                    searchActive={Boolean(approvalQuery.trim())}
                    onClearFilters={() => {
                      changeApprovalQuery("");
                      changeApprovalFilter("pending");
                    }}
                    labels={listToolbarLabels()}
                    count={listCount(approvals.data.unfiltered_total, approvals.data.total, Boolean(approvalQuery.trim()) || approvalFilter !== "all")}
                    testId="approval-list-toolbar"
                  />
                  <ServerPagedDataTable
                    page={approvals.data}
                    onPageChange={setApprovalPage}
                    onPageSizeChange={approvalPaging.setPageSize}
                    columns={columns}
                    getRowKey={(approval) => approval.id}
                    selectedRowKey={selectedId}
                    onRowClick={(approval) => openApproval(approval.id)}
                    rowProps={(approval) => ({
                      className: "align-top",
                      "data-testid": `approval-row-${approval.id}`,
                    })}
                    ariaLabel={t("approval.list")}
                    paginationTestId="approval-list-pagination"
                    empty={
                      approvalQuery || (approvals.data.unfiltered_total > 0 && approvalFilter !== "pending") ? (
                        <NoMatchState
                          title={t("approval.noMatch")}
                          clearLabel={approvalFilter === "pending" ? t("common.clearSearch") : t("common.clearFilters")}
                          onClear={() => {
                            changeApprovalQuery("");
                            changeApprovalFilter("pending");
                          }}
                        />
                      ) : (
                        <EmptyState
                          title={
                            approvalFilter === "pending" ? t("approval.empty.pendingTitle") : t("approval.empty.title")
                          }
                          hint={t("approval.empty.hint")}
                        />
                      )
                    }
                  />
                </CardContent>
              </Card>
            ) : null}
          </QueryState>
        ) : detail.error && isApprovalGone(detail.error) ? (
          <MissingEditorTarget
            id={editor.target.kind === "edit" ? editor.target.id : "new"}
            onBack={() => editor.backToList()}
          />
        ) : (
          <QueryState query={detail} loadingLabel={t("loading.approvals")} skeleton={<FormSkeleton fields={4} />}>
            {selected ? (
              <section className="space-y-5" aria-label={t("approval.detail")}>
                <Card className="min-w-0">
                  <CardHeader className="flex-row flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0 space-y-2">
                      <CardTitle className="break-words [overflow-wrap:anywhere]">
                        {selected.approval.tool_call.name}
                      </CardTitle>
                      <StatusBadge {...approvalStatusView(selected.approval.status)} />
                    </div>
                    <ObjectActionBar
                      actions={approvalActions({
                        id: selected.approval.id,
                        toolName: selected.approval.tool_call.name,
                        status: selected.approval.status,
                        goal: selected.run.goal,
                      })}
                      ariaLabel={t("common.entityActions", { name: selected.approval.tool_call.name })}
                      moreLabel={t("common.moreActions")}
                      testId="approval-object-actions"
                    />
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <dl className="grid gap-4 sm:grid-cols-2">
                      <ApprovalFact label={t("run.form.goal")} value={selected.run.goal} className="sm:col-span-2" />
                      <ApprovalFact
                        label={t("approval.reason")}
                        value={selected.approval.reason || t("approval.notRecorded")}
                        className="sm:col-span-2"
                      />
                      <ApprovalFact label={t("approval.createdAt")} value={approvalDate(selected.approval.created_at)} />
                      <ApprovalFact label={t("approval.id")} value={selected.approval.id} />
                    </dl>
                    {selected.approval.status === "pending" && !capabilities.decideApprovals ? (
                      <Banner severity="info">{t("approval.viewOnly")}</Banner>
                  ) : null}
                  </CardContent>
                </Card>
                <Card className="min-w-0">
                  <CardHeader>
                    <CardTitle>{t("approval.arguments")}</CardTitle>
                    <CardDescription>{t("approval.argumentsDescription")}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <JsonPreview value={selected.approval.tool_call.arguments} />
                  </CardContent>
                </Card>
                {selected.approval.status !== "pending" ? (
                  <Card data-testid="approval-decision-record">
                    <CardHeader>
                      <CardTitle>{t("approval.decisionRecord")}</CardTitle>
                      <CardDescription>{t("approval.finished")}</CardDescription>
                    </CardHeader>
                    <CardContent>
                      <dl className="grid gap-4 sm:grid-cols-2">
                        <ApprovalFact
                          label={t("approval.decidedBy")}
                          value={
                            actorDisplayName(selected.approval.decided_by, selected.user_display_names) ??
                            t("approval.notRecorded")
                          }
                        />
                        <ApprovalFact
                          label={t("approval.decidedAt")}
                          value={approvalDate(selected.approval.decided_at)}
                        />
                      </dl>
                    </CardContent>
                  </Card>
                ) : null}
                <Card className="min-w-0">
                  <CardHeader>
                    <CardTitle>{t("approval.relatedRun")}</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <dl className="grid gap-4 sm:grid-cols-2">
                      <ApprovalFact label={t("audit.runId")} value={selected.run.id} />
                      <ApprovalFact label={t("audit.runStatus")} value={runStatusView(selected.run.status).label} />
                    </dl>
                    {capabilities.viewRuns ? (
                      <ButtonLink
                        to={`/runs?id=${encodeURIComponent(selected.run.id)}`}
                        linkComponent={Link}
                        variant="secondary"
                        icon={History}
                      >
                        {t("approval.openRun")}
                      </ButtonLink>
                    ) : null}
                  </CardContent>
                </Card>
              </section>
            ) : null}
          </QueryState>
        )}
      </PageBody>
    </>
  );
}

function ApprovalFact({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className={className}>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="break-words text-sm text-fg [overflow-wrap:anywhere]">{value}</dd>
    </div>
  );
}
