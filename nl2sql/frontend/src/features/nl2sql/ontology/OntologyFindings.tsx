import { Disclosure, StatusBadge } from "@production-ready/ui";
import { t } from "@/lib/i18n";
import { findingName, findingTarget, partitionFindings } from "./publicationSummary";
import type { OntologyFinding } from "./types";

interface FindingGroup { message: string; severity: string; items: OntologyFinding[] }

function groupFindings(findings: OntologyFinding[]): FindingGroup[] {
  const groups = new Map<string, FindingGroup>();
  for (const finding of findings) {
    const message = (finding.message_ja || finding.message || "").trim();
    if (!message) continue;
    const key = `${finding.severity}:${message.replace(/\s+/g, " ")}`;
    const group = groups.get(key);
    if (group) group.items.push(finding);
    else groups.set(key, { message, severity: finding.severity, items: [finding] });
  }
  return [...groups.values()].sort((a, b) => Number(b.severity === "error") - Number(a.severity === "error"));
}

/**
 * 診断の対象は業務の名前で出し、内部の ID・項目名・診断コードは「技術的な詳細」に畳む（#1729）。
 */
function FindingTargets({ items, names }: { items: OntologyFinding[]; names?: Map<string, string> }) {
  const targets = [...new Set(items.map(item => findingName(item, names)).filter(Boolean))];
  const technical = [...new Set(items.map(item => findingTarget(item)).filter(Boolean))];
  return <div className="grid min-w-0 gap-2">
    {targets.length > 0 && <p className="break-words text-xs text-fg-muted">{t("markdownOntology.findingTargetsLabel", { names: targets.join("、") })}</p>}
    {technical.length > 0 && <Disclosure variant="plain" size="sm" summary={t("ontologyUi.technicalDetails")}>
      <ul className="grid min-w-0 gap-1">
        {technical.map(target => <li key={target} className="break-all font-mono text-xs text-fg-muted">{target}</li>)}
      </ul>
    </Disclosure>}
  </div>;
}

/**
 * 公開を止めるエラーと警告は 1 件ずつのまとまりで出し、graph を作れるが補えば精度が上がる項目
 * （info）は 1 つの畳んだ領域にまとめる（#1720。同じ補足を何件も並べない）。
 * 表示だけを集約し、公開版に保存した対象・コード・原文は変更しない。
 */
export function OntologyFindings({ findings, label, names }: {
  findings: OntologyFinding[];
  label: string;
  names?: Map<string, string>;
}) {
  const { blocking, warnings, improvements } = partitionFindings(findings);
  const others = findings.filter(finding => !["error", "warning", "info"].includes(finding.severity));
  const groups = groupFindings([...blocking, ...warnings, ...others]);
  const improvementGroups = groupFindings(improvements);
  if (!groups.length && !improvementGroups.length) return null;
  return <div className="grid min-w-0 gap-3">
    {groups.length > 0 && <div role="region" aria-label={label} tabIndex={0}
      className="max-h-72 min-w-0 overflow-y-auto overscroll-contain rounded-md border border-border bg-surface p-4">
      <ul className="grid min-w-0 gap-4">
        {groups.map(group => <li key={`${group.severity}:${group.message}`} className="grid min-w-0 gap-2 border-b border-border pb-4 last:border-b-0 last:pb-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge variant={group.severity === "error" ? "danger" : "warning"}
              label={t(group.severity === "error" ? "markdownOntology.blockingError" : "markdownOntology.warning")} />
            <span className="text-xs text-fg-muted">{t("markdownOntology.occurrences", { count: group.items.length })}</span>
          </div>
          <p className="whitespace-pre-wrap break-words text-sm">{group.message}</p>
          <FindingTargets items={group.items} names={names} />
        </li>)}
      </ul>
    </div>}
    {improvementGroups.length > 0 && <Disclosure data-testid="ontology-improvement-findings"
      summary={t("markdownOntology.improvements")}
      meta={<span className="tnum text-xs text-fg-muted">{t("markdownOntology.occurrences", { count: improvements.length })}</span>}
      summaryClassName="px-4 py-3 font-medium" contentClassName="grid gap-4 p-4">
      <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.improvementsHint")}</p>
      <ul className="grid min-w-0 gap-3">
        {improvementGroups.map(group => <li key={group.message} className="grid min-w-0 gap-1">
          <p className="whitespace-pre-wrap break-words text-sm">
            {group.message}
            <span className="ml-2 text-xs text-fg-muted">{t("markdownOntology.occurrences", { count: group.items.length })}</span>
          </p>
          <FindingTargets items={group.items} names={names} />
        </li>)}
      </ul>
    </Disclosure>}
  </div>;
}
