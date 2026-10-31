import { Button, Disclosure, FormActionBar, FormSkeleton, FormStatus, TextareaField, TimedLoadingState } from "@production-ready/ui";
import { FlaskConical, RotateCcw, Save } from "lucide-react";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { LanguagePackFieldsPanel, useLanguagePackPanelState } from "@/components/settings/LanguagePackFieldsPanel";
import { type LanguagePackSections, type QuestionUnderstandingPreviewData } from "@/lib/api";
import { t } from "@/lib/i18n";
import { languagePackErrors, overridesFromTexts, textsFromSections, textKey, type LanguagePackTexts } from "@/lib/language-pack";
import { useLeaveGuard } from "@/lib/leave-guard";
import { usePreviewQuestionUnderstanding, useQuestionUnderstanding, useSaveQuestionUnderstanding } from "@/lib/queries";
import { toast } from "@/lib/toast";

/** 質問の認識に使う差分だけを保存し、全体と取込の設定は変更しない。 */
export function QuestionUnderstandingPanel({ searchAnswerProfileId, readOnly = false }: { searchAnswerProfileId: string; readOnly?: boolean }) {
  const query = useQuestionUnderstanding(searchAnswerProfileId);
  const save = useSaveQuestionUnderstanding(searchAnswerProfileId);
  const preview = usePreviewQuestionUnderstanding(searchAnswerProfileId);
  const panel = useLanguagePackPanelState();
  const [edited, setEdited] = useState<LanguagePackTexts>({});
  const [text, setText] = useState("");
  const data = query.data;
  const baseline = data ? textsFromSections(data.fields, data.effective) : {};
  const texts = { ...baseline, ...edited };
  const dirty = Object.keys(edited).length > 0;
  const errors = data ? languagePackErrors(data.fields, texts) : {};
  const invalid = Object.keys(errors).length > 0;
  useLeaveGuard(dirty, save.isPending);
  if (query.isPending) return <TimedLoadingState label={t("settings.language.loading")} operationKey={`question-understanding-${searchAnswerProfileId}`}><FormSkeleton fields={3} /></TimedLoadingState>;
  if (query.isError || !data) return <ApiErrorState error={query.error} fallback={t("settings.language.loadError")} onRetry={() => void query.refetch()} />;
  // 保存した明示的な上書きは、全体値が追いついても別の欄の編集では消さない。
  const overrides: LanguagePackSections = structuredClone(data.overrides);
  for (const field of data.fields) {
    const key = textKey(field);
    if (!(key in edited)) continue;
    const change = overridesFromTexts([field], texts, data.builtin);
    const section = { ...overrides[field.section] };
    if (change[field.section]) section[field.key] = change[field.section][field.key];
    else delete section[field.key];
    if (Object.keys(section).length) overrides[field.section] = section;
    else delete overrides[field.section];
  }
  return <div className="space-y-4" data-testid="question-understanding-panel">
    <p className="text-sm text-fg-muted">{t("searchAnswerProfiles.questionUnderstanding.helper")}</p>
    <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.questionUnderstanding.boundary")}</p>
    <LanguagePackFieldsPanel idPrefix="question-understanding" fields={data.fields} baseline={data.builtin}
      texts={texts} errors={errors} disabled={save.isPending || readOnly} state={panel}
      changedLabel={t("searchAnswerProfiles.languagePack.changed")} resetLabel={t("searchAnswerProfiles.languagePack.reset")}
      onChange={(key, value) => { save.reset(); preview.reset(); setEdited((now) => {
        const next = { ...now, [key]: value };
        if (value === baseline[key]) delete next[key];
        return next;
      }); }} />
    <FormActionBar ariaLabel={t("settings.language.actions.label")}
      primaryActions={[{ id: "save-understanding", label: t("searchAnswerProfiles.questionUnderstanding.save"), icon: Save,
        loading: save.isPending, disabled: !dirty || invalid || readOnly, onClick: () => save.mutate(overrides, { onSuccess: () => {
          setEdited({}); preview.reset(); toast.success(t("searchAnswerProfiles.questionUnderstanding.savedToast"));
        } }) }]}
      secondaryActions={[{ id: "discard-understanding", label: t("settings.language.actions.reset"), icon: RotateCcw,
        disabled: !dirty || save.isPending, onClick: () => { setEdited({}); save.reset(); preview.reset(); } }]}
      status={save.isError ? <FormStatus tone="danger" message={save.error.message} /> : null} />
    <section className="space-y-3" aria-label={t("searchAnswerProfiles.questionUnderstanding.preview")}>
      <h3 className="text-sm font-semibold">{t("searchAnswerProfiles.questionUnderstanding.preview")}</h3>
      <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.questionUnderstanding.previewHelp")}</p>
      <TextareaField id="question-understanding-example" width="full" rows={3}
        label={t("searchAnswerProfiles.runtime.previewQuestion")} maxLength={5000} value={text}
        onChange={(event) => { setText(event.target.value); preview.reset(); }} disabled={preview.isPending} />
      <Button icon={FlaskConical} loading={preview.isPending} disabled={!text.trim() || invalid || save.isPending}
        onClick={() => preview.mutate({ text, overrides })}>{t("settings.language.preview.run")}</Button>
      {preview.isError ? <FormStatus tone="danger" message={preview.error.message} /> : null}
      {preview.data ? <div data-testid="question-understanding-result" className="space-y-3">
        <TextareaField id="question-understanding-original" label={t("searchAnswerProfiles.questionUnderstanding.original")} width="full" value={preview.data.original_text} readOnly rows={2} />
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          <RecognitionResult kind="saved" result={preview.data.saved} />
          <RecognitionResult kind="draft" result={preview.data.draft} />
        </div>
      </div> : null}
    </section>
  </div>;
}

function RecognitionResult({ kind, result }: { kind: "saved" | "draft"; result: QuestionUnderstandingPreviewData["saved"] }) {
  return <Disclosure summary={t(`searchAnswerProfiles.questionUnderstanding.${kind}`)} defaultOpen>
    <div className="min-w-0 space-y-2 p-3">
      <p className="break-all text-xs text-fg-muted">{t("searchAnswerProfiles.questionUnderstanding.version")}: {result.hash}</p>
      <TextareaField id={`question-understanding-${kind}-result`} label={t("searchAnswerProfiles.questionUnderstanding.details")} width="full" rows={8} readOnly
        value={JSON.stringify({ inquiry: result.inquiry, task_contract: result.task_contract }, null, 2)} />
    </div>
  </Disclosure>;
}
