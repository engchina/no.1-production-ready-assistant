import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ClipboardList, Download, Eye, FlaskConical, Pencil, Plus, Trash2 } from "lucide-react";
import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ClarificationRecord,
  DataTable,
  DEFAULT_PAGE_SIZE,
  Disclosure,
  EmptyState,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  ListSkeleton,
  ListToolbar,
  OffsetPagination,
  PageBody,
  PageHeader,
  PAGE_SIZE_OPTIONS,
  ProcessingIndicator,
  RowActionMenu,
  RowTitleButton,
  SelectField,
  SideSheet,
  StatusBadge,
  TableSkeleton,
  TimedLoadingState,
  toast,
  useConfirm,
  type ClarificationRecordEntry,
  type DataTableColumn,
  type EntityAction,
  type StatusVariant,
  ApiErrorState,
  apiErrorMessage,
  adjacentListItems,
} from "@production-ready/ui";

import { AnswerBody } from "@/components/chat/ResultTables";
import { MissingEditorTarget } from "@/components/EntityLayout";
import { listTotal } from "@/components/ListFilters";
import { agentPaginationLabels, listScrollLabel, PagedDataTable, usePersistedServerPaging } from "@/components/ListViews";
import { EvaluationCompare } from "@/components/evaluation/EvaluationCompare";
import { EvaluationSetEditor, downloadBlob } from "@/components/evaluation/EvaluationSetEditor";
import {
  EvaluationVersionField,
  evaluatedVersionLabel,
  usableEvaluationTarget,
  useCanEvaluateDraft,
} from "@/components/evaluation/EvaluationVersionField";
import {
  agentApi,
  type AgentProfile,
  type EvaluationCaseResult,
  type EvaluationCapabilitySummary,
  type EvaluationCaseTrials,
  type EvaluationFieldResult,
  type EvaluationInterval,
  type EvaluationStability,
  type EvaluationOutputValue,
  type EvaluationClarificationRound,
  type EvaluationJob,
  type EvaluationJobItem,
  type EvaluationJobStatus,
  type EvaluationSetItem,
  type EvaluationSummary,
  type EvaluationTarget,
} from "@/lib/api";
import { useEditorRoute } from "@/lib/editor-route";
import { formatDateTime, formatNumber } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { isNullableString, isString, useWorkspaceState } from "@/lib/workspace-state";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";

// 品質評価（#776）。業務 Agent ごとの評価セット（評価ケースの集まり）で業務 Agent を実行し、
// 期待する回答の要点と比べて合否を判定する。評価の Run は承認が要るツールを実行しない（dry-run）で、
// 期待するツールを指定したケースはツールの選択も判定する。画面の形は RAG の品質評価・NL2SQL の
// SQL生成評価にそろえる（評価セット → 実行状況 → 評価概要（前回との比較）→ ケース別結果 → 最近の評価）。

const POLL_INTERVAL_MS = 1500;
// 最近の評価の 1 ページ（サーバー側のページング。終わった評価は削除せずに残る。#794・#3235）。
const JOBS_PAGE_SIZE = DEFAULT_PAGE_SIZE;
const ACTIVE: ReadonlySet<EvaluationJobStatus> = new Set(["queued", "running"]);
// 1 ケースを反復する回数の選択肢（#62。backend の上限は 10 回）。
const TRIAL_OPTIONS = [1, 2, 3, 5, 10] as const;

/** ケース × 試行の結果のキー（#62。反復しない評価ではケースの ID と同じ）。 */
function resultKey(result: EvaluationCaseResult): string {
  const trial = result.trial ?? 1;
  return trial > 1 ? `${result.case.id}#${trial}` : result.case.id;
}

export function EvaluationPage() {
  const queryClient = useQueryClient();
  const editor = useEditorRoute();
  const [agentId, setAgentId] = useWorkspaceState("evaluation", "agentId", "", isString);
  const [jobId, setJobId] = useWorkspaceState<"evaluation", string | null>("evaluation", "jobId", null, isNullableString);
  // 業務 Agent の画面の「品質評価で比べる」（`?agent=<id>`。#1867）。その業務 Agent を選び、URL からは外す。
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedAgentId = searchParams.get("agent");
  useEffect(() => {
    if (!linkedAgentId) return;
    setAgentId(linkedAgentId);
    setJobId(null);
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("agent");
        return next;
      },
      { replace: true }
    );
  }, [linkedAgentId, setAgentId, setJobId, setSearchParams]);

  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents });
  const usableAgents = useMemo(
    () => (agents.data?.agents ?? []).filter((agent) => agent.enabled),
    [agents.data]
  );
  const selectedAgentId = usableAgents.some((agent) => agent.id === agentId) ? agentId : (usableAgents[0]?.id ?? "");
  const selectedAgent = usableAgents.find((agent) => agent.id === selectedAgentId);
  // 評価する版（#810）。選んだ業務 Agent ごとの選択で、業務 Agent を変えたら既定に戻す。
  // 実行の意思なので作業状態には残さない（workspace-state.md）。
  const [versionChoice, setVersionChoice] = useState<{ agentId: string; value: EvaluationTarget } | null>(null);
  const canEvaluateDraft = useCanEvaluateDraft(selectedAgent);
  const target = usableEvaluationTarget(
    selectedAgent,
    versionChoice?.agentId === selectedAgentId ? versionChoice.value : null,
    canEvaluateDraft
  );

  // 1 ケースを反復する回数（#62）。実行の意思なので作業状態には残さない。既定の 1 回は送らない。
  const [trials, setTrials] = useState(1);
  const start = useMutation({
    mutationFn: ({ setId, version, agentId: evaluated }: { setId: string; version: EvaluationTarget; agentId?: string }) =>
      agentApi.createEvaluation({
        set_id: setId,
        agent_version: version,
        ...(evaluated ? { agent_id: evaluated } : {}),
        ...(trials > 1 ? { trials } : {}),
      }),
    onSuccess: (created) => {
      // 一覧に戻ったとき、評価セットの欄も評価した業務 Agent のものにする（#2589。評価セットを URL で開いたとき、
      // 業務 Agent の選択は既定のままなので、評価概要と対象が食い違わないように）。
      setAgentId(created.agent_id);
      setJobId(created.id);
      queryClient.setQueryData(["evaluation", created.id], created);
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      toast.success(t("evaluation.form.started"));
      editor.backToList();
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });

  const editorTarget = editor.target;
  const editingId = editorTarget.kind === "edit" ? editorTarget.id : null;
  const editingSet = useQuery({
    queryKey: ["evaluation-set", editingId],
    queryFn: () => agentApi.getEvaluationSet(editingId ?? ""),
    enabled: Boolean(editingId),
    retry: false,
  });

  if (editorTarget.kind === "new") {
    return (
      <EvaluationSetEditor
        agents={usableAgents}
        defaultAgentId={selectedAgentId}
        onBack={() => editor.backToList()}
        onSaved={(saved) => {
          setAgentId(saved.agent_id);
          editor.openItem(saved.id, { replace: true });
        }}
        onStart={(set, version, evaluated) =>
          start.mutate({ setId: set.id, version, agentId: evaluated === set.agent_id ? undefined : evaluated })
        }
        onDeleted={() => editor.backToList({ replace: true })}
        starting={start.isPending}
      />
    );
  }
  if (editorTarget.kind === "edit") {
    if (editingSet.isLoading) {
      return (
        <PageBody wide>
          <TimedLoadingState label={t("loading.evaluationSet")} testId="evaluation-set-loading">
            <ListSkeleton rows={4} />
          </TimedLoadingState>
        </PageBody>
      );
    }
    if (!editingSet.data) {
      return <MissingEditorTarget id={editorTarget.id} onBack={() => editor.backToList({ replace: true })} />;
    }
    return (
      <EvaluationSetEditor
        key={editingSet.data.id}
        evaluationSet={editingSet.data}
        agents={usableAgents}
        defaultAgentId={selectedAgentId}
        onBack={() => editor.backToList()}
        onSaved={(saved) => queryClient.setQueryData(["evaluation-set", saved.id], saved)}
        onStart={(set, version, evaluated) =>
          start.mutate({ setId: set.id, version, agentId: evaluated === set.agent_id ? undefined : evaluated })
        }
        onDeleted={() => editor.backToList({ replace: true })}
        starting={start.isPending}
      />
    );
  }
  return (
    <EvaluationOverview
      agentId={selectedAgentId}
      agent={selectedAgent}
      version={target}
      onVersionChange={(value) => setVersionChoice({ agentId: selectedAgentId, value })}
      trials={trials}
      onTrialsChange={setTrials}
      onAgentChange={setAgentId}
      agentOptions={usableAgents.map((agent) => ({ value: agent.id, label: agent.name }))}
      jobId={jobId}
      onJobChange={setJobId}
      onCreate={() => editor.openNew()}
      onOpen={(set) => editor.openItem(set.id)}
      itemHref={editor.itemHref}
      onStart={(set) => start.mutate({ setId: set.id, version: target })}
      starting={start.isPending}
    />
  );
}

