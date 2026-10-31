import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, RefreshCw, Rocket } from "lucide-react";
import {
  Banner,
  Button,
  ButtonLink,
  SideSheet,
  ListToolbar,
  Pagination,
  usePagination,
  toast,
  useConfirm,
  apiErrorMessage,
} from "@production-ready/ui";

import { agentPaginationLabels, usePersistedServerPaging } from "@/components/ListViews";
import { listTotal } from "@/components/ListFilters";
import { SkillUpdateDiffDisclosure } from "@/components/agents/SkillUpdateDiff";
import { agentApi, type AgentProfile, type AgentSkill, type SkillUpdatePublishResult } from "@/lib/api";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

/** 品質評価の画面を、その業務 Agent を選んだ状態で開く（#1867。評価の比較への導線）。 */
export function evaluationHref(agentId: string): string {
  return `${APP_ROUTES.evaluation}?agent=${encodeURIComponent(agentId)}`;
}

function skillNames(agent: AgentProfile, skills: AgentSkill[]): string {
  return (agent.skill_updates ?? []).map((id) => skills.find((skill) => skill.id === id)?.name ?? id).join("、");
}

/**
 * まとめて再公開した結果の Toast（#1867）。取り消し（公開中の版を前の版へ）を action に置く
 * （messaging.md の undo-support）。
 */
