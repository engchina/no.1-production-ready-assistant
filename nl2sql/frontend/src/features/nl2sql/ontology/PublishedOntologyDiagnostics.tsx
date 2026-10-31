import { Banner, Button } from "@production-ready/ui";
import { Download } from "lucide-react";
import { t } from "@/lib/i18n";
import { OntologyFindings } from "./OntologyFindings";
import type { OntologyMarkdownState } from "./types";

export function PublishedOntologyDiagnostics({ state }: { state: OntologyMarkdownState }) {
  const findings = state.published_findings ?? [];
  const available = state.published_diagnostics_available;
  const label = t("markdownOntology.publishedFindings", { version: state.published_version ?? state.published_revision?.version ?? "" });
  function download() {
    const blob = new Blob([JSON.stringify({
      snapshot_id: state.published_revision?.id,
      display_version: state.published_version,
      published_at: state.published_at,
      findings,
      data_report: state.published_data_report,
    }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `ontology-diagnostics-${state.published_revision?.id ?? "published"}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className="grid min-w-0 gap-3" aria-label={label}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">{label}</h3>
      {available && <Button icon={Download} type="button" variant="secondary" size="sm" onClick={download}>{t("markdownOntology.exportDiagnostics")}</Button>}
    </div>
    {available ? findings.length ? <>
      {/* 補えば精度が上がる項目（info）だけなら警告の帯を出さない（#1720）。 */}
      {findings.some(finding => finding.severity !== "info") && <Banner severity="warning">{t("markdownOntology.publishedWarningsRetained")}</Banner>}
      <OntologyFindings findings={findings} label={t("markdownOntology.publishedFindingList")} />
    </> : <p className="text-sm text-fg-muted">{t("markdownOntology.noFindings")}</p>
      : <p className="text-sm text-fg-muted">{t("markdownOntology.diagnosticsUnavailable")}</p>}
  </section>;
}
