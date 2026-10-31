import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1119: mock を backend にそろえた承認の判断と再開。
// - 判断の前にほかの操作者が判断した承認は、backend が状態を変えずに 200 で返す。返った判断が押した判断と
//   違う（またはほかの操作者の判断の）ときは、成功と案内しない。
// - 再開は、承認がすべて決まり SDK の状態を保存した組み込み Runtime の Run（queued）だけを backend が受け付ける。

const CHANGED_DURING_REVIEW = "この承認の状態が変わりました。最新の内容を確認してください。";

function seedRun(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  const run: Record<string, unknown> = {
    id: "run-decision",
    goal: "受注を更新する",
    agent_id: "default",
    runtime_id: "builtin",
    status: "waiting_approval",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  };
  mockApi.state.runs.push(run);
  return run;
}

function seedPendingApproval(mockApi: MockApi) {
  const approval: Record<string, unknown> = {
    id: "approval-decision",
    run_id: "run-decision",
    step_id: "step-update",
    status: "pending",
    tool_call: { name: "erp__update_order", arguments: {} },
    reason: "受注を変更します。",
    created_at: MOCK_NOW,
    decided_by: null,
    decided_at: null,
  };
  seedRun(mockApi, { approvals: [approval] });
  return approval;
}

async function clickAction(page: Page, testId: string, name: string) {
  const bar = page.getByTestId(testId);
  await expect(bar).toBeVisible();
  const direct = bar.getByRole("button", { name, exact: true });
  if (await direct.count()) return direct.click();
  await bar.getByRole("button", { name: /その他の操作/ }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

/** 判断を送った後、backend に届く前にほかの操作者が却下した（backend は却下のままの Run を 200 で返す）。 */
async function rejectByAnotherReviewerInFlight(page: Page, approval: Record<string, unknown>) {
  await page.route("**/api/approvals/approval-decision/decision", async (route) => {
    Object.assign(approval, { status: "rejected", decided_by: "another.reviewer", decided_at: MOCK_NOW });
    await route.fallback();
  });
}

test("実行履歴: ほかの操作者が先に却下した承認を承認しても、成功（却下しました）と案内しない", async ({
  page,
  mockApi,
}) => {
  const approval = seedPendingApproval(mockApi);
  await rejectByAnotherReviewerInFlight(page, approval);
  await page.goto("/runs?id=run-decision");
  await clickAction(page, "run-object-actions", "承認");
  await page.getByRole("alertdialog").getByRole("button", { name: "承認", exact: true }).click();
  await expect(page.getByText(CHANGED_DURING_REVIEW, { exact: true })).toBeVisible();
  await expect(page.getByText("ツールの実行を却下しました", { exact: true })).toHaveCount(0);
  await expect(page.getByText("ツールの実行を承認しました", { exact: true })).toHaveCount(0);
});

test("承認: ほかの操作者が先に却下した承認を承認しても、成功（却下しました）と案内しない", async ({ page, mockApi }) => {
  const approval = seedPendingApproval(mockApi);
  await rejectByAnotherReviewerInFlight(page, approval);
  await page.goto("/approvals?id=approval-decision");
  await clickAction(page, "approval-object-actions", "承認");
  await page.getByRole("alertdialog").getByRole("button", { name: "承認", exact: true }).click();
  await expect(page.getByText(CHANGED_DURING_REVIEW, { exact: true })).toBeVisible();
  await expect(page.getByText("ツールの実行を却下しました", { exact: true })).toHaveCount(0);
});

test("承認: 自分の判断が通ったときは成功を案内し、承認待ちが残らない Run は待ちに戻る", async ({ page, mockApi }) => {
  seedPendingApproval(mockApi);
  await page.goto("/approvals?id=approval-decision");
  await clickAction(page, "approval-object-actions", "却下");
  await page.getByRole("alertdialog").getByRole("button", { name: "却下", exact: true }).click();
  await expect(page.getByText("ツールの実行を却下しました", { exact: true })).toBeVisible();
  expect(mockApi.state.runs[0].status).toBe("queued");
});

test("再開は backend が受け付ける Run（承認が決まり状態を保存した組み込み Runtime の待ち）にだけ出す", async ({
  page,
  mockApi,
}) => {
  // 旧エンジンの実行中の Run は backend が再開を断る（409）ので、操作を出さない。
  seedRun(mockApi, { id: "run-legacy", runtime_id: "legacy-native", status: "running" });
  // 承認が決まり、保存した SDK の状態から再開を待つ組み込み Runtime の Run。
  seedRun(mockApi, {
    id: "run-resumable",
    status: "queued",
    metadata: { _builtin_sdk_state: "{}" },
  });
  await page.goto("/runs?id=run-legacy");
  await expect(page.getByTestId("run-object-actions")).toBeVisible();
  await expect(page.getByTestId("run-object-actions").getByRole("button", { name: "再開", exact: true })).toHaveCount(0);

  await page.goto("/runs?id=run-resumable");
  await clickAction(page, "run-object-actions", "再開");
  await expect.poll(() => mockApi.lastRequest("POST", "/api/runs/run-resumable/resume")).toBeTruthy();
  await expect(page.getByText("承認待ちが残っているか、再開できる状態ではありません。")).toHaveCount(0);
});
