"use client";

import {
  Button,
  FormGrid,
  SearchField,
  SelectField,
  type SelectFieldOption,
  StatusBadge,
  Tabs,
  TextareaField,
  TextField,
} from "@production-ready/ui";
import { RotateCcw } from "lucide-react";
import { useState } from "react";

import type { LanguagePackField, LanguagePackSections } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import {
  type LanguagePackTexts,
  groupSections,
  isChanged,
  matchesFieldQuery,
  textKey,
} from "@/lib/language-pack";

/** 語の数が多い項目は欄を高くする。 */
const LONG_FIELD_ITEMS = 8;

/** 言語資源の欄の並び（使い道の組・節・項目の検索）の状態。持ち主（保存の誤りの節を開く）と共有する。 */
export function useLanguagePackPanelState() {
  const [group, setGroup] = useState<string | null>(null);
  const [section, setSection] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  /** 指定の節を開く（検索を解き、その節の組を開く）。 */
  const reveal = (name: string) => {
    setQuery("");
    setGroup(null);
    setSection(name);
  };
  return { group, setGroup, section, setSection, query, setQuery, reveal };
}

export type LanguagePackPanelState = ReturnType<typeof useLanguagePackPanelState>;

export function languagePackFieldId(idPrefix: string, field: Pick<LanguagePackField, "section" | "key">) {
  return `${idPrefix}-${field.section}-${field.key}`.replaceAll("_", "-");
}

/**
 * 言語資源の項目の欄（#2181）。上の段は使い道の組、下の段はその組の節、検索中はすべての節から当たった項目。
 * ``baseline`` は「〜に戻す」の値と、変えたかどうかを比べる値（全体の画面は組み込み、プロファイルは全体の値）。
 */
