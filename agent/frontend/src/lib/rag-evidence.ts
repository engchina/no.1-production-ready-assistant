import { t } from "@/lib/i18n";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

/** 表計算の根拠の場所（例: 「コード表 · 3–6 行（A3:C6）」。#1221）。シートが無ければ null。 */
function sheetLocation(value: Record<string, unknown>): string | null {
  const sheet = typeof value.sheet_name === "string" && value.sheet_name.trim() ? value.sheet_name.trim() : null;
  if (!sheet) return null;
  const start = positiveInteger(value.row_start);
  const end = positiveInteger(value.row_end);
  const rows =
    start == null
      ? null
      : end != null && end !== start
        ? t("evidence.rowRange", { start, end })
        : t("evidence.row", { row: start });
  const range = typeof value.cell_range === "string" && value.cell_range.trim() ? value.cell_range.trim() : null;
  const rowPart = rows && range ? `${rows}（${range}）` : rows ?? range;
  return [sheet, rowPart].filter(Boolean).join(" · ");
}

/** 根拠の場所の表示（例: 「p.3 · 規程 > 第2条」「コード表 · 3–6 行（A3:C6）」）。無ければ null。 */
export function evidenceLocation(locator: unknown): string | null {
  const value = record(locator);
  if (!value) return null;
  const sheet = sheetLocation(value);
  if (sheet) return sheet;
  const start = positiveInteger(value.page_start);
  const end = positiveInteger(value.page_end);
  const page =
    start == null
      ? null
      : end != null && end !== start
        ? t("evidence.pageRange", { start, end })
        : t("evidence.page", { page: start });
  const sections = Array.isArray(value.section_path)
    ? value.section_path.filter((item): item is string => typeof item === "string" && item.trim() !== "")
    : [];
  const parts = [page, sections.length ? sections.join(" > ") : null].filter((part): part is string => Boolean(part));
  return parts.length ? parts.join(" · ") : null;
}
