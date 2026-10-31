"use client";

import { Disclosure, FormSkeleton, StatusBadge, TimedLoadingState } from "@production-ready/ui";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { LanguagePackFieldsPanel, useLanguagePackPanelState } from "@/components/settings/LanguagePackFieldsPanel";
import type { LanguagePackSections } from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  type LanguagePackTexts,
  languagePackErrors,
  overridesFromTexts,
  textsFromSections,
  textKey,
} from "@/lib/language-pack";
import { useLanguagePack } from "@/lib/queries";

/** 全体の値に上書きを重ねた「節 → 項目 → 値」。 */
function overlay(base: LanguagePackSections, overrides: LanguagePackSections): LanguagePackSections {
  const merged: LanguagePackSections = {};
  for (const [section, values] of Object.entries(base)) merged[section] = { ...values, ...overrides[section] };
  return merged;
}

/**
 * 検索・回答プロファイルの言語資源の上書き（#2181）。全体の言語資源の値に重ね、このプロファイルの検索・回答の
 * 間だけ使う。取込の項目は文書の分割と食い違うので出さない（検索・回答の項目だけ）。
 */
export function LanguagePackOverridesFieldset({
  value,
  disabled,
  onChange,
}: {
  value: LanguagePackSections;
  disabled: boolean;
  onChange: (overrides: LanguagePackSections) => void;
}) {
  const query = useLanguagePack();
  const panel = useLanguagePackPanelState();
  const valueKey = JSON.stringify(value);
  const [draft, setDraft] = useState<{ key: string; texts: LanguagePackTexts }>({ key: valueKey, texts: {} });
  const data = query.data;
  const fields = data ? data.fields.filter((field) => !field.ingest && !["question_cues", "inquiry_cues", "enumeration_cues", "definition_cues"].includes(field.section)) : [];
  // 継承値は最新の全体値から描画し、入力途中の文字だけを保持する。保存・破棄では入力も同期する。
  if (draft.key !== valueKey) setDraft({ key: valueKey, texts: {} });
  const baseline = data ? textsFromSections(fields, overlay(data.effective, value)) : {};
  const texts = { ...baseline, ...(draft.key === valueKey ? draft.texts : {}) };
  const count = fields.filter((field) => field.key in (value[field.section] ?? {})).length;

  return (
    <Disclosure
      summary={t("searchAnswerProfiles.languagePack.title")}
      defaultOpen={false}
      contentClassName="space-y-3 border-t border-border p-4"
      data-testid="search-answer-profile-language-pack"
    >
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.languagePack.helper")}</p>
        {count > 0 ? (
          <StatusBadge
            variant="info"
            icon={false}
            label={t("searchAnswerProfiles.languagePack.count", { count })}
          />
        ) : null}
      </div>
      {query.isPending ? (
        <TimedLoadingState
          label={t("settings.language.loading")}
          operationKey="search-answer-profile-language-pack"
          testId="search-answer-profile-language-pack-loading"
        >
          <FormSkeleton fields={2} />
        </TimedLoadingState>
      ) : query.isError || !data ? (
        <ApiErrorState
          error={query.error}
          fallback={t("settings.language.loadError")}
          onRetry={() => void query.refetch()}
        />
      ) : (
        <LanguagePackFieldsPanel
          idPrefix="profile-language"
          fields={fields}
          baseline={data.effective}
          texts={texts}
          errors={languagePackErrors(fields, texts)}
          disabled={disabled}
          state={panel}
          changedLabel={t("searchAnswerProfiles.languagePack.changed")}
          resetLabel={t("searchAnswerProfiles.languagePack.reset")}
          onChange={(key, text) => {
            const field = fields.find((item) => textKey(item) === key);
            if (!field) return;
            const changed = overridesFromTexts([field], { [key]: text }, data.effective);
            const section = { ...value[field.section] };
            if (changed[field.section]) section[field.key] = changed[field.section][field.key];
            else delete section[field.key];
            const overrides = { ...value, [field.section]: section };
            if (!Object.keys(section).length) delete overrides[field.section];
            const nextTexts = { ...draft.texts, [key]: text };
            if (!changed[field.section]) delete nextTexts[key];
            setDraft({ key: JSON.stringify(overrides), texts: nextTexts });
            onChange(overrides);
          }}
        />
      )}
    </Disclosure>
  );
}
