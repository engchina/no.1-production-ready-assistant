import { type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Plus, Power, PowerOff, Rocket, RotateCcw } from "lucide-react";
import {
  Button,
  Banner,
  EmptyState,
  FormSkeleton,
  ListToolbar,
  TableSkeleton,
  PageHeader,
  RowActionMenu,
  Section,
  StatusBadge,
  toast,
  useConfirm,
  type DataTableColumn,
  type EntityAction,
  PageBody,
  publishedVersionColumns,
  RowTitleButton,
  ApiErrorBanner,
  apiErrorMessage,
  compareNames,
} from "@production-ready/ui";
import { agentApi, type AgentProfile, type AgentVersion } from "@/lib/api";
import { MissingEditorTarget } from "@/components/EntityLayout";
import { listScrollLabel, PagedDataTable, QueryState } from "@/components/ListViews";
import {
  ListSearchField,
  listCount,
  listTotal,
  matchesSearch,
  NoMatchState,
  useListSearch,
} from "@/components/ListFilters";
import { useEditorRoute } from "@/lib/editor-route";
import { t } from "@/lib/i18n";
import { useCapabilities } from "@/lib/permissions";
import { AgentEditorView } from "@/pages/agents/AgentEditorView";
import { AgentSharingSection } from "@/components/agents/AgentSharingSection";
import { useAuth } from "@/components/security/AuthProvider";
import { formatDate } from "@/pages/shared/page-helpers";
import { actorDisplayName } from "@/pages/shared/user-names";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";
import { SkillUpdatesPanel } from "@/components/agents/SkillUpdatesPanel";

