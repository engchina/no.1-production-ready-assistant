import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #72: 結果が不明な外部への書き込み（届いた後に応答を失った）を、実行の詳細で照合を求める警告として出す。

const SCREENSHOT_DIR = process.env.E2E_SCREENSHOT_DIR;
const TOOL = "ext__create_ticket";
// 合成の操作の ID（実在の値ではない）。
const OPERATION_ID = "op-synthetic-72";

function runWithUnknownWrite(mockApi: MockApi, id: string, status: string, stepStatus: string, result: unknown) {
  mockApi.state.runs.push({
    id,
    goal: "合成の起票の依頼",
    agent_id: "default",
    runtime_id: "builtin",
    status,
    steps: [
      {
        id: `${id}-step`,
        run_id: id,
        kind: "tool",
        status: stepStatus,
        tool_call: { name: TOOL, arguments: { title: "合成の件名" }, trace_id: "call-1", operation_key: OPERATION_ID },
        tool_result: result,
        started_at: MOCK_NOW,
        completed_at: MOCK_NOW,
      },
    ],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: { tool_titles: { [TOOL]: "起票" } },
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

const UNKNOWN_RESULT = {
  name: TOOL,
  success: false,
  error: "MCP 接続「ext」が 30 秒以内に応答しませんでした。",
  error_code: "mcp.timeout",
  error_details: { outcome: "unknown" },
  started_at: MOCK_NOW,
  completed_at: MOCK_NOW,
  duration_ms: 30000,
  policy_decision: "allow",
  approval_required: false,
  guardrail_warnings: [],
  audit_metadata: {},
};

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const width of [1280, 1920]) {
  test(`結果不明の書き込みを、表示名と操作の ID で照合を求める警告にする (${width}px)`, async ({ page, mockApi }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "幅を指定する確認は desktop だけ");
    await page.setViewportSize({ width, height: 900 });
    runWithUnknownWrite(mockApi, `run-72-${width}`, "failed", "failed", UNKNOWN_RESULT);
    await page.goto(`/runs?id=run-72-${width}`);
    const notice = page.getByTestId("run-outcome-unknown");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("外部への書き込みの結果が不明です");
    await expect(notice).toContainText("同じ操作を再実行すると重複する可能性があります");
    await expect(notice.getByRole("listitem")).toHaveText([`起票（操作の ID: ${OPERATION_ID}）`]);
    // 状態は色だけに頼らない（共有の Banner のアイコン）。
    await expect(notice.locator("svg").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    if (SCREENSHOT_DIR) {
      await page.getByTestId("run-summary").screenshot({ path: `${SCREENSHOT_DIR}/agent-run-outcome-unknown-${width}.png` });
    }
  });
}

test("取り消した実行で結果が届かないまま止まった書き込みも警告する", async ({ page, mockApi }) => {
  runWithUnknownWrite(mockApi, "run-72-cancelled", "cancelled", "running", null);
  await page.goto("/runs?id=run-72-cancelled");
  await expect(page.getByTestId("run-outcome-unknown")).toBeVisible();
});

test("結果を受け取った書き込みは警告しない", async ({ page, mockApi }) => {
  runWithUnknownWrite(mockApi, "run-72-known", "failed", "failed", { ...UNKNOWN_RESULT, error_details: {} });
  await page.goto("/runs?id=run-72-known");
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByTestId("run-outcome-unknown")).toHaveCount(0);
});

test("結果不明の警告は 375px でも横にあふれない", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-375", "375px の確認");
  runWithUnknownWrite(mockApi, "run-72-mobile", "failed", "failed", UNKNOWN_RESULT);
  await page.goto("/runs?id=run-72-mobile");
  await expect(page.getByTestId("run-outcome-unknown")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  if (SCREENSHOT_DIR) {
    await page.getByTestId("run-summary").screenshot({ path: `${SCREENSHOT_DIR}/agent-run-outcome-unknown-375.png` });
  }
});
