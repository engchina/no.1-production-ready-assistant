import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #3348: 実行の詳細の「取消」（取り消した人と入口。外部の MCP の取消はキーの名前を添える）。

const SCREENSHOT_DIR = process.env.E2E_SCREENSHOT_DIR;

function cancelledRun(mockApi: MockApi, id: string, cancellation: Record<string, unknown> | null) {
  mockApi.state.runs.push({
    id,
    goal: "合成の質問",
    agent_id: "default",
    runtime_id: "builtin",
    status: "cancelled",
    steps: [],
    events: [
      {
        id: `${id}-cancelled`,
        run_id: id,
        type: "run.cancelled",
        message: "実行をキャンセルしました。",
        payload: { cancelled_approval_ids: [] },
        created_at: MOCK_NOW,
      },
    ],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    cancellation,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

const CASES = [
  {
    name: "MCP（キーの名前）",
    cancellation: { via: "mcp", actor_display_name: "管理 花子", by_me: false, api_key_name: "合成のキー A" },
    text: "管理 花子 · MCP（合成のキー A）",
  },
  {
    name: "見られないキーは MCP だけ",
    cancellation: { via: "mcp", actor_display_name: "管理 花子", by_me: false, api_key_name: "" },
    text: "管理 花子 · MCP",
  },
  {
    name: "自分が画面で取り消した",
    cancellation: { via: "screen", actor_display_name: "ローカル利用者", by_me: true, api_key_name: "" },
    text: "自分 · 画面",
  },
  {
    name: "削除済みの利用者",
    cancellation: { via: "screen", actor_display_name: "", by_me: false, api_key_name: "" },
    text: "（削除済みのユーザー） · 画面",
  },
  { name: "自動の取消", cancellation: { via: "system", actor_display_name: "", by_me: false, api_key_name: "" }, text: "システム" },
];

for (const [index, item] of CASES.entries()) {
  test(`実行の詳細の概要とイベントの記録に取消の人と入口を出す (${item.name})`, async ({ page, mockApi }) => {
    const id = `run-cancel-3348-${index}`;
    cancelledRun(mockApi, id, item.cancellation);
    await page.goto(`/runs?id=${id}`);
    const summary = page.getByTestId("run-summary");
    const fact = summary.locator('[data-run-fact="cancellation"]');
    await expect(fact.locator("dt")).toHaveText("取消");
    await expect(fact.locator("dd")).toHaveText(item.text);
    // 事実の並び: 実行者の後、対象（業務 Agent）の前。
    const order = await summary
      .locator("[data-run-fact]")
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-run-fact")));
    expect(order.indexOf("cancellation")).toBe(order.indexOf("executor") + 1);
    expect(order.indexOf("cancellation")).toBeLessThan(order.indexOf("target"));
    // イベントの記録の取消の行にも同じ文を添える（終わった実行の処理の経過は閉じているので開く）。
    await page.getByTestId("run-timeline-section-summary").click();
    await page.getByTestId("run-process-details").click();
    await expect(page.getByTestId("run-technical-section").getByTestId(`run-event-log-event-${id}-cancelled`)).toHaveCount(0);
  });
}

test("取り消していない実行・記録の無い古い取消は「取消」を出さない", async ({ page, mockApi }) => {
  cancelledRun(mockApi, "run-cancel-3348-legacy", null);
  await page.goto("/runs?id=run-cancel-3348-legacy");
  const summary = page.getByTestId("run-summary");
  await expect(summary.locator('[data-run-fact="executor"]')).toBeVisible();
  await expect(summary.locator('[data-run-fact="cancellation"]')).toHaveCount(0);
});

test("取消の事実は 375px でも折り返して横にあふれない", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  cancelledRun(mockApi, "run-cancel-3348-wide", {
    via: "mcp",
    actor_display_name: "とても長い表示名の合成の利用者",
    by_me: false,
    api_key_name: "とても長い名前の合成の API キー（外部のクライアント）",
  });
  await page.goto("/runs?id=run-cancel-3348-wide");
  const fact = page.getByTestId("run-summary").locator('[data-run-fact="cancellation"]');
  await expect(fact.locator("dd")).toHaveText(
    "とても長い表示名の合成の利用者 · MCP（とても長い名前の合成の API キー（外部のクライアント））"
  );
  const box = await fact.boundingBox();
  const viewport = page.viewportSize();
  expect(box && viewport && box.x + box.width <= viewport.width).toBeTruthy();
  await expectNoHorizontalOverflow(page);
  if (SCREENSHOT_DIR) {
    await page.getByTestId("run-summary").screenshot({ path: `${SCREENSHOT_DIR}/agent-run-cancellation-${testInfo.project.name}.png` });
  }
});
