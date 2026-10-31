import type { Page } from "@playwright/test";

import { expect, test, type MockApi } from "./fixtures/mock-api";

/**
 * 行の操作メニュー（RowActionMenu）は、一覧の定期的な再取得（実行履歴・承認の 5 秒ごと）で行のデータが変わっても
 * 開いたまま残る（#831）。行の key（Run の ID・承認の ID）は変わらないので、メニューの状態を持つ部品は作り直されない。
 */

const MOCK_NOW = "2026-10-03T00:00:00Z";

function seedRun(mockApi: MockApi, id: string, overrides: Record<string, unknown> = {}) {
  mockApi.state.runs.push({
    id,
    goal: "受注状況を確認する",
    agent_id: "default",
    runtime_id: "legacy-native",
    status: "running",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  });
}

function pendingApproval(id: string, runId: string) {
  return {
    id,
    run_id: runId,
    step_id: `${id}-step`,
    tool_call: { name: "rag__rag_search", arguments: { query: "受注" } },
    status: "pending",
    reason: "承認が必要です",
    decided_by: null,
    created_at: MOCK_NOW,
    decided_at: null,
  };
}

/** 次の一覧の再取得（GET `pathname`）の応答を待ち、画面に反映されるまで待つ。 */
async function waitForPoll(page: Page, pathname: string, settled: () => Promise<void>) {
  await page.waitForResponse(
    (response) => new URL(response.url()).pathname === pathname && response.request().method() === "GET",
    { timeout: 10_000 }
  );
  await settled();
}

test("実行履歴: 行の操作メニューは 5 秒ごとの再取得で行が変わっても開いたまま", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-poll-a");
  seedRun(mockApi, "run-poll-b", { goal: "在庫を確認する" });

  await page.goto("/runs");
  const trigger = page.getByTestId("run-row-actions-run-poll-a-trigger");
  await trigger.click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");

  // 次の再取得で、別の行の状態と更新時刻が変わり、行が 1 件増える。
  mockApi.state.runs[1].status = "completed";
  mockApi.state.runs[1].updated_at = "2026-10-03T00:00:05Z";
  seedRun(mockApi, "run-poll-c", { goal: "出荷を確認する" });
  await waitForPoll(page, "/api/runs", () => expect(page.getByTestId("run-row-run-poll-c")).toBeVisible());

  await expect(menu).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
});

test("承認: 行の操作メニューは 5 秒ごとの再取得で行が変わっても開いたまま", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-approval", {
    status: "waiting_approval",
    approvals: [pendingApproval("approval-poll-a", "run-approval")],
  });

  await page.goto("/approvals");
  const trigger = page.getByTestId("approval-row-actions-approval-poll-a-trigger");
  await trigger.click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();

  // 次の再取得で、承認が 1 件増える。
  (mockApi.state.runs[0].approvals as Record<string, unknown>[]).push(
    pendingApproval("approval-poll-b", "run-approval")
  );
  await waitForPoll(page, "/api/approvals", () =>
    expect(page.getByTestId("approval-row-actions-approval-poll-b-trigger")).toBeVisible()
  );

  await expect(menu).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
});
