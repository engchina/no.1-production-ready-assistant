import { useMemo } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import {
  compareNames,
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  EmptyState,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  ListToolbar,
  offsetForPage,
  OffsetPagination,
  PAGE_SIZE_OPTIONS,
  TableSkeleton,
  PageHeader,
  ProcessingIndicator,
  StatusBadge,
  toast,
  type DataTableColumn,
  PageBody,
  isSubmitEnter,
  SelectField,
  TextField,
  useActionPending,
  apiErrorMessage,
} from "@production-ready/ui";
import { agentApi, type ToolCallAuditFilters, type ToolCallAuditRecord } from "@/lib/api";
import { listTotal } from "@/components/ListFilters";
import { agentPaginationLabels, listScrollLabel, QueryState, usePersistedPageSize } from "@/components/ListViews";
import { t } from "@/lib/i18n";
import { runSourceLabel } from "@/lib/run-source";
import {
  approvalStatusOptions,
  approvalStatusView,
  permissionView,
  policyDecisionView,
  stepStatusOptions,
  stepStatusView,
} from "@/lib/status-labels";
import { useWorkspaceState, type WorkspaceValidator } from "@/lib/workspace-state";
import { formatDate } from "@/pages/shared/page-helpers";

type AuditWarningsFilter = "any" | "true" | "false";

/** 監査の絞り込みフォーム。入力中の条件と適用済みの条件をこの形で sessionStorage に残す（#87）。 */
interface AuditFilterForm {
  runId: string;
  toolName: string;
  stepStatus: string;
  approvalStatus: string;
  errorCode: string;
  warnings: AuditWarningsFilter;
}

const DEFAULT_AUDIT_FILTER_FORM: AuditFilterForm = {
  runId: "",
  toolName: "",
  stepStatus: "",
  approvalStatus: "",
  errorCode: "",
  warnings: "any",
};

const isAuditFilterForm: WorkspaceValidator<AuditFilterForm> = (value): value is AuditFilterForm => {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    Object.keys(DEFAULT_AUDIT_FILTER_FORM).every((key) => typeof record[key] === "string") &&
    ["any", "true", "false"].includes(record.warnings as string)
  );
};

function auditFiltersOf(form: AuditFilterForm): ToolCallAuditFilters {
  return {
    run_id: form.runId.trim() || undefined,
    tool_name: form.toolName || undefined,
    status: form.stepStatus || undefined,
    approval_status: form.approvalStatus || undefined,
    error_code: form.errorCode.trim() || undefined,
    has_guardrail_warnings: form.warnings === "any" ? undefined : form.warnings === "true",
  };
}

/**
 * 一覧の 1 ページの取得条件（条件の欄の値とページ番号・表示件数から作る）。
 * 1 ページの件数は条件の欄ではなく、ページ送りの表示件数（10 / 25 / 50 / 100。作業状態。#1891）。
 */
function auditPageFilters(form: AuditFilterForm, page: number, pageSize: number) {
  return { ...auditFiltersOf(form), limit: pageSize, offset: offsetForPage(page, pageSize) };
}

function auditQueryKey(filters: ReturnType<typeof auditPageFilters>) {
  return ["audit", "tool-calls", filters] as const;
}

const isAuditPage: WorkspaceValidator<number> = (value): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 1;

