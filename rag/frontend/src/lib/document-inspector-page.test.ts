import { describe, expect, it } from "vitest";
import { chunksOnPage, extractionOnPage } from "./document-inspector-page";
import type { DocumentChunkView } from "./api";

const extraction = { raw_text: "全体の本文", elements: [
  {element_id: "one", text: "一頁の本文", kind: "text", order: 0, page_number: 1},
  {element_id: "two", text: "二頁の本文", kind: "text", order: 1, page_number: 2},
  {element_id: "mapped", text: "対応表の本文", kind: "text", order: 2},
  {element_id: "unknown", text: "ページ不明の本文", kind: "text", order: 3},
], pages: [{page_number: 1, element_ids: ["one", "mapped"]}, {page_number: 2, element_ids: ["two"]}],
 tables: [{table_id: "table", page_number: 1, cells: [
   {row: 0, col: 0, text: "一頁セル", row_span: 1, col_span: 1},
   {row: 1, col: 0, text: "二頁セル", row_span: 1, col_span: 1, page_number: 2},
 ]}], assets: [{asset_id: "a", kind: "figure", page_number: 2}],
 fields: [{name: "a", value: "v", value_type: "string", page_number: 1}], navigation: [],
};
const chunk = (id: string, start: number | null, end: number | null, ids: string[] = []) => ({
 document_id: "doc", chunk_id: id, chunk_index: 0, text: id, page_start: start, page_end: end,
 bbox: null, section_path: null, content_kind: "text", chunk_group_id: null, source_parser: null, element_ids: ids, metadata: {},
} satisfies DocumentChunkView);

describe("表示中のページだけを検査する", () => {
 it("本文と要素をページ対応から選び、他のページと不明の本文を混ぜない", () => {
   const first = extractionOnPage(extraction, 1);
   expect(first.display_text).toBe("一頁の本文\n\n対応表の本文");
   expect(extractionOnPage(extraction, 2).display_text).toBe("二頁の本文");
   expect(extractionOnPage(extraction, 3).display_text).toBe("");
   expect(extraction.raw_text).toBe("全体の本文");
 });
 it("跨頁の表セルを各ページへ分け、図と項目もページで選ぶ", () => {
   const second = extractionOnPage(extraction, 2);
   expect((second.tables as {cells: {text: string}[]}[])[0].cells.map(cell => cell.text)).toEqual(["二頁セル"]);
   expect(second.assets).toHaveLength(1); expect(second.fields).toHaveLength(0);
 });
 it("跨頁 chunk と要素由来のページを扱い、不明を全ページへ割り当てない", () => {
   const chunks = [chunk("single", 1, null), chunk("span", 1, 3), chunk("linked", null, null, ["two"]), chunk("unknown", null, null)];
   expect(chunksOnPage(chunks, extraction, 2).map(item => item.chunk_id)).toEqual(["span", "linked"]);
   expect(chunksOnPage(chunks, extraction, 4)).toEqual([]);
   expect(chunksOnPage(chunks, extraction, null)).toBe(chunks);
 });
 it("ページのない表示では元の抽出結果を保つ", () => expect(extractionOnPage(extraction, null)).toBe(extraction));
});