export function AgentsPage() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const editor = useEditorRoute();
  // 業務 Agent の変更は Agent 管理の権限（admin）と、個人の業務 Agent の持ち主だけ。それ以外は閲覧だけ
  // にする（#215・#2523）。作成は Agent 管理か、自分の業務 Agent の作成の権限。
  const { admin: canManage, buildAgents: canCreate } = useCapabilities();
  const { user } = useAuth();
  const managesAgent = (agent: AgentProfile) =>
    canManage || (canCreate && Boolean(agent.owner_user_uuid) && agent.owner_user_uuid === user?.user_uuid);
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents });
  const skills = useQuery({ queryKey: ["skills"], queryFn: agentApi.listSkills });
  // 組み込み Runtime で選べるモデル（システム設定 > モデル の登録モデル。#754）。
  const runtimeStatus = useQuery({ queryKey: ["runtime-status"], queryFn: agentApi.getRuntimeStatus });
  const toggleAgent = useMutation({
    mutationFn: (agent: AgentProfile) => agentApi.patchAgent(agent.id, { enabled: !agent.enabled }),
    onSuccess: () => {
      toast.success(t("agent.enabledUpdated"));
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });

  const publishAgent = useMutation({
    mutationFn: (agent: AgentProfile) => agentApi.publishAgent(agent.id),
    onSuccess: (published) => {
      toast.success(t("agent.version.published", { version: published.published_version ?? 0 }));
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });

  async function confirmPublish(agent: AgentProfile) {
    const next = Math.max(0, ...(agent.versions ?? []).map((item) => item.version)) + 1;
    const ok = await confirm({
      title: t("agent.version.publishTitle", { version: next }),
      description: t("agent.version.publishMessage"),
      confirmLabel: t("agent.version.publish"),
      cancelLabel: t("common.cancel"),
    });
    if (ok) publishAgent.mutate(agent);
  }

  // 一覧の行と詳細（エディタの概要）で同じ定義を使う（UX 契約 buttons.md §5.1）。
  const agentActions = (agent: AgentProfile): EntityAction[] =>
    managesAgent(agent)
      ? [
          {
            // 下書きを版として公開する（#770）。公開していない変更があるときだけ出す。
            id: "publish",
            label: t("agent.version.publish"),
            icon: Rocket,
            visible: agent.unpublished_changes,
            disabled: publishAgent.isPending,
            onSelect: () => void confirmPublish(agent),
          },
          {
            id: "toggle-enabled",
            label: agent.enabled ? t("agent.disable") : t("agent.enable"),
            icon: agent.enabled ? PowerOff : Power,
            disabled: toggleAgent.isPending,
            onSelect: () => toggleAgent.mutate(agent),
          },
        ]
      : [];

  const agentList = agents.data?.agents ?? [];
  const shareApprovals = agents.data?.share_approvals ?? [];
  // 一覧の絞り込み（名前・ID・説明。#808）。検索語は作業状態に残し、変わったら 1 ページ目へ戻す。
  const [agentQuery, setAgentQuery] = useListSearch("agents");
  // 全件を名前の自然順にしてからページングする。同名は ID で安定させ、取得順に依存させない。
  const visibleAgents = agentList
    .filter((agent) => matchesSearch(agentQuery, [agent.name, agent.id, agent.description]))
    .sort((left, right) => compareNames(left.name, right.name) || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0));
  // 作成できない利用者が `?id=new` を開いたら一覧を出す。
  const target = !canCreate && editor.target.kind === "new" ? ({ kind: "list" } as const) : editor.target;

  const selectedApproval = target.kind === "edit" ? shareApprovals.find((notice) => notice.agent_id === target.id) : undefined;

  if (target.kind === "list") {
    return (
      <>
        <PageHeader
          wide
          title={t("nav.agents")}
          subtitle={t("page.agents.subtitle")}
          actions={
            canCreate
              ? [{ id: "create", kind: "primary", label: t("agent.create"), icon: Plus, onClick: editor.openNew }]
              : []
          }
        />
        <PageBody wide>
          <NonPersistentStorageNotice />
          {shareApprovals.map((notice) => (
            <div key={notice.agent_id} data-testid="agent-share-approved-notice"><Banner severity="info">
              {t("agent.share.approvedNotice", { name: notice.name })}
            </Banner></div>
          ))}
          {skills.error ? <ApiErrorBanner error={skills.error} fallback={t("common.error.load")} /> : null}
          {/* スキルの更新があります（N 件）と、確かめてまとめて再公開する（#1867）。 */}
          <SkillUpdatesPanel agents={agentList} skills={skills.data?.skills ?? []} canManage={canManage} />
          <Section title={t("agent.list")}>
            <ListToolbar
              search={
                <ListSearchField
                  id="agent-search"
                  label={t("agent.search")}
                  value={agentQuery}
                  onSearch={setAgentQuery}
                />
              }
              count={agents.data ? listCount(agentList.length, visibleAgents.length, Boolean(agentQuery.trim())) : undefined}
              testId="agent-list-toolbar"
            />
            <QueryState query={agents} loadingLabel={t("loading.agents")} skeleton={<TableSkeleton columns={7} />}>
              <AgentTable
                agents={visibleAgents}
                resetKey={agentQuery}
                onOpen={(agent) => editor.openItem(agent.id)}
                hrefFor={(agent) => editor.itemHref(agent.id)}
                actionsFor={agentActions}
                empty={
                  agentList.length ? (
                    <NoMatchState title={t("agent.noMatch")} onClear={() => setAgentQuery("")} />
                  ) : (
                    <EmptyState
                      title={t("agent.empty.title")}
                      hint={canCreate ? t("agent.empty.hint") : t("agent.empty.restrictedHint")}
                      action={
                        canCreate ? (
                          <Button variant="secondary" icon={Plus} onClick={editor.openNew}>
                            {t("agent.createFirst")}
                          </Button>
                        ) : undefined
                      }
                    />
                  )
                }
              />
            </QueryState>
          </Section>
        </PageBody>
      </>
    );
  }

  const agent = target.kind === "edit" ? agentList.find((candidate) => candidate.id === target.id) : undefined;
  if (target.kind === "edit" && !agent) {
    return (
      <>
        <PageHeader
          wide
          title={t("nav.agents")}
        />
        <PageBody wide>
          <QueryState query={agents} loadingLabel={t("loading.agents")} skeleton={<FormSkeleton fields={3} />}>
            {selectedApproval ? (
              <div data-testid="agent-share-approved-notice"><Banner severity="info">
                <div className="grid gap-3">
                  <p>{t("agent.share.approvedNotice", { name: selectedApproval.name })}</p>
                  <Button variant="secondary" icon={ArrowLeft} onClick={() => editor.backToList()}>
                    {t("common.backToList")}
                  </Button>
                </div>
              </Banner></div>
            ) : <MissingEditorTarget id={target.id} onBack={() => editor.backToList()} />}
          </QueryState>
        </PageBody>
      </>
    );
  }

  return (
    <AgentEditorView
      key={agent?.id ?? "new"}
      agent={agent}
      availableSkills={skills.data?.skills ?? []}
      skillsLoading={skills.isLoading}
      skillsError={skills.error}
      models={runtimeStatus.data?.models ?? []}
      defaultModelId={runtimeStatus.data?.model_id ?? ""}
      modelsLoading={runtimeStatus.isLoading}
      actions={agent ? agentActions(agent) : []}
      readOnly={agent ? !managesAgent(agent) : !canCreate}
      sharing={
        agent && (agent.owner_user_uuid || (canManage && agent.share_request?.status === "approved")) ? (
          <AgentSharingSection
            agent={agent}
            // 持ち主は申請、Agent 管理は承認・却下（Agent 管理が自分の個人の Agent を開いたときも承認の側）。
            mode={canManage ? "admin" : "owner"}
            ownerName={agents.data?.owner_display_names?.[agent.owner_user_uuid ?? ""] ?? t("agent.share.ownerMissing")}
          />
        ) : null
      }
      onBack={() => editor.backToList()}
      onCreated={(created) => editor.openItem(created.id, { replace: true })}
    />
  );
}

