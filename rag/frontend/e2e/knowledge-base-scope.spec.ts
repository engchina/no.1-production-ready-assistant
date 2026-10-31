import { expect, type Page, test } from "./fixtures/test";
import { closeFileListFilters, expectNoPageOverflow, mockDatabaseReady, mockLocalAuth, openFileListFilters, selectSearchAnswerProfile } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockKnowledgeBases(page);
  await mockSearchAnswerProfiles(page);
});

test("狭い画面幅(375px)でも検索ページがページを横スクロール(崩れ)させない", async ({ page }) => {
  await page.route("**/api/search/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody(),
    });
  });

  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/search");

  await expect(page.getByRole("textbox", { name: "RAG 検索" })).toBeVisible();
  // 検索前(結果カラムが空)の初期表示で横はみ出しが無いこと。
  await expectNoPageOverflow(page);

  // 検索・回答プロファイルを選択してから検索する(検索は search answer profile 選択が前提)。
  await selectSearchAnswerProfile(page, /経理ビュー/);

  // 検索実行後(結果・引用カードが出た状態)でも横はみ出しが無いこと。
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("経費申請の承認フロー");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expectNoPageOverflow(page);
});

test("文書インデックスはナレッジベースで絞り込み、所属を表示する", { tag: "@responsive" }, async ({ page }) => {
  let lastKnowledgeBaseId: string | null = null;
  await page.route("**/api/documents**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/documents") {
      await route.fulfill({ status: 404, json: { detail: "not found" } });
      return;
    }
    const knowledgeBaseId = url.searchParams.get("knowledge_base_id");
    // 絞り込み中の件数の「全 M 件」を数える 1 件の取得（limit=1。#1891）は一覧の取得ではないので記録しない。
    if (url.searchParams.get("limit") !== "1") lastKnowledgeBaseId = knowledgeBaseId;
    await route.fulfill({
      json: {
        data: {
          items: documents(knowledgeBaseId),
          total: documents(knowledgeBaseId).length,
          limit: Number(url.searchParams.get("limit") ?? 20),
          offset: Number(url.searchParams.get("offset") ?? 0),
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/file-list");
  await openFileListFilters(page);

  // 検索できる単一選択（#578）: ボタンを押すと検索欄と候補の一覧が開く。
  const filter = page.getByRole("button", { name: /^ナレッジベース/ });
  await filter.click();
  await page.getByRole("listbox", { name: "ナレッジベース" }).getByRole("option", { name: /社内規程/ }).click();
  await expect(filter).toHaveAccessibleName("ナレッジベース 社内規程");
  await closeFileListFilters(page);

  await expect.poll(() => lastKnowledgeBaseId).toBe("kb-1");
  await expect(page.getByRole("link", { name: "policy.txt" })).toBeVisible();
  await expect(page.locator("tr").filter({ hasText: "policy.txt" }).getByText("社内規程")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

async function mockSearchAnswerProfiles(page: Page) {
  await page.route("**/api/search-answer-profiles**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "bv-1",
              name: "経理ビュー",
              description: null,
              status: "ACTIVE",
              knowledge_base_count: 1,
              created_at: "2026-06-19T00:00:00Z",
              updated_at: "2026-06-19T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

async function mockKnowledgeBases(page: Page) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "kb-1",
              name: "社内規程",
              description: "経費・人事・情報管理",
              status: "ACTIVE",
              default_search_mode: "hybrid",
              document_count: 3,
              indexed_document_count: 3,
              error_document_count: 0,
              searchable_chunk_count: 16,
              created_at: "2026-06-15T00:00:00Z",
              updated_at: "2026-06-15T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

function documents(knowledgeBaseId: string | null) {
  const items = [
    {
      id: "doc-1",
      file_name: "policy.txt",
      status: "INDEXED",
      category_name: "規程",
      content_type: "text/plain",
      file_size_bytes: 120,
      content_sha256: "a".repeat(64),
      duplicate_of_document_id: null,
      uploaded_at: "2026-06-15T00:00:00Z",
      indexed_at: "2026-06-15T00:01:00Z",
      knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
    },
    {
      id: "doc-2",
      file_name: "manual.txt",
      status: "UPLOADED",
      category_name: null,
      content_type: "text/plain",
      file_size_bytes: 64,
      content_sha256: "b".repeat(64),
      duplicate_of_document_id: null,
      uploaded_at: "2026-06-15T00:02:00Z",
      indexed_at: null,
      knowledge_bases: [],
    },
  ];
  return knowledgeBaseId ? items.filter((item) => item.knowledge_bases.some((kb) => kb.id === knowledgeBaseId)) : items;
}

function searchStreamBody(): string {
  return [
    `event: metadata\ndata: ${JSON.stringify({
      trace_id: "trace-kb",
      elapsed_ms: 10,
      guardrail_warnings: [],
      diagnostics: {},
    })}\n\n`,
    `event: delta\ndata: ${JSON.stringify({ text: "承認フローを確認しました。" })}\n\n`,
    `event: citations\ndata: ${JSON.stringify([])}\n\n`,
    `event: done\ndata: ${JSON.stringify({ trace_id: "trace-kb" })}\n\n`,
  ].join("");
}

async function expectNoHorizontalOverflow(page: Page) {
  await expectNoPageOverflow(page);
}