export function LanguagePackFieldsPanel({
  idPrefix,
  fields: allFields,
  baseline,
  texts,
  errors,
  disabled,
  state,
  changedLabel,
  resetLabel,
  onChange,
}: {
  idPrefix: string;
  fields: LanguagePackField[];
  baseline: LanguagePackSections;
  texts: LanguagePackTexts;
  errors: Record<string, string>;
  disabled: boolean;
  state: LanguagePackPanelState;
  changedLabel: string;
  resetLabel: string;
  onChange: (key: string, value: string) => void;
}) {
  const sections = [...new Set(allFields.map((field) => field.section))];
  const groups = groupSections(sections);
  const currentGroup =
    groups.find((item) => item.id === state.group) ??
    groups.find((item) => item.sections.includes(state.section ?? "")) ??
    groups[0];
  if (!currentGroup) return null;
  const current =
    state.section && currentGroup.sections.includes(state.section) ? state.section : currentGroup.sections[0];
  const searching = state.query.trim().length > 0;
  const fields = searching
    ? allFields.filter((field) => matchesFieldQuery(field, state.query))
    : allFields.filter((field) => field.section === current);
  const renderField = (field: LanguagePackField) => (
    <LanguageField
      key={textKey(field)}
      id={languagePackFieldId(idPrefix, field)}
      field={field}
      baseline={baseline}
      texts={texts}
      error={errors[textKey(field)]}
      disabled={disabled}
      changedLabel={changedLabel}
      resetLabel={resetLabel}
      onChange={(value) => onChange(textKey(field), value)}
    />
  );
  return (
    <div className="space-y-4">
      <SearchField
        id={`${idPrefix}-field-search`}
        label={t("settings.language.search.label")}
        labelHidden
        value={state.query}
        width="lg"
        onSearch={state.setQuery}
        clearLabel={t("common.clearSearch")}
        resultCountLabel={t("settings.language.search.count", { count: fields.length })}
        placeholder={t("settings.language.search.placeholder")}
      />
      {searching ? (
        fields.length === 0 ? (
          <p className="text-sm text-fg-muted">{t("settings.language.search.empty")}</p>
        ) : (
          <div className="space-y-6" data-testid={`${idPrefix}-search-results`}>
            {[...new Set(fields.map((field) => field.section))].map((name) => (
              <section key={name} aria-labelledby={`${idPrefix}-search-${name}`} className="space-y-3">
                <h3 id={`${idPrefix}-search-${name}`} className="text-sm font-semibold text-fg">
                  {t(`settings.language.section.${name}` as I18nKey)}
                </h3>
                <FormGrid>{fields.filter((field) => field.section === name).map(renderField)}</FormGrid>
              </section>
            ))}
          </div>
        )
      ) : (
        <>
          {groups.length > 1 ? (
            <Tabs
              idPrefix={`${idPrefix}-group`}
              ariaLabel={t("settings.language.groups.label")}
              value={currentGroup.id}
              onChange={(value) => {
                state.setGroup(value);
                state.setSection(null);
              }}
              items={groups.map((item) => ({
                id: item.id,
                label: t(`settings.language.group.${item.id}` as I18nKey),
              }))}
            />
          ) : null}
          <Tabs
            idPrefix={idPrefix}
            ariaLabel={t("nav.settingsLanguagePack")}
            value={current}
            onChange={(value) => state.setSection(value)}
            items={currentGroup.sections.map((name) => ({
              id: name,
              label: t(`settings.language.section.${name}` as I18nKey),
            }))}
          />
          <div id={`${idPrefix}-panel-${current}`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-${current}`}>
            <p className="mb-4 text-xs text-fg-muted">{t(`settings.language.section.${current}.help` as I18nKey)}</p>
            <FormGrid>{fields.map(renderField)}</FormGrid>
          </div>
        </>
      )}
    </div>
  );
}

function LanguageField({
  id,
  field,
  baseline,
  texts,
  error,
  disabled,
  changedLabel,
  resetLabel,
  onChange,
}: {
  id: string;
  field: LanguagePackField;
  baseline: LanguagePackSections;
  texts: LanguagePackTexts;
  error: string | undefined;
  disabled: boolean;
  changedLabel: string;
  resetLabel: string;
  onChange: (value: string) => void;
}) {
  const key = textKey(field);
  const label = t(`settings.language.field.${field.key}` as I18nKey);
  const helper = t(`settings.language.field.${field.key}.help` as I18nKey);
  const changed = isChanged(field, texts, baseline);
  const base = baseline[field.section]?.[field.key];
  const resetRow = changed ? (
    <div className="flex flex-wrap items-center gap-2">
      <StatusBadge variant="info" icon={false} label={changedLabel} />
      <Button
        size="sm"
        variant="ghost"
        icon={RotateCcw}
        disabled={disabled}
        onClick={() => onChange(Array.isArray(base) ? base.join("\n") : (base ?? ""))}
      >
        {resetLabel}
      </Button>
    </div>
  ) : null;
  if (field.kind === "choice") {
    const options: SelectFieldOption<string>[] = field.choices.map((value) => ({
      value,
      label: t(`settings.language.choice.${value}` as I18nKey),
    }));
    return (
      <div className="space-y-2">
        <SelectField
          id={id}
          label={label}
          helper={helper}
          value={texts[key] ?? ""}
          options={options}
          width="lg"
          disabled={disabled}
          error={error}
          onValueChange={onChange}
        />
        {resetRow}
      </div>
    );
  }
  if (field.kind === "text") {
    return (
      <div className="space-y-2">
        <TextField
          id={id}
          label={label}
          helper={helper}
          error={error}
          value={texts[key] ?? ""}
          // 文の項目（回答の定型文）は長いので欄を広くする。
          width={field.max_chars > 40 ? "full" : "md"}
          disabled={disabled}
          onValueChange={onChange}
        />
        {resetRow}
      </div>
    );
  }
  const lines = (texts[key] ?? "").split("\n").length;
  return (
    <div className="space-y-2">
      <TextareaField
        id={id}
        label={label}
        helper={helper}
        error={error}
        value={texts[key] ?? ""}
        rows={lines > LONG_FIELD_ITEMS ? 6 : 3}
        width="md"
        disabled={disabled}
        onValueChange={onChange}
      />
      {resetRow}
    </div>
  );
}
