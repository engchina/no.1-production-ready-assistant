"use client";

import {
  Button,
  FieldError,
  FormGrid,
  ListSkeleton,
  SelectField,
  type SelectFieldOption,
  TextField,
  TimedLoadingState,
} from "@production-ready/ui";
import { Plus, Trash2 } from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import { type ExtractionFieldDefinition } from "@/lib/api";
import { t } from "@/lib/i18n";

import {
  type ExtractionFieldFilterError,
  type ExtractionFieldFilterRow,
  MAX_EXTRACTION_FIELD_FILTER_ROWS,
  newExtractionFieldFilterRow,
} from "./extraction-field-filters";

type FieldsState =
  | { status: "pending" }
  | { status: "error"; error: unknown; retry: () => void }
  | { status: "ready"; fields: ExtractionFieldDefinition[] };

/**
 * 検索の詳細条件の「抽出項目の値で絞り込む」（#549）。選んだ検索・回答プロファイルのナレッジベースで定義された
 * 項目を選び、型に合った入力（文字列・真偽値は一致、数値・日付は範囲）で条件を足す。
 */
export function ExtractionFieldFilters({
  fieldsState,
  rows,
  onRowsChange,
  errors,
  disabled,
}: {
  fieldsState: FieldsState;
  rows: ExtractionFieldFilterRow[];
  onRowsChange: (rows: ExtractionFieldFilterRow[]) => void;
  errors: Record<string, ExtractionFieldFilterError>;
  disabled: boolean;
}) {
  if (fieldsState.status === "pending") {
    return (
      <TimedLoadingState
        label={t("search.filters.fields.loading")}
        operationKey="search-extraction-fields"
        testId="search-extraction-fields-loading"
      >
        <ListSkeleton rows={2} rowClassName="h-9" />
      </TimedLoadingState>
    );
  }
  if (fieldsState.status === "error") {
    return (
      <ApiErrorState
        error={fieldsState.error}
        fallback={t("search.filters.fields.loadError")}
        onRetry={fieldsState.retry}
      />
    );
  }
  const { fields } = fieldsState;
  if (fields.length === 0 && rows.length === 0) {
    return <p className="text-sm text-fg-muted">{t("search.filters.fields.none")}</p>;
  }

  const update = (key: string, patch: Partial<ExtractionFieldFilterRow>) =>
    onRowsChange(rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  const add = () => {
    const row = newExtractionFieldFilterRow();
    onRowsChange([...rows, row]);
    requestAnimationFrame(() => document.getElementById(`${row.key}-name`)?.focus());
  };
  const atLimit = rows.length >= MAX_EXTRACTION_FIELD_FILTER_ROWS;

  return (
    <div className="space-y-3" data-testid="search-extraction-field-filters">
      {rows.length > 0 ? (
        // 条件の行はフォームの最大幅まで（削除の操作が欄から離れすぎない。#1891）。
        <FormGrid columns={1}>
          <ol className="space-y-2">
            {rows.map((row, index) => {
              const field = fields.find((item) => item.name === row.name);
              const error = errors[row.key];
              const options: SelectFieldOption<string>[] = [
                { value: "", label: t("search.filters.fields.choose") },
                ...fields.map((item) => ({
                  value: item.name,
                  label: `${item.name}（${t(`settings.extractionFields.valueType.${item.value_type}`)}）`,
                })),
                // 検索・回答プロファイルを変えて候補から消えた項目も、選んだまま見せて誤りを出す。
                ...(row.name && !field ? [{ value: row.name, label: row.name }] : []),
              ];
              const rowLabel = t("search.filters.fields.rowLabel", { index: index + 1 });
              return (
                <li
                  key={row.key}
                  aria-label={rowLabel}
                  className="grid min-w-0 grid-cols-1 gap-3 rounded-md border border-border bg-surface p-3 md:grid-cols-[minmax(0,20rem)_minmax(0,1fr)_auto] md:items-start"
                >
                  <SelectField
                    id={`${row.key}-name`}
                    width="md"
                    label={t("search.filters.fields.field")}
                    value={row.name}
                    options={options}
                    error={
                      error === "fieldMissing" || error === "fieldsUnavailable"
                        ? t(`search.filters.fields.error.${error}`)
                        : undefined
                    }
                    onValueChange={(name) => update(row.key, { name, value: "", min: "", max: "" })}
                    buttonClassName="bg-surface"
                  />
                  <ConditionInputs row={row} field={field} error={error} disabled={disabled} update={update} />
                  {/* 入力欄のラベルの高さの分だけ下げ、入力欄と同じ行にそろえる（md 以上）。 */}
                  <div className="flex justify-end md:pt-6">
                    <Button
                      type="button"
                      variant="ghost"
                      tone="danger"
                      iconOnly
                      icon={Trash2}
                      aria-label={t("search.filters.fields.remove", { index: index + 1 })}
                      disabled={disabled}
                      onClick={() => onRowsChange(rows.filter((item) => item.key !== row.key))}
                    />
                  </div>
                </li>
              );
            })}
          </ol>
        </FormGrid>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="secondary"
          icon={Plus}
          disabled={atLimit || disabled || fields.length === 0}
          onClick={add}
        >
          {t("search.filters.fields.add")}
        </Button>
        {atLimit ? (
          <span className="text-xs text-fg-muted">
            {t("search.filters.fields.limit", { max: MAX_EXTRACTION_FIELD_FILTER_ROWS })}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function ConditionInputs({
  row,
  field,
  error,
  disabled,
  update,
}: {
  row: ExtractionFieldFilterRow;
  field: ExtractionFieldDefinition | undefined;
  error: ExtractionFieldFilterError | undefined;
  disabled: boolean;
  update: (key: string, patch: Partial<ExtractionFieldFilterRow>) => void;
}) {
  if (!field) {
    return <p className="text-xs text-fg-muted md:pt-7">{t("search.filters.fields.chooseFirst")}</p>;
  }
  if (field.value_type === "string") {
    return (
      <TextField
        id={`${row.key}-value`}
        width="md"
        label={t("search.filters.fields.equals")}
        value={row.value}
        disabled={disabled}
        autoComplete="off"
        onValueChange={(value) => update(row.key, { value })}
      />
    );
  }
  if (field.value_type === "bool") {
    return (
      <SelectField
        id={`${row.key}-value`}
        width="sm"
        label={t("search.filters.fields.equals")}
        value={row.value}
        options={[
          { value: "", label: t("search.filters.fields.any") },
          { value: "true", label: t("search.filters.fields.true") },
          { value: "false", label: t("search.filters.fields.false") },
        ]}
        onValueChange={(value) => update(row.key, { value })}
        buttonClassName="bg-surface"
      />
    );
  }
  const isDate = field.value_type === "date";
  // 範囲の欄は日付なら日付の段（sm）、数値なら数値の段（xs。#1891）。
  const rangeWidth = isDate ? "sm" : "xs";
  const rangeError =
    error === "numberInvalid" || error === "rangeInverted"
      ? t(`search.filters.fields.error.${error}`)
      : undefined;
  return (
    <div className="space-y-1.5">
      {/* 下限・上限は値の段の幅で隣に並べる（sm 未満は縦に積む）。 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
        <TextField
          id={`${row.key}-min`}
          width={rangeWidth}
          type={isDate ? "date" : "text"}
          inputMode={isDate ? undefined : "decimal"}
          label={t(isDate ? "search.filters.fields.from" : "search.filters.fields.min")}
          value={row.min}
          disabled={disabled}
          autoComplete="off"
          aria-invalid={rangeError ? true : undefined}
          aria-describedby={rangeError ? `${row.key}-range-error` : undefined}
          onValueChange={(min) => update(row.key, { min })}
        />
        <TextField
          id={`${row.key}-max`}
          width={rangeWidth}
          type={isDate ? "date" : "text"}
          inputMode={isDate ? undefined : "decimal"}
          label={t(isDate ? "search.filters.fields.to" : "search.filters.fields.max")}
          value={row.max}
          disabled={disabled}
          autoComplete="off"
          aria-invalid={rangeError ? true : undefined}
          aria-describedby={rangeError ? `${row.key}-range-error` : undefined}
          onValueChange={(max) => update(row.key, { max })}
        />
      </div>
      {rangeError ? <FieldError id={`${row.key}-range-error`} message={rangeError} /> : null}
    </div>
  );
}
