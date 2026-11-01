import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";

// #2462: 図の根拠（AI が図を読んだ説明）のカードと根拠のサイドパネルに、注記「AI が図を読んだ説明です。…」・
// 「図を開く」・「文書の詳細を別タブで開く」の帯を出さない（Agent と同じ）。サイドパネルは要約と原文のプレビューだけ。
// 内容はすべて合成のデータ。

const PROFILE = {
  id: "profile-a",
  name: "プロファイル A",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-10-18T00:00:00Z",
  updated_at: "2026-10-18T00:00:00Z",
  archived_at: null,
};

function citation(chunkId: string, fileName: string, metadata: Record<string, unknown>) {
  return {
    document_id: "doc-a",
    chunk_id: chunkId,
    text: `合成の本文（${chunkId}）。`,
    score: 0.5,
    rerank_score: 0.7,
    file_name: fileName,
    category_name: null,
    metadata: { context_role: "evidence", vector_rank: 1, ...metadata },
  };
}

const CITATIONS = [
  citation("doc-a:cs-1:1", "文書 A.pdf", {
    content_kind: "figure",
    figure_text_source: "vision",
    page_start: 2,
    section_path: "第 1 章",
  }),
  citation("doc-a:cs-1:2", "文書 B.pdf", { content_kind: "text", page_start: 1 }),
];

function searchStreamBody(): string {
  return [
    `event: metadata\ndata: ${JSON.stringify({
      trace_id: "trace-figure",
      elapsed_ms: 12,
      diagnostics: { retrieval_strategy: "hybrid", retrieval_strategy_adapter: "grounded" },
    })}\n\n`,
    `event: citations\ndata: ${JSON.stringify(CITATIONS)}\n\n`,
    `event: done\ndata: ${JSON.stringify({ trace_id: "trace-figure" })}\n\n`,
  ].join("");
}

async function search(page: Page) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [PROFILE], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: searchStreamBody() })
  );
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /プロファイル A/);
  await page.keyboard.press("Escape");
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("合成の質問");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect(page.getByTestId("evidence-card")).toHaveCount(2);
}

test("図の根拠のカードとサイドパネルに、図を開く・文書の詳細へのリンクの帯を出さない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await search(page);
  const card = page.getByTestId("evidence-card").nth(0);
  // カードの操作は「根拠を表示」だけ（注記・「図を開く」は出さない）。
  await expect(card.getByRole("button", { name: /図を開く/ })).toHaveCount(0);
  await expect(page.getByText("AI が図を読んだ説明です", { exact: false })).toHaveCount(0);
  const sourceButton = card.getByRole("button", { name: "文書 A.pdf の根拠を表示" });
  await expect(sourceButton).toBeVisible();
  await card.screenshot({ path: testInfo.outputPath(`rag-figure-card-${testInfo.project.name}.png`) });

  await sourceButton.click();
  const sheet = page.getByRole("dialog", { name: "検索で見つかった箇所" });
  const summary = sheet.getByTestId("evidence-source-summary");
  await expect(summary.getByTestId("evidence-file-name")).toHaveText("文書 A.pdf");
  // 要約は本文・タグ・スコアまでで、その下の操作の行（図を開く・文書の詳細へのリンク）は無い。
  // 抜粋と頁・章節のチップは既定で閉じ、「抜粋と頁・章節」で開く（Agent と同じ。#2502）。
  await expect(summary.getByTestId("citation-score-panel")).toBeVisible();
  await expect(summary.getByTestId("citation-text")).toBeHidden();
  await expect(summary.getByTestId("evidence-chips")).toBeHidden();
  await summary.getByTestId("evidence-excerpt-toggle").click();
  await expect(summary.getByTestId("citation-text")).toBeVisible();
  await expect(summary.getByTestId("evidence-chips")).toContainText("p.2");
  await expect(summary.getByRole("button", { name: /図を開く/ })).toHaveCount(0);
  await expect(sheet.getByRole("link", { name: "文書の詳細を別タブで開く" })).toHaveCount(0);
  await expect(sheet).not.toContainText("AI が図を読んだ説明です");
  // 要約の下は原文のプレビュー。
  await expect(sheet.getByTestId("evidence-source-preview")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`rag-figure-sheet-${testInfo.project.name}.png`) });
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(sourceButton).toBeFocused();
});
