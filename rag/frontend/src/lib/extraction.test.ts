import { describe, expect, it } from "vitest";

import { parseStructuredExtraction, summarizeDocumentElements } from "./extraction";

describe("parseStructuredExtraction", () => {
  it("elements と raw_text を安全に読み取る", () => {
    const parsed = parseStructuredExtraction({
      raw_text: "本文",
      document_type: "規程",
      confidence: 0.91,
      warnings: ["低解像度"],
      elements: [
        {
          kind: "table",
          text: "| 項目 | 金額 |",
          order: 2,
          element_id: "tbl-1",
          content_kind: "table",
          source_parser: "local_office_structure",
          page_number: 2,
          bbox: [0, 0, 100, 50],
          section_path: ["経費申請"],
          confidence: 0.8,
          metadata: { raw_start: 10, nested: { ignored: true } },
        },
        {
          kind: "title",
          text: "経費申請",
          order: 1,
          page_number: 1,
        },
      ],
      pages: [{ page_number: 2, element_ids: ["tbl-1"] }],
      tables: [
        {
          table_id: "table-1",
          element_id: "tbl-1",
          cells: [
            {
              row: 0,
              col: 0,
              text: "項目",
              page_number: 2,
              metadata: { formula_cell_ref: "B2", nested: { ignored: true } },
            },
          ],
        },
      ],
      assets: [{ asset_id: "fig-1", kind: "figure", page_number: 2 }],
      parser_artifacts: { parser_backend: "local_partition" },
    });

    expect(parsed.rawText).toBe("本文");
    expect(parsed.documentType).toBe("規程");
    expect(parsed.confidence).toBe(0.91);
    expect(parsed.warnings).toEqual(["低解像度"]);
    expect(parsed.elements.map((element) => element.kind)).toEqual(["title", "table"]);
    expect(parsed.elements[1].element_id).toBe("tbl-1");
    expect(parsed.elements[1].content_kind).toBe("table");
    expect(parsed.elements[1].source_parser).toBe("local_office_structure");
    expect(parsed.elements[1].bbox).toEqual([0, 0, 100, 50]);
    expect(parsed.elements[1].metadata).toEqual({ raw_start: 10 });
    expect(parsed.pages[0].element_ids).toEqual(["tbl-1"]);
    expect(parsed.tables[0].cells[0].text).toBe("項目");
    expect(parsed.tables[0].cells[0].page_number).toBe(2);
    expect(parsed.tables[0].cells[0].metadata).toEqual({ formula_cell_ref: "B2" });
    expect(parsed.assets[0].asset_id).toBe("fig-1");
    expect(parsed.parserArtifacts.parser_backend).toBe("local_partition");
  });

  it("不完全な旧 extraction は raw text fallback として扱う", () => {
    const parsed = parseStructuredExtraction({
      raw_text: "旧データの本文だけ",
      elements: [{ kind: "text", text: "" }],
      confidence: "bad",
      warnings: "bad",
    });

    expect(parsed.rawText).toBe("旧データの本文だけ");
    expect(parsed.confidence).toBeNull();
    expect(parsed.warnings).toEqual([]);
    expect(parsed.elements).toEqual([]);
  });

  it("派生系譜(source_derivation)を parser_artifacts から取り出す", () => {
    const parsed = parseStructuredExtraction({
      raw_text: "本文",
      parser_artifacts: {
        source_derivation: {
          derivation_id: "d1",
          preprocess_profile: "office_to_pdf",
          converted: true,
          converter_name: "libreoffice",
          converter_version: "v1",
          source_sha256: "aaa",
          derived_object_path: "artifacts/canonical/doc/trace/canonical.pdf",
          derived_content_type: "application/pdf",
          derived_sha256: "bbb",
          page_map: { "1": 1, "2": 2 },
          warnings: [],
        },
      },
    });

    expect(parsed.sourceDerivation?.derivationId).toBe("d1");
    expect(parsed.sourceDerivation?.converted).toBe(true);
    expect(parsed.sourceDerivation?.preprocessProfile).toBe("office_to_pdf");
    expect(parsed.sourceDerivation?.pageMap).toEqual({ "1": 1, "2": 2 });
  });

  it("派生系譜が無ければ null を返す", () => {
    const parsed = parseStructuredExtraction({ raw_text: "本文" });
    expect(parsed.sourceDerivation).toBeNull();
  });

  it("navigation / fields / asset summary を安全に読み取る", () => {
    const parsed = parseStructuredExtraction({
      raw_text: "本文",
      navigation: [
        {
          section_id: "sec-1",
          title: "第1章",
          section_path: ["第1章"],
          depth: 0,
          page_start: 1,
          summary: "章の要約",
        },
        { section_id: "", title: "壊れた node" },
        "garbage",
      ],
      fields: [
        { name: "請求書番号", value: "INV-1", value_type: "string", confidence: 0.9 },
        { name: "空値", value: "  " },
      ],
      assets: [
        { asset_id: "a1", kind: "figure", summary: "図の説明", page_number: 2 },
        { asset_id: "a2", kind: "figure" },
      ],
    });

    expect(parsed.navigation).toEqual([
      {
        section_id: "sec-1",
        title: "第1章",
        section_path: ["第1章"],
        depth: 0,
        parent_section_id: null,
        page_start: 1,
        page_end: null,
        summary: "章の要約",
      },
    ]);
    expect(parsed.fields).toEqual([
      {
        name: "請求書番号",
        value: "INV-1",
        value_type: "string",
        confidence: 0.9,
        page_number: null,
      },
    ]);
    expect(parsed.assets.map((asset) => asset.summary)).toEqual(["図の説明", null]);
  });

  it("navigation / fields が無ければ空配列を返す", () => {
    const parsed = parseStructuredExtraction({ raw_text: "本文" });
    expect(parsed.navigation).toEqual([]);
    expect(parsed.fields).toEqual([]);
  });
});

describe("summarizeDocumentElements", () => {
  it("種別とページを集計する", () => {
    const stats = summarizeDocumentElements([
      { kind: "title", text: "A", order: 0, page_number: 1 },
      { kind: "list", text: "- A", order: 1, page_number: 1 },
      { kind: "table", text: "| A |", order: 2, page_number: 2 },
      { kind: "text", text: "本文", order: 3, page_number: 2 },
    ]);

    expect(stats).toEqual({
      elementCount: 4,
      titleCount: 1,
      textCount: 1,
      tableCount: 1,
      listCount: 1,
      pageCount: 2,
    });
  });
});


it("表示用本文を元の分割用本文と独立に保持する", () => {
  const parsed = parseStructuredExtraction({ raw_text: "a\n<<<JSONL_RECORD_BOUNDARY>>>\nb", display_text: "a\nb" });
  expect(parsed.rawText).toContain("<<<JSONL_RECORD_BOUNDARY>>>");
  expect(parsed.displayText).toBe("a\nb");
  expect(parseStructuredExtraction({ raw_text: "本文" }).displayText).toBe("本文");
});
