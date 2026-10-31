import { describe, expect, it } from "vitest";

import { recipeConfigGroups } from "@/components/documents/DocumentProcessingConfigPanel.logic";
import { t } from "@/lib/i18n";

import { isAutoAdvanceItem, splitGateItems } from "./PipelineRecipeDefaults.logic";

// #528: 設定の概要は、レシピの項目の定義（#523）と同じ順で、工程の間に自動進行のゲートを置く。
describe("設定の概要の取込の流れ", () => {
  it("工程ごとに、項目と工程の最後のゲートに分ける", () => {
    const flow = recipeConfigGroups().map((group) => {
      const { items, gate } = splitGateItems(group.items);
      return {
        phase: t(group.label),
        items: items.map((item) => t(item.label)),
        gate: gate ? t(gate.label) : null,
      };
    });
    expect(flow).toEqual([
      { phase: "ファイル準備", items: ["ファイル準備"], gate: "ファイル準備後に抽出へ進む" },
      {
        phase: "抽出",
        items: [
          "文書解析",
          "図・画像を AI で読み取る",
          "見出しの階層の推定（AI）",
          "メタデータ/項目抽出",
          "章節の抽出規則",
          "章節の要約",
        ],
        gate: "抽出後に Chunk 作成へ進む",
      },
      {
        phase: "Chunk 作成",
        items: ["文書分割"],
        gate: "Chunk 後に Embedding / 索引へ進む",
      },
      { phase: "Embedding / 索引", items: ["実体・別名索引", "文書構造索引の構築"], gate: null },
    ]);
  });

  it("つなげると 13 項目の処理順に戻り、ゲートは 3 つ", () => {
    const groups = recipeConfigGroups();
    const flattened = groups.flatMap((group) => {
      const { items, gate } = splitGateItems(group.items);
      return gate ? [...items, gate] : items;
    });
    expect(flattened.map((item) => item.field)).toEqual(
      groups.flatMap((group) => group.items.map((item) => item.field))
    );
    expect(flattened).toHaveLength(13);
    expect(flattened.filter(isAutoAdvanceItem).map((item) => item.field)).toEqual([
      "auto_parse_after_preprocess_enabled",
      "auto_chunk_after_extract_enabled",
      "auto_index_after_chunk_enabled",
    ]);
  });

  it("ゲートが最後に無い工程は分けない", () => {
    const [extract] = recipeConfigGroups().filter((group) => group.phase === "EXTRACT");
    const reordered = [extract.items.at(-1)!, ...extract.items.slice(0, -1)];
    expect(splitGateItems(reordered)).toEqual({ items: reordered, gate: null });
  });
});
