import { StatusBadge } from "@production-ready/ui";

import type { CragDiagnostics } from "@/lib/crag-diagnostics";
import { t, type I18nKey } from "@/lib/i18n";

const VALIDATION_LABELS: Record<string, I18nKey> = {
  matched: "search.answerDetails.crag.validation.matched",
  unverified: "search.answerDetails.crag.validation.unverified",
  invalid_source_or_quote: "search.answerDetails.crag.validation.invalid",
};

/** 生成へ進めることと、回答に必要な観点が揃ったことを分けて示す。 */
export function CragEvidenceDetails({ data }: { data: CragDiagnostics }) {
  return (
    <section className="space-y-2 rounded-md border border-border p-3 text-xs" aria-label={t("search.answerDetails.crag.title")} data-testid="answer-crag-details">
      <p className="font-medium">{t("search.answerDetails.crag.title")}</p>
      <p className="text-fg-muted">{t("search.answerDetails.crag.hint")}</p>
      <dl className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <dt>{t("search.answerDetails.crag.generation")}</dt>
          <dd><StatusBadge variant="neutral" label={t(data.generationUsable === true ? "search.answerDetails.crag.usable" : data.generationUsable === false ? "search.answerDetails.crag.unusable" : "search.answerDetails.crag.unknown")} /></dd>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <dt>{t("search.answerDetails.crag.coverage")}</dt>
          <dd><StatusBadge variant={data.coverage === "complete" ? "info" : "neutral"} label={t(`search.answerDetails.crag.coverage.${data.coverage}`)} /></dd>
        </div>
      </dl>
      {data.execution !== "evaluated" ? <p className="text-fg-muted">{t(`search.answerDetails.crag.execution.${data.execution}`)}</p> : null}
      {data.reason ? <p className="break-words">{data.reason}</p> : null}
      {data.missingAspects.length ? <p className="break-words">{t("search.answerDetails.crag.missing", { items: data.missingAspects.join("、") })}</p> : null}
      {data.unknownAspects.length ? <p className="break-words">{t("search.answerDetails.crag.unconfirmed", { items: data.unknownAspects.join("、") })}</p> : null}
      {data.aspects.length ? (
        <ul className="space-y-2">
          {data.aspects.map((item, index) => (
            <li key={`${item.aspect}-${index}`} className="space-y-1 break-words border-t border-border pt-2">
              <p>{item.aspect}: {t(`search.answerDetails.crag.aspect.${item.status}`)}</p>
              {item.reason ? <p className="text-fg-muted">{item.reason}</p> : null}
              {item.sourceIds.length ? <p>{t("search.answerDetails.crag.sources", { items: item.sourceIds.join("、") })}</p> : null}
              {item.evidence.length ? <div className="space-y-1">
                <p className="text-fg-muted">{t("search.answerDetails.crag.evidence")}</p>
                {item.evidence.map((evidence, evidenceIndex) => <blockquote key={`${evidence.sourceId}-${evidenceIndex}`} className="whitespace-pre-wrap border-l border-border pl-2">{evidence.sourceId}: {evidence.quote}</blockquote>)}
              </div> : null}
              <p className="text-fg-muted">{t("search.answerDetails.crag.validation", { value: t(VALIDATION_LABELS[item.evidenceValidation] ?? "search.answerDetails.crag.validation.unverified") })}</p>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
