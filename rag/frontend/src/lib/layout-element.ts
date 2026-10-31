import type { DocumentElement } from "@/lib/api";
import { appPath } from "@/lib/base-path";

/** Docling の要素の Vision 図説明の表示用情報。該当しなければ null。 */
export type ElementVision = {
  status: string;
  retrievalText: string;
  excluded: boolean;
};

export function elementVision(element: DocumentElement): ElementVision | null {
  const metadata = element.metadata ?? {};
  const status = typeof metadata.vision_status === "string" ? metadata.vision_status : "";
  const retrievalText =
    typeof metadata.vision_retrieval_text === "string" ? metadata.vision_retrieval_text : "";
  const excluded = metadata.rag_excluded === true || metadata.visual_role === "decorative";
  if (!status && !retrievalText && !excluded) return null;
  return { status, retrievalText, excluded };
}

/** Vision 説明の表示用 1 行(ラベルキーと値)。 */
export type VisionDetailLine = { field: string; value: string };

const VISION_DETAIL_FIELDS = [
  "visual_kind",
  "main_topic",
  "surrounding_context",
  "menu_route",
  "visible_screen_names",
  "visible_buttons",
  "visible_fields",
  "visible_values",
  "codes_and_errors",
  "table_headers",
  "table_rows",
  "diagram_nodes",
  "diagram_edges",
  "chart_series",
  "form_fields",
  "operation_steps",
  "condition_result_pairs",
  "exception_or_cautions",
  "correction_notes",
] as const;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function formatVisionValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (Array.isArray(item)) return item.map(String).join(" | ");
        if (item && typeof item === "object") {
          return Object.values(item as Record<string, unknown>)
            .map((part) => String(part ?? "").trim())
            .filter(Boolean)
            .join(" → ");
        }
        return String(item ?? "").trim();
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

/**
 * 抽出 JSON の layout_records から要素(record)の Vision 説明を引き、値のある項目だけ返す。
 * 装飾・アイコンとして除外された図は reason に理由を返す。layout_records(Docling)の無い抽出では null。
 */
export function layoutVisionDetails(
  extraction: Record<string, unknown>,
  elementId: string
): { lines: VisionDetailLine[]; excludedReason: string } | null {
  const layout = record(record(extraction.parser_artifacts).layout_records);
  const records = Array.isArray(layout.records) ? layout.records : [];
  const found = records.map(record).find((item) => item.id === elementId);
  if (!found) return null;
  const raw = record(found.raw);
  const description = record(raw.vision_description);
  const lines = VISION_DETAIL_FIELDS.map((field) => ({
    field,
    value: formatVisionValue(description[field]),
  })).filter((line) => line.value);
  const excludedReason =
    raw.visual_role === "decorative" || raw.visual_role === "inline_icon"
      ? String(raw.visual_role_reason ?? raw.visual_role)
      : "";
  if (!lines.length && !excludedReason) return null;
  return { lines, excludedReason };
}

/** 図・表要素の切り出し画像 URL(crop API)。bbox / ページ寸法が無ければ null。 */
export function elementCropUrl(documentId: string, element: DocumentElement): string | null {
  const bbox = element.bbox;
  const metadata = element.metadata ?? {};
  const width = metadata.page_width;
  const height = metadata.page_height;
  if (
    !bbox ||
    bbox.length !== 4 ||
    typeof element.page_number !== "number" ||
    typeof width !== "number" ||
    typeof height !== "number"
  ) {
    return null;
  }
  const [x0, y0, x1, y1] = bbox;
  if (!(x1 > x0 && y1 > y0)) return null;
  const search = new URLSearchParams({
    page: String(element.page_number),
    x0: String(Math.max(0, x0)),
    y0: String(Math.max(0, y0)),
    x1: String(Math.min(width, x1)),
    y1: String(Math.min(height, y1)),
    page_width: String(width),
    page_height: String(height),
  });
  // `<img src>` に入るので配信の base を付ける（#1316）。
  return appPath(`/api/documents/${encodeURIComponent(documentId)}/crop?${search.toString()}`);
}
