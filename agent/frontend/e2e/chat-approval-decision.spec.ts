import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// チャットの承認の判断は、返った承認の状態で結果を案内する（実行履歴・承認の画面と同じ。#1119）。
// backend はほかの操作者が先に判断した承認を、状態を変えずに 200 で返す。押した判断が自分の判断として
// 残ったときだけ「承認しました」「却下しました」と案内し、それ以外は現在の状態の確認を促す。

const THREAD_ID = `thread_${"c".repeat(32)}`;
const CHANGED_DURING_REVIEW = "この承認の状態が変わりました。最新の内容を確認してください。";

function seedWaitingThread(mockApi: MockApi) {
  const approval: Record<string, unknown> = {
    id: "approval-chat-decision",
    run_id: "run-chat-decision",
    step_id: "step-update",
    tool_call: { name: "erp__update_order", arguments: { order_id: "A-1" } },
    status: "pending",
    reason: "承認が必要です。",
    created_at: MOCK_NOW,
    decided_by: null,
    decided_at: null,
  };
  const run: Record<string, unknown> = {
    id: "run-chat-decision",
    goal: "受注を更新して",
    agent_id: "default",
    runtime_id: "builtin",
    status: "waiting_approval",
    steps: [
      {
        id: "step-update",
        run_id: "run-chat-decision",
        kind: "tool",
        status: "waiting_approval",
        tool_call: { name: "erp__update_order", arguments: { order_id: "A-1" } },
        approval_id: "approval-chat-decision",
      },
    ],
    events: [],
    approvals: [approval],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  };
  mockApi.state.runs.push(run);
  return { run, approval };
}

async function openThread(page: Page) {
  await page.goto("/chat");
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: /^受注を更新して/ }).click();
  } else {
    await page.getByRole("dialog", { name: "会話の履歴" }).getByText("受注を更新して").click();
  }
}

test("ほかの操作者が先に却下した承認を承認しても、「承認しました」と案内しない", async ({ page, mockApi }) => {
  const { run, approval } = seedWaitingThread(mockApi);
  // 判断を送った後、backend に届く前にほかの操作者が却下した。backend は却下のままの Run を 200 で返す。
  await page.route("**/api/approvals/approval-chat-decision/decision", async (route) => {
    Object.assign(approval, { status: "rejected", decided_by: "another.reviewer", decided_at: MOCK_NOW });
    Object.assign(run, { status: "failed" });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: run, error_messages: [], warning_messages: [] }),
    });
  });
  await openThread(page);

  const turn = page.getByTestId("chat-turn-run-chat-decision");
  await turn.getByRole("button", { name: "承認して実行" }).click();

  await expect(page.getByText(CHANGED_DURING_REVIEW, { exact: true })).toBeVisible();
  await expect(page.getByText("承認しました", { exact: true })).toHaveCount(0);
  await expect(page.getByText("却下しました", { exact: true })).toHaveCount(0);
  // 会話を取り直し、判断を待つ表示を外す。
  await expect(turn.getByRole("button", { name: "承認して実行" })).toHaveCount(0);
});

test("自分の判断が通ったときは「却下しました」と案内する", async ({ page, mockApi }) => {
  seedWaitingThread(mockApi);
  await openThread(page);

  const turn = page.getByTestId("chat-turn-run-chat-decision");
  await turn.getByRole("button", { name: "却下", exact: true }).click();

  await expect(page.getByText("却下しました", { exact: true })).toBeVisible();
  await expect(page.getByText(CHANGED_DURING_REVIEW, { exact: true })).toHaveCount(0);
  expect(mockApi.lastRequest("POST", "/api/approvals/approval-chat-decision/decision")?.body).toEqual({
    approved: false,
  });
});
