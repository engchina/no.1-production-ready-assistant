import { expect, type Page, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth, openChatHistory } from "./_helpers";

/**
 * チャットの会話の履歴の期間（3 製品の 4 つのチャットで同じ。#2340）。`GET /api/chat/conversations` は最後の更新が
 * 直近 N 日の会話だけを返し、`history_days` を付ける。見出しの横（lg 未満はシートの先頭）に「最近 N 日」を出し、
 * 0 件は期間を含めた文にする。desktop と mobile-375 の 2 project で実行する。
 */

function envelope<T>(data: T) {
  return { data, error_messages: [], warning_messages: [] };
}

async function mockConversations(page: Page, { historyDays, empty = false }: { historyDays?: number; empty?: boolean }) {
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: envelope({
        items: [
          {
            id: "bv-1",
            name: "検索・回答プロファイル A",
            description: "",
            status: "ACTIVE",
            knowledge_base_count: 1,
            created_at: "2026-09-28T00:00:00Z",
            updated_at: "2026-09-28T00:00:00Z",
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_next: false,
      }),
    }),
  );
  await page.route("**/api/chat/models", (route) => route.fulfill({ json: envelope([]) }));
  await page.route("**/api/chat/conversations**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET" || url.pathname !== "/api/chat/conversations") {
      await route.fulfill({ status: 404, json: { detail: "not found" } });
      return;
    }
    const items = empty
      ? []
      : [
          {
            id: "conv-1",
            search_answer_profile_id: "bv-1",
            title: "期間の中の会話",
            status: "ACTIVE",
            message_count: 2,
            created_at: "2026-10-17T00:00:00Z",
            updated_at: "2026-10-17T00:00:00Z",
          },
        ];
    await route.fulfill({
      json: envelope({
        items,
        total: items.length,
        limit: 25,
        offset: 0,
        has_next: false,
        ...(historyDays === undefined ? {} : { history_days: historyDays }),
      }),
    });
  });
}

test.describe("会話の履歴の期間", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await mockDatabaseReady(page);
    await mockLocalAuth(page);
  });

  test("見出しの横に「最近 90 日」を出し、横にはみ出さない", async ({ page }, testInfo) => {
    await mockConversations(page, { historyDays: 90 });
    await page.goto("/chat?search_answer_profile_id=bv-1");
    const history = await openChatHistory(page);
    await expect(history.getByRole("button", { name: /^期間の中の会話/ })).toBeVisible();
    await expect(page.getByTestId("chat-history-caption")).toHaveText("最近 90 日");
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("history-window.png") });
  });

  test("期間の中に会話が無いときは期間を含めた文を出す", async ({ page }) => {
    await mockConversations(page, { historyDays: 30, empty: true });
    await page.goto("/chat?search_answer_profile_id=bv-1");
    const history = await openChatHistory(page);
    await expect(history.getByText("最近 30 日の会話はありません")).toBeVisible();
    await expect(page.getByTestId("chat-history-caption")).toHaveText("最近 30 日");
  });

  test("期間で絞らない（history_days が無い）ときは補足を出さない", async ({ page }) => {
    await mockConversations(page, { empty: true });
    await page.goto("/chat?search_answer_profile_id=bv-1");
    const history = await openChatHistory(page);
    await expect(history.getByText("まだ会話がありません。", { exact: false })).toBeVisible();
    await expect(page.getByTestId("chat-history-caption")).toHaveCount(0);
  });
});
