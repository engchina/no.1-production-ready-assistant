import { describe, expect, it } from "vitest";

import { isPreparedPdfArtifact, pdfPreviewUrl } from "./DocumentPreview";

describe("isPreparedPdfArtifact", () => {
  it("保存済み PDF だけをプレビュー可能と判定する", () => {
    expect(
      isPreparedPdfArtifact({
        object_storage_path: "prepared/report.bin",
        content_type: "application/pdf; charset=binary",
        file_name: "report.bin",
      })
    ).toBe(true);
    expect(
      isPreparedPdfArtifact({
        object_storage_path: "prepared/legacy.pdf",
        content_type: null,
        file_name: "legacy.PDF",
      })
    ).toBe(true);
    expect(
      isPreparedPdfArtifact({
        object_storage_path: "prepared/report.json",
        content_type: "application/json",
        file_name: "misleading.pdf",
      })
    ).toBe(false);
    expect(
      isPreparedPdfArtifact({
        object_storage_path: null,
        content_type: "application/pdf",
        file_name: "missing.pdf",
      })
    ).toBe(false);
  });
});

describe("pdfPreviewUrl", () => {
  it("PDF 原本プレビューではナビゲーションペインを初期表示しない", () => {
    expect(pdfPreviewUrl("/api/documents/doc-1/content")).toBe(
      "/api/documents/doc-1/content#pagemode=none&navpanes=0"
    );
  });

  it("ページ指定とナビゲーションペイン非表示を同時に保持する", () => {
    expect(pdfPreviewUrl("/api/documents/doc-1/content", 11)).toBe(
      "/api/documents/doc-1/content#page=11&pagemode=none&navpanes=0"
    );
  });
});
