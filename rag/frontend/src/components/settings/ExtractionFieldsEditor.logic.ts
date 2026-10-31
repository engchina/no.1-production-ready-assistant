import type { ExtractionFieldDefinition, ExtractionFieldValueType } from "@/lib/api";

/** backend の `MAX_FIELD_DEFINITIONS` / `FieldDefinitionData` と同じ上限（#528）。 */
export const MAX_EXTRACTION_FIELDS = 50;
export const EXTRACTION_FIELD_NAME_MAX = 120;
export const EXTRACTION_FIELD_DESCRIPTION_MAX = 500;
export const EXTRACTION_FIELD_VALUE_TYPES: readonly ExtractionFieldValueType[] = [
  "string",
  "number",
  "date",
  "bool",
];

/** 編集中の 1 行。`key` は React の行の識別子で、保存しない。 */
export interface ExtractionFieldRow extends ExtractionFieldDefinition {
  key: string;
}

export type ExtractionFieldRowError = "nameRequired" | "nameDuplicate";

let rowSequence = 0;

export function newExtractionFieldRow(
  field: ExtractionFieldDefinition = { name: "", description: "", value_type: "string" }
): ExtractionFieldRow {
  rowSequence += 1;
  return { ...field, key: `extraction-field-${rowSequence}` };
}

export function rowsFromDefinitions(
  fields: readonly ExtractionFieldDefinition[]
): ExtractionFieldRow[] {
  return fields.map((field) => newExtractionFieldRow(field));
}

function normalizeDefinition(field: ExtractionFieldDefinition): ExtractionFieldDefinition {
  return {
    name: field.name.trim(),
    description: field.description.trim(),
    value_type: field.value_type,
  };
}

/** 保存する形（前後の空白を除く。行の key は落とす）。 */
export function definitionsFromRows(rows: readonly ExtractionFieldRow[]): ExtractionFieldDefinition[] {
  return rows.map(normalizeDefinition);
}

/** 保存値と編集中の行が同じ内容か（前後の空白の違いは同じとみなす）。 */
export function sameDefinitions(
  saved: readonly ExtractionFieldDefinition[],
  rows: readonly ExtractionFieldRow[]
): boolean {
  return (
    JSON.stringify(saved.map(normalizeDefinition)) === JSON.stringify(definitionsFromRows(rows))
  );
}

/**
 * 行ごとの入力の誤り。backend と同じ判定（項目名は必須、大文字小文字を区別せず重複不可）。
 * 重複は後から出てきた行に付ける（先の行は正しい入力として残す）。
 */
export function validateExtractionFieldRows(
  rows: readonly ExtractionFieldRow[]
): Record<string, ExtractionFieldRowError> {
  const errors: Record<string, ExtractionFieldRowError> = {};
  const seen = new Set<string>();
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) {
      errors[row.key] = "nameRequired";
      continue;
    }
    const folded = name.toLocaleLowerCase();
    if (seen.has(folded)) {
      errors[row.key] = "nameDuplicate";
      continue;
    }
    seen.add(folded);
  }
  return errors;
}
