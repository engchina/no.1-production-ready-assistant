import type { ReactNode } from "react";

import type { SupportGuideContent, SupportGuideReference, SupportGuideSheetRange } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { formatPageRanges } from "@/lib/support-guide-form";

function joined(values: readonly string[]): string {
  return values.length > 0 ? values.join("、") : t("supportGuides.view.none");
}

/** 表計算の資料のシートの行の表示（「シートA 行 42-45」。セルの範囲があればセルの範囲）。 */
function sheetText(range: SupportGuideSheetRange): string {
  if (range.cell_range) return `${range.sheet_name}!${range.cell_range}`;
  if (range.row_start == null) return range.sheet_name;
  const end = range.row_end ?? range.row_start;
  const rows = end === range.row_start ? `${range.row_start}` : `${range.row_start}-${end}`;
  return t("supportGuides.view.sheetRows", { sheet: range.sheet_name, rows });
}

/** 資料の表示（表示名・節・頁の範囲か表計算のシートの行）。 */
function referenceText(reference: SupportGuideReference): string {
  const pages = formatPageRanges(reference.page_ranges ?? []);
  const sheets = (reference.sheet_ranges ?? []).map(sheetText).join("、");
  return [
    reference.title || reference.document_id,
    reference.section_path.length > 0 ? ` — ${reference.section_path.join(" > ")}` : "",
    pages ? `（${t("supportGuides.view.pages", { pages })}）` : "",
    sheets ? `（${sheets}）` : "",
  ].join("");
}

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-fg-muted">{label}</dt>
      <dd className="mt-0.5 whitespace-pre-wrap break-words text-sm text-fg">{children}</dd>
    </div>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-2">
      <h6 className="text-sm font-semibold text-fg">{title}</h6>
      {children}
    </section>
  );
}

/** 公開した版の内容を読むだけの表示（「この版を見る」）。 */
export function SupportGuideContentView({ content }: { content: SupportGuideContent }) {
  const conditionLabel = new Map(content.conditions.map((item) => [item.id, item.label]));
  const stepTitle = new Map(content.steps.map((item) => [item.id, item.title]));
  return (
    <div className="space-y-4" data-testid="support-guide-content-view">
      <Block title={t("supportGuides.section.basic")}>
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Item label={t("supportGuides.field.title")}>{content.title}</Item>
          <Item label={t("supportGuides.field.expectedResult")}>{content.goal.expected_result}</Item>
          {content.description ? (
            <Item label={t("supportGuides.field.description")}>{content.description}</Item>
          ) : null}
          <Item label={t("supportGuides.field.intentExamples")}>
            {joined(content.goal.intent_examples)}
          </Item>
          <Item label={t("supportGuides.field.matchTerms")}>{joined(content.goal.match_terms)}</Item>
        </dl>
      </Block>
      {content.conditions.length > 0 ? (
        <Block title={t("supportGuides.section.conditions")}>
          <ul className="space-y-1 text-sm text-fg">
            {content.conditions.map((condition) => (
              <li key={condition.id} className="break-words">
                <span className="font-medium">{condition.label}</span>
                {condition.allowed_values.length > 0 ? `（${condition.allowed_values.join(" / ")}）` : ""}
                {condition.question ? <span className="text-fg-muted"> — {condition.question}</span> : null}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}
      <Block title={t("supportGuides.section.steps")}>
        <ol className="list-decimal space-y-2 pl-5 text-sm text-fg">
          {content.steps.map((step) => (
            <li key={step.id} className="break-words">
              <span className="font-medium">{step.title}</span>
              {step.purpose ? <p className="text-fg-muted">{step.purpose}</p> : null}
              {step.done_when ? (
                <p className="text-xs text-fg-muted">
                  {t("supportGuides.field.doneWhen")}: {step.done_when}
                </p>
              ) : null}
              {(step.references ?? []).length > 0 ? (
                <p className="text-xs text-fg-muted">
                  {t("supportGuides.field.stepReferences")}: {(step.references ?? []).map(referenceText).join("、")}
                </p>
              ) : null}
            </li>
          ))}
        </ol>
      </Block>
      {content.branches.length > 0 ? (
        <Block title={t("supportGuides.section.branches")}>
          <ul className="space-y-1 text-sm text-fg">
            {content.branches.map((branch) => (
              <li key={branch.id} className="break-words">
                {t("supportGuides.view.branchRule", {
                  condition: conditionLabel.get(branch.when.condition_id) ?? branch.when.condition_id,
                  operator: t(`supportGuides.operator.${branch.when.operator}` as I18nKey),
                  values: branch.when.values.length > 0 ? `（${branch.when.values.join(" / ")}）` : "",
                  step: stepTitle.get(branch.goto_step) ?? branch.goto_step,
                })}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}
      {content.references.length > 0 ? (
        <Block title={t("supportGuides.section.references")}>
          <ul className="space-y-1 text-sm text-fg">
            {content.references.map((reference, index) => (
              <li key={`${reference.document_id}-${index}`} className="break-words">
                {referenceText(reference)}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}
      {content.completion.length > 0 ? (
        <Block title={t("supportGuides.section.completion")}>
          <ul className="list-disc space-y-1 pl-5 text-sm text-fg">
            {content.completion.map((item) => (
              <li key={item.id} className="break-words">
                {item.description}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}
      <Block title={t("supportGuides.section.impact")}>
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Item label={t("supportGuides.field.impactScope")}>
            {t(`supportGuides.scope.${content.impact.scope}` as I18nKey)}・
            {t(
              content.impact.approval_required
                ? "supportGuides.view.approvalRequired"
                : "supportGuides.view.approvalNotRequired",
            )}
          </Item>
          <Item label={t("supportGuides.field.impactSteps")}>
            {(content.impact.steps ?? []).length > 0
              ? joined(
                  (content.impact.steps ?? []).map(
                    (id) => content.steps.find((step) => step.id === id)?.title ?? id,
                  ),
                )
              : t("supportGuides.view.allCases")}
          </Item>
          <Item label={t("supportGuides.field.handoffContact")}>
            {content.handoff.contact || t("supportGuides.view.none")}
          </Item>
          <Item label={t("supportGuides.field.handoffConditions")}>
            {joined(content.handoff.conditions)}
          </Item>
        </dl>
      </Block>
    </div>
  );
}
