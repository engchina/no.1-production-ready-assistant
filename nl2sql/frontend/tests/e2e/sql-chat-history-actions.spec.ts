import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { mockChatRequirement } from "./_helpers/chat-requirement";

/**
 * チャットの会話の履歴の「名前の変更」「削除」（3 製品共通の useChatHistoryActions。#1817）。
 * 行の操作は sm 以上でホバー・フォーカスの間だけ出す（タッチ端末は常に出す）ので、desktop と mobile-375 の
 * 2 project で確かめる。
 */

const profile = {
  id: "sales",
  name: "売上分析",
  description: "売上の集計",
  archived: false,
  allowed_tables: ["APP.SALES"],
  allowed_views: [],
  allowed_table_count: 1,
  allowed_view_count: 0,
  version: 1,
};

type Conversation = { id: string; title: string; profile_id: string; created_at: string };

async function setup(page: Page) {
  const conversations: Conversation[] = [
    { id: "chat-1", title: "今月の売上", profile_id: "sales", created_at: "2026-10-07T09:59:00Z" },
    { id: "chat-busy", title: "生成中の会話", profile_id: "sales", created_at: "2026-10-07T09:58:00Z" },
  ];
  const calls: string[] = [];
  await mockDatabaseGateReady(page);
  await mockChatRequirement(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/nl2sql/chats") {
      return route.fulfill({
        json: {
          data: { items: conversations, next_cursor: null, total: conversations.length, limit: 25 },
        },
      });
    }
    const id = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    const target = conversations.find((item) => item.id === id);
    if (!target) return route.fulfill({ status: 404, json: { error_messages: ["会話が見つかりません。"] } });
    calls.push(`${request.method()} ${id}`);
    if (request.method() === "PATCH") {
      target.title = String((request.postDataJSON() as { title: string }).title);
      return route.fulfill({ json: { data: target } });
    }
    if (request.method() === "DELETE") {
      if (id === "chat-busy") {
        return route.fulfill({
          status: 409,
          json: {
            error_messages: [
              "SQL の生成中の会話は削除できません。停止するか、生成が終わってから削除してください。",
            ],
          },
        });
      }
      conversations.splice(conversations.indexOf(target), 1);
      return route.fulfill({ json: { data: null } });
    }
    return route.fallback();
  });
  return calls;
}

async function openHistory(page: Page) {
  await page.goto("/chat");
  const toggle = page.getByTestId("sql-chat-history-toggle");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  const history = page.getByTestId("sql-chat-history");
  await expect(history.locator('[title="今月の売上"]')).toBeVisible();
  return history;
}

test.describe("会話の履歴の名前の変更・削除", { tag: "@responsive" }, () => {
  test("名前をその場で変え、空の名前は保存せず、Esc で取り消せる", async ({ page, isMobile }) => {
    const calls = await setup(page);
    const history = await openHistory(page);

    if (!isMobile) await history.getByRole("listitem").first().hover();
    await history.getByRole("button", { name: "「今月の売上」の名前を変更" }).click();
    const input = history.getByRole("textbox", { name: "会話名" });
    await expect(input).toBeFocused();
    await input.fill("   ");
    await input.press("Enter");
    await expect(history.getByText("会話名を入力してください。")).toBeVisible();
    expect(calls).toEqual([]);

    await input.fill("売上の確認");
    await input.press("Enter");
    await expect(history.locator('[title="売上の確認"]')).toBeVisible();
    expect(calls).toEqual(["PATCH chat-1"]);

    if (!isMobile) await history.getByRole("listitem").first().hover();
    await history.getByRole("button", { name: "「売上の確認」の名前を変更" }).click();
    await history.getByRole("textbox", { name: "会話名" }).fill("保存しない名前");
    await history.getByRole("textbox", { name: "会話名" }).press("Escape");
    await expect(history.locator('[title="売上の確認"]')).toBeVisible();
    expect(calls).toEqual(["PATCH chat-1"]);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("削除は確認のダイアログを通し、生成中の会話は理由を出して残す", async ({ page, isMobile }) => {
    await setup(page);
    const history = await openHistory(page);

    if (!isMobile) await history.getByRole("listitem").first().hover();
    const remove = history.getByRole("button", { name: "「今月の売上」を削除" });
    await remove.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("会話を削除しますか？");
    await expect(dialog).toContainText("「今月の売上」を会話の履歴から削除します。");
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(history.getByRole("listitem")).toHaveCount(2);

    if (!isMobile) await history.getByRole("listitem").first().hover();
    await remove.click();
    await dialog.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("会話を削除しました。")).toBeVisible();
    await expect(history.getByRole("listitem")).toHaveCount(1);

    if (!isMobile) await history.getByRole("listitem").first().hover();
    await history.getByRole("button", { name: "「生成中の会話」を削除" }).click();
    await dialog.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("SQL の生成中の会話は削除できません。", { exact: false })).toBeVisible();
    await expect(history.getByRole("listitem")).toHaveCount(1);
  });
});
