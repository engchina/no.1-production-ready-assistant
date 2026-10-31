import type { Page } from "@playwright/test";

import { LOCAL_CURRENT_USER } from "./fixtures/auth";
import { MOCK_NOW, emptyUsageReport, expect, test, type MockApi } from "./fixtures/mock-api";

// #772: 利用状況（Run のモデル利用量の集計）と、Run の詳細の利用量。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

function totals(runs: number, input: number, output: number, recorded = runs) {
  return {
    runs,
    runs_with_usage: recorded,
    requests: recorded * 2,
    input_tokens: input,
    output_tokens: output,
    total_tokens: input + output,
  };
}

function seedReport(mockApi: MockApi) {
  const report = emptyUsageReport(30) as Record<string, unknown> & { by_day: Record<string, unknown>[] };
  report.totals = totals(12, 90_000, 10_000, 11);
  report.previous = totals(8, 60_000, 5_000);
  report.by_agent = [
    { agent_id: "sales", agent_name: "営業の Agent", ...totals(7, 70_000, 5_000) },
    { agent_id: "default", agent_name: "資料・データ横断アシスタント", ...totals(4, 20_000, 5_000) },
    { agent_id: "removed-agent", agent_name: "", ...totals(1, 0, 0, 0) },
  ];
  report.by_user = [
    { user_uuid: "u-1", display_name: "山田 太郎", ...totals(9, 80_000, 8_000) },
    { user_uuid: null, display_name: "", ...totals(3, 10_000, 2_000) },
  ];
  report.by_model = [
    { model: "xai.grok-4", ...totals(11, 90_000, 10_000) },
    { model: "", ...totals(1, 0, 0, 0) },
  ];
  const last = report.by_day[report.by_day.length - 1];
  report.by_day[report.by_day.length - 1] = { ...last, ...totals(5, 40_000, 4_000) };
  mockApi.state.usageReports["30"] = report;
}

