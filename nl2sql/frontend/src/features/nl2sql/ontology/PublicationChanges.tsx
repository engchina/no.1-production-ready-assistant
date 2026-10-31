import { Disclosure, StatusBadge } from "@production-ready/ui";
import { t } from "@/lib/i18n";
import { DefinitionFields, TechnicalDetails } from "./ontologyResultPresentation";
import { conceptLabel } from "./unifiedConcepts";
import {
  summarizeChanges,
  type ChangeRow,
  type PublicationDefinition,
  type PublicationDifference,
} from "./publicationSummary";
import type { OntologyFinding } from "./types";

const STATUS_VARIANT = { added: "success", changed: "info", removed: "danger" } as const;

function fieldNames(fields: string[]): string {
  return fields.map(field => {
    const key = `ontologyResults.field.${field}` as Parameters<typeof t>[0];
    const label = t(key);
    return label === key ? field : label.replace(/（[^）]*）$/u, "");
  }).join("・");
}

/** 1 つの概念の差分の行。業務の名前と、平易な 1 文の「何が変わるか / 何が問題か / どうすればよいか」。 */
function ChangeItem({ row }: { row: ChangeRow }) {
  const what = row.status === "added"
    ? t("markdownOntology.changes.addedText", { kind: conceptLabel(row.kind).replace(/（[^）]*）$/u, "") })
    : row.status === "removed"
      ? t("markdownOntology.changes.removedText")
      : row.fields.length
        ? t("markdownOntology.changes.changedText", { fields: fieldNames(row.fields) })
        : t("markdownOntology.changes.changedNoField");
  return <li className="grid min-w-0 gap-2 border-b border-border pb-3 last:border-b-0 last:pb-0" data-testid="ontology-publication-difference">
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="break-words text-sm font-medium">{row.name}</span>
      <span className="text-xs text-fg-muted">{conceptLabel(row.kind)}</span>
      <StatusBadge variant={STATUS_VARIANT[row.status]} label={t(`markdownOntology.changes.${row.status}`)} />
    </div>
    <p className="break-words text-sm">{what}</p>
    {row.issues.map((issue, index) => <p key={`${issue.code ?? ""}-${index}`} className="whitespace-pre-wrap break-words text-sm text-fg-muted">
      {issue.message_ja || issue.message}
    </p>)}
    {row.status === "removed" && <p className="break-words text-sm text-fg-muted">{t("markdownOntology.changes.removedAction")}</p>}
    <TechnicalDetails>
      <div className="grid min-w-0 gap-3 text-xs">
        <p className="break-all font-mono text-fg-muted">{[row.apiName, row.id, ...row.issues.map(issue => [issue.field, issue.code].filter(Boolean).join(" / "))].filter(Boolean).join(" ・ ")}</p>
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          <div className="min-w-0 space-y-2"><h5 className="text-xs font-semibold text-fg-muted">{t("markdownOntology.changes.before")}</h5><DefinitionFields definition={row.before ?? {}} /></div>
          <div className="min-w-0 space-y-2"><h5 className="text-xs font-semibold text-fg-muted">{t("markdownOntology.changes.after")}</h5><DefinitionFields definition={row.after ?? {}} /></div>
        </div>
      </div>
    </TechnicalDetails>
  </li>;
}

/**
 * 「概念ごとの差分」（#1729）。公開すると前の公開版から何が変わるかを確かめる所。
 * 既定では確認が要るもの（問題・警告のある概念と削除）だけを件数とともに上に出し、問題の無い変更は
 * 「問題なし N 件」に畳む。初回の公開は件数の 1 行と確認が要るものだけにする（「追加」のバッジを
 * 並べない）。内部の ID・項目名・診断コードは「技術的な詳細」に畳む。
 */
export function PublicationChanges({ differences, findings, definitions }: {
  differences: PublicationDifference[];
  findings: OntologyFinding[];
  definitions: PublicationDefinition[];
}) {
  const summary = summarizeChanges(differences, findings, definitions);
  return <section className="grid min-w-0 gap-3 rounded-md border border-border bg-surface p-4" aria-label={t("markdownOntology.changes.title")} data-testid="ontology-publication-differences">
    <div className="grid min-w-0 gap-1">
      <h4 className="text-sm font-semibold">{t("markdownOntology.changes.title")}</h4>
      <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.changes.hint")}</p>
    </div>
    <p className="tnum break-words text-sm" data-testid="ontology-publication-change-summary">
      {summary.total === 0
        ? t("markdownOntology.changes.nothing")
        : summary.first
          ? t("markdownOntology.changes.first", { count: summary.total })
          : t("markdownOntology.changes.summary", summary.counts)}
    </p>
    {summary.total > 0 && <div className="grid min-w-0 gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h5 className="text-sm font-semibold">{t("markdownOntology.changes.review")}</h5>
        <span className="tnum text-xs text-fg-muted">{t("markdownOntology.occurrences", { count: summary.review.length })}</span>
      </div>
      {summary.review.length
        ? <ul className="grid min-w-0 gap-3" aria-label={t("markdownOntology.changes.review")}>{summary.review.map(row => <ChangeItem key={row.id} row={row} />)}</ul>
        : <p className="text-sm text-fg-muted">{t("markdownOntology.changes.noReview")}</p>}
    </div>}
    {!summary.first && summary.others.length > 0 && <Disclosure data-testid="ontology-publication-unchanged-issues"
      summary={t("markdownOntology.changes.ok")}
      meta={<span className="tnum text-xs text-fg-muted">{t("markdownOntology.occurrences", { count: summary.others.length })}</span>}
      summaryClassName="px-4 py-3 font-medium" contentClassName="grid gap-3 p-4">
      <ul className="grid min-w-0 gap-3">{summary.others.map(row => <ChangeItem key={row.id} row={row} />)}</ul>
    </Disclosure>}
  </section>;
}
