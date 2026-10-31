import { describe, expect, it } from "vitest";

import {
  documentFileTypeLabel,
  isSameParserBackend,
  parserProfileKey,
  parserBackendLabel,
  qualityCodeLabel,
  sourceWarningKey,
  unsupportedReasonLabel,
  visibleSourceWarningKeys,
} from "./source-profile-labels";
import { t } from "./i18n";

describe("source profile labels", () => {
  it("unsupported parser profiles use specific Japanese i18n keys", () => {
    expect(t(parserProfileKey("unsupported_outlook_msg"))).toBe("Outlook MSG 未対応");
    expect(t(parserProfileKey("unsupported_tiff_image"))).toBe("TIFF 画像未対応");
    expect(t(parserProfileKey("unsupported_legacy_office_binary"))).toBe("旧 Office(要変換)");
    expect(t(parserProfileKey("unsupported_macro_enabled_office"))).toBe("マクロ付き Office(要変換)");
    expect(t(parserProfileKey("unsupported_odt_rtf"))).toBe("ODT・RTF(要変換)");
  });

  it("unsupported warnings avoid falling back to generic copy", () => {
    expect(t(sourceWarningKey("unsupported_outlook_msg"))).toContain("Outlook MSG");
    expect(t(sourceWarningKey("unsupported_tiff_image"))).toContain("TIFF");
    expect(t(sourceWarningKey("unsupported_legacy_office_binary"))).toContain("旧形式");
  });

  it("unsupported reasons are presented as user-facing Japanese messages", () => {
    expect(unsupportedReasonLabel("tiff_image_not_supported")).toContain("PNG/JPEG/WEBP");
    // 手で変換させず、文書レシピのファイル準備で変換を選ぶよう案内する（#2470）。
    expect(unsupportedReasonLabel("macro_enabled_office_not_supported")).toContain("マクロは実行しません");
    expect(unsupportedReasonLabel("legacy_office_binary_not_supported")).toBe(
      "旧形式の Office は変換が必要です（文書レシピのファイル準備で変換を選択）。"
    );
    expect(unsupportedReasonLabel("outlook_msg_not_supported")).toContain("EML");
    // ODT・RTF は Office→PDF で PDF にしてから読む（#3351）。
    expect(unsupportedReasonLabel("odt_rtf_not_supported")).toContain("Office→PDF");
    expect(unsupportedReasonLabel("custom_reason")).toBe("custom_reason");
  });

  it("does not repeat an unsupported reason as a warning line", () => {
    expect(
      visibleSourceWarningKeys(["unsupported_legacy_office_binary", "large_file"], "legacy_office_binary_not_supported")
    ).toEqual(["sourceProfile.warning.largeFile"]);
    expect(visibleSourceWarningKeys(["unsupported_legacy_office_binary"], null)).toEqual([
      "sourceProfile.warning.unsupportedLegacyOffice",
    ]);
  });

  it("evaluation quality labels share the same unsupported warning mapping", () => {
    expect(qualityCodeLabel("unsupported_tiff_image")).toBe("TIFF 画像未対応");
    expect(qualityCodeLabel("unsupported_legacy_office_binary")).toBe("旧 Office(要変換)");
    expect(qualityCodeLabel("unsupported_odt_rtf")).toBe("ODT・RTF(要変換)");
  });

  it("evaluation quality labels expose segment artifact cache misses in Japanese", () => {
    expect(qualityCodeLabel("segment_extraction_artifact_cache_miss")).toBe(
      "Segment artifact 再抽出"
    );
  });

  it("parser backend labels hide external adapter implementation names", () => {
    expect(parserBackendLabel("docling_adapter")).toBe("Docling");
    expect(isSameParserBackend("docling_adapter", "docling")).toBe(true);
    expect(parserBackendLabel("unstructured_adapter")).toBe("Unstructured");
    expect(isSameParserBackend("unstructured_adapter", "unstructured")).toBe(true);
  });

  it("削除済みの解析エンジン名は過去の解析結果でも生の値のまま表示する", () => {
    // Marker / Unlimited-OCR / GLM-OCR は #270、外部の OCR の解析エンジンは #22 で削除した。
    // 過去の解析結果に残る名前は壊さず表示する。
    expect(parserBackendLabel("marker_adapter")).toBe("marker_adapter");
    expect(parserBackendLabel("unlimited_ocr_adapter")).toBe("unlimited_ocr_adapter");
    expect(parserBackendLabel("glm_ocr")).toBe("glm_ocr");
    expect(parserBackendLabel("glm_ocr_adapter")).toBe("glm_ocr_adapter");
    expect(parserBackendLabel("oci_genai_vision")).toBe("oci_genai_vision");
  });
});


describe("documentFileTypeLabel", () => {
  it.each([
    ["sample.PDF", "PDF"], ["sample.docx", "Word"], ["sample.doc", "Word"],
    ["sample.xlsx", "Excel"], ["sample.xlsm", "Excel"], ["sample.pptx", "PowerPoint"],
    ["sample.csv", "CSV"], ["sample.custom", "CUSTOM"],
  ])("profile がない %s も原本の拡張子で表示する", (file_name, expected) => {
    expect(documentFileTypeLabel({file_name, source_profile: null, content_type: null})).toBe(expected);
  });

  it("profile の判定をファイル名より優先する", () => {
    const profile: import("./api").SourceProfile = {
      original_file_name: "source", sanitized_file_name: "source", extension: null,
      content_type: "application/pdf", inferred_content_type: "application/pdf",
      file_size_bytes: 1, content_sha256: "", modality: "pdf", parser_profile: "",
      parser_backend: "", parser_version: "", preview_kind: "pdf", text_charset: null,
      duplicate_of_document_id: null, unsupported_reason: null, quality_status: "ready", quality_warnings: [],
    };
    expect(documentFileTypeLabel({file_name: "source.txt", source_profile: profile, content_type: null})).toBe("PDF");
    expect(documentFileTypeLabel({file_name: "source", source_profile: {...profile, modality: "office", extension: ".xlsx"}, content_type: null})).toBe("Excel");
  });

  it.each([
    ["application/pdf", "PDF"], ["image/png", "画像"], ["text/html; charset=utf-8", "HTML"],
    ["text/plain", "テキスト"], ["application/octet-stream", "不明"], [null, "不明"],
  ])("拡張子もないときは MIME %s で表示する", (content_type, expected) => {
    expect(documentFileTypeLabel({file_name: "source", source_profile: null, content_type})).toBe(expected);
  });
});