function EvaluationOverview({
  agentId,
  agent,
  version,
  onVersionChange,
  trials,
  onTrialsChange,
  onAgentChange,
  agentOptions,
  jobId,
  onJobChange,
  onCreate,
  onOpen,
  itemHref,
  onStart,
  starting,
}: {
  agentId: string;
  agent: AgentProfile | undefined;
  version: EvaluationTarget;
  onVersionChange: (version: EvaluationTarget) => void;
  trials: number;
  onTrialsChange: (trials: number) => void;
  onAgentChange: (agentId: string) => void;
  agentOptions: { value: string; label: string }[];
  jobId: string | null;
  onJobChange: (jobId: string | null) => void;
  onCreate: () => void;
  onOpen: (set: EvaluationSetItem) => void;
  itemHref: (id: string) => string;
  onStart: (set: EvaluationSetItem) => void;
  starting: boolean;
}) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  // シートで開いているケースの試行（`resultKey`）。内容は表示中の評価の結果から引き、実行中の評価の取り直しで更新する。
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);

  const sets = useQuery({
    queryKey: ["evaluation-sets", agentId],
    queryFn: () => agentApi.listEvaluationSets(agentId),
    enabled: Boolean(agentId),
  });
  // 最近の評価のページと表示件数（作業状態。#1891）。
  const jobsPaging = usePersistedServerPaging("evaluationJobs");
  // 最新の評価（1 ページ目）。表示する評価の既定と、実行中の評価の有無に使う。
  // 一覧が 1 ページ目のときは同じ query key なので 1 回だけ取得する。
  const latest = useQuery({
    queryKey: ["evaluations", 1],
    queryFn: () => agentApi.listEvaluations({ offset: 0, limit: JOBS_PAGE_SIZE }),
    // 表示していない実行中の評価（前の評価を表示している間の最新の評価）は、ここで終わりを待つ。
    // 表示中の評価は `job` が取り直す（#965）。
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const shown = jobId ?? items[0]?.id;
      return items.some((item) => ACTIVE.has(item.status) && item.id !== shown) ? POLL_INTERVAL_MS : false;
    },
  });
  const jobs = useQuery({
    // 既定の件数の 1 ページ目は `latest` と同じ query key（1 回だけ取得する）。
    queryKey:
      jobsPaging.limit === JOBS_PAGE_SIZE ? ["evaluations", jobsPaging.page] : ["evaluations", jobsPaging.page, jobsPaging.limit],
    queryFn: () => agentApi.listEvaluations({ offset: jobsPaging.offset, limit: jobsPaging.limit }),
    // ページを送っている間は今のページを出したまま取り直す。
    placeholderData: keepPreviousData,
  });
  // 表示する評価は、選んだ評価か、無ければ最新の評価。
  const shownJobId = jobId ?? latest.data?.items[0]?.id ?? null;
  const job = useQuery({
    queryKey: ["evaluation", shownJobId],
    queryFn: () => agentApi.getEvaluation(shownJobId ?? ""),
    enabled: Boolean(shownJobId),
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => (query.state.data && ACTIVE.has(query.state.data.status) ? POLL_INTERVAL_MS : false),
  });
  useEffect(() => {
    if (jobId && job.isError) onJobChange(null);
  }, [jobId, job.isError, onJobChange]);
  // シートのケースと「前へ / 次へ」の前後（評価の結果の並び。結果の表の並びと同じ）。
  const caseResults = job.data?.results ?? [];
  const selectedCase = caseResults.find((result) => resultKey(result) === selectedCaseId) ?? null;
  const { previous: previousCase, next: nextCase } = adjacentListItems(caseResults, selectedCaseId, resultKey);
  // 実行中の評価が終わったら、評価セットと最近の評価を取り直す。
  const shownStatus = job.data?.status;
  useEffect(() => {
    if (shownStatus && !ACTIVE.has(shownStatus)) {
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
    }
  }, [shownStatus, queryClient]);
  // 最新の一覧の実行中の評価が終わったら、評価セット（前回の結果）と最近の評価の他のページも取り直す（#965）。
  const latestActive = (latest.data?.items ?? []).some((item) => ACTIVE.has(item.status));
  const wasLatestActive = useRef(latestActive);
  useEffect(() => {
    if (wasLatestActive.current && !latestActive) {
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
    }
    wasLatestActive.current = latestActive;
  }, [latestActive, queryClient]);
  const running = latestActive || (job.data ? ACTIVE.has(job.data.status) : false);

  const cancel = useMutation({
    mutationFn: (id: string) => agentApi.cancelEvaluation(id),
    onSuccess: (cancelled) => {
      queryClient.setQueryData(["evaluation", cancelled.id], cancelled);
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      toast.success(t("evaluation.progress.cancelled"));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  const removeJob = useMutation({
    mutationFn: (id: string) => agentApi.deleteEvaluation(id),
    onSuccess: (_data, id) => {
      if (jobId === id) onJobChange(null);
      queryClient.removeQueries({ queryKey: ["evaluation", id] });
      void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
      toast.success(t("evaluation.jobs.deleted"));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  // 業種テンプレートの評価ケースで評価セットを作る（評価セットの無い、テンプレートから作った業務 Agent。#810）。
  const fromTemplate = useMutation({
    mutationFn: (id: string) => agentApi.createEvaluationSetFromTemplate(id),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      toast.success(t("evaluation.sets.fromTemplateCreated", { name: created.name }));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  const removeSet = useMutation({
    mutationFn: (id: string) => agentApi.deleteEvaluationSet(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      toast.success(t("evaluation.set.deleted"));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });

  async function confirmDeleteJob(item: EvaluationJobItem) {
    const ok = await confirm({
      title: t("evaluation.jobs.deleteTitle"),
      description: t("evaluation.jobs.deleteDescription", {
        agent: item.set_name || item.agent_name || item.agent_id,
        started: formatDateTime(item.created_at),
      }),
      confirmLabel: t("evaluation.jobs.delete"),
      tone: "danger",
    });
    if (ok) removeJob.mutate(item.id);
  }

  async function confirmDeleteSet(item: EvaluationSetItem) {
    const ok = await confirm({
      title: t("evaluation.set.deleteTitle", { name: item.name }),
      description: t("evaluation.set.deleteDescription"),
      confirmLabel: t("evaluation.set.delete"),
      tone: "danger",
    });
    if (ok) removeSet.mutate(item.id);
  }

  async function exportSet(item: EvaluationSetItem) {
    try {
      downloadBlob(await agentApi.downloadEvaluationSetXlsx(item.id), `${item.name}.xlsx`);
    } catch (error) {
      toast.error(apiErrorMessage(error, t("common.error.operation")));
    }
  }

  return (
    <>
      <PageHeader
        wide
        title={t("nav.evaluation")}
        subtitle={t("page.evaluation.subtitle")}
        actions={[
          {
            id: "create",
            kind: "primary",
            label: t("evaluation.set.create"),
            icon: Plus,
            disabled: !agentId,
            onClick: onCreate,
          },
        ]}
      />
      <PageBody wide>
        <NonPersistentStorageNotice />
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("evaluation.sets.title")}</CardTitle>
            <CardDescription>{t("evaluation.sets.description")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
              <SelectField<string>
                id="evaluation-agent"
                label={t("evaluation.form.agent")}
                width="md"
                value={agentId}
                options={agentOptions}
                onValueChange={onAgentChange}
                disabled={agentOptions.length === 0}
              />
              <EvaluationVersionField id="evaluation-version" agent={agent} value={version} onChange={onVersionChange} />
              <SelectField<string>
                id="evaluation-trials"
                data-testid="evaluation-trials"
                label={t("evaluation.form.trials")}
                helper={t("evaluation.form.trialsHelper")}
                width="sm"
                value={String(trials)}
                options={TRIAL_OPTIONS.map((count) => ({ value: String(count), label: t("evaluation.form.trialsOption", { count }) }))}
                onValueChange={(value) => onTrialsChange(Number(value))}
              />
            </div>
            {sets.isLoading ? (
              <TimedLoadingState label={t("loading.evaluationSets")} testId="evaluation-sets-loading">
                <TableSkeleton columns={4} />
              </TimedLoadingState>
            ) : sets.error ? (
              // 取得の失敗を「評価セットがありません」と出さない（messaging.md §3.6。#818）。
              <ApiErrorState error={sets.error} fallback={t("common.error.load")} retryLabel={t("common.retry")} onRetry={() => void sets.refetch()} />
            ) : (sets.data?.sets ?? []).length === 0 ? (
              <EmptyState
                title={t("evaluation.sets.empty")}
                hint={agent?.template_id ? t("evaluation.sets.fromTemplateHint") : t("evaluation.sets.emptyHint")}
                action={
                  <div className="flex flex-wrap justify-center gap-2">
                    {agent?.template_id ? (
                      <Button
                        icon={ClipboardList}
                        loading={fromTemplate.isPending}
                        onClick={() => fromTemplate.mutate(agent.id)}
                        data-testid="evaluation-sets-from-template"
                      >
                        {t("evaluation.sets.fromTemplate")}
                      </Button>
                    ) : null}
                    <Button variant="secondary" icon={Plus} onClick={onCreate} disabled={!agentId}>
                      {t("evaluation.set.create")}
                    </Button>
                  </div>
                }
              />
            ) : (
              <SetsTable
                sets={sets.data?.sets ?? []}
                running={running || starting}
                itemHref={itemHref}
                onOpen={onOpen}
                onStart={onStart}
                onExport={(item) => void exportSet(item)}
                onDelete={(item) => void confirmDeleteSet(item)}
              />
            )}
            {running ? <p className="text-xs text-fg-muted">{t("evaluation.form.busy")}</p> : null}
          </CardContent>
        </Card>

        {shownJobId && job.isLoading ? (
          <TimedLoadingState label={t("loading.evaluation")} testId="evaluation-loading">
            <TableSkeleton columns={4} />
          </TimedLoadingState>
        ) : job.data ? (
          <JobView
            job={job.data}
            cancelling={cancel.isPending}
            onCancel={() => cancel.mutate(job.data.id)}
            selectedCaseId={selectedCaseId}
            onOpenCase={(result) => setSelectedCaseId(resultKey(result))}
          />
        ) : null}

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("evaluation.jobs.title")}</CardTitle>
            <CardDescription>{t("evaluation.jobs.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            {jobs.isLoading ? (
              <TimedLoadingState label={t("loading.evaluations")} testId="evaluations-loading">
                <ListSkeleton rows={3} />
              </TimedLoadingState>
            ) : jobs.error && !jobs.data ? (
              <ApiErrorState error={jobs.error} fallback={t("common.error.load")} retryLabel={t("common.retry")} onRetry={() => void jobs.refetch()} />
            ) : !jobs.data || jobs.data.total === 0 ? (
              <EmptyState title={t("evaluation.jobs.empty")} hint={t("evaluation.jobs.emptyHint")} />
            ) : (
              <div className="grid min-w-0 gap-2">
                <ListToolbar count={listTotal(jobs.data.total)} testId="evaluation-jobs-toolbar" />
                <JobsTable
                  jobs={jobs.data.items}
                  shownJobId={shownJobId}
                  onShow={(item) => onJobChange(item.id)}
                  onDelete={(item) => void confirmDeleteJob(item)}
                />
                <OffsetPagination
                  offset={jobs.data.offset}
                  limit={jobs.data.limit}
                  total={jobs.data.total}
                  count={jobs.data.items.length}
                  onPageChange={(page) => jobsPaging.setPage(page)}
                  onPageSizeChange={jobsPaging.setPageSize}
                  pageSizeOptions={PAGE_SIZE_OPTIONS}
                  labels={agentPaginationLabels()}
                  ariaLabel={t("evaluation.jobs.pagerLabel")}
                  testId="evaluation-jobs-pagination"
                />
              </div>
            )}
          </CardContent>
        </Card>

        <EvaluationCompare />

        {/* ケースの詳細は一覧の文脈を保ったまま 1 件をのぞく補助の内容なので、40rem（md）のシートで読み、
            見出しの行の「前へ / 次へ」で評価の結果の前後のケースへ移る（page-archetypes.md §1「詳細の出し方」。#2283）。 */}
        <SideSheet
          open={selectedCase !== null}
          onClose={() => setSelectedCaseId(null)}
          title={t("evaluation.detail.title")}
          closeLabel={t("evaluation.detail.close")}
          id="evaluation-case-detail"
          side="right"
          size="md"
          pager={{
            onPrevious: previousCase ? () => setSelectedCaseId(resultKey(previousCase)) : null,
            onNext: nextCase ? () => setSelectedCaseId(resultKey(nextCase)) : null,
            labels: { previous: t("listDetail.previous"), next: t("listDetail.next") },
            testId: "evaluation-case-pager",
          }}
        >
          {selectedCase ? <CaseDetail result={selectedCase} /> : null}
        </SideSheet>
      </PageBody>
    </>
  );
}

function SetsTable({
  sets,
  running,
  itemHref,
  onOpen,
  onStart,
  onExport,
  onDelete,
}: {
  sets: EvaluationSetItem[];
  running: boolean;
  itemHref: (id: string) => string;
  onOpen: (item: EvaluationSetItem) => void;
  onStart: (item: EvaluationSetItem) => void;
  onExport: (item: EvaluationSetItem) => void;
  onDelete: (item: EvaluationSetItem) => void;
}) {
  const columns: DataTableColumn<EvaluationSetItem>[] = [
    {
      key: "name",
      header: t("evaluation.set.name"),
      rowHeader: true,
      className: "min-w-56",
      render: (item) => (
        <RowTitleButton
          title={item.name}
          subtitle={item.description || undefined}
          href={itemHref(item.id)}
          onClick={() => onOpen(item)}
        />
      ),
    },
    {
      key: "cases",
      header: t("evaluation.jobs.cases"),
      align: "right",
      className: "tabular-nums text-fg",
      render: (item) => formatNumber(item.case_count),
    },
    {
      key: "last",
      header: t("evaluation.sets.last"),
      render: (item) =>
        item.last_job_status ? (
          <span className="inline-flex flex-wrap items-center gap-2">
            <StatusBadge
              variant={jobStatusVariant(item.last_job_status)}
              label={t(`evaluation.jobStatus.${item.last_job_status}` as I18nKey)}
            />
            <span className="text-xs tabular-nums text-fg">{formatRate(item.last_pass_rate)}</span>
          </span>
        ) : (
          <span className="text-xs text-fg-muted">{t("evaluation.sets.never")}</span>
        ),
    },
    {
      key: "updated",
      header: t("common.updatedAt"),
      className: "whitespace-nowrap text-xs tabular-nums text-fg-muted",
      render: (item) => formatDateTime(item.updated_at),
    },
    {
      key: "actions",
      header: t("settings.mcpServers.actions"),
      align: "right",
      render: (item) => {
        const actions: EntityAction[] = [
          {
            id: "start",
            label: t("evaluation.form.start"),
            icon: FlaskConical,
            disabled: running,
            onSelect: () => onStart(item),
            testId: `evaluation-set-start-${item.id}`,
          },
          { id: "edit", label: t("evaluation.set.edit"), icon: Pencil, onSelect: () => onOpen(item) },
          { id: "export", label: t("evaluation.set.export"), icon: Download, onSelect: () => onExport(item) },
          { id: "delete", label: t("evaluation.set.delete"), icon: Trash2, tone: "danger", onSelect: () => onDelete(item) },
        ];
        return (
          <RowActionMenu
            actions={actions}
            ariaLabel={t("common.entityActions", { name: item.name })}
            testId={`evaluation-set-row-actions-${item.id}`}
          />
        );
      },
    },
  ];
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar count={listTotal(sets.length)} testId="evaluation-sets-toolbar" />
      <PagedDataTable<EvaluationSetItem>
        pageKey="evaluationSets"
        rows={sets}
        columns={columns}
        getRowKey={(item) => item.id}
        ariaLabel={t("evaluation.sets.label")}
        tableClassName="w-full min-w-[51rem]"
      />
    </div>
  );
}

function JobView({
  job,
  cancelling,
  onCancel,
  selectedCaseId,
  onOpenCase,
}: {
  job: EvaluationJob;
  cancelling: boolean;
  onCancel: () => void;
  /** シートで開いているケース（結果の表の題名のボタンに aria-current を付ける）。 */
  selectedCaseId: string | null;
  onOpenCase: (result: EvaluationCaseResult) => void;
}) {
  const active = ACTIVE.has(job.status);
  const { summary } = job;
  return (
    <div className="space-y-4">
      {active ? (
        <Card>
          <CardHeader>
            <CardTitle>{t("evaluation.progress.title")}</CardTitle>
          </CardHeader>
          <CardContent>
            <ProcessingIndicator
              active
              operationKey={job.id}
              startedAt={job.started_at ?? job.created_at}
              placement="job"
              label={
                job.status === "queued"
                  ? t("evaluation.progress.queued")
                  : t("evaluation.progress.running", {
                      agent: job.set_name || job.agent_name || job.agent_id,
                      done: summary.completed,
                      total: summary.total,
                    })
              }
              onCancel={cancelling ? undefined : onCancel}
              labels={{ cancel: t("evaluation.progress.cancel") }}
              testId="evaluation-progress"
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle>{t("evaluation.summary.title")}</CardTitle>
            <StatusBadge variant={jobStatusVariant(job.status)} label={t(`evaluation.jobStatus.${job.status}` as I18nKey)} />
          </div>
          <CardDescription>
            {t("evaluation.summary.description", {
              set: job.set_name || "—",
              agent: job.agent_name || job.agent_id,
              version: evaluatedVersionLabel(job.agent_version),
              started: formatDateTime(job.created_at),
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {job.status === "failed" ? (
            // 1 文目に何が起きたかと対処、サーバーのエラーの本文は「詳細」に畳む（失敗なので開いて出す。messaging.md §10.3）。
            <Banner severity="danger" title={t("evaluation.summary.failed")}>
              <div className="min-w-0 space-y-2">
                <p>{t("evaluation.summary.failedHint")}</p>
                {job.error ? (
                  <Disclosure variant="plain" size="sm" summary={t("evaluation.summary.failedDetails")} defaultOpen>
                    <p className="break-words text-xs text-fg-muted [overflow-wrap:anywhere]">{job.error}</p>
                  </Disclosure>
                ) : null}
              </div>
            </Banner>
          ) : null}
          <SummaryMetrics summary={summary} previous={job.previous_summary} />
          <p className="text-xs text-fg-muted" data-testid="evaluation-previous">
            {/* どの版どうしを比べたかを出す（同じ評価ケースの前回とだけ比べる。#810）。 */}
            {job.previous_summary
              ? t("evaluation.summary.comparedWithPrevious", {
                  version: evaluatedVersionLabel(job.previous_agent_version),
                  started: job.previous_created_at ? formatDateTime(job.previous_created_at) : "—",
                })
              : t("evaluation.summary.noPrevious")}
          </p>
        </CardContent>
      </Card>

      {summary.capabilities?.length ? (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("evaluation.capability.title")}</CardTitle>
            <CardDescription>{t("evaluation.capability.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            <CapabilityTable items={summary.capabilities} jobId={job.id} />
          </CardContent>
        </Card>
      ) : null}

      {(summary.trials ?? 1) > 1 && job.case_trials ? (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("evaluation.stability.title")}</CardTitle>
            <CardDescription>{t("evaluation.stability.description", { trials: summary.trials ?? 1 })}</CardDescription>
          </CardHeader>
          <CardContent>
            <StabilityTable items={job.case_trials} jobId={job.id} />
          </CardContent>
        </Card>
      ) : null}

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>{t("evaluation.results.title")}</CardTitle>
          <CardDescription>{t("evaluation.results.description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <ResultsTable
            results={job.results}
            jobId={job.id}
            trials={summary.trials ?? 1}
            selectedCaseId={selectedCaseId}
            onOpen={onOpenCase}
          />
        </CardContent>
      </Card>
    </div>
  );
}

/** 95% の区間の文（#62。区間の無い評価は空）。 */
function intervalText(interval: EvaluationInterval | null | undefined): string {
  return interval ? t("evaluation.summary.interval", { low: formatRate(interval[0]), high: formatRate(interval[1]) }) : "";
}

function SummaryMetrics({ summary, previous }: { summary: EvaluationSummary; previous: EvaluationSummary | null }) {
  const ofTotal = t("evaluation.summary.ofTotal", { total: formatNumber(summary.total) });
  const trials = summary.trials ?? 1;
  const passRateBasis = [
    previous ? rateDelta(summary.pass_rate, previous.pass_rate) : ofTotal,
    intervalText(summary.pass_rate_interval),
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    // 統計のタイルはカードの幅を等分する（6 個なので 2 / 3 列、出力の項目を判定したら 8 個なので 2 / 4 列。
    // design-system README §4「wide 画面の 100% 充填」）。
    <div
      className={`grid grid-cols-2 gap-x-5 gap-y-4 ${summary.field_cases || trials > 1 ? "xl:grid-cols-4" : "xl:grid-cols-3"}`}
      data-testid="evaluation-summary"
    >
      <Metric label={t("evaluation.summary.passRate")} value={formatRate(summary.pass_rate)} detail={passRateBasis} />
      {trials > 1 ? (
        // 反復試行（#62）。Pass^k（k 回すべて成功）と Pass@k（どれか 1 回成功）は、k 回すべてを判定できたケースが分母。
        <>
          <Metric
            label={t("evaluation.summary.passHatK", { trials })}
            value={formatRate(summary.pass_hat_k ?? null)}
            detail={[
              t("evaluation.summary.completeCases", { count: summary.complete_cases ?? 0 }),
              intervalText(summary.pass_hat_k_interval),
            ]
              .filter(Boolean)
              .join(" · ")}
          />
          <Metric
            label={t("evaluation.summary.passAtK", { trials })}
            value={formatRate(summary.pass_at_k ?? null)}
            detail={intervalText(summary.pass_at_k_interval) || t("evaluation.summary.completeCases", { count: 0 })}
          />
          <Metric
            label={t("evaluation.summary.unstable")}
            value={formatNumber(summary.unstable_cases ?? 0)}
            detail={t("evaluation.summary.stableCases", { count: summary.stable_cases ?? 0 })}
          />
          <Metric
            label={t("evaluation.summary.missing")}
            value={formatNumber(summary.missing_trials ?? 0)}
            detail={t("evaluation.summary.incompleteCases", { count: summary.incomplete_cases ?? 0 })}
          />
        </>
      ) : null}
      <Metric
        label={t("evaluation.summary.averageScore")}
        value={summary.average_score === null ? "—" : summary.average_score.toFixed(2)}
        detail={previous ? scoreDelta(summary.average_score, previous.average_score) : t("evaluation.summary.scoreRange")}
      />
      <Metric
        label={t("evaluation.summary.toolAccuracy")}
        value={formatRate(summary.tool_accuracy)}
        detail={
          summary.tool_cases
            ? t("evaluation.summary.toolCases", { correct: summary.tool_correct, total: summary.tool_cases })
            : t("evaluation.summary.noToolCases")
        }
      />
      {summary.field_cases ? (
        // 出力の項目の正しさ（#2558。合格率には含めない）。
        <>
          <Metric
            label={t("evaluation.summary.fieldItemAccuracy")}
            value={formatRate(summary.field_item_accuracy ?? null)}
            detail={t("evaluation.summary.fieldItems", {
              correct: summary.field_items_correct ?? 0,
              total: summary.field_items ?? 0,
            })}
          />
          <Metric
            label={t("evaluation.summary.fieldAccuracy")}
            value={formatRate(summary.field_accuracy ?? null)}
            detail={t("evaluation.summary.fieldCases", {
              correct: summary.field_correct ?? 0,
              total: summary.field_cases,
            })}
          />
        </>
      ) : null}
      {/* 安全違反（#63。合格率とは別に数え、1 件でもあれば公開前の評価の門が断る）。 */}
      <Metric
        label={t("evaluation.summary.safetyViolations")}
        value={formatNumber(summary.safety_violation_cases ?? 0)}
        detail={t("evaluation.summary.safetyDetail", {
          trials: summary.safety_violations ?? 0,
          blocked: summary.blocked_attempts ?? 0,
        })}
      />
      {summary.approval_blocked ? <Metric label={t("evaluation.summary.approvalBlocked")} value={formatNumber(summary.approval_blocked)} detail={t("evaluation.summary.approvalBlockedDetail", { count: summary.approval_blocked })} /> : null}
      <Metric label={t("evaluation.summary.correct")} value={formatNumber(summary.correct)} detail={ofTotal} />
      <Metric
        label={t("evaluation.summary.incorrect")}
        value={formatNumber(summary.incorrect + summary.uncertain)}
        detail={t("evaluation.summary.uncertainIncluded", { count: summary.uncertain })}
      />
      <Metric
        label={t("evaluation.summary.errors")}
        value={formatNumber(summary.errors)}
        detail={summary.unjudged ? t("evaluation.summary.unjudgedIncluded", { count: summary.unjudged }) : ofTotal}
      />
    </div>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0 border-l-2 border-accent-emphasis pl-3">
      <p className="truncate text-xs text-fg-muted" title={label}>
        {label}
      </p>
      <p className="mt-0.5 text-xl font-semibold tabular-nums text-fg">{value}</p>
      <p className="mt-0.5 text-xs tabular-nums text-fg-muted">{detail}</p>
    </div>
  );
}

function ResultsTable({
  results,
  jobId,
  trials,
  selectedCaseId,
  onOpen,
}: {
  results: EvaluationCaseResult[];
  jobId: string;
  /** 1 ケースを反復した回数（#62。2 回以上なら試行の列を出す）。 */
  trials: number;
  selectedCaseId: string | null;
  onOpen: (result: EvaluationCaseResult) => void;
}) {
  const columns: DataTableColumn<EvaluationCaseResult>[] = [
    {
      key: "case",
      header: t("evaluation.column.case"),
      rowHeader: true,
      className: "min-w-56 max-w-96",
      render: (result) => (
        <RowTitleButton
          title={result.case.question}
          subtitle={result.case.id}
          current={resultKey(result) === selectedCaseId}
          onClick={() => onOpen(result)}
        />
      ),
    },
    ...(trials > 1
      ? [
          {
            key: "trial",
            header: t("evaluation.column.trial"),
            align: "right" as const,
            className: "whitespace-nowrap tabular-nums text-fg",
            render: (result: EvaluationCaseResult) => t("evaluation.trial.number", { trial: result.trial ?? 1, trials }),
          },
        ]
      : []),
    {
      key: "verdict",
      header: t("evaluation.column.verdict"),
      render: (result) => (
        <span className="flex flex-col items-start gap-1">
          <CaseBadge result={result} />
          <SafetyBadge result={result} />
          <ClarificationNote result={result} />
        </span>
      ),
    },
    {
      key: "score",
      header: t("evaluation.column.score"),
      align: "right",
      className: "tabular-nums text-fg",
      render: (result) => (result.judgement ? result.judgement.score.toFixed(2) : "—"),
    },
    {
      key: "tools",
      header: t("evaluation.column.tools"),
      className: "max-w-64 text-xs",
      render: (result) => <ToolSelection result={result} />,
    },
    // 出力の項目（#2558）。期待する出力のあるケースがあるときだけ出す。
    ...(results.some((result) => result.field_correct !== null && result.field_correct !== undefined)
      ? [
          {
            key: "output",
            header: t("evaluation.column.output"),
            className: "whitespace-nowrap text-xs",
            render: (result: EvaluationCaseResult) => <OutputBadge result={result} />,
          },
        ]
      : []),
    {
      key: "duration",
      header: t("evaluation.column.duration"),
      align: "right",
      className: "whitespace-nowrap tabular-nums text-xs text-fg-muted",
      render: (result) => (result.duration_ms === null ? "—" : `${(result.duration_ms / 1000).toFixed(1)} 秒`),
    },
  ];
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar count={listTotal(results.length)} testId="evaluation-results-toolbar" />
      <PagedDataTable<EvaluationCaseResult>
        pageKey="evaluationResults"
        rows={results}
        columns={columns}
        getRowKey={resultKey}
        ariaLabel={t("evaluation.results.label")}
        tableClassName="w-full min-w-[54rem]"
        resetKey={jobId}
      />
    </div>
  );
}

/** 能力分類の表示名（分類の無いケースは「未分類」）。 */
function capabilityLabel(capability: string): string {
  return capability || t("evaluation.capability.none");
}

/** 能力分類ごとの母数・判定・欠測・合格率（区間）・安全違反（#63）。 */
function CapabilityTable({ items, jobId }: { items: EvaluationCapabilitySummary[]; jobId: string }) {
  const columns: DataTableColumn<EvaluationCapabilitySummary>[] = [
    {
      key: "capability",
      header: t("evaluation.capability.column"),
      rowHeader: true,
      className: "min-w-40 break-words font-medium text-fg",
      render: (item) => capabilityLabel(item.capability),
    },
    {
      key: "cases",
      header: t("evaluation.jobs.cases"),
      align: "right",
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (item) => formatNumber(item.cases),
    },
    {
      key: "passRate",
      header: t("evaluation.summary.passRate"),
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (item) => (
        <span className="flex flex-col">
          <span>{formatRate(item.pass_rate)}</span>
          <span className="text-xs text-fg-muted">{intervalText(item.pass_rate_interval)}</span>
        </span>
      ),
    },
    {
      key: "judged",
      header: t("evaluation.capability.judged"),
      align: "right",
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (item) => t("evaluation.capability.judgedValue", { judged: item.judged_trials, missing: item.missing_trials }),
    },
    {
      key: "safety",
      header: t("evaluation.summary.safetyViolations"),
      render: (item) =>
        item.safety_violations ? (
          <StatusBadge variant="danger" label={t("evaluation.safety.count", { count: item.safety_violations })} />
        ) : (
          <StatusBadge variant="success" label={t("evaluation.safety.none")} />
        ),
    },
  ];
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar count={listTotal(items.length)} testId="evaluation-capability-toolbar" />
      <PagedDataTable<EvaluationCapabilitySummary>
        pageKey="evaluationCapabilities"
        rows={items}
        columns={columns}
        getRowKey={(item) => item.capability}
        ariaLabel={t("evaluation.capability.label")}
        tableClassName="w-full min-w-[40rem]"
        resetKey={jobId}
      />
    </div>
  );
}

const STABILITY_VARIANT: Record<EvaluationStability, StatusVariant> = {
  stable_pass: "success",
  stable_fail: "danger",
  unstable: "warning",
  incomplete: "neutral",
  unjudged: "neutral",
};

/** ケースごとの k 回の試行の安定性（#62。状態は文と色で出す）。 */
function StabilityTable({ items, jobId }: { items: EvaluationCaseTrials[]; jobId: string }) {
  const columns: DataTableColumn<EvaluationCaseTrials>[] = [
    {
      key: "case",
      header: t("evaluation.column.case"),
      rowHeader: true,
      className: "min-w-56 max-w-96",
      render: (item) => (
        <span className="flex min-w-0 flex-col">
          <span className="break-words font-medium text-fg">{item.question}</span>
          <span className="break-all text-xs text-fg-muted">{item.case_id}</span>
        </span>
      ),
    },
    {
      key: "stability",
      header: t("evaluation.stability.column"),
      render: (item) => (
        <StatusBadge variant={STABILITY_VARIANT[item.stability]} label={t(`evaluation.stability.${item.stability}` as I18nKey)} />
      ),
    },
    {
      key: "passed",
      header: t("evaluation.stability.passed"),
      align: "right",
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (item) => t("evaluation.stability.passedValue", { passed: item.passed, judged: item.judged }),
    },
    {
      key: "missing",
      header: t("evaluation.stability.missing"),
      align: "right",
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (item) => formatNumber(item.missing),
    },
  ];
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar count={listTotal(items.length)} testId="evaluation-stability-toolbar" />
      <PagedDataTable<EvaluationCaseTrials>
        pageKey="evaluationStability"
        rows={items}
        columns={columns}
        getRowKey={(item) => item.case_id}
        ariaLabel={t("evaluation.stability.label")}
        tableClassName="w-full min-w-[40rem]"
        resetKey={jobId}
      />
    </div>
  );
}

/** 出力の項目の正誤（#2558。正しい項目の数 / 判定した項目の数）。期待する出力の無いケースは「—」。 */
function OutputBadge({ result }: { result: EvaluationCaseResult }) {
  if (result.field_correct === null || result.field_correct === undefined) return <span className="text-fg-muted">—</span>;
  const items = result.field_results ?? [];
  const correct = items.filter((item) => item.correct).length;
  return (
    <StatusBadge
      variant={result.field_correct ? "success" : "danger"}
      label={t("evaluation.output.badge", { correct, total: items.length })}
    />
  );
}

function outputValue(value: EvaluationOutputValue): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? t("chat.output.yes") : t("chat.output.no");
  return String(value);
}

/** ケースの詳細の、出力の項目ごとの判定の表（#2558）。 */
function OutputFieldTable({ items }: { items: EvaluationFieldResult[] }) {
  const columns: DataTableColumn<EvaluationFieldResult>[] = [
    { key: "row", header: t("evaluation.output.row"), align: "right", className: "tabular-nums", render: (item) => item.row },
    { key: "field", header: t("evaluation.output.field"), render: (item) => item.label || item.field },
    { key: "expected", header: t("evaluation.output.expected"), className: "break-words", render: (item) => outputValue(item.expected) },
    { key: "actual", header: t("evaluation.output.actual"), className: "break-words", render: (item) => outputValue(item.actual) },
    {
      key: "correct",
      header: t("evaluation.output.result"),
      render: (item) => (
        <span className="flex flex-col items-start gap-1">
          <StatusBadge
            variant={item.correct ? "success" : "danger"}
            label={item.correct ? t("evaluation.output.correct") : t("evaluation.output.incorrect")}
          />
          {item.method === "meaning" ? <span className="text-fg-muted">{t("evaluation.output.byMeaning")}</span> : null}
          {item.reason ? <span className="break-words text-fg-muted">{item.reason}</span> : null}
          {item.note ? <span className="text-fg-muted">{item.note}</span> : null}
        </span>
      ),
    },
  ];
  return (
    <DataTable<EvaluationFieldResult>
      rows={items}
      columns={columns}
      getRowKey={(item) => `${item.row}-${item.field}`}
      ariaLabel={t("evaluation.detail.output")}
      scrollAriaLabel={listScrollLabel(t("evaluation.detail.output"))}
      tableClassName="w-full min-w-[32rem] text-xs"
    />
  );
}

function ToolSelection({ result }: { result: EvaluationCaseResult }) {
  return (
    <span className="flex flex-col items-start gap-1">
      {result.tool_selection_correct === null ? null : (
        <StatusBadge
          variant={result.tool_selection_correct ? "success" : "danger"}
          label={
            result.tool_selection_correct
              ? t("evaluation.tools.correct")
              : // すべて呼んだが順が違うときは、順が違うと出す（#1845）。
                result.tool_order_correct === false && allExpectedCalled(result)
                ? t("evaluation.tools.orderIncorrect")
                : t("evaluation.tools.incorrect")
          }
        />
      )}
      <span className="break-all font-mono text-fg-muted">
        {result.tool_calls.length ? result.tool_calls.join(", ") : t("evaluation.tools.none")}
        {result.blocked_tool_calls?.length ? <span className="block text-warning-fg">{t("evaluation.tools.blocked", { tools: result.blocked_tool_calls.join(", ") })}</span> : null}
        {result.executed_tool_calls ? <span className="block text-fg-muted">{t("evaluation.tools.executed", { tools: result.executed_tool_calls.join(", ") || t("evaluation.tools.none") })}</span> : null}
      </span>
    </span>
  );
}

/** 問い返しへの往復の数と、問い返しで終わったか（#1845。色だけでなく文で出す）。 */
function ClarificationNote({ result }: { result: EvaluationCaseResult }) {
  const rounds = result.clarification_rounds?.length ?? 0;
  if (!rounds && !result.ended_with_clarification) return null;
  return (
    <span className="text-xs text-fg-muted" data-testid="evaluation-clarification-note">
      {[
        rounds ? t("evaluation.clarification.rounds", { count: rounds }) : null,
        result.ended_with_clarification ? t("evaluation.clarification.ended") : null,
      ]
        .filter(Boolean)
        .join("・")}
    </span>
  );
}

/**
 * 問い返しの往復（#1845）。1 往復を 3 製品共通の `ClarificationRecord`（`variant="disclosure"`。回答前の確認の
 * 履歴と同じ表示。#1861）で出し、題は往復の番号と実行にする。次の実行へ送った文はその下に添える。
 */
function ClarificationRounds({ result }: { result: EvaluationCaseResult }) {
  const rounds = result.clarification_rounds ?? [];
  if (!rounds.length && !result.ended_with_clarification) return null;
  return (
    <DetailSection title={t("evaluation.clarification.detailTitle")}>
      <ol className="space-y-3" data-testid="evaluation-clarification-rounds">
        {rounds.map((round, index) => (
          <li key={round.run_id} className="min-w-0 space-y-1">
            <ClarificationRecord
              variant="disclosure"
              defaultOpen
              entries={clarificationRoundEntries(round)}
              labels={{ recordTitle: t("evaluation.clarification.detailRound", { number: index + 1, run: round.run_id }) }}
              testId={`evaluation-clarification-round-${index + 1}`}
            />
            <p className="whitespace-pre-wrap break-words text-xs text-fg">
              <span className="text-fg-muted">{`${t("evaluation.clarification.sent")}: `}</span>
              {round.message}
            </p>
          </li>
        ))}
      </ol>
      {result.ended_with_clarification ? (
        <p className="text-xs text-fg-muted">{t("evaluation.clarification.endedHint")}</p>
      ) : null}
    </DetailSection>
  );
}

/** 往復の確認事項と当てた答え → 共通の `ClarificationRecord` の確認事項（答えを当てなかった確認事項は「（答えなし）」）。 */
function clarificationRoundEntries(round: EvaluationClarificationRound): ClarificationRecordEntry[] {
  return round.questions.map((question, index) => ({
    id: question.item_id || String(index),
    prompt: question.label ? `${question.label}: ${question.question}` : question.question,
    selected: question.selected ?? [],
    otherText: question.other_text || undefined,
  }));
}

function allExpectedCalled(result: EvaluationCaseResult): boolean {
  return result.case.expected_tools.every((tool) =>
    result.tool_calls.some((called) => called === tool || called.endsWith(`__${tool}`))
  );
}

function caseStatusKey(result: EvaluationCaseResult): string {
  return result.status === "judged" && result.judgement ? result.judgement.verdict : result.status;
}

/** 安全違反（#63。合否とは別のバッジ。違反が無ければ出さない）。 */
function SafetyBadge({ result }: { result: EvaluationCaseResult }) {
  const count = result.safety_violations?.length ?? 0;
  return count ? <StatusBadge variant="danger" label={t("evaluation.safety.count", { count })} /> : null;
}

function CaseBadge({ result }: { result: EvaluationCaseResult }) {
  const key = caseStatusKey(result);
  return <StatusBadge variant={caseVariant(key)} label={t(`evaluation.status.${key}` as I18nKey)} />;
}

function caseVariant(key: string): StatusVariant {
  if (key === "correct") return "success";
  if (key === "incorrect") return "danger";
  if (key === "uncertain" || key === "needs_approval" || key === "approval_blocked" || key === "timed_out") return "warning";
  if (key === "running" || key === "pending") return "pending";
  if (key === "run_failed" || key === "judge_failed") return "danger";
  return "neutral";
}

function jobStatusVariant(status: EvaluationJobStatus): StatusVariant {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "cancelled") return "neutral";
  return "pending";
}

function CaseDetail({ result }: { result: EvaluationCaseResult }) {
  const judgement = result.judgement;
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <CaseBadge result={result} />
        <SafetyBadge result={result} />
        {result.case.capability ? (
          <span className="text-xs text-fg-muted">{t("evaluation.detail.capability", { capability: result.case.capability })}</span>
        ) : null}
        {judgement ? (
          <span className="tabular-nums text-fg">{`${t("evaluation.column.score")} ${judgement.score.toFixed(2)}`}</span>
        ) : null}
      </div>
      <DetailSection title={t("evaluation.detail.question")}>
        <p data-surface-tint="accent" className="whitespace-pre-wrap break-words rounded-md bg-accent-subtle px-3 py-2 text-fg">{result.case.question}</p>
      </DetailSection>
      <DetailSection title={t("evaluation.detail.expected")}>
        <p className="whitespace-pre-wrap break-words text-fg">{result.case.expected}</p>
      </DetailSection>
      <ClarificationRounds result={result} />
      <DetailSection title={t("evaluation.detail.answer")}>
        {result.answer ? (
          <div className="rounded-md border border-border p-3">
            <AnswerBody text={result.answer} className="text-sm text-fg" />
          </div>
        ) : (
          <p className="text-fg-muted">{t("evaluation.detail.noAnswer")}</p>
        )}
      </DetailSection>
      {judgement ? (
        <>
          <DetailSection title={t("evaluation.detail.reason")}>
            <p className="whitespace-pre-wrap break-words text-fg">{judgement.summary}</p>
          </DetailSection>
          {judgement.missing_points.length ? (
            <DetailSection title={t("evaluation.detail.missing")}>
              <ul className="list-disc space-y-1 pl-5 text-fg">
                {judgement.missing_points.map((point) => (
                  <li key={point} className="break-words">
                    {point}
                  </li>
                ))}
              </ul>
            </DetailSection>
          ) : null}
        </>
      ) : null}
      {result.field_results?.length ? (
        <DetailSection title={t("evaluation.detail.output")}>
          <div data-testid="evaluation-output-fields">
            <OutputFieldTable items={result.field_results} />
          </div>
        </DetailSection>
      ) : null}
      <DetailSection title={t("evaluation.detail.tools")}>
        <div className="space-y-1 text-xs">
          <p className="text-fg">
            <span className="text-fg-muted">{`${t("evaluation.detail.expectedTools")}: `}</span>
            <span className="font-mono">{result.case.expected_tools?.length ? result.case.expected_tools.join(", ") : "—"}</span>
            {result.case.expected_tools_in_order && result.case.expected_tools.length ? (
              <span className="text-fg-muted">{t("evaluation.detail.expectedToolsInOrder")}</span>
            ) : null}
          </p>
          <p className="text-fg">
            <span className="text-fg-muted">{`${t("evaluation.detail.calledTools")}: `}</span>
            <span className="font-mono">{result.tool_calls.length ? result.tool_calls.join(", ") : "—"}</span>
          </p>
          {result.blocked_tool_calls?.length ? <p className="text-warning-fg">{t("evaluation.tools.blocked", { tools: result.blocked_tool_calls.join(", ") })}</p> : null}
          {result.executed_tool_calls ? <p className="text-fg-muted">{t("evaluation.tools.executed", { tools: result.executed_tool_calls.join(", ") || t("evaluation.tools.none") })}</p> : null}
          <p className="text-fg-muted">{t("evaluation.detail.dryRunNote")}</p>
        </div>
      </DetailSection>
      {result.safety_violations?.length || result.blocked_attempts?.length || result.case.forbidden_tools?.length || result.case.completion_claims?.length ? (
        <DetailSection title={t("evaluation.detail.safety")}>
          <div className="space-y-1 text-xs" data-testid="evaluation-safety-detail">
            {result.safety_violations?.length ? (
              <ul className="list-disc space-y-1 pl-5 text-danger-fg">
                {result.safety_violations.map((violation, index) => (
                  <li key={`${violation.kind}-${violation.tool}-${index}`} className="break-words">
                    {t(`evaluation.safety.kind.${violation.kind}` as I18nKey, { tool: violation.tool })}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-fg">{t("evaluation.safety.none")}</p>
            )}
            {result.blocked_attempts?.length ? (
              <p className="text-fg-muted">{t("evaluation.safety.blocked", { tools: result.blocked_attempts.join(", ") })}</p>
            ) : null}
            {result.case.forbidden_tools?.length ? (
              <p className="text-fg-muted">{t("evaluation.safety.forbidden", { tools: result.case.forbidden_tools.join(", ") })}</p>
            ) : null}
            {result.case.completion_claims?.length ? (
              <p className="text-fg-muted">{t("evaluation.safety.claims", { claims: result.case.completion_claims.join("、") })}</p>
            ) : null}
          </div>
        </DetailSection>
      ) : null}
      {result.error ? (
        <DetailSection title={t("evaluation.detail.error")}>
          <p className="break-words text-fg">{result.error}</p>
        </DetailSection>
      ) : null}
      {(result.judge_attempts ?? 0) > 1 ? (
        <p className="text-xs text-fg-muted" data-testid="evaluation-judge-retried">
          {t("evaluation.detail.judgeRetried", { attempts: result.judge_attempts ?? 0 })}
        </p>
      ) : null}
      {result.run_id ? (
        <DetailSection title={t("evaluation.detail.run")}>
          <p className="break-all font-mono text-xs text-fg">{result.run_id}</p>
        </DetailSection>
      ) : null}
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1">
      <h3 className="text-xs font-semibold text-fg-muted">{title}</h3>
      {children}
    </section>
  );
}

function JobsTable({
  jobs,
  shownJobId,
  onShow,
  onDelete,
}: {
  jobs: EvaluationJobItem[];
  shownJobId: string | null;
  onShow: (item: EvaluationJobItem) => void;
  onDelete: (item: EvaluationJobItem) => void;
}) {
  const columns: DataTableColumn<EvaluationJobItem>[] = [
    {
      key: "started",
      header: t("evaluation.jobs.startedAt"),
      rowHeader: true,
      className: "whitespace-nowrap",
      render: (item) => (
        <RowTitleButton
          title={formatDateTime(item.created_at)}
          subtitle={item.id === shownJobId ? t("evaluation.jobs.showing") : undefined}
          onClick={() => onShow(item)}
        />
      ),
    },
    { key: "set", header: t("evaluation.jobs.set"), className: "text-xs text-fg", render: (item) => item.set_name || "—" },
    {
      key: "version",
      header: t("evaluation.version.column"),
      className: "whitespace-nowrap text-xs tabular-nums text-fg",
      render: (item) => evaluatedVersionLabel(item.agent_version),
    },
    { key: "agent", header: t("evaluation.form.agent"), className: "text-xs text-fg", render: (item) => item.agent_name || item.agent_id },
    {
      key: "status",
      header: t("evaluation.jobs.status"),
      render: (item) => (
        <StatusBadge variant={jobStatusVariant(item.status)} label={t(`evaluation.jobStatus.${item.status}` as I18nKey)} />
      ),
    },
    {
      key: "passRate",
      header: t("evaluation.summary.passRate"),
      align: "right",
      className: "tabular-nums text-fg",
      render: (item) => formatRate(item.summary.pass_rate),
    },
    {
      key: "actions",
      header: t("settings.mcpServers.actions"),
      align: "right",
      render: (item) => {
        const actions: EntityAction[] = [
          { id: "show", label: t("evaluation.jobs.show"), icon: Eye, onSelect: () => onShow(item) },
          {
            id: "delete",
            label: t("evaluation.jobs.delete"),
            icon: Trash2,
            tone: "danger",
            disabled: ACTIVE.has(item.status),
            onSelect: () => onDelete(item),
          },
        ];
        return (
          <RowActionMenu
            actions={actions}
            ariaLabel={t("common.entityActions", { name: formatDateTime(item.created_at) })}
            testId={`evaluation-row-actions-${item.id}`}
          />
        );
      },
    },
  ];
  return (
    <DataTable<EvaluationJobItem>
      rows={jobs}
      columns={columns}
      getRowKey={(item) => item.id}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      ariaLabel={t("evaluation.jobs.label")}
      scrollAriaLabel={listScrollLabel(t("evaluation.jobs.label"))}
      tableClassName="w-full min-w-[51rem]"
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
    />
  );
}

function formatRate(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function rateDelta(current: number | null, previous: number | null): string {
  if (current === null || previous === null) return t("evaluation.summary.previousUnknown");
  const delta = Math.round((current - previous) * 100);
  if (delta === 0) return t("evaluation.summary.previousSame");
  return t("evaluation.summary.previousPoints", { value: `${delta > 0 ? "+" : "−"}${Math.abs(delta)}` });
}

function scoreDelta(current: number | null, previous: number | null): string {
  if (current === null || previous === null) return t("evaluation.summary.previousUnknown");
  const delta = current - previous;
  if (Math.abs(delta) < 0.005) return t("evaluation.summary.previousSame");
  return t("evaluation.summary.previousScore", { value: `${delta > 0 ? "+" : "−"}${Math.abs(delta).toFixed(2)}` });
}
