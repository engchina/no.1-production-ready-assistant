import { FlaskConical, Sparkles } from "lucide-react";
import { useState } from "react";

import {
  ApiErrorBanner,
  Banner,
  Button,
  FieldActionRow,
  FieldError,
  Fieldset,
  FormGrid,
  ProcessingIndicator,
  SelectField,
  StatusBadge,
  TextareaField,
  TextField,
  isSubmitEnter,
  type SelectFieldOption,
} from "@production-ready/ui";

import { CitationCard } from "@/components/search/CitationCard";
import { AnswerText } from "@/components/search/AnswerText";
import type { SupportGuideDetail, SupportGuideDraftTryData } from "@/lib/api";
import { citationCountLabel } from "@/lib/chunk-metadata";
import { t, type I18nKey } from "@/lib/i18n";
import { useTrySupportGuideDraft } from "@/lib/queries";
import {
  SUPPORT_GUIDE_DECISION_VARIANT,
  SUPPORT_GUIDE_STEP_STATUS_VARIANT,
  supportGuideStepCounts,
  tryConditionOptions,
  tryConditionsPayload,
} from "@/lib/support-guide-form";

/**
 * 保存した下書きで試しに答える（#1288）。公開の版は変えず、試した回答は利用者の回答の履歴・
 * フィードバック・評価に入らない（backend が回答の記録に印を残して外す）。
 *
 * 試すのは保存した下書き（`draft_revision`）なので、未保存の変更があるときは押せない（検証・公開と
 * 同じ）。結果は操作の行の直下に出し、次に試すまで残す（UX 契約 messaging.md §10）。
 */
