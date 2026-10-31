import { describe, expect, it } from "vitest";

import type { ParserBackendCapabilityData } from "@/lib/api";
import {
  effectiveModalityForParser,
  findParserCapability,
  formatSupportedExtensions,
  formatSupportedFormats,
  parserSupportsDocument,
} from "@/lib/parser-capabilities";

const CAPABILITIES: ParserBackendCapabilityData[] = [
  { backend: "docling", modalities: ["pdf", "image"], extensions: [".pdf", ".png"] },
  {
    backend: "unstructured",
    modalities: ["pdf", "image", "text", "html", "email", "office"],
    extensions: [],
  },
];

describe("formatSupportedFormats", () => {
  it("modality を日本語ラベルで「・」連結する", () => {
    expect(formatSupportedFormats(findParserCapability(CAPABILITIES, "docling"))).toBe(
      "PDF・画像"
    );
  });

  it("宣言なしは空文字", () => {
    expect(formatSupportedFormats(null)).toBe("");
    expect(formatSupportedFormats(findParserCapability(CAPABILITIES, "no_such"))).toBe("");
  });
});

describe("effectiveModalityForParser", () => {
  it("office_to_pdf は office を pdf として扱う", () => {
    expect(effectiveModalityForParser("office", "office_to_pdf")).toBe("pdf");
  });

  it("変換対象外の modality はそのまま", () => {
    expect(effectiveModalityForParser("pdf", "office_to_pdf")).toBe("pdf");
    expect(effectiveModalityForParser("office", null)).toBe("office");
  });
});

describe("parserSupportsDocument", () => {
  it("docling × office は非対応", () => {
    expect(
      parserSupportsDocument({
        capabilities: CAPABILITIES,
        backend: "docling",
        modality: "office",
      })
    ).toBe(false);
  });

  it("docling × office でも office_to_pdf 変換があれば対応", () => {
    expect(
      parserSupportsDocument({
        capabilities: CAPABILITIES,
        backend: "docling",
        modality: "office",
        preprocessProfile: "office_to_pdf",
      })
    ).toBe(true);
  });

  it("判定材料不足(宣言なし/未取得/unknown)は null", () => {
    expect(
      parserSupportsDocument({
        capabilities: CAPABILITIES,
        backend: "no_such",
        modality: "pdf",
      })
    ).toBeNull();
    expect(
      parserSupportsDocument({ capabilities: undefined, backend: "docling", modality: "pdf" })
    ).toBeNull();
    expect(
      parserSupportsDocument({
        capabilities: CAPABILITIES,
        backend: "docling",
        modality: "unknown",
      })
    ).toBeNull();
  });
});

describe("formatSupportedExtensions", () => {
  it("拡張子をソートして空白連結する", () => {
    expect(
      formatSupportedExtensions({
        backend: "unstructured",
        modalities: ["text"],
        extensions: [".md", ".csv", ".pdf"],
      })
    ).toBe(".csv .md .pdf");
  });

  it("宣言なしは空文字", () => {
    expect(formatSupportedExtensions(null)).toBe("");
    expect(
      formatSupportedExtensions({ backend: "x", modalities: [], extensions: [] })
    ).toBe("");
  });
});

describe("modality を持たない backend（XML ツリー JSON。issue 2042）", () => {
  const capabilities = [{ backend: "xml_tree_json", modalities: [], extensions: [".json"] }];

  it("対応形式は拡張子の名前で出す", () => {
    expect(formatSupportedFormats(capabilities[0])).toBe("JSON");
  });

  it("テキストの文書は判定せず、ほかの形式は扱えないと判定する", () => {
    expect(
      parserSupportsDocument({ capabilities, backend: "xml_tree_json", modality: "text" })
    ).toBeNull();
    expect(
      parserSupportsDocument({ capabilities, backend: "xml_tree_json", modality: "pdf" })
    ).toBe(false);
  });
});
