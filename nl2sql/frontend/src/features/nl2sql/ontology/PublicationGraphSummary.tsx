import { Disclosure } from "@production-ready/ui";
import { t } from "@/lib/i18n";
import { summarizeOntologyGraph, type PublicationDefinition } from "./publicationSummary";

/**
 * 公開前の確認で、graph として何ができたか（オブジェクト・関係・プロパティ）を示す（#1720）。
 * 概念ごとの差分の一覧は利用者に目的が分からないため出さない（#1729）。
 */
export function PublicationGraphSummary({ definitions }: { definitions: PublicationDefinition[] }) {
  const summary = summarizeOntologyGraph(definitions);
  const counts = [
    { key: "objects", label: t("markdownOntology.graph.objects"), value: summary.objectCount },
    { key: "links", label: t("markdownOntology.graph.links"), value: summary.linkCount },
    { key: "properties", label: t("markdownOntology.graph.properties"), value: summary.propertyCount },
    ...(summary.otherCount ? [{ key: "others", label: t("markdownOntology.graph.others"), value: summary.otherCount }] : []),
  ];
  return <section className="grid min-w-0 gap-3 rounded-md border border-border bg-surface p-4" aria-label={t("markdownOntology.graph.title")} data-testid="ontology-publication-graph">
    <h4 className="text-sm font-semibold">{t("markdownOntology.graph.title")}</h4>
    <dl className="grid min-w-0 grid-cols-2 gap-3 sm:grid-cols-4">
      {counts.map(count => <div key={count.key} className="grid min-w-0 gap-1">
        <dt className="text-xs text-fg-muted">{count.label}</dt>
        <dd className="tnum text-lg font-semibold">{count.value}</dd>
      </div>)}
    </dl>
    {summary.objectCount > 0 && summary.linkCount === 0 && <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.graph.noLinks")}</p>}
    {summary.objects.length > 0 && <ul className="grid min-w-0 gap-2" aria-label={t("markdownOntology.graph.objectList")}>
      {summary.objects.map(object => <li key={object.apiName} className="min-w-0">
        <Disclosure variant="plain" size="sm"
          summary={<span className="grid min-w-0 gap-0.5">
            <span className="break-words">{object.label}</span>
            {object.physical && <span className="break-all font-mono text-xs font-normal text-fg-muted">{object.physical}</span>}
            <span className="tnum text-xs font-normal text-fg-muted">{t("markdownOntology.graph.objectCounts", { properties: object.properties.length, links: object.links.length })}</span>
          </span>}>
          <dl className="grid min-w-0 gap-2 text-sm">
            <div className="grid min-w-0 gap-1">
              <dt className="text-xs text-fg-muted">{t("markdownOntology.graph.properties")}</dt>
              <dd className="break-words">{object.properties.join("、") || t("markdownOntology.noConcept")}</dd>
            </div>
            <div className="grid min-w-0 gap-1">
              <dt className="text-xs text-fg-muted">{t("markdownOntology.graph.links")}</dt>
              <dd className="break-words">{object.links.length ? <ul className="grid gap-1">{object.links.map(link => <li key={link}>{link}</li>)}</ul> : t("markdownOntology.noConcept")}</dd>
            </div>
          </dl>
        </Disclosure>
      </li>)}
    </ul>}
  </section>;
}
