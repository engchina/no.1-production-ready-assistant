"use client";

import { useState } from "react";

import { FormGrid, TextField, ToggleChip } from "@production-ready/ui";
import { t } from "@/lib/i18n";

import { parseNameList } from "./ExcelOptionsRow";

/** 列名の数の上限（backend の `entity_name_columns` / `entity_attribute_columns` の検証と同じ）。 */
export const ENTITY_NAME_COLUMNS_MAX = 20;
export const ENTITY_ATTRIBUTE_COLUMNS_MAX = 40;
/** 1 つの列名の長さの上限（backend の `EntityColumnName`）。 */
export const ENTITY_COLUMN_NAME_MAX_CHARS = 80;

export interface EntityColumnsValue {
  entity_name_columns: string[] | null;
  entity_attribute_columns: string[] | null;
}

/** 列名の入力の読めない所（数・長さの上限を超えた）。保存は backend の 422 で止まるため、欄の下に示す。 */
export function entityColumnsError(names: string[], max: number): string | undefined {
  if (names.length > max) return t("documents.entityIndex.columns.tooMany", { max });
  const long = names.filter((name) => name.length > ENTITY_COLUMN_NAME_MAX_CHARS);
  if (long.length) {
    return t("documents.entityIndex.columns.tooLong", { max: ENTITY_COLUMN_NAME_MAX_CHARS });
  }
  return undefined;
}

/** 継承の値の表示（「名前の列: ID、正式名 · 属性の列: 列名で決める」）。 */
export function entityColumnsSummary(value: EntityColumnsValue | null | undefined): string {
  const names = value?.entity_name_columns ?? [];
  const attributes = value?.entity_attribute_columns ?? [];
  const auto = t("documents.entityIndex.columns.auto");
  return [
    t("documents.entityIndex.columns.summary.names", { names: names.length ? names.join("、") : auto }),
    t("documents.entityIndex.columns.summary.attributes", {
      names: attributes.length ? attributes.join("、") : auto,
    }),
  ].join(" · ");
}

/**
 * 文書レシピの「実体の索引の列」（実体の索引が有効なときだけ出す。#1388）。
 *
 * 表の行のどの列を実体の名前（同じ実体の別名）にし、どの列の値を属性の実体にするかを選ぶ。空欄は
 * 列名で決める（ID・正式名・略称などは名前、担当部署・重要度などは属性）。列は文書の表ごとに違い、
 * 取込の前には分からないため、列名を「、」で区切って入力する。
 */
export function EntityIndexColumnsRow({
  documentId,
  value,
  effectiveValue,
  disabled,
  onChange,
}: {
  documentId: string;
  value: EntityColumnsValue;
  effectiveValue: EntityColumnsValue;
  disabled: boolean;
  onChange: (value: EntityColumnsValue) => void;
}) {
  const overriding = value.entity_name_columns !== null || value.entity_attribute_columns !== null;
  const current: EntityColumnsValue = {
    entity_name_columns: value.entity_name_columns ?? effectiveValue.entity_name_columns ?? [],
    entity_attribute_columns:
      value.entity_attribute_columns ?? effectiveValue.entity_attribute_columns ?? [],
  };
  // 入力の途中（末尾の「、」など）を保つため、文字列のまま持つ。
  const [text, setText] = useState(() => ({
    names: (current.entity_name_columns ?? []).join("、"),
    attributes: (current.entity_attribute_columns ?? []).join("、"),
  }));
  const names = parseNameList(text.names);
  const attributes = parseNameList(text.attributes);
  const labelId = `document-entity-columns-${documentId}`;
  const startOverride = () => {
    setText({
      names: (current.entity_name_columns ?? []).join("、"),
      attributes: (current.entity_attribute_columns ?? []).join("、"),
    });
    onChange({
      entity_name_columns: [...(current.entity_name_columns ?? [])],
      entity_attribute_columns: [...(current.entity_attribute_columns ?? [])],
    });
  };

  return (
    <div
      data-config-field="entity_columns"
      data-testid="document-entity-columns"
      className="space-y-2 rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span id={labelId} className="text-sm font-medium text-fg">
          {t("documents.entityIndex.columns.title")}
        </span>
        <div className="flex flex-wrap gap-1" role="group" aria-labelledby={labelId}>
          <ToggleChip
            selected={!overriding}
            disabled={disabled}
            onClick={() => onChange({ entity_name_columns: null, entity_attribute_columns: null })}
          >
            {t("knowledgeBases.adapter.inherit")}
          </ToggleChip>
          <ToggleChip selected={overriding} disabled={disabled} onClick={() => !overriding && startOverride()}>
            {t("knowledgeBases.adapter.override")}
          </ToggleChip>
        </div>
      </div>
      {overriding ? (
        // 名前の列と属性の列は意味のペアなので同じ行に置く。列名の並び（名前の段 md。2 列の置き場所のセルに入る幅。#1891）。
        <FormGrid>
          <TextField
            id={`${labelId}-names`}
            width="md"
            label={t("documents.entityIndex.columns.names")}
            helper={t("documents.entityIndex.columns.names.hint")}
            error={entityColumnsError(names, ENTITY_NAME_COLUMNS_MAX)}
            value={text.names}
            disabled={disabled}
            spellCheck={false}
            onChange={(event) => {
              const next = event.target.value;
              setText((previous) => ({ ...previous, names: next }));
              onChange({ ...current, entity_name_columns: parseNameList(next) });
            }}
          />
          <TextField
            id={`${labelId}-attributes`}
            width="md"
            label={t("documents.entityIndex.columns.attributes")}
            helper={t("documents.entityIndex.columns.attributes.hint")}
            error={entityColumnsError(attributes, ENTITY_ATTRIBUTE_COLUMNS_MAX)}
            value={text.attributes}
            disabled={disabled}
            spellCheck={false}
            onChange={(event) => {
              const next = event.target.value;
              setText((previous) => ({ ...previous, attributes: next }));
              onChange({ ...current, entity_attribute_columns: parseNameList(next) });
            }}
          />
        </FormGrid>
      ) : (
        <p className="text-xs text-fg-muted" data-testid="document-entity-columns-inherited">
          {t("knowledgeBases.adapter.inheritResolved", { value: entityColumnsSummary(effectiveValue) })}
        </p>
      )}
    </div>
  );
}
