import { describe, expect, it } from "vitest";

import type { ExtractionFieldDefinition } from "@/lib/api";

import {
  extractionFieldConditions,
  extractionFieldFilterRowsFromConditions,
  extractionFieldFilterValue,
} from "./extraction-field-filters";
import {
  EMPTY_CLASSIFICATION_FILTERS,
  hasSearchPrefillParams,
  searchPrefillFromParams,
  searchPrefillHref,
} from "./search-prefill";

const FIELDS: ExtractionFieldDefinition[] = [
  { name: "項目 A", description: "", value_type: "string" },
  { name: "項目 B", description: "", value_type: "number" },
];

const CONDITIONS = [
  { name: "項目 A", value_type: "string" as const, op: "eq" as const, value: "値 A" },
  { name: "項目 B", value_type: "number" as const, op: "gte" as const, value: "10" },
  { name: "項目 B", value_type: "number" as const, op: "lte" as const, value: "20" },
];

function paramsOf(href: string) {
  const [path, query] = href.split("?");
  return { path, params: new URLSearchParams(query) };
}

describe("RAG 検索の画面に入れる初期値（Issue 3309）", () => {
  it("記録の質問・プロファイル・回答の生成・絞り込みを URL で往復する（質問は書き換えない）", () => {
    const question = "  合成の質問 A？\n2 行目  ";
    const href = searchPrefillHref({
      question,
      searchAnswerProfileId: "profile-a",
      generateAnswer: true,
      filters: {
        large_category: "分類 A",
        as_of: "2026-04-01",
        extraction_fields: extractionFieldFilterValue(CONDITIONS),
        // 画面が送れない条件は URL に入れない。
        knowledge_base_id: "kb-a",
      },
    });
    const { path, params } = paramsOf(href);
    expect(path).toBe("/search");
    expect(params.has("knowledge_base_id")).toBe(false);
    expect(hasSearchPrefillParams(params)).toBe(true);

    const prefill = searchPrefillFromParams(params);
    expect(prefill).toMatchObject({
      question,
      searchAnswerProfileId: "profile-a",
      generateAnswer: true,
      classification: { ...EMPTY_CLASSIFICATION_FILTERS, large_category: "分類 A", as_of: "2026-04-01" },
    });
    // 抽出項目の条件は編集の行に戻り、画面が作り直す条件は記録と同じになる。
    expect(prefill?.extractionRows.map(({ name, value, min, max }) => ({ name, value, min, max }))).toEqual([
      { name: "項目 A", value: "値 A", min: "", max: "" },
      { name: "項目 B", value: "", min: "10", max: "20" },
    ]);
    expect(extractionFieldConditions(prefill?.extractionRows ?? [], FIELDS).conditions).toEqual(CONDITIONS);
  });

  it("記録に無い条件は既定に戻し、前の作業の条件を混ぜない", () => {
    const href = searchPrefillHref({ question: "q", searchAnswerProfileId: null, generateAnswer: false });
    const { params } = paramsOf(href);
    // 既定の件数・空の絞り込み・プロファイルの無い実行は URL に入れない。
    expect([...params.keys()]).toEqual(["question", "generate_answer"]);
    expect(searchPrefillFromParams(params)).toEqual({
      question: "q",
      searchAnswerProfileId: null,
      generateAnswer: false,
      classification: EMPTY_CLASSIFICATION_FILTERS,
      extractionRows: [],
    });
  });

  it("質問の無い URL・選べない件数・壊れた条件は初期値にしない", () => {
    expect(searchPrefillFromParams(new URLSearchParams("search_answer_profile_id=p"))).toBeNull();
    expect(searchPrefillFromParams(new URLSearchParams("question=%20"))).toBeNull();
    expect(hasSearchPrefillParams(new URLSearchParams("other=1"))).toBe(false);
    const prefill = searchPrefillFromParams(
      new URLSearchParams({ question: "q", top_k: "7", extraction_fields: "{broken" })
    );
    expect(prefill).not.toHaveProperty("topK");
    expect(prefill?.extractionRows).toEqual([]);
    expect(searchPrefillHref({ question: "q", searchAnswerProfileId: null, generateAnswer: false })).not.toContain(
      "top_k"
    );
  });

  it("形の違う条件は行に戻さず、行は画面の上限まで", () => {
    const many = Array.from({ length: 7 }, (_, index) => ({ name: `項目 ${index}`, op: "eq", value: "v" }));
    expect(extractionFieldFilterRowsFromConditions([null, { name: "x", op: "like", value: "v" }, ...many])).toHaveLength(5);
  });
});
