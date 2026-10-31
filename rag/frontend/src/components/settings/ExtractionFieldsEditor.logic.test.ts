import { describe, expect, it } from "vitest";

import {
  definitionsFromRows,
  newExtractionFieldRow,
  rowsFromDefinitions,
  sameDefinitions,
  validateExtractionFieldRows,
} from "./ExtractionFieldsEditor.logic";

// #528: 文書解析の「解析後の処理」で、項目抽出の項目の定義を編集する。
describe("抽出する項目の定義の編集", () => {
  it("行の key は行ごとに異なり、保存する形には含めない", () => {
    const rows = rowsFromDefinitions([
      { name: "請求書番号", description: "", value_type: "string" },
      { name: "合計金額", description: "税込", value_type: "number" },
    ]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(2);
    expect(definitionsFromRows(rows)).toEqual([
      { name: "請求書番号", description: "", value_type: "string" },
      { name: "合計金額", description: "税込", value_type: "number" },
    ]);
  });

  it("保存する形は前後の空白を除き、空白の違いだけなら未変更とみなす", () => {
    const saved = [{ name: "請求書番号", description: "番号", value_type: "string" as const }];
    const rows = [newExtractionFieldRow({ name: " 請求書番号 ", description: "番号 ", value_type: "string" })];
    expect(definitionsFromRows(rows)).toEqual(saved);
    expect(sameDefinitions(saved, rows)).toBe(true);
    expect(sameDefinitions(saved, [...rows, newExtractionFieldRow()])).toBe(false);
    expect(
      sameDefinitions(saved, [{ ...rows[0], value_type: "date" }])
    ).toBe(false);
  });

  it("項目名の空欄と重複（大文字小文字を区別しない）を、後の行の誤りにする", () => {
    const first = newExtractionFieldRow({ name: "Total", description: "", value_type: "number" });
    const duplicate = newExtractionFieldRow({ name: " total ", description: "", value_type: "number" });
    const blank = newExtractionFieldRow({ name: "  ", description: "説明だけ", value_type: "string" });
    const ok = newExtractionFieldRow({ name: "日付", description: "", value_type: "date" });

    expect(validateExtractionFieldRows([first, duplicate, blank, ok])).toEqual({
      [duplicate.key]: "nameDuplicate",
      [blank.key]: "nameRequired",
    });
    expect(validateExtractionFieldRows([first, ok])).toEqual({});
    expect(validateExtractionFieldRows([])).toEqual({});
  });
});
