import { describe, expect, it } from "vitest";

import {
  RECIPE_CONFIG_FIELDS,
  RECIPE_CONFIG_ITEMS,
  globalSettingsHref,
  preprocessSelectionPatch,
  recipeConfigGroups,
} from "./DocumentProcessingConfigPanel.logic";
import {
  PARSER_OPTIONS,
  PARSER_VALUES,
  PREPROCESS_OPTIONS,
} from "./DocumentProcessingConfigPanel.values";
import type { DocumentProcessingConfig } from "@/lib/api";
import { t } from "@/lib/i18n";

// #523: 要約と上書きの一覧は同じ項目を同じ処理順で出す。
describe("選択中レシピの設定の項目と並び順", () => {
  it("取込の処理順に、3 つの自動進行のゲートを工程の間に置く", () => {
    expect(RECIPE_CONFIG_ITEMS.map((item) => t(item.label))).toEqual([
      "ファイル準備",
      "ファイル準備後に抽出へ進む",
      "文書解析",
      "図・画像を AI で読み取る",
      "見出しの階層の推定（AI）",
      "メタデータ/項目抽出",
      "章節の抽出規則",
      "章節の要約",
      "抽出後に Chunk 作成へ進む",
      "文書分割",
      "Chunk 後に Embedding / 索引へ進む",
      "実体・別名索引",
      "文書構造索引の構築",
    ]);
  });

  it("上書きの件数の対象は項目の正本と同じで、重複しない", () => {
    expect(RECIPE_CONFIG_FIELDS).toEqual(RECIPE_CONFIG_ITEMS.map((item) => item.field));
    expect(new Set(RECIPE_CONFIG_FIELDS).size).toBe(RECIPE_CONFIG_FIELDS.length);
    // 文脈ヘッダは全体の環境設定だけで決める。
    expect(RECIPE_CONFIG_FIELDS).not.toContain("chunk_context_header_enabled");
  });

  it("工程ごとに分けても、つなげると元の並びに戻る", () => {
    const groups = recipeConfigGroups();
    expect(groups.map((group) => [group.index, t(group.label)])).toEqual([
      [1, "ファイル準備"],
      [2, "抽出"],
      [3, "Chunk 作成"],
      [4, "Embedding / 索引"],
    ]);
    expect(groups.flatMap((group) => group.items)).toEqual([...RECIPE_CONFIG_ITEMS]);
    // 各ゲートは、その前の工程の最後に置く。
    expect(groups.map((group) => group.items.at(-1)?.field)).toEqual([
      "auto_parse_after_preprocess_enabled",
      "auto_chunk_after_extract_enabled",
      "auto_index_after_chunk_enabled",
      "graph_profile",
    ]);
  });

  it("項目の無い工程は見出しを出さない", () => {
    const groups = recipeConfigGroups(
      RECIPE_CONFIG_ITEMS.filter((item) => item.phase !== "INDEX")
    );
    expect(groups.map((group) => group.phase)).toEqual(["PREPROCESS", "EXTRACT", "CHUNK"]);
  });
});

// #528: 「グローバル設定に従う」の各行から、その全体の既定を変える画面へ移動する。
describe("グローバル設定を開くリンク", () => {
  it("すべての項目それぞれに、全体の既定を変える画面がある", () => {
    expect(
      Object.fromEntries(RECIPE_CONFIG_ITEMS.map((item) => [item.field, globalSettingsHref(item)]))
    ).toEqual({
      preprocess_profile: "/settings/preprocess",
      auto_parse_after_preprocess_enabled: "/settings/pipeline#pipeline-gate-auto-parse",
      parser_adapter_backend: "/settings/parser-adapters",
      vision_enabled: "/settings/parser-adapters#post-parse-vision",
      heading_level_inference_enabled: "/settings/parser-adapters#post-parse-heading-levels",
      field_extraction_enabled: "/settings/parser-adapters#post-parse-field-extraction",
      navigation_summary_enabled: "/settings/parser-adapters#post-parse-navigation-summary",
      section_rules_mode: "/settings/parser-adapters#post-parse-section-rules",
      auto_chunk_after_extract_enabled: "/settings/pipeline#pipeline-gate-auto-chunk",
      chunking_strategy: "/settings/chunking",
      auto_index_after_chunk_enabled: "/settings/pipeline#pipeline-gate-auto-index",
      // 実体・別名索引も全体の既定を設定画面で選ぶ。
      entity_index_enabled: "/settings/entity-index",
      graph_profile: "/settings/graph",
    });
  });
});

// #2457: 構造化レコードの JSONL を作るファイル準備と、その分割の初期値。
describe("CSV→JSONL・Excel→JSONL の選択", () => {
  const empty = {} as DocumentProcessingConfig;

  it("ファイル準備の並びは CSV→JSONL・Excel→JSONL・Word→JSONL の順", () => {
    const labels = PREPROCESS_OPTIONS.map((option) => option.label);
    expect(labels.indexOf("CSV→JSONL")).toBeGreaterThan(-1);
    expect(labels.indexOf("Excel→JSONL")).toBe(labels.indexOf("CSV→JSONL") + 1);
    expect(labels.indexOf("Word→JSONL")).toBe(labels.indexOf("Excel→JSONL") + 1);
  });

  it("解析エンジンの選択肢は Docling・Unstructured だけで、すべて選べる（#22・#21）", () => {
    expect(PARSER_OPTIONS.filter((option) => option.disabled)).toEqual([]);
    expect(PARSER_OPTIONS.map((option) => option.value)).toEqual(["docling", "unstructured"]);
    expect([...PARSER_VALUES]).toEqual(["docling", "unstructured"]);
  });

  it("新しいレシピで選ぶと、固定分割・境界のマーカーを明示の上書きにする（解析エンジンは変えない）", () => {
    for (const profile of ["csv_to_jsonl", "excel_to_jsonl", "word_to_jsonl"] as const) {
      expect(preprocessSelectionPatch(empty, profile)).toEqual({
        preprocess_profile: profile,
        chunking_strategy: "fixed_delimiter",
        chunk_delimiter: "<<<JSONL_RECORD_BOUNDARY>>>",
      });
    }
  });

  it("利用者が選んだ解析エンジン・分割方式・分割符は書き換えない", () => {
    expect(
      preprocessSelectionPatch(
        { chunking_strategy: "structure_aware", parser_adapter_backend: "docling" } as DocumentProcessingConfig,
        "csv_to_jsonl"
      )
    ).toEqual({ preprocess_profile: "csv_to_jsonl" });
    expect(
      preprocessSelectionPatch(
        { chunking_strategy: "fixed_delimiter", chunk_delimiter: "---" } as DocumentProcessingConfig,
        "excel_to_jsonl"
      )
    ).toEqual({ preprocess_profile: "excel_to_jsonl" });
  });

  it("ほかのファイル準備は値だけを変える", () => {
    expect(preprocessSelectionPatch(empty, "office_to_pdf")).toEqual({
      preprocess_profile: "office_to_pdf",
    });
    expect(preprocessSelectionPatch(empty, null)).toEqual({ preprocess_profile: null });
  });
});
