import { describe, expect, it } from "vitest";

import type { RetrievedChunk } from "@/lib/api";
import {
  citationEvidenceItem,
  variantIdFromChunkId,
} from "./CitationCard";

describe("variantIdFromChunkId", () => {
  it("document:chunk_set:index 形式から chunk_set(variant)id を取り出す", () => {
    expect(variantIdFromChunkId("doc-1:cs_abc123:0")).toBe("cs_abc123");
  });

  it("chunk_set タグの無い document:index は variant 無し(null)", () => {
    expect(variantIdFromChunkId("doc-1:7")).toBeNull();
  });
});

describe("citationEvidenceItem", () => {
  // 共通の根拠のカード（#2328）に渡すデータの契約。見た目は platform の EvidenceCard のテストで確かめる。
  it("引用の印・経路・頁のチップ・レシピ・分類・原典を写す", () => {
    const item = citationEvidenceItem({
      document_id: "doc-1",
      chunk_id: "doc-1:cs_a:3",
      text: "根拠",
      score: 0.91,
      rerank_score: 0.42,
      file_name: null,
      category_name: "分類 A",
      metadata: {
        evidence_model_used: true,
        evidence_answer_use: "reference",
        vector_rank: 1,
        keyword_rank: 2,
        page_start: 3,
        recipe_slot_no: 2,
        document_superseded: true,
        // 取込済みの文書に残った XML ツリー JSON の原典の URL は読まない（issue 42）。
        xml_tree_source_url: "https://example.com/a",
      },
    } satisfies RetrievedChunk);
    expect(item).toMatchObject({
      key: "doc-1:cs_a:3",
      fileName: "doc-1",
      usedInAnswer: true,
      answerUse: "reference",
      superseded: true,
      score: 0.91,
      rerankScore: 0.42,
    });
    expect(item.sourceUrl).toBeUndefined();
    expect(item.routeBadges).toEqual(["Both", "Vector #1", "Keyword #2", "Evidence"]);
    expect(item.chips).toEqual([{ id: "page", label: "ページ", value: "p.3" }, { id: "context_header_enabled", label: "文脈ヘッダの索引への追加", value: "未記録" }]);
    expect(item.tags?.map((tag) => [tag.id, tag.tone ?? "neutral"])).toEqual([
      ["recipe", "neutral"],
      ["category", "info"],
    ]);
  });
});

it.each([
  ["not_run", "内容の裏付け確認は未実施"],
  ["error", "内容確認で実行エラー・裏付け未確定"],
  ["timed_out", "内容確認が時間切れ・裏付け未確定"],
  ["completed", "内容確認実行済み・裏付け未確定"],
  ["partial", "内容確認は一部のみ・裏付け未確定"],
])("意味支持がunknownなら%sの実行状態を独立表示する", (execution, label) => {
  const item = citationEvidenceItem({ document_id: "d", chunk_id: "c", text: "原文", score: 1, rerank_score: null, file_name: null, category_name: null, metadata: { evidence_model_used: true, evidence_answer_use: "confirmed", evidence_claim_support: { scope: "answer", execution, status: "unknown", policy: "diagnostic_only" } } });
  expect(item.usedInAnswer).toBe(true);
  expect(item.answerUse).toBeNull();
  expect(item.tags?.map((tag) => tag.value)).toEqual(["内容の裏付け確認は診断のみ", label]);
});
