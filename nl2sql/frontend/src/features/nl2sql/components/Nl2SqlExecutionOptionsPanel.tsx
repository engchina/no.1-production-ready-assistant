import { Button, DisclosureChevron } from "@production-ready/ui";
import { Eye, FileText, Network, Sparkles } from "lucide-react";

import { t } from "@/lib/i18n";
import type { Nl2SqlEngine } from "../types";

function OptionCheckbox({
  checked,
  disabled,
  icon: Icon,
  label,
  onChange,
}: {
  checked: boolean;
  disabled: boolean;
  icon: typeof Sparkles;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 items-start gap-3 rounded-md border border-border bg-surface p-3 text-fg">
      {/* チェックボックスとアイコンは文字の 1 行目の中心にそろえる（#2371）。 */}
      <span className="pr-icon-line">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.currentTarget.checked)}
          className="h-4 w-4 shrink-0 rounded border-border text-accent-fg disabled:cursor-not-allowed disabled:opacity-60"
        />
      </span>
      <span className="flex min-w-0 items-start gap-2">
        <span className="pr-icon-line text-fg-muted">
          <Icon size={16} aria-hidden="true" />
        </span>
        <span className="min-w-0 [overflow-wrap:anywhere]">{label}</span>
      </span>
    </label>
  );
}

export function Nl2SqlExecutionOptionsPanel({
  disabled,
  engine,
  includeInterpretation,
  includeShowPrompt,
  open,
  selectAiOverridesInactive,
  useOntologyContext,
  onIncludeInterpretationChange,
  onIncludeShowPromptChange,
  onOpenChange,
  onRewriteUseGlossaryChange,
  onUseOntologyContextChange,
  rewriteUseGlossary,
}: {
  disabled: boolean;
  engine: Nl2SqlEngine;
  includeInterpretation: boolean;
  includeShowPrompt: boolean;
  open: boolean;
  selectAiOverridesInactive: boolean;
  useOntologyContext: boolean;
  onIncludeInterpretationChange: (checked: boolean) => void;
  onIncludeShowPromptChange: (checked: boolean) => void;
  onOpenChange: (open: boolean) => void;
  onRewriteUseGlossaryChange: (checked: boolean) => void;
  onUseOntologyContextChange: (checked: boolean) => void;
  rewriteUseGlossary: boolean;
}) {
  const showPromptUnavailable = includeShowPrompt && engine !== "select_ai";
  // 既定値からの変更有無でバッジを出す。用語・同義語と Show Prompt は既定 off なので ON が「変更あり」。
  const hasChangedOptions =
    rewriteUseGlossary ||
    selectAiOverridesInactive ||
    !useOntologyContext ||
    !includeInterpretation ||
    includeShowPrompt;

  return (
    <section
      className="overflow-hidden rounded-md border border-border bg-surface-sunken text-sm"
      aria-labelledby="nl2sql-execution-options-heading"
      data-testid="nl2sql-execution-options"
    >
      <Button className="w-full justify-between"
        type="button"
        variant="ghost"
        size="md"
        aria-expanded={open}
        aria-controls="nl2sql-execution-options-body"
        onClick={() => onOpenChange(!open)}
        disabled={disabled}
      >
        <span className="flex min-w-0 items-center gap-2">
          <Sparkles size={16} className="shrink-0 text-fg" aria-hidden="true" />
          <span
            id="nl2sql-execution-options-heading"
            className="min-w-0 [overflow-wrap:anywhere]"
          >
            {t("nl2sql.executionOptions.title")}
          </span>
          {hasChangedOptions ? (
            <span data-surface-tint="accent" className="shrink-0 rounded-full bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent-fg">
              {t("nl2sql.selectAiOverrides.activeBadge")}
            </span>
          ) : null}
        </span>
        <DisclosureChevron
          expanded={open}
          size={16}
        />
      </Button>
      <div
        id="nl2sql-execution-options-body"
        hidden={!open}
        className={open ? "grid gap-3 border-t border-border p-3" : "hidden"}
      >
        <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.executionOptions.hint")}</p>
        <div className="grid gap-3 md:grid-cols-2">
          <OptionCheckbox
            checked={rewriteUseGlossary}
            disabled={disabled}
            icon={Sparkles}
            label={t("nl2sql.rewrite.useGlossary")}
            onChange={onRewriteUseGlossaryChange}
          />
          <OptionCheckbox
            checked={useOntologyContext}
            disabled={disabled}
            icon={Network}
            label={t("nl2sql.executionOptions.useOntology")}
            onChange={onUseOntologyContextChange}
          />
          <OptionCheckbox
            checked={includeInterpretation}
            disabled={disabled}
            icon={Eye}
            label={t("nl2sql.executionOptions.includeInterpretation")}
            onChange={onIncludeInterpretationChange}
          />
          <OptionCheckbox
            checked={includeShowPrompt}
            disabled={disabled}
            icon={FileText}
            label={t("nl2sql.executionOptions.includeShowPrompt")}
            onChange={onIncludeShowPromptChange}
          />
        </div>
        {showPromptUnavailable ? (
          <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-fg-muted">
            {t("nl2sql.executionOptions.showPromptUnsupported")}
          </p>
        ) : null}
        {selectAiOverridesInactive ? (
          <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-fg-muted">
            {t("nl2sql.executionOptions.selectAiOverridesUnsupported")}
          </p>
        ) : null}
      </div>
    </section>
  );
}