/** 公開の状態（公開中 vN / 未公開 / 公開していない変更あり。#770）。 */
export function AgentVersionBadges({ agent }: { agent: AgentProfile }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <AgentOwnershipBadges agent={agent} />
      <AgentPublishBadges agent={agent} />
    </span>
  );
}

/** 個人の業務 Agent と共有の申請の状態（#2523）。組織の業務 Agent には何も出さない。 */
function AgentOwnershipBadges({ agent }: { agent: AgentProfile }) {
  if (!agent.owner_user_uuid) return null;
  const request = agent.share_request;
  return (
    <>
      <StatusBadge variant="neutral" label={t("agent.share.personalBadge")} icon={false} />
      {request?.status === "requested" ? (
        <StatusBadge variant="info" label={t("agent.share.requestedBadge")} />
      ) : request?.status === "rejected" ? (
        <StatusBadge variant="warning" label={t("agent.share.rejectedBadge")} />
      ) : null}
    </>
  );
}

function AgentPublishBadges({ agent }: { agent: AgentProfile }) {
  if (agent.published_version === null) {
    return <StatusBadge variant="warning" label={t("agent.version.unpublished")} />;
  }
  return (
    <>
      <StatusBadge
        variant="success"
        label={t("agent.version.publishedBadge", { version: agent.published_version })}
      />
      {(agent.draft_changed ?? agent.unpublished_changes) ? (
        <StatusBadge variant="info" label={t("agent.version.changed")} icon={false} />
      ) : null}
      {/* 公開中の版に固定したスキルが変わった（#1855）。再公開で取り込む。 */}
      {agent.skill_updates?.length ? (
        <StatusBadge variant="warning" label={t("agent.version.skillsUpdated")} />
      ) : null}
      {/* 常に最新のスキルを使う業務 Agent（#1867）。 */}
      {agent.skill_policy === "latest" ? (
        <StatusBadge variant="info" label={t("agent.version.latestBadge")} icon={false} />
      ) : null}
    </>
  );
}

/** 公開した版の一覧と「この版に戻す」（#770）。 */
export function AgentVersionsSection({ agent, readOnly }: { agent: AgentProfile; readOnly: boolean }) {
  const queryClient = useQueryClient();
  // 公開者の表示名は一覧の応答にある（同じ query の cache を読む）。
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents });
  const confirm = useConfirm();
  const restore = useMutation({
    mutationFn: (version: number) => agentApi.restoreAgentVersion(agent.id, version),
    onSuccess: (restored) => {
      toast.success(t("agent.version.restored", { version: restored.published_version ?? 0 }));
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });

  async function confirmRestore(version: AgentVersion) {
    const ok = await confirm({
      title: t("agent.version.restoreTitle", { version: version.version }),
      description: t("agent.version.restoreMessage"),
      confirmLabel: t("agent.version.restore"),
      cancelLabel: t("common.cancel"),
    });
    if (ok) restore.mutate(version.version);
  }

  const rows = [...(agent.versions ?? [])].reverse();
  // 版の履歴は直近 N 版（AGENT_MAX_PUBLISHED_VERSIONS。公開中の版は常に残る。#2356）。N は API の値。
  const maxVersions = agents.data?.max_published_versions;
  // 列の並びとバッジは RAG の業務ガイドの公開の履歴と同じ共通の列（#2356）。
  const columns = publishedVersionColumns<AgentVersion>({
    getVersion: (version) => version.version,
    currentVersion: agent.published_version,
    formatVersion: (version) => `v${version}`,
    labels: {
      version: t("agent.version.number"),
      current: t("agent.version.current"),
      publishedAt: t("agent.version.publishedAt"),
      publishedBy: t("agent.version.publishedBy"),
      note: t("agent.version.note"),
      actions: t("settings.mcpServers.actions"),
    },
    renderPublishedAt: (version) => formatDate(version.published_at),
    // 公開者は表示名で出す（ログイン ID は出さない。#1696）。
    renderPublishedBy: (version) => actorDisplayName(version.published_by, agents.data?.user_display_names) ?? "-",
    renderNote: (version) => version.note || "-",
    extraColumns: [
      {
        key: "skills",
        header: t("agent.version.pinnedSkills"),
        className: "max-w-xs break-words text-fg-muted",
        // 公開のときに固定したスキル（#1855）。固定していない版は今のスキルで動く。
        render: (version) =>
          version.skills ? version.skills.map((skill) => skill.name).join("、") || "-" : t("agent.version.notPinned"),
      },
    ],
    renderActions: (version) => (
      <RowActionMenu
        actions={
          readOnly
            ? []
            : [
                {
                  id: "restore",
                  label: t("agent.version.restore"),
                  icon: RotateCcw,
                  visible: version.version !== agent.published_version || agent.unpublished_changes,
                  disabled: restore.isPending,
                  onSelect: () => void confirmRestore(version),
                },
              ]
        }
        ariaLabel={t("common.entityActions", { name: `v${version.version}` })}
        testId={`agent-version-actions-${version.version}`}
      />
    ),
  });
  const description = [
    t("agent.version.description"),
    maxVersions ? t("agent.version.retention", { count: maxVersions }) : null,
  ]
    .filter(Boolean)
    .join("");

  return (
    <Section title={t("agent.version.title")} description={description}>
      {rows.length === 0 ? (
        <EmptyState title={t("agent.version.empty")} />
      ) : (
        <>
          {/* 件数は一覧の上の右に 1 か所（ListCount。RAG の公開の履歴と同じ。#1891・#2356）。 */}
          <ListToolbar count={listTotal(rows.length)} testId="agent-version-toolbar" />
          {/* 表頭を固定して表の中を縦スクロールにし、直下にページ送りを置く（#2135）。 */}
          <PagedDataTable
            rows={rows}
            columns={columns}
            getRowKey={(version) => String(version.version)}
            resetKey={agent.id}
            paginationTestId="agent-version-pagination"
            rowProps={() => ({ className: "align-top" })}
            tableClassName="w-full min-w-[40rem]"
            ariaLabel={t("agent.version.title")}
            // 375px では横にスクロールする（min-w-[40rem]）。キーボードでも届くよう、スクロール領域に名前と Tab を付ける
            // （WCAG 2.1.1。axe の scrollable-region-focusable。#1671）。
            scrollAriaLabel={listScrollLabel(t("agent.version.title"))}
          />
        </>
      )}
    </Section>
  );
}