export function useSkillUpdatePublish(onDone?: () => void) {
  const queryClient = useQueryClient();
  const rollback = useMutation({
    mutationFn: (results: SkillUpdatePublishResult[]) =>
      agentApi.rollbackSkillUpdates(
        results.flatMap((item) =>
          item.status === "published" && item.previous_version ? [{ agent_id: item.agent_id, version: item.previous_version }] : []
        )
      ),
    onSuccess: (data) => {
      toast.success(t("agent.skillUpdates.rolledBack", { count: data.agents.length }));
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  return useMutation({
    mutationFn: (agentIds: string[]) => agentApi.publishSkillUpdates(agentIds),
    onSuccess: (data) => {
      const published = data.results.filter((item) => item.status === "published");
      const skipped = data.results.filter((item) => item.status === "skipped");
      if (published.length) {
        toast.success(t("agent.skillUpdates.published", { count: published.length }), {
          description: skipped.length
            ? t("agent.skillUpdates.skipped", {
                items: skipped.map((item) => `${item.agent_id}（${item.reason ?? ""}）`).join("、"),
              })
            : undefined,
          action: { label: t("agent.skillUpdates.undo"), onClick: () => rollback.mutate(published) },
          // 取り消しを押せるよう、自動では消さない。
          duration: 0,
        });
      } else {
        toast.error(t("agent.skillUpdates.nothingPublished"), {
          description: skipped.map((item) => `${item.agent_id}（${item.reason ?? ""}）`).join("、"),
        });
      }
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
      void queryClient.invalidateQueries({ queryKey: ["agent-skill-updates"] });
      onDone?.();
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
}

/**
 * 業務 Agent の一覧の「スキルの更新があります（N 件）」と、確かめてまとめて（選んで）再公開する
 * side sheet（#1867。Dependabot の「何が変わったか → 確かめる → まとめて取り込む → 戻せる」の形）。
 * 再公開は公開中の版にスキルの更新だけを取り込む新しい版を作り、下書きの変更は公開しない。
 */
export function SkillUpdatesPanel({
  agents,
  skills,
  canManage,
}: {
  agents: AgentProfile[];
  skills: AgentSkill[];
  canManage: boolean;
}) {
  const confirm = useConfirm();
  const updated = agents.filter((agent) => agent.published_version !== null && (agent.skill_updates?.length ?? 0) > 0);
  const [open, setOpen] = useState(false);
  // 選んだ業務 Agent（既定はすべて）。一覧から消えた業務 Agent は数えない。
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const paging = usePersistedServerPaging("skillUpdates");
  const { page, setPage, totalPages, pageItems, range } = usePagination(updated, paging.pageSize, {
    page: paging.page,
    onPageChange: paging.setPage,
  });
  const selected = updated.filter((agent) => !excluded.has(agent.id));
  const evaluationSets = useQuery({
    queryKey: ["evaluation-sets", "all"],
    queryFn: () => agentApi.listEvaluationSets(),
    enabled: open,
  });
  const publish = useSkillUpdatePublish(() => setOpen(false));
  if (!updated.length) return null;

  function setSelected(agentId: string, value: boolean) {
    setExcluded((current) => {
      const next = new Set(current);
      if (value) next.delete(agentId);
      else next.add(agentId);
      return next;
    });
  }

  async function confirmPublish() {
    const ok = await confirm({
      title: t("agent.skillUpdates.confirmTitle", { count: selected.length }),
      description: t("agent.skillUpdates.confirmMessage", {
        items: selected.map((agent) => `${agent.name}（${skillNames(agent, skills)}）`).join("、"),
      }),
      confirmLabel: t("agent.skillUpdates.publish", { count: selected.length }),
      cancelLabel: t("common.cancel"),
      tone: "warning",
    });
    if (ok) publish.mutate(selected.map((agent) => agent.id));
  }

  return (
    <>
      <div data-testid="agent-skill-updates-banner">
        <Banner
          severity="warning"
          title={t("agent.skillUpdates.bannerTitle", { count: updated.length })}
          action={
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={() => setOpen(true)} data-testid="agent-skill-updates-open">
              {canManage ? t("agent.skillUpdates.review") : t("agent.skillUpdates.view")}
            </Button>
          }
        >
          {t("agent.skillUpdates.bannerHint")}
        </Banner>
      </div>
      <SideSheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("agent.skillUpdates.sheetTitle")}
        closeLabel={t("agent.skillUpdates.close")}
        side="right"
        size="wide"
        id="agent-skill-updates"
        data-testid="agent-skill-updates-sheet"
      >
        <div className="space-y-4 text-sm">
          <p className="text-fg-muted">{t("agent.skillUpdates.sheetHint")}</p>
          {canManage ? (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                icon={Rocket}
                loading={publish.isPending}
                disabled={!selected.length}
                onClick={() => void confirmPublish()}
                data-testid="agent-skill-updates-publish"
              >
                {t("agent.skillUpdates.publish", { count: selected.length })}
              </Button>
              <Button variant="ghost" onClick={() => setExcluded(new Set())} disabled={excluded.size === 0}>
                {t("agent.skillUpdates.selectAll")}
              </Button>
              <Button
                variant="ghost"
                onClick={() => setExcluded(new Set(updated.map((agent) => agent.id)))}
                disabled={selected.length === 0}
              >
                {t("agent.skillUpdates.selectNone")}
              </Button>
            </div>
          ) : null}
          <ListToolbar count={listTotal(updated.length)} testId="agent-skill-updates-toolbar" />
          <ul className="space-y-3">
            {pageItems.map((agent) => {
              const sets = (evaluationSets.data?.sets ?? []).filter((set) => set.agent_id === agent.id).length;
              const helperId = `agent-skill-update-${agent.id}-helper`;
              return (
                <li key={agent.id} className="space-y-2 rounded-md border border-border p-3" data-testid={`agent-skill-update-${agent.id}`}>
                  {canManage ? (
                    <label className="flex min-h-11 items-start gap-2 text-fg">
                      {/* 選ぶだけの checkbox（共有部品に checkbox が無いのでネイティブ。AgentTemplatePicker と同じ）。 */}
                      <input
                        type="checkbox"
                        checked={!excluded.has(agent.id)}
                        onChange={(event) => setSelected(agent.id, event.target.checked)}
                        aria-describedby={helperId}
                        className="mt-0.5 h-4 w-4 shrink-0"
                      />
                      <span className="min-w-0">
                        <span className="block font-medium">{agent.name}</span>
                        <span id={helperId} className="mt-0.5 block text-xs text-fg-muted">
                          {t("agent.skillUpdates.itemHint", { version: agent.published_version ?? 0, skills: skillNames(agent, skills) })}
                        </span>
                      </span>
                    </label>
                  ) : (
                    <div>
                      <p className="font-medium text-fg">{agent.name}</p>
                      <p className="text-xs text-fg-muted">
                        {t("agent.skillUpdates.itemHint", { version: agent.published_version ?? 0, skills: skillNames(agent, skills) })}
                      </p>
                    </div>
                  )}
                  {agent.draft_changed ? (
                    <p className="text-xs text-fg-muted">{t("agent.skillUpdates.draftNotPublished")}</p>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-2">
                    {evaluationSets.isLoading ? null : sets ? (
                      <ButtonLink
                        to={evaluationHref(agent.id)}
                        linkComponent={Link}
                        variant="ghost"
                        size="sm"
                        icon={FlaskConical}
                        testId={`agent-skill-update-evaluate-${agent.id}`}
                      >
                        {t("agent.skillUpdates.evaluate", { count: sets })}
                      </ButtonLink>
                    ) : (
                      <span className="text-xs text-fg-muted">{t("agent.skillUpdates.noEvaluationSet")}</span>
                    )}
                  </div>
                  <SkillUpdateDiffDisclosure agentId={agent.id} testId={`agent-skill-update-diff-${agent.id}`} />
                </li>
              );
            })}
          </ul>
          <Pagination
            page={page}
            totalPages={totalPages}
            range={range}
            onPageChange={setPage}
            pageSize={paging.pageSize}
            onPageSizeChange={paging.setPageSize}
            labels={agentPaginationLabels()}
            testId="agent-skill-updates-pagination"
          />
        </div>
      </SideSheet>
    </>
  );
}