for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`期間の合計と前の期間との差、内訳を出す (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      seedReport(mockApi);
      await page.goto("/usage");

      await expect(page.getByRole("heading", { name: "利用状況", level: 1 })).toBeVisible();
      const summary = page.getByTestId("usage-summary");
      await expect(summary).toContainText("合計 token");
      await expect(summary).toContainText("100,000");
      await expect(summary).toContainText("前の期間から +35,000");
      // Run あたりの token は、利用量を記録した Run（11 件）で割る（100,000 / 11 ≒ 9,091。前は 65,000 / 8 ≒ 8,125）。
      await expect(summary).toContainText("9,091");
      await expect(summary).toContainText("前の期間から +966");
      await expect(page.getByTestId("usage-unrecorded")).toContainText("利用量が未記録の実行が 1 件あります");
      await expect(page.getByTestId("usage-unrecorded")).toContainText("未記録は利用量がゼロであることを意味しません");

      // 業務 Agent ごと（既定のタブ）。token の割合を数字でも出す。
      const agents = page.getByRole("table", { name: "業務 Agent ごとの利用量" });
      const sales = agents.getByRole("row", { name: /営業の Agent/ });
      await expect(sales).toContainText("75,000");
      await expect(sales).toContainText("75%");
      await expect(agents.getByRole("row", { name: /removed-agent/ })).toContainText("削除した業務 Agent");

      // 利用者ごと・モデルごとはタブで切り替える（← → でも移れる）。
      await page.getByRole("tab", { name: /利用者ごと/ }).click();
      const users = page.getByRole("table", { name: "利用者ごとの利用量" });
      await expect(users.getByRole("row", { name: /山田 太郎/ })).toContainText("88,000");
      await expect(users).toContainText("作成者の記録なし");
      await page.getByRole("tab", { name: /利用者ごと/ }).press("ArrowRight");
      const models = page.getByRole("table", { name: "モデルごとの利用量" });
      await expect(models).toContainText("xai.grok-4");
      await expect(models).not.toContainText("記録なし");
      await expect(models.getByRole("row")).toHaveCount(2);
      await expect(page.getByRole("tab", { name: /モデルごと/ })).toContainText("1");

      // 日ごとは新しい日から。期間の 30 日をページングする。
      await page.getByRole("tab", { name: "日ごと" }).click();
      const days = page.getByRole("table", { name: "日ごとの利用量" });
      await expect(days.getByRole("row").nth(1)).toContainText("44,000");
      await expect(page.getByText("1–25 / 30 件")).toBeVisible();

      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`usage-${viewport.name}-${theme}.png`), fullPage: true });
    });
  }
}

test("利用量が記録済みならモデル名なし・0 token の実行もモデル内訳に残す", async ({ page, mockApi }) => {
  seedReport(mockApi);
  const report = mockApi.state.usageReports["30"] as Record<string, unknown>;
  report.totals = totals(14, 90_500, 10_050, 13);
  (report.by_model as Record<string, unknown>[]).push(
    { model: "", ...totals(1, 500, 50) },
    { model: "model-zero", ...totals(1, 0, 0), requests: 0 },
  );
  await page.goto("/usage");
  await page.getByRole("tab", { name: /モデルごと/ }).click();
  const models = page.getByRole("table", { name: "モデルごとの利用量" });
  await expect(models.getByRole("row", { name: /モデル名未記録/ })).toContainText("550");
  await expect(models.getByRole("row", { name: /model-zero/ })).toContainText("0%");
  await expect(models.getByRole("row")).toHaveCount(4);
  await expect(page.getByRole("tab", { name: /モデルごと/ })).toContainText("3");
});

test("全件利用量未記録でも実行数を残し、モデル内訳に専用の空状態を出す", async ({ page, mockApi }) => {
  seedReport(mockApi);
  const report = mockApi.state.usageReports["30"] as Record<string, unknown>;
  report.totals = totals(1, 0, 0, 0);
  report.by_agent = [{ agent_id: "default", agent_name: "資料・データ横断アシスタント", ...totals(1, 0, 0, 0) }];
  // 更新前の backend の応答も想定し、欠測を0利用量のモデルとして表示しない。
  report.by_model = [{ model: "", ...totals(1, 0, 0, 0) }];
  await page.goto("/usage");
  await expect(page.getByRole("table", { name: "業務 Agent ごとの利用量" })).toContainText("資料・データ横断アシスタント");
  await expect(page.getByTestId("usage-unrecorded")).toContainText("実行数には含めます");
  await page.getByRole("tab", { name: /モデルごと/ }).click();
  await expect(page.getByText("この期間にモデルの利用量の記録はありません")).toBeVisible();
  await expect(page.getByRole("table", { name: "モデルごとの利用量" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: /モデルごと/ })).toContainText("0");
  // 表示更新後の修正済み API（空配列）でも同じ状態を維持する。
  report.by_model = [];
  const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
  await refresh.focus();
  await refresh.press("Enter");
  await expect(page.getByText("この期間にモデルの利用量の記録はありません")).toBeVisible();
  await expect(refresh).toBeFocused();
});

test("期間を変えると、その期間で集計し直し、Run が無ければ空の状態を出す", async ({ page, mockApi }) => {
  seedReport(mockApi);
  await page.goto("/usage");
  await expect(page.getByTestId("usage-summary")).toContainText("100,000");

  await page.locator("#usage-period").click();
  await page.getByRole("option", { name: "直近 7 日" }).click();

  await expect(page.getByText("この期間の実行はありません")).toBeVisible();
  const request = mockApi.lastRequest("GET", "/api/usage");
  expect(request?.searchParams.get("days")).toBe("7");
  // 日はシステムの設定の時間帯で区切る（既定 UTC。#3265・#3274）。
  expect(request?.searchParams.get("timezone")).toBe("UTC");
  // 期間は作業状態に残す（再読込しても 7 日）。
  await page.reload();
  await expect(page.locator("#usage-period")).toContainText("直近 7 日");
});

test("90 日を超える期間（180・365 日）を選べ、集計元を出す", async ({ page, mockApi }) => {
  // #794: Oracle の構成は保存した Run の履歴（AGENT_RUN_FACTS）を集計する。
  const report = emptyUsageReport(365) as Record<string, unknown>;
  report.source = "history";
  report.totals = totals(3, 3_000, 300);
  mockApi.state.usageReports["365"] = report;
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/usage");
  await expect(page.getByTestId("report-source")).toHaveAttribute("data-source", "memory");

  await page.locator("#usage-period").click();
  await expect(page.getByRole("option", { name: "直近 180 日" })).toBeVisible();
  await page.getByRole("option", { name: "直近 365 日" }).click();

  await expect(page.getByTestId("usage-summary")).toContainText("3,300");
  await expect(page.getByTestId("report-source")).toHaveText("保存した実行の履歴（データベース）から集計しています。");
  expect(mockApi.lastRequest("GET", "/api/usage")?.searchParams.get("days")).toBe("365");
  await page.getByRole("tab", { name: "日ごと" }).click();
  await expect(page.getByText("1–25 / 365 件")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("読み込み中は経過時間と集計の形の Skeleton を出す", async ({ page }) => {
  await page.route("**/api/usage?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await page.goto("/usage");
  await expect(page.getByTestId("usage-loading")).toContainText("利用状況を読み込んでいます");
  await expect(page.getByText("この期間の実行はありません")).toBeVisible();
});

test("取得エラーを利用量未記録と混同せず、再試行で内訳を表示する", async ({ page, mockApi }) => {
  seedReport(mockApi);
  let failed = true;
  await page.route("**/api/usage?*", async (route) => {
    if (failed) {
      await route.fulfill({ status: 503, json: { data: null, error_messages: ["利用状況を取得できませんでした"], warning_messages: [] } });
    } else {
      await route.fallback();
    }
  });
  await page.goto("/usage");
  // TanStack Query の既定の再試行（1・2・4秒）が終わってから取得エラーになる。
  await expect(page.getByText("利用状況を取得できませんでした")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("usage-unrecorded")).toHaveCount(0);
  await expect(page.getByText("この期間にモデルの利用量の記録はありません")).toHaveCount(0);
  failed = false;
  await page.getByRole("button", { name: "再試行", exact: true }).click();
  await expect(page.getByTestId("usage-summary")).toContainText("100,000");
});

test("Run の詳細にモデルの利用量を出す", async ({ page, mockApi }) => {
  mockApi.state.runs.push({
    id: "run-usage-772",
    goal: "今月の売上は？",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    usage: { model: "xai.grok-4", requests: 2, input_tokens: 1200, output_tokens: 300, total_tokens: 1500 },
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
  await page.goto("/runs?id=run-usage-772");
  // 概要の事実（モデル・token・呼び出しの回数と内訳。#772・#1955）。
  const summary = page.getByTestId("run-summary");
  await expect(summary.locator('[data-run-fact="model"] dd')).toHaveText("xai.grok-4");
  await expect(summary.locator('[data-run-fact="tokens"] dd')).toHaveText("1,500 token");
  await expect(summary.locator('[data-run-fact="modelCalls"] dd')).toHaveText("2 回（入力 1,200 / 出力 300 token）");
});

for (const recorded of [false, true]) {
  test(`実行詳細は記録の無い利用量・モデル名を並べない (${recorded ? "モデル名なし" : "利用量なし"})`, async ({ page, mockApi }) => {
    mockApi.state.runs.push({
      id: "run-missing-usage",
      goal: "業務の質問",
      agent_id: "default",
      runtime_id: "builtin",
      status: "completed",
      steps: [], events: [], approvals: [], artifacts: [], pending_tool_calls: [], metadata: {},
      usage: recorded ? { model: "", requests: 1, input_tokens: 100, output_tokens: 20, total_tokens: 120 } : null,
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    });
    await page.goto("/runs?id=run-missing-usage");
    // 記録の無い事実は並べない（「—」の並びにしない。run-history.md §3）。
    const summary = page.getByTestId("run-summary");
    await expect(summary.locator("[data-run-fact]").first()).toBeVisible();
    await expect(summary.locator('[data-run-fact="model"]')).toHaveCount(0);
    await expect(summary.locator('[data-run-fact="tokens"]')).toHaveCount(recorded ? 1 : 0);
    if (recorded) {
      await expect(summary.locator('[data-run-fact="tokens"] dd')).toHaveText("120 token");
      await expect(summary.locator('[data-run-fact="modelCalls"] dd')).toHaveText("1 回（入力 100 / 出力 20 token）");
    } else {
      await expect(summary.locator('[data-run-fact="modelCalls"]')).toHaveCount(0);
    }
  });
}


for (const viewport of VIEWPORTS) {
  test(`システムの設定の時間帯で利用状況を取得する (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // ブラウザの時間帯ではなく、/api/auth/me の timezone（PLATFORM_TIMEZONE）を送る（#3265）。
    mockApi.setCurrentUser({ ...LOCAL_CURRENT_USER, timezone: "Asia/Tokyo" });
    seedReport(mockApi);
    await page.goto("/usage");
    await expect(page.getByTestId("usage-summary")).toContainText("100,000");
    expect(mockApi.lastRequest("GET", "/api/usage")?.searchParams.get("timezone")).toBe("Asia/Tokyo");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`usage-system-timezone-${viewport.name}.png`), fullPage: true });
  });
}


test.describe("ブラウザの時間帯", () => {
  test.use({ timezoneId: "America/New_York" });

  test("ブラウザの時間帯に関係なく、設定の時間帯の無い応答では既定（UTC）を送る", async ({ page, mockApi }) => {
    seedReport(mockApi);
    await page.goto("/usage");
    await expect(page.getByTestId("usage-summary")).toContainText("100,000");
    expect(mockApi.lastRequest("GET", "/api/usage")?.searchParams.get("timezone")).toBe("UTC");
  });
});
