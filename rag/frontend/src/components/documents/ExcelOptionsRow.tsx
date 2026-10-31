"use client";

import { useState } from "react";

import { FormGrid, SelectField, TextField, ToggleChip, type SelectFieldOption } from "@production-ready/ui";
import type { ExcelColumnRoleSetting, ExcelOptions } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 前処理 excel_to_jsonl の選択肢の既定（backend の `ExcelOptions` と同じ）。 */
export const DEFAULT_EXCEL_OPTIONS: ExcelOptions = {
  mode: "auto",
  header_row: null,
  header_row_count: 1,
  sheets: [],
  exclude_sheets: [],
  include_hidden_sheets: false,
  include_hidden_cells: false,
  exclude_columns: [],
  ranges: [],
  split_tables: true,
  table_gap_rows: 1,
  table_gap_columns: 1,
  form_detection_threshold: 0.5,
  column_role_detection: "auto",
  column_roles: {},
};

/**
 * 列の役割の指定の書き方で使う役割の名前（#1281）。入力の語なので i18n ではなく、backend の役割の値と
 * 1 対 1 に対応させる（英語の値もそのまま受け付ける）。
 */
const COLUMN_ROLE_NAMES: Record<ExcelColumnRoleSetting, string> = {
  definition: "説明",
  default: "既定値",
  example: "例示",
  current: "現在値",
  recommended: "推奨値",
  allowed: "範囲",
  none: "なし",
};

const COLUMN_ROLE_BY_NAME = new Map<string, ExcelColumnRoleSetting>(
  (Object.entries(COLUMN_ROLE_NAMES) as [ExcelColumnRoleSetting, string][]).flatMap(
    ([role, name]): [string, ExcelColumnRoleSetting][] => [
      [name, role],
      [role, role],
    ],
  ),
);

/** 「D=例示、設定値=現在値」を役割の指定にする（読めない項目は invalid に返す）。 */
export function parseColumnRoles(text: string): {
  roles: Record<string, ExcelColumnRoleSetting>;
  invalid: string[];
} {
  const roles: Record<string, ExcelColumnRoleSetting> = {};
  const invalid: string[] = [];
  for (const item of parseNameList(text)) {
    const [key, name, ...rest] = item.split(/[=＝]/).map((part) => part.trim());
    const role = name ? COLUMN_ROLE_BY_NAME.get(name.toLowerCase()) : undefined;
    if (!key || !role || rest.length) {
      invalid.push(item);
      continue;
    }
    roles[key] = role;
  }
  return { roles, invalid };
}

/** 役割の指定を入力欄の文字列にする。 */
export function formatColumnRoles(roles: Record<string, ExcelColumnRoleSetting> | undefined): string {
  return Object.entries(roles ?? {})
    .map(([key, role]) => `${key}=${COLUMN_ROLE_NAMES[role] ?? role}`)
    .join("、");
}

const MODE_OPTIONS: SelectFieldOption<ExcelOptions["mode"]>[] = [
  { value: "auto", label: t("documents.excelOptions.mode.auto") },
  { value: "table", label: t("documents.excelOptions.mode.table") },
  { value: "procedure", label: t("documents.excelOptions.mode.procedure") },
  { value: "form", label: t("documents.excelOptions.mode.form") },
];

const HEADER_ROW_COUNT_OPTIONS: SelectFieldOption<string>[] = [1, 2, 3, 4, 5].map((count) => ({
  value: String(count),
  label: t("documents.excelOptions.headerRowCountValue", { count }),
}));

/** 表を分ける空の行・列の数（backend の 1〜20）。 */
const TABLE_GAP_COUNTS = Array.from({ length: 20 }, (_, index) => index + 1);

