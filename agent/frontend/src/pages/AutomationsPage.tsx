import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { History, Pencil, Play, Plus, RotateCcw, Save, ShieldCheck, Trash2 } from "lucide-react";
import {
  Banner,
  Button,
  ButtonLink,
  Card,
  CardContent,
  DataTable,
  EmptyState,
  FormActionBar,
  FormGrid,
  Fieldset,
  ListSkeleton,
  ListToolbar,
  ObjectActionBar,
  PageBody,
  PageHeader,
  RowActionMenu,
  RowTitleButton,
  SaveErrorBanner,
  Section,
  SelectField,
  StatusBadge,
  Switch,
  TableSkeleton,
  TextareaField,
  TextField,
  TimedLoadingState,
  toast,
  useConfirm,
  type DataTableColumn,
  type EntityAction,
  type StatusVariant,
  ApiErrorBanner,
  apiErrorMessage,
  displayTimeZone,
} from "@production-ready/ui";

import { useAuth } from "@/components/security/AuthProvider";
import { MissingEditorTarget } from "@/components/EntityLayout";
import { PagedDataTable, listScrollLabel } from "@/components/ListViews";
import { ListSearchField, listCount, matchesSearch, NoMatchState, useListSearch } from "@/components/ListFilters";
import {
  agentApi,
  type AgentProfile,
  type Automation,
  type AutomationInput,
  type AutomationRun,
  type AutomationSchedule,
  type AutomationToolCheck,
  type ScheduleFrequency,
} from "@/lib/api";
import { isRunnableAgent } from "@/lib/agent-availability";
import { useEditorRoute } from "@/lib/editor-route";
import { focusFirstInvalidField } from "@/lib/field-validation";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { sameDraft, useEditorLeaveGuard } from "@/lib/leave-guard";
import { useCapabilities } from "@/lib/permissions";
import { canOpenRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";
import { isValidTimezone } from "@/lib/timezones";
import { runStatusView } from "@/lib/status-labels";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";

// 業務 Agent の自動実行（スケジュール。#784・#1799）。一覧 → 全画面エディタ（A 型。`?id=`）。
// 自動実行は作った利用者として Run を作り、前回の Run が終わっていなければその回は飛ばす。

const FREQUENCIES: readonly ScheduleFrequency[] = ["daily", "weekdays", "weekly", "hourly"];
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

function defaultSchedule(): AutomationSchedule {
  return { frequency: "daily", time: "09:00", weekdays: [0], minute: 0, timezone: displayTimeZone() };
}

export function describeSchedule(schedule: AutomationSchedule): string {
  if (schedule.frequency === "hourly") return t("automation.describe.hourly", { minute: schedule.minute });
  if (schedule.frequency === "weekly") {
    const days = schedule.weekdays.map((day) => t(`automation.weekday.${day}` as I18nKey)).join("・");
    return t("automation.describe.weekly", { days, time: schedule.time });
  }
  return t(`automation.describe.${schedule.frequency}` as I18nKey, { time: schedule.time });
}

interface LastResultView {
  variant: StatusVariant;
  label: string;
  /** 前回の Run（Run を作ったときだけ。詳細へのリンクに使う）。 */
  runId: string | null;
}

/**
 * 「前回」の表示（#1812）。Run を作った回は「実行を作った」ことではなく、その Run の今の状態
 * （実行履歴と同じ `runStatusView`）を出す。Run を作れなかった回（飛ばした・開始できず）はその結果。
 * Run の記録が無い（保存期間を過ぎた）ときは「記録なし」。
 */
export function lastResultView(item: Automation): LastResultView | null {
  if (!item.last_result) return null;
  if (item.last_result === "skipped") return { variant: "warning", label: t("automation.result.skipped"), runId: null };
  if (item.last_result === "failed_to_start") {
    return { variant: "danger", label: t("automation.result.failed_to_start"), runId: null };
  }
  if (item.last_run_status) return { ...runStatusView(item.last_run_status), runId: item.last_run_id };
  return { variant: "neutral", label: t("automation.result.runMissing"), runId: null };
}

/** 「前回」の文（Run の回は日時、作れなかった回は理由）。 */
function lastResultMessage(item: Automation, view: LastResultView): string {
  if (item.last_result !== "created") return t("automation.lastResult", { message: item.last_message ?? "" });
  const time = formatDateTime(item.last_run_at);
  return view.runId ? t("automation.lastRun", { time }) : t("automation.lastRunMissing", { time });
}

export function AutomationsPage() {
  const editor = useEditorRoute();
  const capabilities = useCapabilities();
  const canManage = capabilities.admin;
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents });
  const usableAgents = useMemo(
    () => (agents.data?.agents ?? []).filter(isRunnableAgent),
    [agents.data]
  );
  const target = editor.target;
  const editingId = target.kind === "edit" ? target.id : null;
  const detail = useQuery({
    queryKey: ["automation", editingId],
    queryFn: () => agentApi.getAutomation(editingId ?? ""),
    enabled: Boolean(editingId),
    retry: false,
  });

  const allAgents = agents.data?.agents ?? [];
  if (target.kind === "new") {
    return (
      <AutomationEditor
        agents={usableAgents}
        allAgents={allAgents}
        agentsLoading={agents.isLoading}
        readOnly={!canManage}
        onBack={() => editor.backToList()}
        onCreated={(item) => editor.openItem(item.id, { replace: true })}
        onDeleted={() => editor.backToList({ replace: true })}
      />
    );
  }
  if (target.kind === "edit") {
    if (detail.isLoading) {
      return (
        <PageBody wide>
          <TimedLoadingState label={t("loading.automation")} testId="automation-loading">
            <ListSkeleton rows={4} />
          </TimedLoadingState>
        </PageBody>
      );
    }
    if (!detail.data) {
      return <MissingEditorTarget id={target.id} onBack={() => editor.backToList({ replace: true })} />;
    }
    return (
      <AutomationEditor
        key={detail.data.automation.id}
        automation={detail.data.automation}
        runs={detail.data.runs}
        agents={usableAgents}
        allAgents={allAgents}
        agentsLoading={agents.isLoading}
        readOnly={!canManage}
        onBack={() => editor.backToList()}
        onCreated={() => undefined}
        onDeleted={() => editor.backToList({ replace: true })}
      />
    );
  }
  return (
    <AutomationList
      canManage={canManage}
      agents={agents.data?.agents ?? []}
      onCreate={() => editor.openNew()}
      onOpen={(item) => editor.openItem(item.id)}
      itemHref={editor.itemHref}
    />
  );
}

function useRunNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => agentApi.runAutomation(id),
    onSuccess: (fired) => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      void queryClient.invalidateQueries({ queryKey: ["automation"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      if (fired.result === "created") toast.success(t("automation.ranNow"));
      else if (fired.result === "skipped") toast.warning(t("automation.skippedNow"));
      else toast.error(apiErrorMessage(fired, t("common.error.operation")));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
}

function useDeleteAutomation(onDeleted?: () => void) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const remove = useMutation({
    mutationFn: (id: string) => agentApi.deleteAutomation(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      toast.success(t("automation.deleted"));
      onDeleted?.();
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  return async (item: Automation) => {
    const ok = await confirm({
      title: t("automation.deleteTitle", { name: item.name }),
      description: t("automation.deleteDescription"),
      confirmLabel: t("automation.delete"),
      tone: "danger",
    });
    if (ok) remove.mutate(item.id);
  };
}

function AutomationList({
  canManage,
  agents,
  onCreate,
  onOpen,
  itemHref,
}: {
  canManage: boolean;
  agents: AgentProfile[];
  onCreate: () => void;
  onOpen: (item: Automation) => void;
  itemHref: (id: string) => string;
}) {
  const list = useQuery({ queryKey: ["automations"], queryFn: agentApi.listAutomations, refetchInterval: 30_000 });
  const runNow = useRunNow();
  const confirmDelete = useDeleteAutomation();
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));
  // 一覧の絞り込み（名前・業務 Agent。#808）。検索語は作業状態に残し、変わったら 1 ページ目へ戻す。
  const [query, setQuery] = useListSearch("automations");
  const allItems = list.data?.automations ?? [];
  const visibleItems = allItems.filter((item) =>
    matchesSearch(query, [item.name, item.goal, item.agent_id, agentNames.get(item.agent_id)])
  );

  const columns: DataTableColumn<Automation>[] = [
    {
      key: "name",
      header: t("automation.column.name"),
      rowHeader: true,
      className: "min-w-56",
      render: (item) => (
        <RowTitleButton
          title={item.name}
          subtitle={agentNames.get(item.agent_id) ?? item.agent_id}
          href={itemHref(item.id)}
          onClick={() => onOpen(item)}
        />
      ),
    },
    { key: "schedule", header: t("automation.column.schedule"), className: "text-xs text-fg", render: (item) => describeSchedule(item.schedule) },
    {
      key: "status",
      header: t("automation.column.status"),
      render: (item) => (
        <StatusBadge
          variant={item.enabled ? "success" : "neutral"}
          label={item.enabled ? t("automation.enabled") : t("automation.disabled")}
        />
      ),
    },
    {
      key: "next",
      header: t("automation.column.next"),
      className: "whitespace-nowrap text-xs tabular-nums text-fg",
      render: (item) => (item.next_run_at ? formatDateTime(item.next_run_at) : "—"),
    },
    {
      key: "last",
      header: t("automation.column.last"),
      render: (item) => {
        const view = lastResultView(item);
        return view ? (
          <span className="inline-flex flex-wrap items-center gap-2" data-testid={`automation-last-${item.id}`}>
            <StatusBadge variant={view.variant} label={view.label} />
            <span className="text-xs tabular-nums text-fg-muted">{formatDateTime(item.last_run_at)}</span>
          </span>
        ) : (
          <span className="text-xs text-fg-muted">—</span>
        );
      },
    },
  ];
  if (canManage) {
    columns.push({
      key: "actions",
      header: t("settings.mcpServers.actions"),
      align: "right",
      render: (item) => {
        const actions: EntityAction[] = [
          { id: "run", label: t("automation.runNow"), icon: Play, onSelect: () => runNow.mutate(item.id) },
          { id: "edit", label: t("automation.edit"), icon: Pencil, onSelect: () => onOpen(item) },
          { id: "delete", label: t("automation.delete"), icon: Trash2, tone: "danger", onSelect: () => void confirmDelete(item) },
        ];
        return (
          <RowActionMenu
            actions={actions}
            ariaLabel={t("common.entityActions", { name: item.name })}
            testId={`automation-row-actions-${item.id}`}
          />
        );
      },
    });
  }

  return (
    <>
      <PageHeader
        wide
        title={t("nav.automations")}
        subtitle={t("page.automations.subtitle")}
        actions={
          canManage
            ? [{ id: "create", kind: "primary", label: t("automation.create"), icon: Plus, onClick: onCreate }]
            : []
        }
      />
      <PageBody wide>
        <NonPersistentStorageNotice />
        <Card className="min-w-0">
          <CardContent className="pt-5">
            {list.isLoading ? (
              <TimedLoadingState label={t("loading.automations")} testId="automations-loading">
                <TableSkeleton columns={5} />
              </TimedLoadingState>
            ) : list.error ? (
              <ApiErrorBanner error={list.error} fallback={t("common.error.load")} />
            ) : allItems.length === 0 ? (
              <EmptyState
                title={t("automation.list.empty")}
                hint={t("automation.list.emptyHint")}
                action={
                  canManage ? (
                    <Button variant="secondary" icon={Plus} onClick={onCreate}>
                      {t("automation.create")}
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <div className="space-y-3">
                <ListToolbar
                  search={
                    <ListSearchField
                      id="automation-search"
                      label={t("automation.search")}
                      value={query}
                      onSearch={setQuery}
                    />
                  }
                  count={listCount(allItems.length, visibleItems.length, Boolean(query.trim()))}
                  testId="automation-list-toolbar"
                />
                <PagedDataTable<Automation>
                  pageKey="automations"
                  resetKey={query}
                  rows={visibleItems}
                  columns={columns}
                  getRowKey={(item) => item.id}
                  ariaLabel={t("automation.list.label")}
                  tableClassName="w-full min-w-[54rem]"
                  empty={<NoMatchState title={t("automation.noMatch")} onClear={() => setQuery("")} />}
                />
              </div>
            )}
          </CardContent>
        </Card>
      </PageBody>
    </>
  );
}

interface AutomationDraft {
  agentId: string;
  name: string;
  goal: string;
  enabled: boolean;
  schedule: AutomationSchedule;
}

function draftOf(item: Automation | undefined, agents: AgentProfile[]): AutomationDraft {
  return {
    agentId: item?.agent_id ?? agents[0]?.id ?? "",
    name: item?.name ?? "",
    goal: item?.goal ?? "",
    enabled: item?.enabled ?? true,
    schedule: item?.schedule ?? defaultSchedule(),
  };
}

function toPayload(draft: AutomationDraft): AutomationInput {
  return {
    agent_id: draft.agentId,
    name: draft.name.trim(),
    goal: draft.goal.trim(),
    enabled: draft.enabled,
    schedule: draft.schedule,
  };
}

function AutomationEditor({
  automation,
  runs = [],
  agents,
  allAgents,
  agentsLoading,
  readOnly,
  onBack,
  onCreated,
  onDeleted,
}: {
  automation?: Automation;
  runs?: AutomationRun[];
  /** 自動実行に選べる（実行できる）業務 Agent。 */
  agents: AgentProfile[];
  /** すべての業務 Agent（保存済みの業務 Agent が実行できなくなったときの表示名に使う）。 */
  allAgents: AgentProfile[];
  agentsLoading: boolean;
  readOnly: boolean;
  onBack: () => void;
  onCreated: (item: Automation) => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const [baseline, setBaseline] = useState<AutomationDraft>(() => draftOf(automation, agents));
  const [draft, setDraft] = useState<AutomationDraft>(baseline);
  // 新規で業務 Agent の一覧が後から届いたら（`?id=new` を直接開いたとき）、まだ選んでいない下書きを先頭の候補で埋める。
  // 基準も同じにして、未保存の変更として扱わない（#927）。
  if (!automation && !baseline.agentId && agents.length) {
    const firstAgentId = agents[0].id;
    setBaseline((current) => ({ ...current, agentId: firstAgentId }));
    setDraft((current) => (current.agentId ? current : { ...current, agentId: firstAgentId }));
  }
  const [submitted, setSubmitted] = useState(false);
  const dirty = !sameDraft(draft, baseline);
  const runNow = useRunNow();
  const capabilities = useCapabilities();
  const { hasPermission } = useAuth();
  const canOpenToolPolicy = canOpenRoute(APP_ROUTES.settingsToolPolicy, hasPermission);
  // 選んだ業務 Agent のスキルが使うツールのうち、自動実行で使えない（承認が要る）もの（#1813）。
  const approvalTools = useQuery({
    queryKey: ["automation-approval-tools", draft.agentId],
    queryFn: () => agentApi.automationApprovalTools(draft.agentId),
    enabled: Boolean(draft.agentId),
    retry: false,
  });
  const lastView = automation ? lastResultView(automation) : null;
  const confirmDelete = useDeleteAutomation(onDeleted);

  const save = useMutation({
    mutationFn: (payload: AutomationInput) =>
      automation ? agentApi.updateAutomation(automation.id, payload) : agentApi.createAutomation(payload),
    onSuccess: async (saved) => {
      const next = draftOf(saved, agents);
      setBaseline(next);
      setDraft(next);
      setSubmitted(false);
      toast.success(automation ? t("automation.saved") : t("automation.created"));
      await queryClient.invalidateQueries({ queryKey: ["automations"] });
      await queryClient.invalidateQueries({ queryKey: ["automation", saved.id] });
      if (!automation) onCreated(saved);
    },
  });
  const { confirmClose } = useEditorLeaveGuard(dirty, save.isPending);

  const nameError = submitted && !draft.name.trim() ? t("automation.nameRequired") : undefined;
  const goalError = submitted && !draft.goal.trim() ? t("automation.goalRequired") : undefined;
  const agentError = submitted && !draft.agentId ? t("automation.agentRequired") : undefined;
  const timezoneError =
    submitted && !isValidTimezone(draft.schedule.timezone)
      ? t("automation.timezoneInvalid")
      : undefined;
  const weekdaysError =
    submitted && weekdaysErrorNow(draft)
      ? t("automation.weekdaysRequired")
      : undefined;
  // 保存済みの業務 Agent が実行できなくなっても（無効・未公開・削除）、候補に残して何が選ばれているかを見せる。
  // 無効にする保存はそのまま通る（backend の update_automation。#927）。
  const savedAgentId = automation?.agent_id ?? "";
  const savedAgentUnavailable = Boolean(savedAgentId) && !agents.some((agent) => agent.id === savedAgentId);
  const savedAgent = allAgents.find((agent) => agent.id === savedAgentId);
  const agentOptions = [
    ...agents.map((agent) => ({ value: agent.id, label: agent.name })),
    ...(savedAgentUnavailable && !agentsLoading
      ? [
          {
            value: savedAgentId,
            label: savedAgent
              ? t("automation.agentUnavailable", { name: savedAgent.name })
              : t("automation.agentMissing", { id: savedAgentId }),
          },
        ]
      : []),
  ];

  function setField<K extends keyof AutomationDraft>(key: K, value: AutomationDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }
  function setSchedule(patch: Partial<AutomationSchedule>) {
    setDraft((current) => ({ ...current, schedule: { ...current.schedule, ...patch } }));
  }
  function toggleWeekday(day: number) {
    const days = draft.schedule.weekdays.includes(day)
      ? draft.schedule.weekdays.filter((item) => item !== day)
      : [...draft.schedule.weekdays, day].sort();
    setSchedule({ weekdays: days });
  }

  async function back() {
    if (await confirmClose()) onBack();
  }

  function submit() {
    setSubmitted(true);
    // 画面の並び順で最初のエラーの欄へフォーカスする（#531 / #541）。
    if (
      focusFirstInvalidField([
        ["automation-name", draft.name.trim() ? undefined : "name"],
        ["automation-agent", draft.agentId ? undefined : "agent"],
        ["automation-goal", draft.goal.trim() ? undefined : "goal"],
        ["automation-timezone", !isValidTimezone(draft.schedule.timezone) ? "timezone" : undefined],
      ]) ||
      weekdaysErrorNow(draft)
    ) {
      return;
    }
    save.mutate(toPayload(draft));
  }

  const objectActions: EntityAction[] =
    automation && !readOnly
      ? [
          {
            id: "run",
            label: t("automation.runNow"),
            icon: Play,
            loading: runNow.isPending,
            disabled: dirty,
            onSelect: () => runNow.mutate(automation.id),
            testId: "automation-run-now",
          },
          { id: "delete", label: t("automation.delete"), icon: Trash2, tone: "danger", onSelect: () => void confirmDelete(automation) },
        ]
      : [];

  return (
    <>
      <PageHeader
        wide
        title={automation ? automation.name : t("automation.create")}
        subtitle={t("page.automations.subtitle")}
        // 一覧へ戻るは左上（#618）。確定（作成・保存と変更を破棄）はフォームの末尾の FormActionBar（#1786）。
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: t("nav.automations") }),
          onClick: () => void back(),
          testId: "editor-back",
        }}
      />
      <PageBody wide className="space-y-6">
        {/* 保存の対象（基本・起動の条件）を包み、その末尾に確定の行を置く（#1786）。 */}
        <div className="min-w-0 space-y-6">
          <fieldset disabled={readOnly} className="min-w-0 space-y-6">
            <Section
              title={t("automation.basic")}
              actions={
                automation && objectActions.length ? (
                  <ObjectActionBar
                    actions={objectActions}
                    ariaLabel={t("common.entityActions", { name: automation.name })}
                    moreLabel={t("common.moreActions")}
                    testId="automation-actions"
                  />
                ) : undefined
              }
            >
              <Card>
                <CardContent className="space-y-4 pt-5">
                  {automation && lastView ? (
                    // 前回の Run の今の状態と、その Run の詳細への導線（#1812）。
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="automation-last">
                      <StatusBadge variant={lastView.variant} label={lastView.label} />
                      <span className="text-xs tabular-nums text-fg-muted">{lastResultMessage(automation, lastView)}</span>
                      {lastView.runId && capabilities.viewRuns ? (
                        <ButtonLink
                          to={`${APP_ROUTES.runs}?id=${encodeURIComponent(lastView.runId)}`}
                          linkComponent={Link}
                          variant="secondary"
                          size="sm"
                          icon={History}
                          testId="automation-last-run-link"
                        >
                          {t("automation.openLastRun")}
                        </ButtonLink>
                      ) : null}
                    </div>
                  ) : null}
                  {/* 欄の並びは FormGrid（最大 60rem・2 列）、欄は値の種類の幅（#1891）。 */}
                  <FormGrid>
                    <TextField
                      id="automation-name"
                      label={t("automation.name")}
                      width="md"
                      required
                      maxLength={100}
                      value={draft.name}
                      error={nameError}
                      onValueChange={(value) => setField("name", value)}
                    />
                    <SelectField<string>
                      id="automation-agent"
                      label={t("automation.agent")}
                      width="lg"
                      required
                      placeholder={t("automation.agentPlaceholder")}
                      value={draft.agentId}
                      options={agentOptions}
                      error={agentError}
                      helper={
                        !agentsLoading && agents.length === 0
                          ? t("automation.agentNone")
                          : draft.agentId && draft.agentId === savedAgentId && savedAgentUnavailable
                            ? t("automation.agentUnavailableHint")
                            : undefined
                      }
                      disabled={agentsLoading}
                      onValueChange={(value) => setField("agentId", value)}
                    />
                  </FormGrid>
                  <ApprovalToolsNotice data={approvalTools.data} canOpenToolPolicy={canOpenToolPolicy} />
                  <TextareaField
                    id="automation-goal"
                    label={t("automation.goal")}
                    helper={t("automation.goalHelper")}
                    width="full"
                    required
                    rows={4}
                    maxLength={4000}
                    value={draft.goal}
                    error={goalError}
                    onValueChange={(value) => setField("goal", value)}
                  />
                  <label className="inline-flex items-center gap-2 text-sm text-fg">
                    <Switch
                      checked={draft.enabled}
                      aria-label={t("automation.enabledLabel")}
                      onCheckedChange={(checked) => setField("enabled", checked)}
                    />
                    {t("automation.enabledLabel")}
                  </label>
                </CardContent>
              </Card>
            </Section>

            {/* 自動実行はスケジュールで起動する（#1799）。中身に合わせて見出しも「スケジュール」にする。 */}
            <Section title={t("automation.schedule")}>
              <Card>
                <CardContent className="space-y-4 pt-5" data-testid="automation-schedule">
                  {/* 頻度・時刻・タイムゾーンは短い値。値の幅で横に並べる（#1891）。 */}
                  <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
                    <SelectField<ScheduleFrequency>
                      id="automation-frequency"
                      label={t("automation.frequency")}
                      width="sm"
                      value={draft.schedule.frequency}
                      options={FREQUENCIES.map((item) => ({ value: item, label: t(`automation.frequency.${item}` as I18nKey) }))}
                      onValueChange={(value) => setSchedule({ frequency: value })}
                    />
                    {draft.schedule.frequency === "hourly" ? (
                      <TextField
                        id="automation-minute"
                        label={t("automation.minute")}
                        width="xs"
                        type="number"
                        min="0"
                        max="59"
                        value={String(draft.schedule.minute)}
                        onValueChange={(value) => setSchedule({ minute: Math.min(59, Math.max(0, Number(value) || 0)) })}
                      />
                    ) : (
                      <TextField
                        id="automation-time"
                        label={t("automation.time")}
                        width="sm"
                        type="time"
                        value={draft.schedule.time}
                        onValueChange={(value) => setSchedule({ time: value })}
                      />
                    )}
                    <TextField
                      id="automation-timezone"
                      label={t("automation.timezone")}
                      helper={t("automation.timezoneHelper")}
                      width="md"
                      error={timezoneError}
                      value={draft.schedule.timezone}
                      onValueChange={(value) => setSchedule({ timezone: value })}
                    />
                  </div>
                  {draft.schedule.frequency === "weekly" ? (
                    <Fieldset legend={t("automation.weekdays")} error={weekdaysError} className="space-y-2">
                      <div className="flex flex-wrap gap-2">
                        {WEEKDAYS.map((day) => (
                          <label
                            key={day}
                            data-surface-tint="accent"
                            className="flex cursor-pointer items-center gap-2 rounded-md border border-border-control bg-surface px-3 py-2 text-sm text-fg has-[:checked]:border-accent-emphasis has-[:checked]:bg-accent-subtle"
                          >
                            <input
                              type="checkbox"
                              checked={draft.schedule.weekdays.includes(day)}
                              onChange={() => toggleWeekday(day)}
                              className="accent-accent-emphasis"
                            />
                            {t(`automation.weekday.${day}` as I18nKey)}
                          </label>
                        ))}
                      </div>
                    </Fieldset>
                  ) : null}
                  {automation?.next_run_at && !dirty ? (
                    <p className="text-sm text-fg" data-testid="automation-next">
                      {t("automation.nextRun", { time: formatDateTime(automation.next_run_at) })}
                    </p>
                  ) : null}
                </CardContent>
              </Card>
            </Section>
          </fieldset>
          {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
          <SaveErrorBanner message={save.error ? apiErrorMessage(save.error, t("common.error.save")) : null} attemptKey={save.submittedAt} testId="automation-save-error" />
          {!readOnly ? (
            // 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。最後のカードの下に置き、画面の下端に貼り付けない（#1786 / #2715）。
            <FormActionBar
              ariaLabel={t("editor.actions.formLabel")}
              testId="automation-form-actions"
              primaryActions={[
                {
                  id: "save",
                  label: automation ? t("common.save") : t("common.create"),
                  icon: Save,
                  loading: save.isPending,
                  onClick: submit,
                },
              ]}
              secondaryActions={[
                {
                  id: "discard",
                  label: t("common.discardChanges"),
                  icon: RotateCcw,
                  disabled: !dirty || save.isPending,
                  onClick: () => {
                    setDraft(baseline);
                    setSubmitted(false);
                  },
                },
              ]}
            />
          ) : null}
        </div>

        {automation ? (
          <Section title={t("automation.history")}>
            <Card className="min-w-0">
              <CardContent className="pt-5">
                {runs.length === 0 ? (
                  <p className="text-sm text-fg-muted">{t("automation.history.empty")}</p>
                ) : (
                  <DataTable
                    rows={runs}
                    columns={historyColumns()}
                    getRowKey={(run) => run.run_id}
                    ariaLabel={t("automation.history.label")}
                    scrollAriaLabel={listScrollLabel(t("automation.history.label"))}
                    tableClassName="w-full min-w-[40rem]"
                    stickyHeader
                  />
                )}
              </CardContent>
            </Card>
          </Section>
        ) : null}
      </PageBody>
    </>
  );
}

/**
 * 自動実行で使えない（承認が要る）ツールの警告（#1813）。自動実行の Run は無人なので、承認が要る
 * ツールを呼ばずに「確かめられなかった点」として回答する。ツール権限で許可すると使える。
 */
function ApprovalToolsNotice({
  data,
  canOpenToolPolicy,
}: {
  data: AutomationToolCheck | undefined;
  canOpenToolPolicy: boolean;
}) {
  if (!data || (data.approval_required_tools.length === 0 && data.warnings.length === 0)) return null;
  const tools = data.approval_required_tools;
  return (
    <div data-testid="automation-approval-tools">
      <Banner
        severity="warning"
        title={tools.length ? t("automation.approvalTools.title") : t("automation.approvalTools.uncheckedTitle")}
        action={
          tools.length && canOpenToolPolicy ? (
            <ButtonLink
              to={APP_ROUTES.settingsToolPolicy}
              linkComponent={Link}
              size="sm"
              icon={ShieldCheck}
              testId="automation-open-tool-policy"
            >
              {t("automation.approvalTools.openPolicy")}
            </ButtonLink>
          ) : undefined
        }
      >
        <div className="space-y-2">
          {tools.length ? (
            <>
              <ul className="flex flex-wrap gap-2" aria-label={t("automation.approvalTools.listLabel")}>
                {tools.map((name) => (
                  <li key={name}>
                    <code className="break-all font-mono text-xs">{name}</code>
                  </li>
                ))}
              </ul>
              <p>{t("automation.approvalTools.body")}</p>
              {canOpenToolPolicy ? null : <p>{t("automation.approvalTools.askAdmin")}</p>}
            </>
          ) : null}
          {data.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      </Banner>
    </div>
  );
}

function weekdaysErrorNow(draft: AutomationDraft): boolean {
  return draft.schedule.frequency === "weekly" && draft.schedule.weekdays.length === 0;
}

function historyColumns(): DataTableColumn<AutomationRun>[] {
  return [
    {
      key: "started",
      header: t("automation.history.started"),
      rowHeader: true,
      className: "whitespace-nowrap tabular-nums text-fg",
      render: (run) => formatDateTime(run.created_at),
    },
    {
      key: "trigger",
      header: t("automation.history.trigger"),
      className: "text-xs text-fg",
      render: (run) => t(`automation.history.trigger.${run.trigger || "schedule"}` as I18nKey),
    },
    {
      key: "status",
      header: t("automation.history.status"),
      render: (run) => <StatusBadge {...runStatusView(run.status)} />,
    },
    {
      key: "run",
      header: t("automation.history.run"),
      className: "break-all font-mono text-xs text-fg-muted",
      render: (run) => run.run_id,
    },
  ];
}
