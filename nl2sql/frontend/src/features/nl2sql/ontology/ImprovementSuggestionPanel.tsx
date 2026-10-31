import { TextQuote } from "lucide-react";
import { Button, ContentActionBar, CopyButton } from "@production-ready/ui";
import { t } from "@/lib/i18n";
import { pendingSuggestions, type ImprovementSuggestion } from "./publicationSummary";

/**
 * 「業務説明に追記する改善案」（#1729）。残ったエラー・警告から backend が決定論で作った文を出し、
 * コピーと「業務説明に追記」を押せるようにする。業務説明は利用者が押したときだけ書き換える
 * （自動で書き換えない。#1727）。既に業務説明に書かれた文は重ねて出さない。
 */
export function ImprovementSuggestionPanel({ suggestions, businessText, onAppend }: {
  suggestions: ImprovementSuggestion[];
  businessText: string;
  onAppend?: (text: string) => void;
}) {
  const pending = pendingSuggestions(suggestions, businessText);
  if (!pending.length) return null;
  const text = pending.map(item => item.text).join("\n");
  return <section className="grid min-w-0 gap-3 rounded-md border border-border bg-surface p-4" aria-label={t("markdownOntology.suggestion.title")} data-testid="ontology-improvement-suggestion">
    <div className="grid min-w-0 gap-1">
      <h4 className="text-sm font-semibold">{t("markdownOntology.suggestion.title")}</h4>
      <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.suggestion.hint")}</p>
    </div>
    <div tabIndex={0} role="region" aria-label={t("markdownOntology.suggestion.body")} className="max-h-60 min-w-0 overflow-y-auto overscroll-contain whitespace-pre-wrap break-words rounded-md border border-border bg-surface-sunken p-3 text-sm" data-testid="ontology-improvement-suggestion-text">
      {text}
    </div>
    <ContentActionBar ariaLabel={t("markdownOntology.suggestion.title")}>
      <CopyButton size="md" text={text} label={t("markdownOntology.suggestion.copy")} copiedMessage={t("common.action.copied")} failedMessage={t("common.action.copyFailed")} />
      {onAppend && <Button icon={TextQuote} type="button" variant="secondary" size="md" onClick={() => onAppend(text)}>{t("markdownOntology.suggestion.append")}</Button>}
    </ContentActionBar>
  </section>;
}
