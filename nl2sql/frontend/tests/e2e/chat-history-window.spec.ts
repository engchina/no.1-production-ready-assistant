import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { mockChatRequirement } from "./_helpers/chat-requirement";

/**
 * チャットの会話の履歴の期間（3 製品の 4 つのチャットで同じ。#2340）。会話の一覧の API は最後の更新が直近 N 日の
 * 会話だけを返し、`history_days` を付ける。見出しの横（lg 未満はシートの先頭）に「最近 N 日」を出し、0 件は
 * 期間を含めた文にする。「チャット」と「DB 管理チャット」で同じ。desktop と mobile-375 の 2 project で実行する。
 */

const profile = {
  id: "sales",
  name: "業務プロファイル A",
  description: "",
  archived: false,
  allowed_tables: ["APP.SALES"],
  allowed_views: [],
  allowed_table_count: 1,
  allowed_view_count: 0,
  version: 1,
};

function listPage(items: unknown[], historyDays: number | undefined) {
  return {
    data: {
      items,
      total: items.length,
      limit: 25,
      offset: 0,
      has_next: false,
      ...(historyDays === undefined ? {} : { history_days: historyDays }),
    },
  };
}

async function setup(
  page: Page,
  { historyDays, empty = false }: { historyDays?: number; empty?: boolean },
) {
  await mockDatabaseGateReady(page);
  await mockChatRequirement(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({
      json: { data: { items: [profile], total: 1, next_cursor: null } },
    }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  const item = {
    id: "chat-1",
    title: "期間の中の会話",
    profile_id: "sales",
    created_at: "2026-10-17T09:30:00Z",
  };
  await page.route("**/api/nl2sql/chats**", (route) => {
    if (new URL(route.request().url()).pathname !== "/api/nl2sql/chats")
      return route.fallback();
    return route.fulfill({ json: listPage(empty ? [] : [item], historyDays) });
  });
  await page.route("**/api/nl2sql/db-admin-chat/conversations**", (route) => {
    if (
      new URL(route.request().url()).pathname !==
      "/api/nl2sql/db-admin-chat/conversations"
    ) {
      return route.fallback();
    }
    const summary = {
      id: "admin-1",
      title: "期間の中の会話",
      created_at: "2026-10-17T09:30:00Z",
      owned: true,
    };
    return route.fulfill({
      json: listPage(empty ? [] : [summary], historyDays),
    });
  });
}

const screens = [
  { path: "/chat", prefix: "sql-chat" },
  { path: "/db-admin-chat", prefix: "db-admin-chat" },
] as const;

test.describe("会話の履歴の期間", { tag: "@responsive" }, () => {
  for (const { path, prefix } of screens) {
    test(`${path}: 見出しの横に「最近 90 日」を出し、横にはみ出さない`, async ({
      page,
    }, testInfo) => {
      await setup(page, { historyDays: 90 });
      await page.goto(path);
      const toggle = page.getByTestId(`${prefix}-history-toggle`);
      await expect(toggle).toBeEnabled();
      await toggle.click();
      const history = page.getByTestId(`${prefix}-history`);
      await expect(
        history.getByRole("button", { name: /^期間の中の会話/ }),
      ).toBeVisible();
      await expect(page.getByTestId(`${prefix}-history-caption`)).toHaveText(
        "最近 90 日",
      );
      const overflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
      await page.screenshot({
        path: testInfo.outputPath(`${prefix}-history-window.png`),
      });
    });

    test(`${path}: 期間の中に会話が無いときは期間を含めた文を出す`, async ({
      page,
    }) => {
      await setup(page, { historyDays: 30, empty: true });
      await page.goto(path);
      const toggle = page.getByTestId(`${prefix}-history-toggle`);
      await expect(toggle).toBeEnabled();
      await toggle.click();
      await expect(
        page
          .getByTestId(`${prefix}-history`)
          .getByText("最近 30 日の会話はありません"),
      ).toBeVisible();
      await expect(page.getByTestId(`${prefix}-history-caption`)).toHaveText(
        "最近 30 日",
      );
    });
  }

  test("期間で絞らない（history_days が無い）ときは補足を出さない", async ({
    page,
  }) => {
    await setup(page, { empty: true });
    await page.goto("/chat");
    await page.getByTestId("sql-chat-history-toggle").click();
    await expect(
      page.getByTestId("sql-chat-history").getByText("会話はまだありません"),
    ).toBeVisible();
    await expect(page.getByTestId("sql-chat-history-caption")).toHaveCount(0);
  });
});
