import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiErrorState, Disclosure, ListSkeleton, StatusBadge, TimedLoadingState } from "@production-ready/ui";

import { agentApi, type SkillDiffLine, type SkillUpdateDetail } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 差分の行の印（色だけで表さない。追加は +、削除は −）。 */
const LINE_MARK: Record<SkillDiffLine["kind"], string> = { hunk: "", context: " ", added: "+", removed: "−" };
const LINE_CLASS: Record<SkillDiffLine["kind"], string> = {
  hunk: "text-fg-muted",
  context: "text-fg",
  added: "bg-success-subtle text-success-fg",
  removed: "bg-danger-subtle text-danger-fg",
};

function Changes({ label, added, removed }: { label: string; added: string[]; removed: string[] }) {
  if (!added.length && !removed.length) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold text-fg-muted">{label}</p>
      <ul className="space-y-0.5 font-mono text-xs">
        {added.map((item) => (
          <li key={`+${item}`} className="break-all text-success-fg">{`+ ${item}`}</li>
        ))}
        {removed.map((item) => (
          <li key={`-${item}`} className="break-all text-danger-fg">{`− ${item}`}</li>
        ))}
      </ul>
    </div>
  );
}

function SkillUpdateItem({ item }: { item: SkillUpdateDetail }) {
  const descriptionChanged = item.current !== null && item.pinned.description !== item.current.description;
  return (
    <section className="min-w-0 space-y-3 rounded-md border border-border p-3" data-testid={`skill-update-${item.skill_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-fg">{item.current?.name ?? item.pinned.name}</h4>
        <StatusBadge
          variant={item.change === "removed" ? "danger" : "warning"}
          label={item.change === "removed" ? t("agent.skillUpdates.removed") : t("agent.skillUpdates.changed")}
        />
      </div>
      {descriptionChanged ? (
        <div className="space-y-1 text-xs">
          <p className="font-semibold text-fg-muted">{t("agent.skillUpdates.description")}</p>
          <p className="break-words text-danger-fg">{`− ${item.pinned.description || "—"}`}</p>
          <p className="break-words text-success-fg">{`+ ${item.current?.description || "—"}`}</p>
        </div>
      ) : null}
      <Changes label={t("agent.skillUpdates.tools")} added={item.tools_added} removed={item.tools_removed} />
      <Changes
        label={t("agent.skillUpdates.references")}
        added={[...item.references_added, ...item.references_changed.map((path) => t("agent.skillUpdates.referenceChanged", { path }))]}
        removed={item.references_removed}
      />
      <div className="space-y-1">
        <p className="text-xs font-semibold text-fg-muted">{t("agent.skillUpdates.instructions")}</p>
        {item.instructions_diff.length ? (
          // 指示の差分（固定した版 → 今のスキル）。横に長い行は折り返し、横スクロールにしない。
          <ol
            className="max-h-80 overflow-y-auto rounded-md border border-border bg-surface-sunken py-1 font-mono text-xs"
            aria-label={t("agent.skillUpdates.instructionsLabel", { name: item.current?.name ?? item.pinned.name })}
          >
            {item.instructions_diff.map((line, index) => (
              <li key={`${index}-${line.kind}`} className={`flex gap-2 px-2 ${LINE_CLASS[line.kind]}`}>
                <span aria-hidden className="w-3 shrink-0 select-none">
                  {LINE_MARK[line.kind]}
                </span>
                <span className="min-w-0 whitespace-pre-wrap break-words">
                  {line.kind === "added" ? <span className="sr-only">{t("agent.skillUpdates.added")}</span> : null}
                  {line.kind === "removed" ? <span className="sr-only">{t("agent.skillUpdates.deleted")}</span> : null}
                  {line.text || " "}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-xs text-fg-muted">{t("agent.skillUpdates.instructionsSame")}</p>
        )}
        {item.truncated ? <p className="text-xs text-fg-muted">{t("agent.skillUpdates.truncated")}</p> : null}
      </div>
    </section>
  );
}

/**
 * 業務 Agent の公開中の版に固定したスキルと、今のスキルの差（#1867）。指示は unified diff の行で、
 * 追加・削除を + / − の印と色の両方で出す（色だけで表さない）。
 */
export function SkillUpdateDiff({ agentId }: { agentId: string }) {
  const updates = useQuery({
    queryKey: ["agent-skill-updates", agentId],
    queryFn: () => agentApi.getAgentSkillUpdates(agentId),
  });
  if (updates.isLoading) {
    return (
      <TimedLoadingState label={t("loading.skillUpdates")} testId="skill-updates-loading">
        <ListSkeleton rows={3} />
      </TimedLoadingState>
    );
  }
  if (updates.error) {
    return (
      <ApiErrorState
        error={updates.error}
        fallback={t("common.error.load")}
        retryLabel={t("common.retry")}
        onRetry={() => void updates.refetch()}
      />
    );
  }
  const items = updates.data?.items ?? [];
  if (!items.length) return <p className="text-xs text-fg-muted">{t("agent.skillUpdates.none")}</p>;
  return (
    <div className="min-w-0 space-y-3" data-testid={`skill-update-diff-${agentId}`}>
      {items.map((item) => (
        <SkillUpdateItem key={item.skill_id} item={item} />
      ))}
    </div>
  );
}

/** 「差分を見る」（開いたときだけ差分を取りに行く。一覧の業務 Agent ごとに置く。#1867）。 */
export function SkillUpdateDiffDisclosure({ agentId, testId }: { agentId: string; testId?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Disclosure
      variant="plain"
      size="sm"
      summary={t("agent.skillUpdates.showDiff")}
      open={open}
      onOpenChange={setOpen}
      summaryProps={testId ? { "data-testid": testId } : undefined}
    >
      {open ? <SkillUpdateDiff agentId={agentId} /> : null}
    </Disclosure>
  );
}
