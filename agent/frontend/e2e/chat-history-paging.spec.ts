import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

/**
 * チャットの会話の履歴のページング（3 製品共通。#1265）。`GET /api/threads` は limit / offset と全件数を返し、
 * 一覧の下に件数「a - b / n 件」と「前へ / N / M ページ / 次へ」を出す。ページは作業状態に残す。
 * desktop と mobile-375 の 2 project で実行する。
 */

function seedThreads(mockApi: MockApi, count: number) {
  for (let index = 0; index < count; index += 1) {
    mockApi.state.runs.push({
      id: `run-paging-${index + 1}`,
      goal: `会話 ${index + 1}`,
      agent_id: "default",
      runtime_id: "builtin",
      status: "completed",
      steps: [],
      events: [],
      approvals: [],
      artifacts: [],
      pending_tool_calls: [],
      metadata: {},
      created_by_user_uuid: "local",
      thread_id: `thread_${String(index + 1).padStart(32, "0")}`,
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    });
  }
}

test("会話の履歴は 25 件ずつ前へ / 次へで送り、再読込でも同じページに戻る", async ({ page, mockApi }, testInfo) => {
  // mock は新しい順（追加の逆順）に返すので、1 ページ目は「会話 29」〜「会話 5」、2 ページ目は「会話 4」〜「会話 1」。
  seedThreads(mockApi, 29);
  const requests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/threads") requests.push(url.search);
  });
  await page.goto("/chat");
  const toggle = page.getByTestId("chat-history-toggle");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  const history = page.getByTestId("chat-history");
  const pager = page.getByTestId("chat-threads-pagination");
  await expect(history.getByRole("button", { name: /^会話 \d+/ })).toHaveCount(25);
  await expect(pager).toContainText("1–25 / 29 件");
  await expect(pager).toContainText("1 / 2 ページ");
  await expect(pager.getByRole("button", { name: "前へ" })).toBeDisabled();
  expect(requests.at(-1)).toContain("limit=25");
  expect(requests.at(-1)).toContain("offset=0");

  await pager.getByRole("button", { name: "次へ" }).click();
  await expect(history.getByRole("button", { name: /^会話 \d+/ })).toHaveCount(4);
  await expect(history.locator('[title="会話 1"]')).toBeVisible();
  await expect(pager).toContainText("26–29 / 29 件");
  await expect(pager).toContainText("2 / 2 ページ");
  await expect(pager.getByRole("button", { name: "次へ" })).toBeDisabled();
  expect(requests.at(-1)).toContain("offset=25");
  // 狭い画面でも一覧とページ送りが横にはみ出さない。
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath("history-page-2.png") });

  // ページは作業状態に残す（再読込で同じページ）。lg 未満のシートは閉じて戻るので開き直す。
  await page.reload();
  await expect(toggle).toBeEnabled();
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(pager).toContainText("2 / 2 ページ");
  await pager.getByRole("button", { name: "前へ" }).click();
  await expect(pager).toContainText("1 / 2 ページ");
  await expect(history.locator('[title="会話 29"]')).toBeVisible();
});
