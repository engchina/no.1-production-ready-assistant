import { describe, expect, it } from "vitest";

import { matchCitationLine } from "./answer-text";
import type { RetrievedChunk } from "./api";
import {
  bboxCoordinateModeFromMetadata,
  bboxFromMetadata,
  bboxPageRotationFromMetadata,
  bboxPageSizeFromMetadata,
  bboxUnitFromMetadata,
  buildPreviewHighlights,
  displayRegionsFromMetadata,
} from "@production-ready/ui";
import { citationCountLabel, citationMetadataChips, citationModelUsed } from "./chunk-metadata";
import fixture from "./stored-citations.fixture.json" with { type: "json" };

// 回答の記録・会話の回答に保存する引用は、metadata を画面と評価が使う項目だけにする（#1371）。
// fixture の full は検索の結果のままの引用、stored は backend の stored_citation が作る保存の形
// （backend の tests/test_stored_answer.py が同じ fixture で照合する）。画面の表示に使う値が
// 両者で同じことを確かめる。
type Case = { name: string; full: RetrievedChunk; stored: RetrievedChunk };
const cases = (fixture as unknown as { cases: Case[] }).cases;

// 部品が metadata から直接読む項目（CitationCard の検索の順位の印・版の印、answer-text の表計算の行）。
const DIRECT_KEYS = [
  "recipe_slot_no",
  "document_superseded",
  "vector_rank",
  "keyword_rank",
  "rerank_rank",
  "context_role",
  "sheet_name",
  "row_start",
  "row_end",
  "page_number",
  "page",
  "section_title",
] as const;

function displayed(chunk: RetrievedChunk) {
  const metadata = chunk.metadata;
  const focusPage =
    typeof metadata.page_start === "number"
      ? metadata.page_start
      : typeof metadata.page === "number"
        ? metadata.page
        : null;
  return {
    text: chunk.text,
    fileName: chunk.file_name,
    score: chunk.score,
    rerankScore: chunk.rerank_score,
    chips: citationMetadataChips(metadata),
    modelUsed: citationModelUsed(chunk),
    highlights: buildPreviewHighlights({
      focusPage,
      focusBbox: bboxFromMetadata(metadata),
      focusBboxMode: bboxCoordinateModeFromMetadata(metadata),
      focusBboxUnit: bboxUnitFromMetadata(metadata),
      focusPageSize: bboxPageSizeFromMetadata(metadata),
      regions: displayRegionsFromMetadata(metadata),
    }),
    rotation: bboxPageRotationFromMetadata(metadata),
    direct: Object.fromEntries(DIRECT_KEYS.map((key) => [key, metadata[key] ?? null])),
  };
}

describe("保存の形の引用（issue 1371）", () => {
  it("fixture に検索の結果の典型（親子階層の子・表計算・rerank・出典行・前後の文脈・表のセル）がある", () => {
    expect(cases.map((item) => item.name)).toEqual([
      "pdf_small_to_big_child",
      "excel_rows",
      "reranked",
      "answer_citation_line",
      "neighbor_context",
      "synthetic_table_cell",
    ]);
  });

  it.each(cases.map((item) => [item.name, item] as const))(
    "%s: 引用カード・プレビュー・強調する領域の表示が同じ",
    (_name, item) => {
      expect(displayed(item.stored)).toEqual(displayed(item.full));
    }
  );

  it("親子階層の子は、要素の表示領域（engine_metadata_json から取り出したもの）で強調する", () => {
    const item = cases.find((entry) => entry.name === "pdf_small_to_big_child");
    const highlights = item ? displayed(item.stored).highlights : [];
    expect(highlights.length).toBeGreaterThan(0);
    expect(highlights.every((highlight) => highlight.label)).toBe(true);
  });

  it("保存の形は索引の内部の項目と親の本文を持たない", () => {
    for (const item of cases) {
      for (const key of [
        "parent_text",
        "engine_metadata_json",
        "engine_search_text",
        "source_record_refs_json",
      ]) {
        expect(item.stored.metadata).not.toHaveProperty(key);
      }
    }
  });

  it("件数の表示と本文の出典行の対応が同じ", () => {
    const full = cases.map((item) => item.full);
    const stored = cases.map((item) => item.stored);
    expect(citationCountLabel(stored)).toBe(citationCountLabel(full));
    for (const ordinal of [1, 2, 3]) {
      expect(matchCitationLine(ordinal, null, stored)).toBe(matchCitationLine(ordinal, null, full));
    }
    const sheet = cases.find((item) => item.name === "excel_rows")?.full;
    const ref = {
      fileName: sheet?.file_name ?? "",
      page: null,
      sheet: String(sheet?.metadata.sheet_name ?? ""),
      rowStart: Number(sheet?.metadata.row_start ?? 0),
      rowEnd: Number(sheet?.metadata.row_end ?? 0),
    };
    expect(matchCitationLine(undefined, ref, stored)).toBe(matchCitationLine(undefined, ref, full));
  });
});