export function SupportGuideDraftTry({
  searchAnswerProfileId,
  detail,
  dirty,
  disabled,
}: {
  searchAnswerProfileId: string;
  detail: SupportGuideDetail;
  dirty: boolean;
  disabled: boolean;
}) {
  const [query, setQuery] = useState("");
  const [queryError, setQueryError] = useState<string | null>(null);
  const [conditions, setConditions] = useState<Record<string, string>>({});
  const run = useTrySupportGuideDraft(searchAnswerProfileId);
  const result = run.data ?? null;
  const conditionFields = detail.draft.conditions.filter((condition) => condition.source === "user");
  const inputId = `support-guide-try-query-${detail.guide_id}`;

  const submit = () => {
    if (run.isPending || dirty || disabled) return;
    if (!query.trim()) {
      setQueryError(t("supportGuides.check.required", { field: t("supportGuides.try.query") }));
      document.getElementById(inputId)?.focus();
      return;
    }
    setQueryError(null);
    run.mutate({
      guideId: detail.guide_id,
      query: query.trim(),
      draftRevision: detail.draft_revision,
      conditions: tryConditionsPayload(conditions),
    });
  };

  return (
    <section
      className="min-w-0 space-y-3 border-t border-border pt-3"
      aria-labelledby="support-guide-try-title"
      data-testid="support-guide-try"
    >
      <div className="space-y-0.5">
        <h5 id="support-guide-try-title" className="flex items-center gap-1.5 text-sm font-semibold text-fg">
          <FlaskConical size={16} aria-hidden />
          {t("supportGuides.try.title")}
        </h5>
        <p className="text-xs leading-relaxed text-fg-muted">
          {t("supportGuides.try.hint", { revision: detail.draft_revision })}
        </p>
      </div>

      {conditionFields.length > 0 ? (
        <Fieldset
          legend={t("supportGuides.try.conditions")}
          helper={t("supportGuides.try.conditionsHelp")}
        >
          {/* 条件の値は名前・一般の選択の段（md）。2 列で並べる（#1891）。 */}
          <FormGrid data-testid="support-guide-try-conditions">
            {conditionFields.map((condition) => {
              const id = `support-guide-try-condition-${detail.guide_id}-${condition.id}`;
              const values = tryConditionOptions(condition);
              const value = conditions[condition.id] ?? "";
              const onValueChange = (next: string) =>
                setConditions((current) => ({ ...current, [condition.id]: next }));
              if (values.length === 0) {
                return (
                  <TextField
                    key={condition.id}
                    id={id}
                    width="md"
                    label={condition.label}
                    value={value}
                    maxLength={200}
                    disabled={run.isPending}
                    onValueChange={onValueChange}
                  />
                );
              }
              const options: SelectFieldOption[] = [
                { value: "", label: t("supportGuides.try.unset") },
                ...values.map((item) => ({ value: item, label: item })),
              ];
              return (
                <SelectField
                  key={condition.id}
                  id={id}
                  width="md"
                  label={condition.label}
                  value={value}
                  options={options}
                  disabled={run.isPending}
                  onValueChange={onValueChange}
                />
              );
            })}
          </FormGrid>
        </Fieldset>
      ) : null}

      <FieldActionRow
        actions={
          <Button
            icon={FlaskConical}
            loading={run.isPending}
            disabled={dirty || disabled}
            onClick={submit}
            data-testid="support-guide-try-run"
          >
            {t("supportGuides.try.run")}
          </Button>
        }
        footer={
          queryError ? (
            <FieldError id={`${inputId}-error`} message={queryError} />
          ) : dirty ? (
            <p className="text-xs text-fg-muted" data-testid="support-guide-try-save-first">
              {t("supportGuides.try.saveFirst")}
            </p>
          ) : null
        }
      >
        {/* Enter で試し、Shift+Enter で改行する（IME の変換を確定する Enter では試さない）。 */}
        <TextareaField
          id={inputId}
          width="full"
          label={t("supportGuides.try.query")}
          value={query}
          rows={2}
          maxLength={2000}
          required
          placeholder={t("supportGuides.try.queryPlaceholder")}
          disabled={run.isPending}
          aria-invalid={queryError ? true : undefined}
          aria-describedby={queryError ? `${inputId}-error` : undefined}
          onChange={(event) => {
            setQuery(event.target.value);
            setQueryError(null);
          }}
          onKeyDown={(event) => {
            if (isSubmitEnter(event) && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
      </FieldActionRow>

      <div aria-live="polite" className="space-y-3" data-testid="support-guide-try-result">
        {run.isPending ? (
          <ProcessingIndicator
            active
            label={t("supportGuides.try.running")}
            operationKey={`support-guide-try-${detail.guide_id}`}
            placement="action"
            activityIcon="none"
            testId="support-guide-try-processing"
          />
        ) : run.isError ? (
          <ApiErrorBanner
            error={run.error}
            fallback={t("supportGuides.try.error")}
            testId="support-guide-try-error"
          />
        ) : result ? (
          <DraftTryResult result={result} />
        ) : null}
      </div>
    </section>
  );
}

function DraftTryResult({ result }: { result: SupportGuideDraftTryData }) {
  const guide = result.guide;
  const steps = result.steps ?? [];
  const { missing, partial, unknown } = supportGuideStepCounts(steps);
  const seconds = (result.elapsed_ms / 1000).toFixed(1);
  const title = result.guide_used
    ? t("supportGuides.try.used", { revision: result.draft_revision })
    : guide
      ? t("supportGuides.try.usedOther", { title: guide.title })
      : t("supportGuides.try.unused");
  return (
    <div className="min-w-0 space-y-3" data-testid="support-guide-try-answer">
      <Banner severity={result.guide_used ? "info" : "warning"} title={title}>
        <div className="space-y-1">
          {result.guide_used ? null : <p>{t("supportGuides.try.unusedHint")}</p>}
          <p>{t("supportGuides.try.recordNote")}</p>
          <p className="tnum">{t("supportGuides.try.duration", { seconds })}</p>
        </div>
      </Banner>

      {guide && result.guide_used ? (
        <dl className="grid min-w-0 gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
          <dt className="text-xs font-medium text-fg-muted">{t("supportGuides.try.decisionLabel")}</dt>
          <dd className="min-w-0">
            <StatusBadge
              variant={SUPPORT_GUIDE_DECISION_VARIANT[guide.decision]}
              label={t(`supportGuides.try.decision.${guide.decision}` as I18nKey)}
            />
          </dd>
          {guide.known_conditions.length > 0 ? (
            <>
              <dt className="text-xs font-medium text-fg-muted">{t("supportGuides.try.known")}</dt>
              <dd className="min-w-0 break-words text-fg" data-testid="support-guide-try-known">
                {guide.known_conditions
                  .map((item) =>
                    t("supportGuides.try.conditionValue", { label: item.label, value: item.value ?? "" }),
                  )
                  .join(t("supportGuides.change.fieldSeparator"))}
              </dd>
            </>
          ) : null}
          {guide.unknown_conditions.length > 0 ? (
            <>
              <dt className="text-xs font-medium text-fg-muted">{t("supportGuides.try.unknown")}</dt>
              <dd className="min-w-0 break-words text-fg" data-testid="support-guide-try-unknown">
                {guide.unknown_conditions
                  .map((item) => item.label)
                  .join(t("supportGuides.change.fieldSeparator"))}
              </dd>
            </>
          ) : null}
          {steps.length > 0 ? (
            <>
              <dt className="text-xs font-medium text-fg-muted">{t("supportGuides.try.steps")}</dt>
              <dd className="min-w-0 space-y-1.5" data-testid="support-guide-try-steps">
                {missing > 0 ? (
                  <p className="text-fg" data-testid="support-guide-try-steps-missing">
                    {t("supportGuides.try.stepsMissing", { count: missing })}
                  </p>
                ) : null}
                {partial > 0 ? (
                  <p className="text-fg" data-testid="support-guide-try-steps-partial">
                    {t("supportGuides.try.stepsPartial", { count: partial })}
                  </p>
                ) : null}
                {unknown > 0 ? (
                  <p className="text-fg" data-testid="support-guide-try-steps-unknown">
                    {t("supportGuides.try.stepsUnknown", { count: unknown })}
                  </p>
                ) : null}
                {/* 手順ごとの充足（#2686）。状態は色だけでなく文言で示し、375px では折り返す。 */}
                <ul className="space-y-1.5">
                  {steps.map((step) => (
                    <li
                      key={step.step_id}
                      className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1"
                      data-testid="support-guide-try-step"
                      data-status={step.status}
                    >
                      <span className="min-w-0 break-words text-fg">{step.title}</span>
                      <StatusBadge
                        variant={SUPPORT_GUIDE_STEP_STATUS_VARIANT[step.status]}
                        label={t(`supportGuides.try.stepStatus.${step.status}` as I18nKey)}
                      />
                      {step.required ? null : (
                        <span className="text-xs text-fg-muted">{t("supportGuides.try.stepOptional")}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}

      <section className="min-w-0 rounded-md border border-border bg-surface p-3">
        <h6 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-fg">
          <Sparkles size={16} className="text-accent-fg" aria-hidden />
          {t("supportGuides.try.answer")}
        </h6>
        <AnswerText text={result.answer} citations={result.citations} />
        {result.notice ? (
          <Banner severity="info" title={t("answer.notice.label")} className="mt-3">
            <p className="whitespace-pre-wrap break-words">{result.notice}</p>
          </Banner>
        ) : null}
      </section>

      {result.citations.length > 0 ? (
        <section className="min-w-0 space-y-2">
          <h6 className="text-sm font-semibold text-fg">
            {citationCountLabel(result.citations)}
          </h6>
          {/* trace_id を渡さない（試した回答にはフィードバックを付けない）。 */}
          <ul className="bounded-scroll-area-lg space-y-2 pr-1">
            {result.citations.map((chunk, index) => (
              <CitationCard key={chunk.chunk_id} chunk={chunk} index={index} sourceKind="answer" />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
