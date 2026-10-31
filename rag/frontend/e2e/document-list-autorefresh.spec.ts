import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";

type FileStatus = "UPLOADED" | "INGESTING" | "INDEXING" | "REVIEW" | "INDEXED" | "ERROR";

interface DocumentSummary {
  id: string;
  file_name: string;
  status: FileStatus;
  category_name: string | null;
  content_type: string | null;
  file_size_bytes: number | null;
  content_sha256: string | null;
  duplicate_of_document_id: string | null;
  uploaded_at: string;
  indexed_at: string | null;
  knowledge_bases: { id: string; name: string }[];
  source_profile: null;
  ingestion_active?: boolean;
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

async function mockKnowledgeBases(page: Page) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [{ id: "kb-default", name: "既定", document_count: 2 }],
          total: 1,
          limit: 100,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

/** 文書の一覧の GET だけを `items` で返し、呼ばれた回数を数える。 */
async function mockDocumentList(
  page: Page,
  items: (call: number) => DocumentSummary[],
  options: { delayMs?: (call: number) => number } = {}
) {
  const calls = { count: 0 };
  await page.route("**/api/documents**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/documents") {
      calls.count += 1;
      const call = calls.count;
      const delay = options.delayMs?.(call) ?? 0;
      if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
      const rows = items(call);
      await route.fulfill({
        json: {
          data: { items: rows, total: rows.length, limit: 25, offset: 0, has_next: false },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fallback();
  });
  return calls;
}

// 取込中の文書がある間は自動でポーリングし、手動リロードなしで状態バッジが更新される。
test("取込中の文書がポーリングで索引済みに自動遷移する", async ({ page }) => {
  let listCalls = 0;
  // 初回 GET は取込中、3 回目以降は索引済みを返す(backend 側の進行を模擬)。
  const statusFor = (): FileStatus => (listCalls >= 3 ? "INDEXED" : "INGESTING");

  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [{ id: "kb-default", name: "既定", document_count: 2 }],
          total: 1,
          limit: 100,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.route("**/api/documents**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/documents") {
      listCalls += 1;
      await route.fulfill({
        json: {
          data: {
            items: [
              documentSummary("doc-1", "report.pdf", statusFor()),
              documentSummary("doc-error", "failed.pdf", "ERROR"),
            ],
            total: 2,
            limit: 20,
            offset: 0,
            has_next: false,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fallback();
  });

  await page.goto("/file-list");

  const row = page.locator("tbody tr").filter({ hasText: "report.pdf" });
  await expect(row).toContainText("解析（抽出）中");
  const errorRow = page.locator("tbody tr").filter({ hasText: "failed.pdf" });
  // 行の操作は RowActionMenu にまとまっている（#131）。失敗行のメニューに再実行が出る。
  await errorRow.getByRole("button", { name: "failed.pdf の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "ファイル準備を再実行" })).toBeVisible();
  await page.keyboard.press("Escape");

  // クリックやリロードなしで、ポーリングにより索引済みへ更新される。
  await expect(row).toContainText("索引済み", { timeout: 15000 });
});

// 一覧の状態はレシピ1の状態（詳細と同じ正本。#2012）。投入直後の job は worker が拾うまで状態が
// 「ファイル準備待ち」のままなので、待機中の job（ingestion_active）がある間も取り直し、
// レシピが「抽出確認待ち」に進んだら一覧にも出す。処理中の行が無くなれば取り直しを止める。
test("待機中の job がある間は取り直し、状態が進んだら一覧に出して取り直しを止める", async ({ page }) => {
  await mockKnowledgeBases(page);
  const calls = await mockDocumentList(page, (call) =>
    call >= 3
      ? [documentSummary("doc-1", "document-a-01.pdf", "REVIEW")]
      : [{ ...documentSummary("doc-1", "document-a-01.pdf", "UPLOADED"), ingestion_active: true }]
  );

  await page.goto("/file-list");

  const row = page.locator("tbody tr").filter({ hasText: "document-a-01.pdf" });
  await expect(row).toContainText("ファイル準備待ち");
  // 待機中の job がある行には、二重に投入する「取込」を出さない。
  await row.getByRole("button", { name: "document-a-01.pdf の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "ファイル準備を実行" })).toHaveCount(0);
  await expect(page.getByRole("menuitem").first()).toBeVisible();
  await page.keyboard.press("Escape");

  await expect(row).toContainText("抽出確認待ち", { timeout: 15000 });
  // 処理中の行が無くなったら取り直しを止める（取り直しの間隔は 4 秒）。
  const settled = calls.count;
  await page.waitForTimeout(5000);
  expect(calls.count).toBe(settled);
});

// 「最新情報を取得」（PageHeader の utility）で、処理中の行が無くても一覧を取り直せる。
// 押した取り直しの間だけボタンが loading になり、前の一覧は出したまま（messaging.md §3.6）。
// 狭い画面ではヘッダーの操作を「その他の操作」にまとめるため、375px でもボタンが出ることを確かめる。
test("「最新情報を取得」で一覧を取り直し、取り直しの間だけボタンが loading になる", { tag: "@responsive" }, async ({ page }) => {
  await mockKnowledgeBases(page);
  const calls = await mockDocumentList(
    page,
    (call) => [documentSummary("doc-1", "document-a-01.pdf", call >= 2 ? "REVIEW" : "UPLOADED")],
    { delayMs: (call) => (call >= 2 ? 1200 : 0) }
  );

  await page.goto("/file-list");

  const row = page.locator("tbody tr").filter({ hasText: "document-a-01.pdf" });
  await expect(row).toContainText("ファイル準備待ち");
  expect(calls.count).toBe(1);

  const refresh = page.getByRole("button", { name: "最新情報を取得" });
  await expect(refresh).toBeVisible();
  await refresh.click();
  await expect(refresh).toHaveAttribute("aria-busy", "true");
  // 取り直しの間も前の一覧を出したまま。
  await expect(row).toContainText("ファイル準備待ち");
  await expect(row).toContainText("抽出確認待ち");
  await expect(refresh).not.toHaveAttribute("aria-busy", "true");
  expect(calls.count).toBe(2);
});

function documentSummary(id: string, fileName: string, status: FileStatus): DocumentSummary {
  return {
    id,
    file_name: fileName,
    status,
    category_name: "社内規程",
    content_type: "application/pdf",
    file_size_bytes: 2048,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: null,
    uploaded_at: "2026-06-16T09:00:00Z",
    indexed_at: status === "INDEXED" ? "2026-06-16T09:05:00Z" : null,
    knowledge_bases: [{ id: "kb-default", name: "既定" }],
    source_profile: null,
  };
}
