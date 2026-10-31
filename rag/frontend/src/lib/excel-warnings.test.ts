import { describe, expect, it } from "vitest";

import {
  excelWarningLabel,
  extractionWarningView,
  isHeaderLowConfidenceWarning,
  lowConfidenceHeaderSheets,
} from "./excel-warnings";

describe("excelWarningLabel", () => {
  it("Excel の前処理の warning を日本語にする", () => {
    expect(excelWarningLabel("excel_header_low_confidence:費目")).toContain("シート「費目」の表頭");
    expect(excelWarningLabel("excel_formula_without_cached_value:集計:3")).toContain(
      "シート「集計」の数式 3 件"
    );
    expect(excelWarningLabel("excel_sheet_not_found:無い")).toContain("「無い」");
    expect(excelWarningLabel("excel_range_sheet_not_found:手順 1")).toContain("「手順 1」");
  });

  it("Excel の warning でなければ null", () => {
    expect(excelWarningLabel("office_to_pdf_failed")).toBeNull();
  });
});

describe("lowConfidenceHeaderSheets", () => {
  it("表頭の推定の信頼度が低いシートを重複なく返す", () => {
    expect(
      lowConfidenceHeaderSheets([
        "excel_header_low_confidence:費目",
        "table_structure_review",
        "excel_header_low_confidence:費目",
        "excel_header_low_confidence:時刻 10:00",
      ])
    ).toEqual(["費目", "時刻 10:00"]);
    expect(lowConfidenceHeaderSheets([])).toEqual([]);
  });
});

describe("extractionWarningView", () => {
  it("warning を利用者の言葉の文にし、backend のコードは codes に分ける", () => {
    const view = extractionWarningView([
      "excel_header_low_confidence:シート A",
      "table_structure_review",
      "backend_internal_warning",
      "another_internal_warning",
      "解析の文として届いた警告です。",
    ]);
    expect(view.messages).toEqual([
      expect.stringContaining("シート「シート A」の表頭"),
      "表構造確認",
      "抽出の処理で確認が必要な点があります。",
      "解析の文として届いた警告です。",
    ]);
    expect(view.codes).toEqual([
      "excel_header_low_confidence:シート A",
      "table_structure_review",
      "backend_internal_warning",
      "another_internal_warning",
    ]);
  });

  it("omit に当たる warning（先頭の案内が出すもの）は文にもコードにも出さない", () => {
    const view = extractionWarningView(
      ["excel_header_low_confidence:シート A", "table_structure_review"],
      isHeaderLowConfidenceWarning
    );
    expect(view.messages).toEqual(["表構造確認"]);
    expect(view.codes).toEqual(["table_structure_review"]);
    expect(extractionWarningView(["excel_header_low_confidence:シート A"], isHeaderLowConfidenceWarning)).toEqual({
      messages: [],
      codes: [],
    });
  });
});
