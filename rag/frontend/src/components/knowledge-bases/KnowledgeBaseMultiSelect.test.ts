import { describe, expect, it } from "vitest";

import type { KnowledgeBaseSummary } from "@/lib/api";

import { knowledgeBaseOptions } from "./KnowledgeBaseMultiSelect";

function kb(name: string, documentCount: number): KnowledgeBaseSummary {
  return {
    id: `kb-${name}`,
    name,
    description: null,
    status: "ACTIVE",
    default_search_mode: "hybrid",
    document_count: documentCount,
    indexed_document_count: documentCount,
    error_document_count: 0,
    searchable_chunk_count: documentCount,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    archived_at: null,
  };
}

// 名前の並びは共通の compareNames（#2753）。
describe("knowledgeBaseOptions の並び", () => {
  it("全件を手元に持つときは、既定の KB を先頭に名前の順（記号 → 数字は数値の順 → 英字 → かな → 漢字）で並べる", () => {
    const items = [
      kb("漢字", 1),
      kb("かな", 0),
      kb("Beta", 9),
      kb("10_b", 2),
      kb("2_a", 0),
      kb("DEFAULT", 0),
      kb("alpha", 5),
      kb("_x", 0),
    ];
    const options = knowledgeBaseOptions(items, { local: true });
    expect(options.map((option) => option.label)).toEqual([
      "DEFAULT",
      "_x",
      "2_a",
      "10_b",
      "alpha",
      "Beta",
      "かな",
      "漢字",
    ]);
    // 文書の最も多い KB の印は、並びに依らず付ける。
    expect(options.find((option) => option.badge)?.label).toBe("Beta");
  });

  it("サーバー側の検索のときは、サーバーの順のまま並べる", () => {
    const items = [kb("b", 0), kb("a", 0)];
    expect(knowledgeBaseOptions(items, { local: false }).map((option) => option.label)).toEqual(["b", "a"]);
  });
});
