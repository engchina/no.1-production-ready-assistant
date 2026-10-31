import { describe, expect, it } from "vitest";

import {
  findTableCellTarget,
  firstMetadataToken,
  integerMetadataValue,
  tableCellKey,
  tableCellRef,
} from "./table-cell-focus";
import type { ExtractionTable } from "./api";

const tables: ExtractionTable[] = [
  {
    table_id: "tbl-1",
    element_id: "el-table",
    page_number: 2,
    cells: [
      {
        row: 0,
        col: 0,
        text: "項目",
        row_span: 1,
        col_span: 1,
        metadata: { formula_cell_ref: "A1" },
      },
      {
        row: 1,
        col: 1,
        text: "1000",
        row_span: 1,
        col_span: 1,
        bbox: [0.1, 0.2, 0.3, 0.4],
        metadata: { cell_ref: "B2", formula_cell_ref: "B2" },
      },
      {
        row: 1,
        col: 2,
        text: "1100",
        row_span: 1,
        col_span: 1,
        metadata: { cell_ref: "C2" },
      },
    ],
  },
];

describe("table cell focus helpers", () => {
  it("builds stable table cell keys and refs", () => {
    expect(tableCellKey("tbl-1", tables[0].cells[1])).toBe("tbl-1:r1:c1");
    expect(tableCellRef(tables[0].cells[1])).toBe("B2");
    expect(tableCellRef(tables[0].cells[2])).toBe("C2");
  });

  it("finds cells by formula ref or row/col", () => {
    expect(findTableCellTarget(tables, { tableId: "tbl-1", cellRef: "b2" })?.key).toBe(
      "tbl-1:r1:c1"
    );
    expect(findTableCellTarget(tables, { tableId: "tbl-1", cellRef: "c2" })?.key).toBe(
      "tbl-1:r1:c2"
    );
    expect(findTableCellTarget(tables, { tableId: "tbl-1", row: 0, col: 0 })?.cell.text).toBe(
      "項目"
    );
    expect(findTableCellTarget(tables, { tableId: "missing", cellRef: "B2" })).toBeNull();
  });

  it("reads first scalar metadata token and integer params", () => {
    expect(firstMetadataToken(" B2, C3 ")).toBe("B2");
    expect(firstMetadataToken(" A1\nB2 ")).toBe("A1");
    expect(firstMetadataToken(["", " D4 "])).toBe("D4");
    expect(firstMetadataToken('[" E5 ", "F6"]')).toBe("E5");
    expect(firstMetadataToken({ cell_ref: " G7 " })).toBe("G7");
    expect(firstMetadataToken([{ ignored: "" }, { formula_cell_ref: "H8" }])).toBe("H8");
    expect(firstMetadataToken({ table_id: "tbl-2" })).toBe("tbl-2");
    expect(firstMetadataToken({ table_id: "tbl-2" }, { preferTableId: true })).toBe("tbl-2");
    expect(firstMetadataToken(null)).toBeNull();
    expect(integerMetadataValue("2")).toBe(2);
    expect(integerMetadataValue("2.9")).toBe(2);
    expect(integerMetadataValue(false)).toBeNull();
  });

  it("prefers nested cell refs over adapter table ids for citation targeting", () => {
    expect(
      firstMetadataToken({
        table_id: "tbl-adapter",
        cells: [{ metadata: { formula_cell_ref: "C7" } }],
      })
    ).toBe("C7");
    expect(
      firstMetadataToken(
        {
          table_id: "tbl-adapter",
          cells: [{ metadata: { formula_cell_ref: "C7" } }],
        },
        { preferTableId: true }
      )
    ).toBe("tbl-adapter");
    expect(
      firstMetadataToken(
        '{"table_id":"tbl-adapter","cells":[{"metadata":{"cell_ref":"D8"}}]}'
      )
    ).toBe("D8");
  });
});
