import type { DocumentExtractionExportFormat } from "@/lib/api";

/** 抽出エクスポートでファイルとコピーを出す形式（Chunk は「Chunk / Citation」タブで扱う。#561）。 */
export const EXTRACTION_EXPORT_FORMATS = [
  { format: "markdown", extension: ".md" },
  { format: "html", extension: ".html" },
  { format: "json", extension: ".json" },
] as const satisfies ReadonlyArray<{
  format: Exclude<DocumentExtractionExportFormat, "chunks">;
  extension: string;
}>;

export type ExtractionExportFileFormat = (typeof EXTRACTION_EXPORT_FORMATS)[number]["format"];
