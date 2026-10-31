import { describe, expect, it } from "vitest";

import {
  DEFAULT_EXCEL_OPTIONS,
  excelOptionsSummary,
  formatColumnRoles,
  parseColumnRoles,
  parseFormThreshold,
} from "./ExcelOptionsRow";

// 列の役割の指定の書き方（#1281）。
describe("parseColumnRoles", () => {
  it("列名か列の記号と役割を読み、日本語と英語の役割の名前を受け付ける", () => {
    expect(parseColumnRoles("D=例示、設定値＝現在値, E=なし\nF=Default")).toEqual({
      roles: { D: "example", 設定値: "current", E: "none", F: "default" },
      invalid: [],
    });
  });

  it("読めない項目は保存せず invalid に返す", () => {
    expect(parseColumnRoles("D=実際の値、=既定値、C、B=既定値=例示、A=範囲")).toEqual({
      roles: { A: "allowed" },
      invalid: ["D=実際の値", "=既定値", "C", "B=既定値=例示"],
    });
  });

  it("書き戻すと同じ指定に読める", () => {
    const roles = { D: "example", 説明: "definition" } as const;
    expect(parseColumnRoles(formatColumnRoles(roles)).roles).toEqual(roles);
  });
});

describe("excelOptionsSummary", () => {
  it("判定しない設定と役割の指定を要約に出す", () => {
    const summary = excelOptionsSummary({
      ...DEFAULT_EXCEL_OPTIONS,
      column_role_detection: "off",
      column_roles: { D: "example" },
    });
    expect(summary).toContain("列の役割は判定しない");
    expect(summary).toContain("列の役割の指定: D=例示");
    expect(excelOptionsSummary(DEFAULT_EXCEL_OPTIONS)).not.toContain("列の役割");
  });
});

// 非表示の行・列（#2526）と表の分割・帳票の判定の下限（#2516）。
describe("Excel の読み方の追加の項目", () => {
  it("既定は backend の ExcelOptions と同じ", () => {
    expect(DEFAULT_EXCEL_OPTIONS).toMatchObject({
      include_hidden_cells: false,
      split_tables: true,
      table_gap_rows: 1,
      table_gap_columns: 1,
      form_detection_threshold: 0.5,
    });
  });

  it("既定と違う値だけを要約に出す", () => {
    expect(excelOptionsSummary(DEFAULT_EXCEL_OPTIONS)).toBe("自動 · 表頭は自動");
    expect(
      excelOptionsSummary({
        ...DEFAULT_EXCEL_OPTIONS,
        include_hidden_cells: true,
        table_gap_rows: 2,
        form_detection_threshold: 0.75,
      }),
    ).toBe("自動 · 表頭は自動 · 表の区切り: 空の行 2・空の列 1 · 帳票の判定の下限: 0.75 · 非表示の行・列も読む");
    // 分けないときは区切りの数を出さず、帳票の判定の下限は自動の読み方のときだけ出す。
    expect(
      excelOptionsSummary({
        ...DEFAULT_EXCEL_OPTIONS,
        mode: "table",
        split_tables: false,
        table_gap_rows: 3,
        form_detection_threshold: 0.75,
      }),
    ).toBe("表（1 行ずつ） · 表頭は自動 · 表を分けない");
  });

  it("項目が足りない値は既定で補って要約する", () => {
    const partial: Partial<typeof DEFAULT_EXCEL_OPTIONS> = { ...DEFAULT_EXCEL_OPTIONS };
    delete partial.split_tables;
    expect(excelOptionsSummary(partial as typeof DEFAULT_EXCEL_OPTIONS)).toBe("自動 · 表頭は自動");
  });

  it("帳票の判定の下限は 0〜1 の数だけを読む", () => {
    expect(parseFormThreshold("0.7")).toBe(0.7);
    expect(parseFormThreshold("0")).toBe(0);
    expect(parseFormThreshold("1")).toBe(1);
    expect(parseFormThreshold("1.2")).toBeNull();
    expect(parseFormThreshold("-0.1")).toBeNull();
    expect(parseFormThreshold("")).toBeNull();
    expect(parseFormThreshold("abc")).toBeNull();
  });
});
