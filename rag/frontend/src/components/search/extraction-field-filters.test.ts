import { describe, expect, it } from "vitest";

import type { ExtractionFieldDefinition } from "@/lib/api";

import {
  type ExtractionFieldFilterRow,
  extractionFieldConditions,
  extractionFieldFilterValue,
  isExtractionFieldFilterRows,
  parseExtractionFieldFilterValue,
} from "./extraction-field-filters";

const FIELDS: ExtractionFieldDefinition[] = [
  { name: "契約番号", description: "", value_type: "string" },
  { name: "金額", description: "", value_type: "number" },
  { name: "契約日", description: "", value_type: "date" },
  { name: "自動更新", description: "", value_type: "bool" },
];

function row(key: string, patch: Partial<ExtractionFieldFilterRow>): ExtractionFieldFilterRow {
  return { key, name: "", value: "", min: "", max: "", ...patch };
}

describe("extractionFieldConditions", () => {
  it("型ごとに一致と範囲の条件を作る", () => {
    const { conditions, errors } = extractionFieldConditions(
      [
        row("a", { name: "契約番号", value: " C-1 " }),
        row("b", { name: "金額", min: "1000000" }),
        row("c", { name: "契約日", min: "2025-01-01", max: "2025-12-31" }),
        row("d", { name: "自動更新", value: "false" }),
      ],
      FIELDS
    );
    expect(errors).toEqual({});
    expect(conditions).toEqual([
      { name: "契約番号", value_type: "string", op: "eq", value: "C-1" },
      { name: "金額", value_type: "number", op: "gte", value: "1000000" },
      { name: "契約日", value_type: "date", op: "gte", value: "2025-01-01" },
      { name: "契約日", value_type: "date", op: "lte", value: "2025-12-31" },
      { name: "自動更新", value_type: "bool", op: "eq", value: "false" },
    ]);
  });

  it("項目を選んでいない行と値の無い行は条件にも誤りにもしない", () => {
    const result = extractionFieldConditions(
      [row("a", {}), row("b", { name: "金額" })],
      FIELDS
    );
    expect(result).toEqual({ conditions: [], errors: {} });
  });

  it("数値でない値・逆転した範囲・候補に無い項目・未取得を誤りにして条件から外す", () => {
    const { conditions, errors } = extractionFieldConditions(
      [
        row("a", { name: "金額", min: "百万" }),
        row("b", { name: "金額", min: "10", max: "9" }),
        row("c", { name: "契約日", min: "2025-02-01", max: "2025-01-01" }),
        row("d", { name: "消えた項目", value: "x" }),
      ],
      FIELDS
    );
    expect(conditions).toEqual([]);
    expect(errors).toEqual({
      a: "numberInvalid",
      b: "rangeInverted",
      c: "rangeInverted",
      d: "fieldMissing",
    });
    expect(
      extractionFieldConditions([row("a", { name: "金額", min: "1" })], undefined).errors
    ).toEqual({ a: "fieldsUnavailable" });
  });

  it("数値の範囲は数として比べる（文字列の順ではない）", () => {
    const { errors } = extractionFieldConditions(
      [row("a", { name: "金額", min: "9", max: "10" })],
      FIELDS
    );
    expect(errors).toEqual({});
  });
});

describe("filters の値", () => {
  it("条件の JSON を往復し、条件が無ければ空文字にする", () => {
    const conditions = [
      { name: "金額", value_type: "number" as const, op: "gte" as const, value: "1" },
    ];
    const value = extractionFieldFilterValue(conditions);
    expect(parseExtractionFieldFilterValue(value)).toEqual(conditions);
    expect(extractionFieldFilterValue([])).toBe("");
    expect(parseExtractionFieldFilterValue("{broken")).toEqual([]);
    expect(parseExtractionFieldFilterValue(undefined)).toEqual([]);
  });

  it("作業状態から戻す値の形を確かめる", () => {
    expect(isExtractionFieldFilterRows([row("a", { name: "金額" })])).toBe(true);
    expect(isExtractionFieldFilterRows([{ key: "a", name: "金額" }])).toBe(false);
    expect(isExtractionFieldFilterRows("x")).toBe(false);
  });
});