/** 帳票と判定する下限の入力を数にする（0〜1 の外・数でない値は null）。 */
export function parseFormThreshold(text: string): number | null {
  if (!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

/** 「、」「,」・改行で区切った名前の一覧（空は除く）。 */
export function parseNameList(text: string): string[] {
  return text
    .split(/[,，、\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 選択肢の要約（継承の値の表示）。 */
export function excelOptionsSummary(options: ExcelOptions | null | undefined): string {
  const value = { ...DEFAULT_EXCEL_OPTIONS, ...options };
  const parts = [
    MODE_OPTIONS.find((option) => option.value === value.mode)?.label ?? value.mode,
    value.header_row
      ? t("documents.excelOptions.summary.headerRow", { row: value.header_row })
      : t("documents.excelOptions.summary.headerDetected"),
  ];
  if (value.sheets.length) parts.push(t("documents.excelOptions.summary.sheets", { names: value.sheets.join("、") }));
  if (value.exclude_columns.length) {
    parts.push(t("documents.excelOptions.summary.excludeColumns", { names: value.exclude_columns.join("、") }));
  }
  if (value.ranges?.length) {
    parts.push(t("documents.excelOptions.summary.ranges", { ranges: value.ranges.join("、") }));
  }
  if (!value.split_tables) {
    parts.push(t("documents.excelOptions.summary.tablesWhole"));
  } else if (
    value.table_gap_rows !== DEFAULT_EXCEL_OPTIONS.table_gap_rows ||
    value.table_gap_columns !== DEFAULT_EXCEL_OPTIONS.table_gap_columns
  ) {
    parts.push(
      t("documents.excelOptions.summary.tableGap", { rows: value.table_gap_rows, columns: value.table_gap_columns }),
    );
  }
  if (value.mode === "auto" && value.form_detection_threshold !== DEFAULT_EXCEL_OPTIONS.form_detection_threshold) {
    parts.push(t("documents.excelOptions.summary.formThreshold", { value: value.form_detection_threshold }));
  }
  if (value.include_hidden_cells) parts.push(t("documents.excelOptions.summary.hiddenCells"));
  if (value.column_role_detection === "off") parts.push(t("documents.excelOptions.summary.columnRolesOff"));
  if (Object.keys(value.column_roles ?? {}).length) {
    parts.push(t("documents.excelOptions.summary.columnRoles", { roles: formatColumnRoles(value.column_roles) }));
  }
  return parts.join(" · ");
}

/**
 * 文書レシピの「Excel の読み方」（前処理が excel_to_jsonl のときだけ出す。#1221）。
 * 継承（全体の既定に従う）か上書きかを選び、上書きのときに読み方・表頭・シート・列を指定する。
 */
export function ExcelOptionsRow({
  documentId,
  value,
  effectiveValue,
  disabled,
  onChange,
}: {
  documentId: string;
  value: ExcelOptions | null;
  effectiveValue: ExcelOptions | null;
  disabled: boolean;
  onChange: (value: ExcelOptions | null) => void;
}) {
  const overriding = value !== null;
  // 既定で埋める（項目が足りない値も、backend と同じ既定で表示・保存する）。
  const current: ExcelOptions = { ...DEFAULT_EXCEL_OPTIONS, ...(value ?? effectiveValue) };
  // 名前の一覧は入力の途中（末尾の「、」など）を保つため、文字列のまま持つ。
  const [lists, setLists] = useState(() => ({
    sheets: current.sheets.join("、"),
    exclude_sheets: current.exclude_sheets.join("、"),
    exclude_columns: current.exclude_columns.join("、"),
    ranges: (current.ranges ?? []).join("、"),
  }));
  const [columnRolesText, setColumnRolesText] = useState(() => formatColumnRoles(current.column_roles));
  const columnRolesInvalid = parseColumnRoles(columnRolesText).invalid;
  const [headerRowText, setHeaderRowText] = useState(current.header_row ? String(current.header_row) : "");
  const [formThresholdText, setFormThresholdText] = useState(String(current.form_detection_threshold));
  const formThresholdInvalid = parseFormThreshold(formThresholdText) === null;
  const labelId = `document-excel-options-${documentId}`;
  const tableGapRowsOptions: SelectFieldOption<string>[] = TABLE_GAP_COUNTS.map((count) => ({
    value: String(count),
    label: t("documents.excelOptions.tableGapRowsValue", { count }),
  }));
  const tableGapColumnsOptions: SelectFieldOption<string>[] = TABLE_GAP_COUNTS.map((count) => ({
    value: String(count),
    label: t("documents.excelOptions.tableGapColumnsValue", { count }),
  }));
  const update = (patch: Partial<ExcelOptions>) => onChange({ ...current, ...patch });
  const updateList = (key: "sheets" | "exclude_sheets" | "exclude_columns" | "ranges", text: string) => {
    setLists((previous) => ({ ...previous, [key]: text }));
    update({ [key]: parseNameList(text) });
  };

  return (
    <div
      data-config-field="excel_options"
      data-testid="document-excel-options"
      className="space-y-2 rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span id={labelId} className="text-sm font-medium text-fg">
          {t("documents.excelOptions.title")}
        </span>
        <div className="flex flex-wrap gap-1" role="group" aria-labelledby={labelId}>
          <ToggleChip selected={!overriding} disabled={disabled} onClick={() => onChange(null)}>
            {t("knowledgeBases.adapter.inherit")}
          </ToggleChip>
          <ToggleChip
            selected={overriding}
            disabled={disabled}
            onClick={() => !overriding && onChange({ ...current })}
          >
            {t("knowledgeBases.adapter.override")}
          </ToggleChip>
        </div>
      </div>
      {overriding ? (
        // 読み方は 1 行、表頭の 2 つの数・読む / 読まないシート（シート名の並び。md）・表を分ける空の行と列・
        // 非表示のシートと行・列は同じ行、長い値は全幅（#1891）。
        <FormGrid>
          <SelectField
            id={`${labelId}-mode`}
            className="col-span-full"
            width="md"
            label={t("documents.excelOptions.mode")}
            helper={t("documents.excelOptions.mode.hint")}
            value={current.mode}
            options={MODE_OPTIONS}
            disabled={disabled}
            onValueChange={(mode) => update({ mode })}
          />
          {current.mode === "auto" ? (
            // 帳票の判定の下限は自動の読み方だけで使う（#2516）。
            <TextField
              id={`${labelId}-form-threshold`}
              className="col-span-full"
              width="xs"
              label={t("documents.excelOptions.formThreshold")}
              helper={t("documents.excelOptions.formThreshold.hint")}
              error={formThresholdInvalid ? t("documents.excelOptions.formThreshold.invalid") : undefined}
              type="number"
              inputMode="decimal"
              min={0}
              max={1}
              step={0.05}
              value={formThresholdText}
              disabled={disabled}
              onChange={(event) => {
                const text = event.target.value;
                setFormThresholdText(text);
                // 範囲の外の値は保存しない（欄の下に示す）。
                const threshold = parseFormThreshold(text);
                if (threshold !== null) update({ form_detection_threshold: threshold });
              }}
            />
          ) : null}
          <TextField
            id={`${labelId}-header-row`}
            width="xs"
            label={t("documents.excelOptions.headerRow")}
            helper={t("documents.excelOptions.headerRow.hint")}
            type="number"
            inputMode="numeric"
            min={1}
            max={1000}
            value={headerRowText}
            disabled={disabled}
            onChange={(event) => {
              const text = event.target.value;
              setHeaderRowText(text);
              const row = Number.parseInt(text, 10);
              update({ header_row: Number.isFinite(row) && row >= 1 && row <= 1000 ? row : null });
            }}
          />
          <SelectField
            id={`${labelId}-header-row-count`}
            width="xs"
            label={t("documents.excelOptions.headerRowCount")}
            value={String(current.header_row_count)}
            options={HEADER_ROW_COUNT_OPTIONS}
            disabled={disabled}
            onValueChange={(count) => update({ header_row_count: Number(count) })}
          />
          <TextField
            id={`${labelId}-sheets`}
            width="md"
            label={t("documents.excelOptions.sheets")}
            helper={t("documents.excelOptions.namesHint")}
            value={lists.sheets}
            disabled={disabled}
            onChange={(event) => updateList("sheets", event.target.value)}
          />
          <TextField
            id={`${labelId}-exclude-sheets`}
            width="md"
            label={t("documents.excelOptions.excludeSheets")}
            helper={t("documents.excelOptions.namesHint")}
            value={lists.exclude_sheets}
            disabled={disabled}
            onChange={(event) => updateList("exclude_sheets", event.target.value)}
          />
          <TextField
            id={`${labelId}-exclude-columns`}
            className="col-span-full"
            width="full"
            label={t("documents.excelOptions.excludeColumns")}
            helper={t("documents.excelOptions.excludeColumns.hint")}
            value={lists.exclude_columns}
            disabled={disabled}
            onChange={(event) => updateList("exclude_columns", event.target.value)}
          />
          <div className="col-span-full">
            <TextField
              id={`${labelId}-ranges`}
              width="full"
              label={t("documents.excelOptions.ranges")}
              helper={t("documents.excelOptions.ranges.hint")}
              value={lists.ranges}
              disabled={disabled}
              spellCheck={false}
              onChange={(event) => updateList("ranges", event.target.value)}
            />
          </div>
          <div className="space-y-1 col-span-full">
            <span id={`${labelId}-split-tables`} className="text-sm font-medium text-fg">
              {t("documents.excelOptions.splitTables")}
            </span>
            <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`${labelId}-split-tables`}>
              <ToggleChip
                selected={current.split_tables}
                disabled={disabled}
                onClick={() => update({ split_tables: true })}
              >
                {t("documents.excelOptions.splitTables.split")}
              </ToggleChip>
              <ToggleChip
                selected={!current.split_tables}
                disabled={disabled}
                onClick={() => update({ split_tables: false })}
              >
                {t("documents.excelOptions.splitTables.whole")}
              </ToggleChip>
            </div>
            <p className="text-xs text-fg-muted">{t("documents.excelOptions.splitTables.hint")}</p>
          </div>
          {current.split_tables ? (
            <>
              <SelectField
                id={`${labelId}-table-gap-rows`}
                width="xs"
                label={t("documents.excelOptions.tableGapRows")}
                value={String(current.table_gap_rows)}
                options={tableGapRowsOptions}
                disabled={disabled}
                onValueChange={(count) => update({ table_gap_rows: Number(count) })}
              />
              <SelectField
                id={`${labelId}-table-gap-columns`}
                width="xs"
                label={t("documents.excelOptions.tableGapColumns")}
                value={String(current.table_gap_columns)}
                options={tableGapColumnsOptions}
                disabled={disabled}
                onValueChange={(count) => update({ table_gap_columns: Number(count) })}
              />
            </>
          ) : null}
          <div className="space-y-1 col-span-full">
            <span id={`${labelId}-role-detection`} className="text-sm font-medium text-fg">
              {t("documents.excelOptions.columnRoleDetection")}
            </span>
            <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`${labelId}-role-detection`}>
              <ToggleChip
                selected={(current.column_role_detection ?? "auto") === "auto"}
                disabled={disabled}
                onClick={() => update({ column_role_detection: "auto" })}
              >
                {t("documents.excelOptions.columnRoleDetection.auto")}
              </ToggleChip>
              <ToggleChip
                selected={current.column_role_detection === "off"}
                disabled={disabled}
                onClick={() => update({ column_role_detection: "off" })}
              >
                {t("documents.excelOptions.columnRoleDetection.off")}
              </ToggleChip>
            </div>
            <p className="text-xs text-fg-muted">{t("documents.excelOptions.columnRoleDetection.hint")}</p>
          </div>
          <div className="col-span-full">
            <TextField
              id={`${labelId}-column-roles`}
              width="full"
              label={t("documents.excelOptions.columnRoles")}
              helper={t("documents.excelOptions.columnRoles.hint")}
              error={
                columnRolesInvalid.length
                  ? t("documents.excelOptions.columnRoles.invalid", { items: columnRolesInvalid.join("、") })
                  : undefined
              }
              value={columnRolesText}
              disabled={disabled}
              spellCheck={false}
              onChange={(event) => {
                const text = event.target.value;
                setColumnRolesText(text);
                // 読めた項目だけを保存する（読めない項目は欄の下に示す）。
                update({ column_roles: parseColumnRoles(text).roles });
              }}
            />
          </div>
          <div className="space-y-1">
            <span id={`${labelId}-hidden`} className="text-sm font-medium text-fg">
              {t("documents.excelOptions.hiddenSheets")}
            </span>
            <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`${labelId}-hidden`}>
              <ToggleChip
                selected={!current.include_hidden_sheets}
                disabled={disabled}
                onClick={() => update({ include_hidden_sheets: false })}
              >
                {t("documents.excelOptions.hiddenSheets.skip")}
              </ToggleChip>
              <ToggleChip
                selected={current.include_hidden_sheets}
                disabled={disabled}
                onClick={() => update({ include_hidden_sheets: true })}
              >
                {t("documents.excelOptions.hiddenSheets.read")}
              </ToggleChip>
            </div>
          </div>
          <div className="space-y-1">
            <span id={`${labelId}-hidden-cells`} className="text-sm font-medium text-fg">
              {t("documents.excelOptions.hiddenCells")}
            </span>
            <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`${labelId}-hidden-cells`}>
              <ToggleChip
                selected={!current.include_hidden_cells}
                disabled={disabled}
                onClick={() => update({ include_hidden_cells: false })}
              >
                {t("documents.excelOptions.hiddenSheets.skip")}
              </ToggleChip>
              <ToggleChip
                selected={current.include_hidden_cells}
                disabled={disabled}
                onClick={() => update({ include_hidden_cells: true })}
              >
                {t("documents.excelOptions.hiddenSheets.read")}
              </ToggleChip>
            </div>
            <p className="text-xs text-fg-muted">{t("documents.excelOptions.hiddenCells.hint")}</p>
          </div>
        </FormGrid>
      ) : (
        <p className="text-xs text-fg-muted" data-testid="document-excel-options-inherited">
          {t("documents.excelOptions.inherited", { summary: excelOptionsSummary(effectiveValue) })}
        </p>
      )}
    </div>
  );
}
