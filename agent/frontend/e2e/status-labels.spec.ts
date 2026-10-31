/**
 * 状態のラベルとページヘッダーの操作を NL2SQL / RAG の基準にそろえたことの確認（#802）。
 *
 * - 状態の enum（running / waiting_approval / pending / read …）を英語のまま出さず、日本語のラベルにする
 *   （design-system ARCHITECTURE §4、lib/status-labels.ts）。
 * - ページの表示更新は `PageHeader` の utility「最新情報を取得」（buttons.md §5）。
 */
import { expect, MOCK_NOW, test, type MockApi } from "./fixtures/mock-api";

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

const RAW_ENUMS = /^(queued|running|waiting_approval|completed|failed|cancelled|pending|approved|rejected|read|write|sensitive|allow|ask|deny|side_effects)$/;

function seedRun(mockApi: MockApi, id: string, status: string, approvals: Record<string, unknown>[] = []) {
  mockApi.state.runs.unshift({
    id,
    goal: `状態の確認 ${id}`,
    agent_id: "default",
    runtime_id: "builtin",
    status,
    steps: [
      {
        id: `${id}-step`,
        run_id: id,
        kind: "tool",
        status: status === "waiting_approval" ? "waiting_approval" : "completed",
        tool_call: { name: "rag__rag_search", arguments: { query: "契約" } },
      },
    ],
    events: [
      { id: `${id}-event`, run_id: id, type: "tool.completed", message: "ツールを実行しました。", payload: {}, created_at: MOCK_NOW },
    ],
    approvals,
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function expectNoRawEnumBadges(page: import("@playwright/test").Page) {
  // 見えるバッジだけ（実行履歴は広い画面で題名の下の 1 行を隠し、列に出す。#1955）。
  const badges = page.locator("main [data-status-variant]:visible");
  await expect(badges.first()).toBeVisible();
  for (const text of await badges.allInnerTexts()) {
    expect(text.trim()).not.toMatch(RAW_ENUMS);
  }
}

for (const viewport of VIEWPORTS) {
  test.describe(`状態のラベル (${viewport.name})`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("実行履歴は状態を日本語で出し、表示の更新は utility の「最新情報を取得」", async ({ page, mockApi }, testInfo) => {
      seedRun(mockApi, "run-status-running", "running");
      seedRun(mockApi, "run-status-waiting", "waiting_approval", [
        {
          id: "approval-status-1",
          run_id: "run-status-waiting",
          step_id: "run-status-waiting-step",
          tool_call: { name: "rag__rag_search", arguments: { query: "契約" } },
          status: "pending",
          reason: "",
          created_at: MOCK_NOW,
        },
      ]);
      await page.goto("/runs");
      const table = page.getByRole("table", { name: "実行履歴" });
      // 状態は共通の値（`data-run-status`）と日本語の文言。広い画面は列、狭い画面は題名の下の 1 行に出す（#1955）。
      const runningStatus = table.getByTestId("run-row-run-status-running").locator("[data-run-status]:visible");
      await expect(runningStatus).toHaveAttribute("data-run-status", "running");
      await expect(runningStatus).toHaveText("実行中");
      const waitingStatus = table.getByTestId("run-row-run-status-waiting").locator("[data-run-status]:visible");
      await expect(waitingStatus).toHaveAttribute("data-run-status", "waiting");
      await expect(waitingStatus).toHaveText("承認待ち");
      // 目標の下の補足は業務 Agent の ID ではなく名前。
      await expect(table.getByTestId("run-row-run-status-running")).toContainText("資料・データ横断アシスタント");
      await expectNoRawEnumBadges(page);

      const refresh = page.getByRole("button", { name: "最新情報を取得" });
      await expect(refresh).toBeVisible();
      const before = mockApi.requests.filter((request) => request.method === "GET" && request.path === "/api/runs").length;
      await refresh.click();
      await expect
        .poll(() => mockApi.requests.filter((request) => request.method === "GET" && request.path === "/api/runs").length)
        .toBeGreaterThan(before);
      await page.screenshot({ path: testInfo.outputPath(`runs-status-${viewport.name}.png`), fullPage: true });

      // 承認は全幅のキュー → `?id=` の全幅の詳細（#877）。一覧は詳細を自動で開かないので、
      // キューの行と、行から開いた詳細の両方で状態を日本語で出すことを確かめる。
      await page.goto("/approvals");
      const approvalRow = page.getByRole("table", { name: "承認一覧" }).getByTestId("approval-row-approval-status-1");
      await expect(approvalRow.getByText("保留中", { exact: true })).toBeVisible();
      await expectNoRawEnumBadges(page);

      await approvalRow.locator('a[data-approval-id="approval-status-1"]').click();
      await expect(page).toHaveURL(/\/approvals\?id=approval-status-1$/);
      const detail = page.getByRole("region", { name: "承認の詳細" });
      await expect(detail.getByText("保留中", { exact: true })).toBeVisible();
      // 関連する実行の状態は「実行の状態」の項目（dt / dd）に日本語のラベルで出す。
      await expect(
        detail.locator("dt", { hasText: /^実行の状態$/ }).locator("xpath=following-sibling::dd[1]"),
      ).toHaveText("承認待ち");
      await expectNoRawEnumBadges(page);
    });

    test("監査ログの絞り込みの選択肢と、ツール・ツール権限の権限を日本語で出す", async ({ page }) => {
      await page.goto("/audit");
      await expect(page.getByRole("button", { name: "最新情報を取得" })).toBeVisible();
      await page.locator("#audit-step-status").click();
      await expect(page.getByRole("option", { name: "承認待ち" })).toBeVisible();
      await expect(page.getByRole("option", { name: "waiting_approval" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await page.locator("#audit-approval-status").click();
      await expect(page.getByRole("option", { name: "却下済み" })).toBeVisible();
      await page.keyboard.press("Escape");

      await page.goto("/settings/tool-policy");
      await expectNoRawEnumBadges(page);
    });
  });
}
