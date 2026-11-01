import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #62: 1 ケースを k 回反復する評価。Pass^k / Pass@k・安定性・欠測・区間と、同じ定義のケースの対応比較を出す。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "wide-1920", width: 1920, height: 1080 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

const CASES = [
  { id: "deadline", question: "経費精算の締め日はいつですか？", expected: "毎月 25 日", expected_tools: [] },
  { id: "approver", question: "経費の承認者は誰ですか？", expected: "部長", expected_tools: [] },
];

const SET = {
  id: "evset-trials",
  agent_id: "default",
  name: "経理の評価",
  description: "",
  cases: CASES,
  created_by_user_uuid: "local",
  created_at: MOCK_NOW,
  updated_at: MOCK_NOW,
};

function result(index: number, trial: number, correct: boolean | null) {
  return {
    case: CASES[index],
    trial,
    status: correct === null ? "judge_failed" : "judged",
    run_id: `run-${index}-${trial}`,
    answer: correct ? "毎月 25 日です。" : "分かりません。",
    judgement:
      correct === null
        ? null
        : { verdict: correct ? "correct" : "incorrect", score: correct ? 1 : 0, summary: "判定", missing_points: [] },
    tool_calls: [],
    tool_selection_correct: null,
    error: correct === null ? "判定のモデルの呼び出しに失敗しました。" : null,
    duration_ms: 1000,
  };
}

function seed(mockApi: MockApi) {
  mockApi.state.evaluationSets.push(structuredClone(SET));
  // deadline は 3 回とも成功、approver は 成功・失敗・評価者の失敗（不安定で欠測あり）。
  const job = {
    id: "eval-trials",
    agent_id: "default",
    agent_name: "資料・データ横断アシスタント",
    set_id: SET.id,
    set_name: SET.name,
    agent_version: "draft",
    trials: 3,
    judge_fingerprint: "judge-b",
    status: "completed",
    created_by_user_uuid: "local",
    results: [result(0, 1, true), result(1, 1, true), result(0, 2, true), result(1, 2, false), result(0, 3, true), result(1, 3, null)],
    case_trials: [
      { case_id: "deadline", question: CASES[0].question, trials: 3, judged: 3, passed: 3, missing: 0, rate: 1, pass_at_k: true, pass_hat_k: true, stability: "stable_pass" },
      { case_id: "approver", question: CASES[1].question, trials: 3, judged: 2, passed: 1, missing: 1, rate: 0.5, pass_at_k: null, pass_hat_k: null, stability: "unstable" },
    ],
    error: null,
    summary: {
      total: 6,
      completed: 6,
      correct: 4,
      incorrect: 1,
      uncertain: 0,
      errors: 1,
      unjudged: 1,
      approval_blocked: 0,
      pass_rate: 0.8,
      average_score: 0.8,
      tool_cases: 0,
      tool_correct: 0,
      tool_accuracy: null,
      trials: 3,
      cases: 2,
      judged_cases: 2,
      judged_trials: 5,
      pass_rate_interval: [0.34, 0.97],
      complete_cases: 1,
      incomplete_cases: 1,
      missing_trials: 1,
      stable_cases: 1,
      unstable_cases: 1,
      pass_at_k: 1,
      pass_at_k_interval: [0.21, 1],
      pass_hat_k: 1,
      pass_hat_k_interval: [0.21, 1],
    },
    created_at: MOCK_NOW,
    started_at: MOCK_NOW,
    finished_at: MOCK_NOW,
    previous_job_id: null,
    previous_summary: null,
    previous_agent_version: null,
    previous_created_at: null,
  };
  const published = {
    ...structuredClone(job),
    id: "eval-published",
    agent_version: 1,
    trials: 1,
    judge_fingerprint: "judge-a",
    results: [result(0, 1, false), result(1, 1, true)],
    case_trials: [],
    summary: { ...job.summary, trials: 1, total: 2, pass_rate: 0.5, pass_rate_interval: [0.09, 0.91] },
  };
  mockApi.state.evaluations.push(job, published);
}

const COMPARISON = {
  rows: [],
  not_comparable: 0,
  improved: 1,
  regressed: 1,
  unchanged: 0,
  base_only: 0,
  target_only: 0,
  same_cases: false,
  definition_mismatch: 1,
  same_judge: false,
  paired: {
    cases: 1,
    base_rate: 0,
    target_rate: 1,
    difference: 1,
    interval: [-0.21, 1],
    conclusive: false,
    improved: 1,
    regressed: 0,
    unchanged: 0,
  },
};

