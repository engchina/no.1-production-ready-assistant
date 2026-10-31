import { describe, expect, it } from "vitest";

import {
  CHUNK_SIZE_MAX_CHARS,
  DEFAULT_SMALL_TO_BIG_PARAMS,
  smallToBigFellBack,
  invalidSmallToBigParam,
  chunkSizeLabelKey,
  chunkingStrategyPreset,
  isSemanticBoundaryStrategy,
  overlapLabelKey,
} from "./chunking";

describe("chunking strategy presentation", () => {
  it.each(["markdown_heading", "page_level"] as const)(
    "%s は意味境界向けの大きな再分割上限を使う",
    (strategy) => {
      expect(isSemanticBoundaryStrategy(strategy)).toBe(true);
      expect(chunkingStrategyPreset(strategy)).toEqual({
        chunkSize: CHUNK_SIZE_MAX_CHARS,
        overlap: 0,
      });
      expect(overlapLabelKey(strategy)).toBe("settings.chunking.params.semanticOverlap");
    }
  );

  it("通常戦略は 800/120 を使う", () => {
    expect(chunkingStrategyPreset("structure_aware")).toEqual({
      chunkSize: 800,
      overlap: 120,
    });
    expect(chunkSizeLabelKey("structure_aware")).toBe(
      "settings.chunking.params.chunkSize"
    );
  });

  it("見出しとページで再分割上限のラベルを分ける", () => {
    expect(chunkSizeLabelKey("markdown_heading")).toBe(
      "settings.chunking.params.headingSplitLimit"
    );
    expect(chunkSizeLabelKey("page_level")).toBe("settings.chunking.params.pageSplitLimit");
  });
});

describe("親子階層（small-to-big）のパラメータ", () => {
  it("rag_poc と同じ既定値を持つ", () => {
    expect(DEFAULT_SMALL_TO_BIG_PARAMS).toEqual({
      chunk_child_target_chars: 1000,
      chunk_table_child_target_chars: 3000,
      chunk_parent_target_chars: 6000,
      chunk_parent_max_pages: 3,
      chunk_parent_max_children: 12,
    });
    expect(invalidSmallToBigParam(DEFAULT_SMALL_TO_BIG_PARAMS)).toBeNull();
  });

  it("範囲外・小数・未入力の項目を返す", () => {
    expect(
      invalidSmallToBigParam({
        ...DEFAULT_SMALL_TO_BIG_PARAMS,
        chunk_table_child_target_chars: 8001,
      })?.field
    ).toBe("chunk_table_child_target_chars");
    expect(
      invalidSmallToBigParam({ ...DEFAULT_SMALL_TO_BIG_PARAMS, chunk_parent_max_pages: 1.5 })
        ?.field
    ).toBe("chunk_parent_max_pages");
    expect(
      invalidSmallToBigParam({ ...DEFAULT_SMALL_TO_BIG_PARAMS, chunk_child_target_chars: null })
        ?.field
    ).toBe("chunk_child_target_chars");
  });
});

describe("smallToBigFellBack", () => {
  it("backend が縮退の印を付けた chunk があれば真", () => {
    expect(
      smallToBigFellBack([
        { metadata: { chunk_strategy: "structure_aware" } },
        {
          metadata: {
            chunk_strategy: "structure_aware",
            chunk_strategy_requested: "small_to_big",
            chunk_strategy_fallback_reason: "layout_missing",
          },
        },
      ])
    ).toBe(true);
  });

  it("親子階層（small-to-big）で分割した chunk や空の一覧では偽", () => {
    expect(smallToBigFellBack([{ metadata: { chunk_strategy: "small_to_big" } }])).toBe(
      false
    );
    expect(smallToBigFellBack([])).toBe(false);
  });
});