export function AuditPage() {
  const tools = useQuery({ queryKey: ["tools"], queryFn: agentApi.listTools });
  const [filterForm, setFilterForm] = useWorkspaceState(
    "audit",
    "filterForm",
    DEFAULT_AUDIT_FILTER_FORM,
    isAuditFilterForm
  );
  const [appliedForm, setAppliedForm] = useWorkspaceState(
    "audit",
    "appliedForm",
    DEFAULT_AUDIT_FILTER_FORM,
    isAuditFilterForm
  );
  // ページ番号も作業状態に残す。ページは API の offset / limit に直して取得する（#265）。
  const [auditPage, setAuditPage] = useWorkspaceState("audit", "page", 1, isAuditPage);
  const [auditPageSize, setAuditPageSize] = usePersistedPageSize("audit");
  const appliedFilters = useMemo(
    () => auditPageFilters(appliedForm, auditPage, auditPageSize),
    [appliedForm, auditPage, auditPageSize]
  );
  const audit = useQuery({
    queryKey: auditQueryKey(appliedFilters),
    queryFn: () => agentApi.listToolCallAudit(appliedFilters),
    // ページを送っている間は今のページを出したまま取り直す（表を Skeleton に戻さない）。
    placeholderData: keepPreviousData,
  });
  // 「最新情報を取得」と「フィルター適用」は同じ一覧を取り直すが、スピナーは押した側だけが出す。ページの切り替え・
  // フォーカスでの取り直しでは、どちらも回さない（#819）。
  const auditQueryClient = useQueryClient();
  const manualRefresh = useActionPending();
  const manualApply = useActionPending();
  // 共通の OffsetPagination（#1266）。残していたページが記録の削除などで範囲外になり 0 件で返ったら、
  // 最後のページへ寄せる（0 件の空の状態の下にも置いて、その移動を効かせる）。
  const auditPager = audit.data ? (
    <OffsetPagination
      offset={audit.data.offset}
      limit={audit.data.limit}
      total={audit.data.total}
      count={audit.data.items.length}
      onPageChange={(page) => setAuditPage(page)}
      onPageSizeChange={(size) => {
        setAuditPageSize(size);
        setAuditPage(1);
      }}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      labels={agentPaginationLabels()}
      ariaLabel={t("audit.pagerLabel")}
      testId="audit-pagination"
    />
  ) : null;
  const { runId, toolName, stepStatus, approvalStatus, errorCode, warnings } = filterForm;
  // ツール名の選択肢は、登録済みのツール（`/api/tools`）と監査に記録されたツール名を合わせる。
  // MCP 接続のツール（`<接続>__<ツール>`）は `/api/tools` に出ない（#983）。選択中の値も残す。
  const auditToolNames = audit.data?.tool_names;
  const toolNameOptions = useMemo(() => {
    const names = new Set([...(tools.data?.tools ?? []).map((tool) => tool.name), ...(auditToolNames ?? [])]);
    if (toolName) names.add(toolName);
    return [...names].sort((left, right) => compareNames(left, right)).map((name) => ({ value: name, label: name }));
  }, [tools.data, auditToolNames, toolName]);

  function setFilter<K extends keyof AuditFilterForm>(key: K, value: AuditFilterForm[K]) {
    setFilterForm((current) => ({ ...current, [key]: value }));
  }

  function applyFilters() {
    if (manualApply.pending) return;
    setAppliedForm(filterForm);
    setAuditPage(1);
    // 条件を変えずに押したときも取り直す。新しい条件の取得は useQuery の取得と重ならない（同じ key）。
    const filters = auditPageFilters(filterForm, 1, auditPageSize);
    void manualApply
      .track(() =>
        auditQueryClient.fetchQuery({
          queryKey: auditQueryKey(filters),
          queryFn: () => agentApi.listToolCallAudit(filters),
        })
      )
      .catch(() => undefined);
  }

  // CSV も Cookie セッションで取得し、401 / 403 は他の API と同じく扱う（#215）。
  const csvDownload = useMutation({
    // CSV は 1 ページではなく、条件に合う記録を backend の既定の件数（1,000 件）まで出力する。
    mutationFn: () =>
      agentApi.downloadToolCallAuditCsv({ ...auditFiltersOf(filterForm), limit: undefined, offset: undefined }),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "agent-tool-call-audit.csv";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      toast.success(t("audit.csvDownloaded"));
    },
    onError: (error) => toast.error(t("audit.csvFailed"), { description: apiErrorMessage(error, t("common.error.retryLater")) }),
  });

  return (
    <>
      <PageHeader
        wide
        title={t("nav.audit")}
        subtitle={t("page.audit.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            disabled: manualApply.pending,
            onClick: () => void manualRefresh.track(() => audit.refetch()),
          },
        ]}
      />
      <PageBody wide>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("audit.filters")}</CardTitle>
            <CardDescription>{t("page.audit.subtitle")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* 条件の欄は値の幅で横に並べる（grid のセルで伸ばさない。#1891）。 */}
            <div className="flex flex-wrap items-end gap-x-4 gap-y-3" data-testid="audit-filters">
              <TextField
                id="audit-run-id"
                width="md"
                label={t("audit.runId")}
                value={runId}
                onValueChange={(value) => setFilter("runId", value)}
                // 条件フォームの Enter は「条件を適用」と同じ（IME の変換を確定する Enter では適用しない。#535）。
                onKeyDown={(event) => {
                  if (isSubmitEnter(event)) applyFilters();
                }}
              />
              <SelectField
                id="audit-tool-name"
                width="lg"
                label={t("audit.toolName")}
                value={toolName}
                options={[{ value: "", label: t("common.all") }, ...toolNameOptions]}
                onValueChange={(value) => setFilter("toolName", value)}
              />
              <SelectField
                id="audit-step-status"
                width="sm"
                label={t("audit.stepStatus")}
                value={stepStatus}
                options={[
                  { value: "", label: t("common.all") },
                  ...stepStatusOptions(),
                ]}
                onValueChange={(value) => setFilter("stepStatus", value)}
              />
              <SelectField
                id="audit-approval-status"
                width="sm"
                label={t("audit.approvalStatus")}
                value={approvalStatus}
                options={[
                  { value: "", label: t("common.all") },
                  ...approvalStatusOptions(),
                ]}
                onValueChange={(value) => setFilter("approvalStatus", value)}
              />
              <TextField
                id="audit-error-code"
                width="md"
                label={t("audit.errorCode")}
                value={errorCode}
                onValueChange={(value) => setFilter("errorCode", value)}
                // 条件フォームの Enter は「条件を適用」と同じ（IME の変換を確定する Enter では適用しない。#535）。
                onKeyDown={(event) => {
                  if (isSubmitEnter(event)) applyFilters();
                }}
              />
              <SelectField<AuditWarningsFilter>
                id="audit-warning-filter"
                width="sm"
                label={t("audit.guardrailWarnings")}
                value={warnings}
                options={[
                  { value: "any", label: t("common.all") },
                  { value: "true", label: t("audit.hasWarnings") },
                  { value: "false", label: t("audit.noWarnings") },
                ]}
                onValueChange={(value) => setFilter("warnings", value)}
              />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={applyFilters}
                loading={manualApply.pending}
                disabled={manualRefresh.pending}
                icon={RefreshCw}
              >
                {t("audit.apply")}
              </Button>
              <Button
                variant="secondary"
                onClick={() => csvDownload.mutate()}
                loading={csvDownload.isPending}
                icon={Download}
              >
                {t("audit.downloadCsv")}
              </Button>
            </div>
            {csvDownload.isPending ? (
              // 監査レコードが多いと CSV の作成に数秒以上かかる。スピナーはボタンの loading が担う。
              <ProcessingIndicator
                active
                label={t("audit.progress.downloadingCsv")}
                operationKey="audit-csv-download"
                placement="action"
                activityIcon="none"
                testId="audit-csv-processing"
              />
            ) : null}
            <p className="text-xs leading-5 text-fg-muted">{t("audit.csvHint")}</p>
            {tools.error ? <Banner severity="warning">{apiErrorMessage(tools.error, t("common.error.load"))}</Banner> : null}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("audit.records")}</CardTitle>
            <CardDescription>{t("page.audit.subtitle")}</CardDescription>
          </CardHeader>
          <CardContent>
            <QueryState
              query={audit}
              loadingLabel={t("loading.audit")}
              skeleton={<TableSkeleton columns={9} />}
            >
              {audit.data?.items.length ? (
                <div className="grid min-w-0 gap-2">
                  {/* 件数はツールバーの右の 1 か所（以前は見出しのバッジ「件数: N」。#1891）。 */}
                  <ListToolbar count={listTotal(audit.data.total)} testId="audit-list-toolbar" />
                  <AuditRecordsTable records={audit.data.items} />
                  {auditPager}
                </div>
              ) : (
                <>
                  <EmptyState title={t("audit.noRecords")} />
                  {auditPager}
                </>
              )}
            </QueryState>
          </CardContent>
        </Card>
      </PageBody>
    </>
  );
}

