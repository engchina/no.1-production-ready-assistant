import type { Page } from "@playwright/test";

import { MOCK_NOW, evaluationSummary, expect, test, type MockApi } from "./fixtures/mock-api";

// #2558: 品質評価で、業務 Agent の出力の項目を項目ごとに判定する。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

const OUTPUT_FIELDS = [
  { key: "item", label: "項目", type: "text", required: true, description: "" },
  { key: "amount", label: "金額", type: "number", required: false, description: "" },
  { key: "matched", label: "一致", type: "boolean", required: false, description: "" },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function useOutputFields(mockApi: MockApi) {
  const agent = mockApi.state.agents.find((item) => item.id === "default");
  if (!agent) throw new Error("default agent missing");
  agent.output_fields = OUTPUT_FIELDS;
}

const CASE = {
  id: "output-case",
  question: "書類の値を読んで",
  expected: "A と B の金額",
  expected_tools: [],
  expected_tools_in_order: false,
  clarification_answers: [],
  source_run_id: null,
  expected_output: [
    { item: "A", amount: 1200 },
    { item: "B", amount: 800 },
  ],
};

function seedJob(mockApi: MockApi) {
  mockApi.state.evaluationSets.push({
    id: "evset-output",
    agent_id: "default",
    name: "出力の評価",
    description: "",
    cases: [CASE],
    created_by_user_uuid: "local",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
  const results = [
    {
      case: CASE,
      status: "judged",
      run_id: "run-out-1",
      answer: "**確認結果**\n\nA は 1200、B は 900 です。\n\n| 項目 | 値 |\n| --- | --- |\n| A | 1200 |",
      judgement: { verdict: "correct", score: 1, summary: "要点を満たしています。", missing_points: [] },
      tool_calls: ["record_output"],
      tool_selection_correct: null,
      field_results: [
        // #2562: 意味で比べる項目は「意味で判定」と出す。
        {
          row: 1,
          field: "item",
          label: "項目",
          expected: "A",
          actual: "A",
          correct: true,
          method: "meaning",
          reason: "同じ品目を指している",
        },
        { row: 1, field: "amount", label: "金額", expected: 1200, actual: 1200, correct: true },
        { row: 2, field: "item", label: "項目", expected: "B", actual: "B", correct: true },
        { row: 2, field: "amount", label: "金額", expected: 800, actual: 900, correct: false },
      ],
      field_correct: false,
      error: null,
      duration_ms: 2400,
    },
  ];
  mockApi.state.evaluations.push({
    id: "eval-output",
    agent_id: "default",
    agent_name: "資料・データ横断アシスタント",
    set_id: "evset-output",
    set_name: "出力の評価",
    status: "completed",
    created_by_user_uuid: "local",
    results,
    error: null,
    summary: evaluationSummary(results),
    created_at: MOCK_NOW,
    started_at: MOCK_NOW,
    finished_at: MOCK_NOW,
    agent_version: "draft",
    previous_job_id: null,
    previous_summary: null,
    previous_agent_version: null,
    previous_created_at: null,
  });
}

for (const viewport of VIEWPORTS) {
  test(`評価ケースに期待する出力を入れて保存できる (${viewport.name})`, async ({ page, mockApi }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    useOutputFields(mockApi);
    await page.goto("/evaluation");
    await page.getByRole("button", { name: "評価セットを作成" }).first().click();
    await page.getByLabel("名前").fill("出力の評価");
    const first = page.getByTestId("evaluation-case-1");
    await first.getByLabel("質問").fill("書類の値を読んで");
    await first.getByLabel("期待する回答の要点").fill("A の金額");

    const output = page.getByTestId("evaluation-case-1-expected-output");
    await expect(output).toContainText("期待する出力");
    await page.getByTestId("evaluation-case-1-expected-output-add").click();
    const row = page.getByTestId("evaluation-case-1-expected-output-row-1");
    await row.getByRole("textbox", { name: "項目" }).fill("A");
    await row.getByRole("textbox", { name: "金額" }).fill("1,200");
    await row.getByLabel("一致").click();
    await page.getByRole("option", { name: "はい" }).click();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "作成", exact: true }).first().click();
    await expect(page.getByText("評価セットを作成しました")).toBeVisible();
    const body = mockApi.lastRequest("POST", "/api/evaluation-sets")?.body as {
      cases: { expected_output?: unknown }[];
    };
    expect(body.cases[0].expected_output).toEqual([{ item: "A", amount: 1200, matched: true }]);
  });

  test(`評価の結果に出力の項目の正答率と項目ごとの判定を出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    useOutputFields(mockApi);
    seedJob(mockApi);
    await page.goto("/evaluation");

    const summary = page.getByTestId("evaluation-summary");
    await expect(summary).toContainText("出力の項目の正答率");
    await expect(summary).toContainText("75%");
    await expect(summary).toContainText("正しい項目 3 / 4");
    await expect(summary).toContainText("0 / 1 ケース");
    const results = page.getByRole("table", { name: "ケース別結果" });
    await expect(results.getByRole("row", { name: /output-case/ })).toContainText("3 / 4 項目");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`evaluation-output-${viewport.name}.png`), fullPage: true });

    await results.getByRole("button", { name: /書類の値を読んで/ }).click();
    const detail = page.getByRole("dialog", { name: "ケースの詳細" });
    await expect(detail.locator("strong").filter({ hasText: "確認結果" })).toBeVisible();
    await expect(detail.getByRole("table", { name: "回答の表", exact: true }).getByRole("cell", { name: "1200", exact: true })).toBeVisible();
    await detail.screenshot({ path: testInfo.outputPath(`evaluation-markdown-${viewport.name}.png`) });
    const table = detail.getByTestId("evaluation-output-fields");
    await expect(table).toContainText("金額");
    await expect(table).toContainText("900");
    await expect(table).toContainText("違う");
    await expect(table.getByRole("row", { name: /^1 項目/ })).toContainText("意味で判定");
    // #2568: 意味の判定の理由も出す。
    await expect(table.getByRole("row", { name: /^1 項目/ })).toContainText("同じ品目を指している");
  });
}

test("期待する出力の無い評価では、出力の列と正答率を出さない", async ({ page, mockApi }) => {
  seedJob(mockApi);
  const job = mockApi.state.evaluations[0];
  const results = (job.results as Record<string, unknown>[]).map((item) => ({
    ...item,
    field_results: [],
    field_correct: null,
  }));
  job.results = results;
  job.summary = evaluationSummary(results);
  await page.goto("/evaluation");
  await expect(page.getByTestId("evaluation-summary")).not.toContainText("出力の項目の正答率");
  await expect(page.getByRole("table", { name: "ケース別結果" }).getByRole("columnheader", { name: "出力" })).toHaveCount(0);
});