function AgentTable({
  agents,
  resetKey,
  empty,
  onOpen,
  hrefFor,
  actionsFor,
}: {
  agents: AgentProfile[];
  /** 検索語など、変わったら 1 ページ目へ戻す契機。 */
  resetKey?: unknown;
  /** 0 件のときの表示（データが無い / 検索に一致しない）。 */
  empty: ReactNode;
  onOpen: (agent: AgentProfile) => void;
  /** 名前のリンクの URL（新しいタブで開ける。#583）。 */
  hrefFor: (agent: AgentProfile) => string;
  actionsFor: (agent: AgentProfile) => EntityAction[];
}) {
  const columns: DataTableColumn<AgentProfile>[] = [
    {
      key: "name",
      header: t("agent.name"),
      rowHeader: true,
      render: (agent) => (
        <RowTitleButton title={agent.name} subtitle={agent.id} href={hrefFor(agent)} onClick={() => onOpen(agent)} />
      ),
    },
    {
      key: "description",
      header: t("agent.description"),
      className: "max-w-xs text-fg-muted",
      render: (agent) => agent.description || "-",
    },
    {
      key: "skills",
      header: t("agent.skillCount"),
      align: "right",
      className: "tabular-nums",
      render: (agent) => agent.skill_ids.length,
    },
    {
      key: "model",
      header: t("agent.model"),
      // 空は「既定のテキストモデル」（システム設定 > モデル）。
      render: (agent) => (
        <span className="break-all text-xs text-fg">{agent.model_id || t("agent.modelDefault")}</span>
      ),
    },
    {
      key: "enabled",
      header: t("common.status"),
      render: (agent) => (
        <StatusBadge
          variant={agent.enabled ? "success" : "neutral"}
          label={agent.enabled ? t("agent.enabled") : t("agent.disabled")}
        />
      ),
    },
    {
      key: "version",
      header: t("agent.version.column"),
      render: (agent) => <AgentVersionBadges agent={agent} />,
    },
    {
      key: "actions",
      header: t("settings.mcpServers.actions"),
      align: "right",
      render: (agent) => (
        <RowActionMenu
          actions={actionsFor(agent)}
          ariaLabel={t("common.entityActions", { name: agent.name })}
          testId={`agent-row-actions-${agent.id}`}
        />
      ),
    },
  ];

  return (
    <PagedDataTable
      pageKey="agents"
      growToViewport
      scrollTestId="agents-scroll-region"
      resetKey={resetKey}
      rows={agents}
      columns={columns}
      getRowKey={(agent) => agent.id}
      onRowClick={onOpen}
      rowProps={(agent) => ({ className: "align-top", "data-testid": `agent-row-${agent.id}` })}
      tableClassName="w-full min-w-[46rem]"
      ariaLabel={t("agent.list")}
      paginationTestId="agent-list-pagination"
      empty={empty}
    />
  );
}
