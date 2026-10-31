import type { DocumentChunkView } from "./api";
import { parseStructuredExtraction } from "./extraction";

/** 表示の絞り込みだけ。原文・章節の目次・保存用の抽出結果は変えない。 */
export function extractionOnPage(extraction: Record<string, unknown>, page: number | null): Record<string, unknown> {
  if (page === null) return extraction;
  const parsed = parseStructuredExtraction(extraction);
  const ids = new Set(parsed.pages.find(item => item.page_number === page)?.element_ids ?? []);
  const elements = parsed.elements.filter(item => item.page_number != null ? item.page_number === page : Boolean(item.element_id && ids.has(item.element_id)));
  const visibleIds = new Set(elements.flatMap(item => item.element_id ? [item.element_id] : []));
  const elementPages = new Map(parsed.elements.flatMap(item => item.element_id && item.page_number != null ? [[item.element_id, item.page_number] as const] : []));
  const tables = parsed.tables.map(table => {
    const tablePage = table.page_number ?? (table.element_id ? elementPages.get(table.element_id) : undefined);
    return {...table, cells: table.cells.filter(cell => (cell.page_number ?? tablePage) === page)};
  }).filter(table => table.cells.length > 0 || table.page_number === page || Boolean(table.element_id && visibleIds.has(table.element_id)));
  const text = [...elements].sort((a,b) => a.order-b.order).map(item => item.text).filter(Boolean).join("\n\n");
  return {...extraction, raw_text: text, display_text: text, elements, tables,
    pages: parsed.pages.filter(item => item.page_number === page),
    assets: parsed.assets.filter(item => item.page_number === page),
    fields: parsed.fields.filter(item => item.page_number === page)};
}

/** 跨頁 chunk は範囲で合わせ、ページ不明は同じページの要素に結び付く場合だけ出す。 */
export function chunksOnPage(chunks: DocumentChunkView[], extraction: Record<string, unknown>, page: number | null): DocumentChunkView[] {
  if (page === null) return chunks;
  const elements = parseStructuredExtraction(extractionOnPage(extraction, page)).elements;
  const ids = new Set(elements.flatMap(item => item.element_id ? [item.element_id] : []));
  return chunks.filter(chunk => {
    const start = chunk.page_start ?? chunk.page_end;
    const end = chunk.page_end ?? chunk.page_start;
    return start != null && end != null ? start <= page && page <= end : chunk.element_ids.some(id => ids.has(id));
  });
}
