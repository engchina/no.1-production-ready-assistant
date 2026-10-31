import { MOCK_NOW, expect, test } from "./fixtures/mock-api";

/**
 * チャットの会話の履歴の期間（3 製品の 4 つのチャットで同じ。#2340）。`GET /api/threads` は最後の更新が直近 N 日の
 * 会話だけを返し、`history_days` を付ける。見出しの横（lg 未満はシートの先頭）に「最近 N 日」を出し、0 件は期間を
 * 含めた文にする。desktop と mobile-375 の 2 project で実行する。
 */
test.describe("会話の履歴の期間", { tag: "@responsive" }, () => {
  test("見出しの横に「最近 90 日」を出し、横にはみ出さない", async ({
    page,
    mockApi,
  }, testInfo) => {
    mockApi.state.chatHistoryDays = 90;
    mockApi.state.runs.push({
      id: "run-window-1",
      goal: "期間の中の会話",
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
      thread_id: `thread_${"1".padStart(32, "0")}`,
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    });
    await page.goto("/chat");
    const toggle = page.getByTestId("chat-history-toggle");
    await expect(toggle).toBeEnabled();
    await toggle.click();
    const history = page.getByTestId("chat-history");
    await expect(
      history.getByRole("button", { name: /^期間の中の会話/ }),
    ).toBeVisible();
    const caption = page.getByTestId("chat-history-caption");
    await expect(caption).toHaveText("最近 90 日");
    await expect(caption).toBeVisible();
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: testInfo.outputPath("history-window.png") });
  });

  test("期間の中に会話が無いときは期間を含めた文を出す", async ({
    page,
    mockApi,
  }) => {
    mockApi.state.chatHistoryDays = 30;
    await page.goto("/chat");
    const toggle = page.getByTestId("chat-history-toggle");
    await expect(toggle).toBeEnabled();
    await toggle.click();
    const history = page.getByTestId("chat-history");
    await expect(
      history.getByText("最近 30 日の会話はありません"),
    ).toBeVisible();
    await expect(page.getByTestId("chat-history-caption")).toHaveText(
      "最近 30 日",
    );
  });

  test("期間で絞らない（history_days が無い）ときは補足を出さない", async ({
    page,
  }) => {
    await page.goto("/chat");
    const toggle = page.getByTestId("chat-history-toggle");
    await expect(toggle).toBeEnabled();
    await toggle.click();
    await expect(
      page.getByTestId("chat-history").getByText("まだ会話がありません"),
    ).toBeVisible();
    await expect(page.getByTestId("chat-history-caption")).toHaveCount(0);
  });
});
