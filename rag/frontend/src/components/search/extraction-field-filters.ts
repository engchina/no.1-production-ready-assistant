import type { ExtractionFieldDefinition, ExtractionFieldValueType } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 検索の「抽出項目の値で絞り込む」（#549）。backend の `filters.extraction_fields`
 * （`app/schemas/search.py` の `ExtractionFieldCondition`）へ渡す条件を作る。
 */

/** 1 行で最大 2 条件（以上・以下）になるため、backend の上限（10 条件）に収まる行数にする。 */
export const MAX_EXTRACTION_FIELD_FILTER_ROWS = 5;
export const EXTRACTION_FIELD_FILTER_KEY = "extraction_fields";

export type ExtractionFieldOperator = "eq" | "gte" | "lte";

export interface ExtractionFieldCondition {
  name: string;
  value_type: ExtractionFieldValueType;
  op: ExtractionFieldOperator;
  value: string;
}

/** 編集中の 1 行。string / bool は `value`、number / date は `min`（以上）と `max`（以下）を使う。 */
export interface ExtractionFieldFilterRow {
  key: string;
  name: string;
  value: string;
  min: string;
  max: string;
}

export type ExtractionFieldFilterError =
  | "fieldMissing"
  | "fieldsUnavailable"
  | "numberInvalid"
  | "rangeInverted";

let rowSequence = 0;

export function newExtractionFieldFilterRow(): ExtractionFieldFilterRow {
  rowSequence += 1;
  // 作業状態として保存し再読込後も残るため、読み込み直しても重ならない key にする。
  return { key: `efr-${Date.now().toString(36)}-${rowSequence}`, name: "", value: "", min: "", max: "" };
}

export function isExtractionFieldFilterRows(value: unknown): value is ExtractionFieldFilterRow[] {
  return (
    Array.isArray(value) &&
    value.every(
      (row) =>
        typeof row === "object" &&
        row !== null &&
        ["key", "name", "value", "min", "max"].every(
          (field) => typeof (row as Record<string, unknown>)[field] === "string"
        )
    )
  );
}

/** 値が入っている行（項目を選び、値か範囲のどちらかがある）。 */
export function isActiveExtractionFieldFilterRow(row: ExtractionFieldFilterRow): boolean {
  return Boolean(row.name && (row.value.trim() || row.min.trim() || row.max.trim()));
}

function isNumber(value: string): boolean {
  return value.trim() !== "" && Number.isFinite(Number(value.trim()));
}

/**
 * 行を backend の条件にする。誤りのある行は条件に入れず、行の key ごとの誤りを返す。
 * `fields` は選んだ検索・回答プロファイルで使える項目（未取得なら undefined）。
 */
export function extractionFieldConditions(
  rows: readonly ExtractionFieldFilterRow[],
  fields: readonly ExtractionFieldDefinition[] | undefined
): { conditions: ExtractionFieldCondition[]; errors: Record<string, ExtractionFieldFilterError> } {
  const conditions: ExtractionFieldCondition[] = [];
  const errors: Record<string, ExtractionFieldFilterError> = {};
  for (const row of rows) {
    if (!isActiveExtractionFieldFilterRow(row)) continue;
    if (!fields) {
      errors[row.key] = "fieldsUnavailable";
      continue;
    }
    const field = fields.find((item) => item.name === row.name);
    if (!field) {
      errors[row.key] = "fieldMissing";
      continue;
    }
    const base = { name: field.name, value_type: field.value_type };
    if (field.value_type === "string" || field.value_type === "bool") {
      if (row.value.trim()) conditions.push({ ...base, op: "eq", value: row.value.trim() });
      continue;
    }
    const min = row.min.trim();
    const max = row.max.trim();
    if (field.value_type === "number" && ((min && !isNumber(min)) || (max && !isNumber(max)))) {
      errors[row.key] = "numberInvalid";
      continue;
    }
    const inverted =
      min && max && (field.value_type === "number" ? Number(min) > Number(max) : min > max);
    if (inverted) {
      errors[row.key] = "rangeInverted";
      continue;
    }
    if (min) conditions.push({ ...base, op: "gte", value: min });
    if (max) conditions.push({ ...base, op: "lte", value: max });
  }
  return { conditions, errors };
}

/** `filters.extraction_fields` の値（条件が無ければ空文字）。 */
export function extractionFieldFilterValue(conditions: readonly ExtractionFieldCondition[]): string {
  return conditions.length ? JSON.stringify(conditions) : "";
}

/**
 * 条件（`filters.extraction_fields`）を編集の行に戻す（実行の詳細の「RAG 検索で開く」。#3309）。
 * 同じ項目の以上・以下は 1 行の範囲にまとめる。形の違う条件は捨て、行は画面の上限まで。
 */
export function extractionFieldFilterRowsFromConditions(
  conditions: readonly unknown[]
): ExtractionFieldFilterRow[] {
  const rows: ExtractionFieldFilterRow[] = [];
  for (const item of conditions) {
    if (typeof item !== "object" || item === null) continue;
    const { name, op, value } = item as Record<string, unknown>;
    if (typeof name !== "string" || !name || typeof value !== "string") continue;
    if (op !== "eq" && op !== "gte" && op !== "lte") continue;
    let row = op === "eq" ? undefined : rows.find((current) => current.name === name && !current.value);
    if (!row) {
      if (rows.length >= MAX_EXTRACTION_FIELD_FILTER_ROWS) continue;
      row = { ...newExtractionFieldFilterRow(), name };
      rows.push(row);
    }
    if (op === "eq") row.value = value;
    else if (op === "gte") row.min = value;
    else row.max = value;
  }
  return rows;
}

/** 条件の表示（「金額 ≥ 100000」など）。検索の適用中の条件と、質問から読み取った条件（#652）で使う。 */
export function extractionFieldConditionLabel(condition: ExtractionFieldCondition): string {
  const value =
    condition.value_type === "bool"
      ? t(condition.value === "true" ? "search.filters.fields.true" : "search.filters.fields.false")
      : condition.value;
  return t(`search.filters.fields.applied.${condition.op}`, { name: condition.name, value });
}

/** 適用中の条件の表示用に `filters.extraction_fields` を読む（壊れた値は空）。 */
export function parseExtractionFieldFilterValue(raw: string | undefined): ExtractionFieldCondition[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as ExtractionFieldCondition[]) : [];
  } catch {
    return [];
  }
}