for (const viewport of VIEWPORTS) {
  test(`反復した評価の Pass^k・安定性・欠測・区間と、対応比較の不確実性を出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seed(mockApi);
    await page.goto("/evaluation");

    const summary = page.getByTestId("evaluation-summary");
    await expect(summary).toContainText("80%");
    await expect(summary).toContainText("95% 区間 34%〜97%");
    await expect(summary).toContainText("Pass^3（全回成功）");
    await expect(summary).toContainText("Pass@3（1 回以上成功）");
    await expect(summary).toContainText("1 ケースで集計");
    await expect(summary).toContainText("欠測のあるケース 1 件");
    const stability = page.getByRole("table", { name: "ケースごとの安定性" });
    await expect(stability.getByRole("row", { name: /経費精算の締め日/ })).toContainText("毎回成功");
    await expect(stability.getByRole("row", { name: /経費の承認者/ })).toContainText("不安定");
    await expect(stability.getByRole("row", { name: /経費の承認者/ })).toContainText("1 / 2");
    await expectNoHorizontalOverflow(page);
    await summary.screenshot({ path: testInfo.outputPath(`evaluation-trials-summary-${viewport.name}.png`) });
    await stability.screenshot({ path: testInfo.outputPath(`evaluation-trials-stability-${viewport.name}.png`) });
    const results = page.getByRole("table", { name: "ケース別結果" });
    await expect(results.getByRole("row")).toHaveCount(7);
    await expect(results.getByRole("row", { name: /approver/ }).nth(1)).toContainText("2 / 3");
    // 試行ごとに別のケースの詳細を開く（同じケースの別の試行と取り違えない）。
    await results.getByRole("row", { name: /approver/ }).nth(1).getByRole("button").click();
    const detail = page.getByRole("dialog", { name: "ケースの詳細" });
    await expect(detail).toContainText("分かりません。");
    await page.screenshot({ path: testInfo.outputPath(`evaluation-trials-detail-${viewport.name}.png`) });
    await page.getByRole("button", { name: "閉じる" }).first().click();

    // 反復の回数を選んで評価を始めると、回数を送る。
    mockApi.state.evaluations.forEach((job) => (job.status = "completed"));
    await page.getByTestId("evaluation-trials").click();
    await page.getByRole("option", { name: "5 回" }).click();
    await page.getByTestId(`evaluation-set-row-actions-${SET.id}`).click();
    await page.getByRole("menuitem", { name: "評価を開始" }).click();
    await expect(page.getByText("評価を開始しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toMatchObject({ set_id: SET.id, trials: 5 });
  });

  test(`対応比較は区間と比較の前提の違いを出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seed(mockApi);
    await page.route("**/api/evaluations/compare?**", async (route) => {
      const [target, base] = mockApi.state.evaluations;
      const item = (job: Record<string, unknown>) => ({ ...job, results: undefined });
      await route.fulfill({
        json: { data: { ...COMPARISON, base: item(base), target: item(target) }, error_messages: [], warning_messages: [] },
      });
    });
    await page.goto("/evaluation");
    const compare = page.getByTestId("evaluation-compare");
    await compare.scrollIntoViewIfNeeded();
    await compare.getByTestId("evaluation-compare-base").click();
    await page.getByRole("option", { name: /v1/ }).click();
    await compare.getByTestId("evaluation-compare-target").click();
    await page.getByRole("option", { name: /下書き/ }).click();
    const paired = compare.getByTestId("evaluation-compare-paired");
    await expect(paired).toContainText("同じ定義のケース 1 件の対応比較: 成功率 0% → 100%（差 +100 pt、95% 区間 -21〜+100 pt）");
    await expect(paired).toContainText("区間が 0 をまたぐため、差は不確かです。");
    const warnings = compare.getByTestId("evaluation-compare-warnings");
    await expect(warnings).toContainText("定義の違うケース（1 件）");
    await expect(warnings).toContainText("評価者");
    await expect(warnings).toContainText("基準 1 回・比べる評価 3 回");
    await expectNoHorizontalOverflow(page);
    await compare.screenshot({ path: testInfo.outputPath(`evaluation-trials-compare-${viewport.name}.png`) });
  });
}
