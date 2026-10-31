import { describe, expect, it } from "vitest";

import { PARSER_VALUES } from "@/components/documents/DocumentProcessingConfigPanel.values";
import { SERVICE_PROFILE_ORDER } from "@/components/settings/ServicesManagementClient";

import { PARSER_BACKEND_ORDER, sortParserBackends } from "./parser-backend-order";

describe("文書解析エンジンの並び", () => {
  it("Dots.OCR → MinerU → OCI Document Understanding の順（利用者の判断）", () => {
    expect(PARSER_BACKEND_ORDER.slice(-3)).toEqual([
      "dots_ocr",
      "mineru",
      "oci_document_understanding",
    ]);
  });

  it("処理レシピの選択肢も同じ正本の順", () => {
    expect([...PARSER_VALUES]).toEqual([...PARSER_BACKEND_ORDER]);
  });

  it("サービス管理の節も CPU → GPU → OCI", () => {
    expect(SERVICE_PROFILE_ORDER).toEqual(["cpu", "gpu", "oci"]);
  });

  it("画面のカードの並びを正本の順にそろえ、知らないエンジンは後ろに残す", () => {
    expect(
      sortParserBackends([
        "oci_document_understanding",
        "mineru",
        "dots_ocr",
        "local",
        "docling",
        "xml_tree_json",
        "unstructured",
        "structured_records_jsonl",
      ])
    ).toEqual([
      "docling",
      "unstructured",
      "structured_records_jsonl",
      "xml_tree_json",
      "dots_ocr",
      "mineru",
      "oci_document_understanding",
      "local",
    ]);
  });
});