function AuditRecordsTable({ records }: { records: ToolCallAuditRecord[] }) {
  const columns: DataTableColumn<ToolCallAuditRecord>[] = [
    {
      key: "run_goal",
      header: t("audit.runGoal"),
      className: "max-w-72",
      render: (record) => (
        <>
          <p className="break-words text-sm font-medium text-fg [overflow-wrap:anywhere]">{record.run_goal}</p>
          <p className="mt-1 break-all text-xs text-fg-muted">{record.run_id}</p>
          <p className="mt-1 text-xs text-fg-muted">{formatDate(record.run_created_at)}</p>
        </>
      ),
    },
    {
      key: "run_source",
      header: t("run.source"),
      className: "text-xs text-fg",
      render: (record) => runSourceLabel(record.run_source, record.api_key_name),
    },
    {
      key: "tool_name",
      header: t("audit.toolName"),
      render: (record) => (
        <>
          <p className="break-all font-medium text-fg">{record.tool_name}</p>
          {record.error_code ? (
            <p className="mt-1 break-words text-xs text-danger-fg [overflow-wrap:anywhere]">{record.error_code}</p>
          ) : null}
        </>
      ),
    },
    {
      key: "status",
      header: t("audit.stepStatus"),
      render: (record) => (
        <StatusBadge {...stepStatusView(record.status)} />
      ),
    },
    {
      key: "approval_status",
      header: t("audit.approvalStatus"),
      render: (record) =>
        record.approval_status ? (
          <StatusBadge {...approvalStatusView(record.approval_status)} />
        ) : (
          <span className="text-xs text-fg-muted">-</span>
        ),
    },
    {
      key: "policy_decision",
      header: t("run.auditPolicy"),
      render: (record) =>
        record.policy_decision ? (
          <StatusBadge {...policyDecisionView(record.policy_decision)} icon={false} />
        ) : (
          <span className="text-xs text-fg-muted">-</span>
        ),
    },
    {
      key: "permission_level",
      header: t("common.permission"),
      render: (record) => (
        record.permission_level ? (
          <StatusBadge {...permissionView(record.permission_level)} icon={false} />
        ) : (
          <span className="text-xs text-fg-muted">-</span>
        )
      ),
    },
    {
      key: "guardrail_warnings",
      header: t("audit.guardrailWarnings"),
      className: "max-w-64",
      render: (record) =>
        record.guardrail_warnings.length ? (
          <div className="space-y-1">
            {record.guardrail_warnings.map((warning) => (
              <p key={warning} className="break-words text-xs text-warning-fg [overflow-wrap:anywhere]">
                {warning}
              </p>
            ))}
          </div>
        ) : (
          <span className="text-xs text-fg-muted">-</span>
        ),
    },
    {
      key: "duration_ms",
      header: t("run.auditDuration"),
      className: "text-xs text-fg",
      render: (record) =>
        record.duration_ms === null || record.duration_ms === undefined ? "-" : `${record.duration_ms}ms`,
    },
    {
      key: "trace_id",
      header: t("run.auditTrace"),
      className: "max-w-48",
      render: (record) => (
        <>
          <p className="break-all text-xs text-fg-muted">{record.trace_id ?? "-"}</p>
          {record.artifact_ids.length ? (
            <p className="mt-1 text-xs text-fg-muted">{`${t("run.auditArtifacts")}: ${record.artifact_ids.length}`}</p>
          ) : null}
        </>
      ),
    },
  ];

  return (
    <DataTable
      rows={records}
      columns={columns}
      getRowKey={(record) => `${record.run_id}:${record.step_id}`}
      rowProps={() => ({ className: `align-top ${INFORMATION_TABLE_ROW_CLASS}` })}
      tableClassName="w-full min-w-[70rem]"
      ariaLabel={t("audit.records")}
      scrollAriaLabel={listScrollLabel(t("audit.records"))}
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
    />
  );
}
