import { describe, expect, it } from "vitest";

import { EXTRACTION_EXPORT_FORMATS } from "./DocumentExtractionExport.logic";

// コピーの書き込み（取得を待つ前に ClipboardItem へ渡す Safari の対応）は共有 UI の copyTextToClipboard が持つ
// （platform/packages/ui/tests/clipboard.test.ts。#2099）。
describe("EXTRACTION_EXPORT_FORMATS", () => {
  it("Markdown / HTML / JSON の 3 形式だけを出し、Chunks を含めない", () => {
    expect(EXTRACTION_EXPORT_FORMATS.map((item) => item.format)).toEqual([
      "markdown",
      "html",
      "json",
    ]);
    expect(EXTRACTION_EXPORT_FORMATS.map((item) => item.extension)).toEqual([
      ".md",
      ".html",
      ".json",
    ]);
  });
});
