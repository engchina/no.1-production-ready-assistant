"use client";

import {
  Banner,
  Button,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormSkeleton,
  FormStatus,
  PageBody,
  TextareaField,
  TimedLoadingState,
  useConfirm,
} from "@production-ready/ui";
import { FlaskConical, Languages, RotateCcw, Save } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { LanguagePackFieldsPanel, useLanguagePackPanelState } from "@/components/settings/LanguagePackFieldsPanel";
import { LanguagePackHistoryCard } from "@/components/settings/LanguagePackHistoryCard";
import {
  api,
  ApiError,
  type LanguagePackData,
  type LanguagePackPreviewResult,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  type LanguagePackTexts,
  fieldId,
  languagePackErrors,
  changesIngestFields,
  overridesFromTexts,
  textKey,
  textsFromSections,
} from "@/lib/language-pack";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useLanguagePack, usePreviewLanguagePack } from "@/lib/queries";
import { focusFirstInvalidField } from "@/lib/required-fields";
import { toast } from "@/lib/toast";
import { APP_ROUTES } from "@/lib/routes";

/** 言語資源（文書と質問を読むときの言語の書き方。#2181）の全体の設定。 */
export function LanguagePackSettingsClient() {
  const query = useLanguagePack();
  const confirm = useConfirm();
  const draft = useQuery({ queryKey: ["language-pack-draft"], queryFn: api.getLanguagePackDraft, retry: false });
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (overrides: import("@/lib/api").LanguagePackSections) => api.updateLanguagePackDraft(draft.data!.draft_revision, overrides),
    onSuccess: (data) => qc.setQueryData(["language-pack-draft"], data),
  });
  const [texts, setTexts] = useState<LanguagePackTexts | null>(null);
  const [base, setBase] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const panel = useLanguagePackPanelState();
  const serverTexts = query.data ? textsFromSections(query.data.fields, (draft.data?.draft_effective ?? query.data.effective)) : null;
  const serverKey = serverTexts ? JSON.stringify(serverTexts) : null;
  if (serverTexts && serverKey !== base) {
    setBase(serverKey);
    if (texts === null || JSON.stringify(texts) === base) setTexts(serverTexts);
  }
  const dirty = Boolean(texts && serverKey && JSON.stringify(texts) !== serverKey);
  // 検索・回答だけの項目（全文検索の質問の語など）は、文書を分割し直さなくても反映する。
  const reingest = Boolean(dirty && query.data && texts && serverTexts && changesIngestFields(query.data.fields, texts, serverTexts));
  useLeaveGuard(dirty, save.isPending);

  if (query.isPending || draft.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.language.loading")}
          operationKey="settings-language-load"
          placement="page"
          testId="settings-language-loading"
        >
          <FormSkeleton fields={4} />
        </TimedLoadingState>
      </PageBody>
    );
  }
  if (query.isError || draft.isError) {
    return (
      <PageBody wide>
        <ApiErrorState
          error={query.error ?? draft.error}
          fallback={t("settings.language.loadError")}
          onRetry={() => { void query.refetch(); void draft.refetch(); }}
        />
      </PageBody>
    );
  }
  const data = query.data;
  if (!data || !texts) return null;
  const saveError = save.error instanceof ApiError ? save.error.message : t("settings.language.saveError");

  function update(key: string, value: string) {
    save.reset();
    setErrors((now) => Object.fromEntries(Object.entries(now).filter(([name]) => name !== key)));
    setTexts((now) => (now ? { ...now, [key]: value } : now));
  }

  function submit() {
    if (!data || !texts || save.isPending) return;
    const found = languagePackErrors(data.fields, texts);
    setErrors(found);
    const firstInvalid = data.fields.find((field) => found[textKey(field)]);
    if (firstInvalid) {
      // 誤りのある項目の節を開いてから（検索中なら検索を解く）、その欄へ移す。
      panel.reveal(firstInvalid.section);
      requestAnimationFrame(() =>
        focusFirstInvalidField(data.fields.map((field) => [fieldId(field), found[textKey(field)]] as const)),
      );
      return;
    }
    save.mutate(overridesFromTexts(data.fields, texts, data.builtin), {
      onSuccess: (saved) => {
        const next = textsFromSections(data.fields, saved.draft_effective);
        setBase(JSON.stringify(next));
        setTexts(next);
        toast.success(t("settings.language.saved"));
      },
    });
  }

  return (
    <PageBody wide>
      {data.current_release ? <div className="space-y-3">
        {data.outdated_documents ? <Banner severity="warning" title={t("settings.language.release.differentCount", { count: data.outdated_documents })}>
          <p>{t("settings.language.release.differentHelp")}</p>
          <ButtonLink className="mt-3" variant="secondary" to={`${APP_ROUTES.fileList}?language_pack_relation=different&current_release_id=${encodeURIComponent(data.current_release.id)}`}>{t("settings.language.release.documents")}</ButtonLink>
        </Banner> : null}
        {data.unrecorded_documents ? <Banner severity="info" title={t("settings.language.release.unrecordedCount", { count: data.unrecorded_documents })}>
          <p>{t("settings.language.release.unrecordedHelp")}</p>
          <ButtonLink className="mt-3" variant="secondary" to={`${APP_ROUTES.fileList}?language_pack_relation=unrecorded&current_release_id=${encodeURIComponent(data.current_release.id)}`}>{t("settings.language.release.documents")}</ButtonLink>
        </Banner> : null}
      </div> : null}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Languages size={16} className="text-accent-fg" aria-hidden />
            {t("settings.language.fields.title")}
          </CardTitle>
          <CardDescription>{t("settings.language.fields.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <LanguagePackFieldsPanel
            idPrefix="language"
            fields={data.fields}
            baseline={data.builtin}
            texts={texts}
            errors={errors}
            disabled={save.isPending}
            state={panel}
            changedLabel={t("settings.language.changed")}
            resetLabel={t("settings.language.resetField")}
            onChange={update}
          />
          <FormActionBar
            ariaLabel={t("settings.language.actions.label")}
            primaryActions={[
              {
                id: "save",
                label: t("settings.language.actions.save"),
                icon: Save,
                loading: save.isPending,
                disabled: !dirty,
                onClick: submit,
              },
            ]}
            secondaryActions={[
              {
                id: "reset",
                label: t("settings.language.actions.reset"),
                icon: RotateCcw,
                disabled: !dirty || save.isPending,
                onClick: () => {
                  save.reset();
                  setErrors({});
                  setTexts(textsFromSections(data.fields, draft.data?.draft_effective ?? data.effective));
                },
              },
            ]}
            status={
              save.isError ? (
                <ApiErrorState error={save.error} fallback={saveError} onRetry={() => void (async () => {
                  const ok = await confirm({ title: t("settings.language.release.reloadDraft"), description: t("settings.language.release.reloadDraftHelp"), confirmLabel: t("settings.language.release.reloadDraft"), tone: "warning" });
                  if (!ok) return;
                  const latest = await draft.refetch();
                  if (!latest.data) return;
                  const next = textsFromSections(data.fields, latest.data.draft_effective);
                  setTexts(next); setBase(JSON.stringify(next)); save.reset();
                })()} />
              ) : reingest ? (
                <FormStatus tone="warning" message={t("settings.language.reingest")} />
              ) : null
            }
          />
        </CardContent>
      </Card>
      <PreviewCard data={data} texts={texts} />
      <LanguagePackHistoryCard dirty={dirty} metadata={data} state={draft.data!} />
    </PageBody>
  );
}

function PreviewCard({ data, texts }: { data: LanguagePackData; texts: LanguagePackTexts }) {
  const preview = usePreviewLanguagePack();
  const [text, setText] = useState("");
  const invalid = Object.keys(languagePackErrors(data.fields, texts)).length > 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FlaskConical size={16} className="text-accent-fg" aria-hidden />
          {t("settings.language.preview.title")}
        </CardTitle>
        <CardDescription>{t("settings.language.preview.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <TextareaField
          id="language-preview-text"
          label={t("settings.language.preview.text")}
          value={text}
          rows={4}
          width="full"
          onValueChange={setText}
        />
        <div>
          <Button
            icon={FlaskConical}
            loading={preview.isPending}
            disabled={!text.trim() || invalid}
            onClick={() =>
              preview.mutate({ text, overrides: overridesFromTexts(data.fields, texts, data.builtin) })
            }
          >
            {t("settings.language.preview.run")}
          </Button>
        </div>
        {preview.isError ? <FormStatus tone="danger" message={t("settings.language.preview.error")} /> : null}
        {preview.data ? (
          <div className="grid gap-4 md:grid-cols-2" data-testid="language-preview-result">
            <PreviewResult title={t("settings.language.preview.saved")} result={preview.data.saved} />
            <PreviewResult title={t("settings.language.preview.draft")} result={preview.data.draft} />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function PreviewResult({ title, result }: { title: string; result: LanguagePackPreviewResult }) {
  const references = result.references.map(
    (ref) => `${ref.label} → ${ref.kind}:${ref.key}${ref.document_title ? `（${ref.document_title}）` : ""}`,
  );
  const rows: [string, string[]][] = [
    [t("settings.language.preview.references"), references],
    [t("settings.language.preview.headingKeys"), result.heading_keys],
    [t("settings.language.preview.titleKeys"), result.title_keys],
  ];
  return (
    <section className="min-w-0 space-y-2 rounded-lg border border-border p-3" aria-label={title}>
      <h3 className="text-sm font-semibold text-fg">{title}</h3>
      <dl className="space-y-2 text-sm">
        {rows.map(([label, items]) => (
          <div key={label}>
            <dt className="text-xs text-fg-muted">{label}</dt>
            <dd className="break-words text-fg">
              {items.length ? items.join(" / ") : t("settings.language.preview.none")}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
