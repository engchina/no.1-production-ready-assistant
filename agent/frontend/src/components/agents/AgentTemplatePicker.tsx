import { useQuery } from "@tanstack/react-query";
import { CheckCircle2 } from "lucide-react";
import { ListSkeleton, Section, TimedLoadingState, ApiErrorBanner } from "@production-ready/ui";

import { agentApi, type AgentTemplate } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 業務 Agent の新規作成の「テンプレートから始める」（#780）。業種テンプレートを選ぶと、親がフォームに入れる。
 * テンプレートは選ぶボタン（`aria-pressed`）で、選んだテンプレートの質問の例を下に出す。
 *
 * `evaluationSet` を渡すと（品質評価の権限を持つ利用者）、選んだテンプレートの評価ケースで評価セットを
 * 作るかを選ぶチェックボックス（既定はオン）を出す（#810）。
 */
export function AgentTemplatePicker({
  selectedId,
  onApply,
  evaluationSet,
}: {
  selectedId: string | null;
  onApply: (template: AgentTemplate) => void;
  evaluationSet?: { checked: boolean; onChange: (checked: boolean) => void };
}) {
  const templates = useQuery({ queryKey: ["agent-templates"], queryFn: agentApi.listAgentTemplates });
  const selected = templates.data?.templates.find((template) => template.id === selectedId) ?? null;
  return (
    <Section title={t("agent.template.title")} description={t("agent.template.description")}>
      {templates.isLoading ? (
        <TimedLoadingState label={t("agent.template.loading")} testId="agent-templates-loading">
          <ListSkeleton rows={4} className="sm:grid-cols-2 xl:grid-cols-4" />
        </TimedLoadingState>
      ) : templates.error ? (
        <ApiErrorBanner error={templates.error} fallback={t("common.error.load")} />
      ) : (
        <div className="space-y-3" data-testid="agent-templates">
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {(templates.data?.templates ?? []).map((template) => {
              const current = template.id === selectedId;
              return (
                <button
                  key={template.id}
                  data-surface-tint={current ? "accent" : undefined}
                  type="button"
                  aria-pressed={current}
                  aria-label={t("agent.template.apply", { name: template.name })}
                  onClick={() => onApply(template)}
                  data-testid={`agent-template-${template.id}`}
                  className={
                    current
                      ? "flex min-w-0 flex-col gap-1 rounded-md border border-accent-emphasis bg-accent-subtle px-3 py-2.5 text-left"
                      : "flex min-w-0 flex-col gap-1 rounded-md border border-border bg-surface px-3 py-2.5 text-left hover:bg-surface-hover"
                  }
                >
                  <span className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate text-xs text-fg-muted">{template.category}</span>
                    {current ? (
                      <span className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-accent-fg">
                        <CheckCircle2 size={14} aria-hidden="true" />
                        {t("agent.template.selected")}
                      </span>
                    ) : null}
                  </span>
                  <span className="text-sm font-medium text-fg">{template.name}</span>
                  <span className="line-clamp-2 text-xs leading-5 text-fg-muted">{template.description}</span>
                </button>
              );
            })}
          </div>
          {selected ? (
            <div className="rounded-md border border-border bg-surface-sunken px-3 py-2" data-testid="agent-template-samples">
              <p className="text-xs font-semibold text-fg-muted">{t("agent.template.samples")}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-fg">
                {selected.sample_questions.map((question) => (
                  <li key={question} className="break-words">
                    {question}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {selected && evaluationSet && selected.evaluation_cases.length ? (
            <label
              className="flex min-h-11 items-start gap-2 rounded-md border border-border px-3 py-2 text-sm text-fg"
              data-testid="agent-template-evaluation-set"
            >
              <input
                type="checkbox"
                checked={evaluationSet.checked}
                onChange={(event) => evaluationSet.onChange(event.target.checked)}
                aria-describedby="agent-template-evaluation-set-helper"
                className="mt-0.5 h-4 w-4 shrink-0"
              />
              <span className="min-w-0">
                <span className="block font-medium">
                  {t("agent.template.evaluationSet", { count: selected.evaluation_cases.length })}
                </span>
                <span id="agent-template-evaluation-set-helper" className="mt-0.5 block text-xs text-fg-muted">
                  {t("agent.template.evaluationSetHelper")}
                </span>
              </span>
            </label>
          ) : null}
        </div>
      )}
    </Section>
  );
}
