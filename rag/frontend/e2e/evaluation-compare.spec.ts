import { expect, test } from "./fixtures/test";

import {
  chooseEvaluationProfile,
  mockEvaluationProfiles,
  evaluationImportResult,
  mockEvaluationImport,
  mockEvaluationJobs,
  uploadEvaluationExcel,
} from "./_evaluation-jobs";
import {
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
} from "./_helpers";

/**
 * 品質評価の案内した画面の指標・検索・回答プロファイルの比較（#2402）。
 * 内容は合成の例（架空の画面・資料の名前）。
 */

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: envelope({
        items: [
          {
            id: "kb-1",
            name: "社内規程",
            description: "",
            status: "ACTIVE",
            default_search_mode: "hybrid",
            document_count: 3,
            indexed_document_count: 3,
            error_document_count: 0,
            searchable_chunk_count: 16,
            created_at: "2026-06-15T00:00:00Z",
            updated_at: "2026-06-15T00:00:00Z",
            archived_at: null,
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_next: false,
      }),
    })
  );
  await page.route("**/api/settings/evaluation-suite", (route) =>
    route.fulfill({
      json: envelope({
        suite: "standard",
        thresholds: { required_evidence_recall: 0.8 },
        suites: [
          {
            name: "standard",
            thresholds: { required_evidence_recall: 0.8 },
            origin: "x",
            recommended_for: ["general"],
            selected: true,
          },
        ],
        config_source: "runtime",
      }),
    })
  );
  await mockEvaluationProfiles(page);
});

function importWithScreens() {
  const base = evaluationImportResult();
  return evaluationImportResult({
    cases: [
      { ...base.cases[0], required_answer_points: ["設定画面A"] },
      base.cases[1],
    ],
    rows: [
      { ...base.rows[0], required_answer_points: ["設定画面A"] },
      base.rows[1],
    ],
  });
}

function caseResult(overrides: Record<string, unknown>) {
  return {
    case_id: "row-2",
    trace_id: "trace-a",
    status: "success",
    retrieved_document_ids: ["doc-a"],
    relevant_document_ids: ["doc-a"],
    hit_document_ids: ["doc-a"],
    required_evidence_recall: 1,
    reciprocal_rank: 1,
    faithfulness: 0.8,
    citation_traceability_coverage: 1,
    answer_keyword_hit: null,
    abstained: false,
    refusal_correct: true,
    answer_evaluation: null,
    failure_reasons: [],
    diagnostics: {},
    elapsed_ms: 10,
    error_type: null,
    error_message: null,
    ...overrides,
  };
}

function metrics(overrides: Record<string, unknown>, caseOverrides: Record<string, unknown>) {
  return {
    case_count: 1,
    error_count: 0,
    evaluation_suite: "standard",
    required_evidence_recall: 1,
    mrr: 1,
    response_decision_accuracy: 1,
    metric_case_counts: {
      required_evidence_recall: 1,
      mrr: 1,
      response_decision_accuracy: 1,
      answer_correctness: 1,
      requirement_coverage: 1,
      claim_support_rate: 1,
    },
    passed: true,
    threshold_failures: [],
    failure_reason_counts: {},
    case_results: [caseResult(caseOverrides)],
    ...overrides,
  };
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`プロファイルと比べた結果と案内した画面を出す (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockEvaluationImport(page, () => importWithScreens());
    const jobs = await mockEvaluationJobs(page, {
      runResult: () => ({
        ...metrics(
          { answer_correctness: 0, requirement_coverage: 0, claim_support_rate: 0, failure_reason_counts: { answer_failed: 1 } },
          {
            screen_match: false,
            wrong_screen: true,
            guided_screens: ["設定画面B"],
            missing_screens: ["設定画面A"],
            wrong_screens: ["設定画面B"],
            screen_judged_by: "deterministic",
            goal_alignment: "off_target",
            failure_reasons: ["screen_missing", "wrong_screen", "off_target"],
          }
        ),
        comparison: {
          search_answer_profile_id: "profile-1",
          metrics: metrics(
            { answer_correctness: 1, requirement_coverage: 1, claim_support_rate: 1 },
            {
              trace_id: "trace-b",
              screen_match: true,
              wrong_screen: false,
              guided_screens: ["設定画面A"],
              matched_screens: ["設定画面A"],
              screen_judged_by: "judge",
              goal_alignment: "aligned",
            }
          ),
        },
      }),
      autoComplete: true,
    });

    await page.goto("/evaluation");
    await chooseEvaluationProfile(page);
    await uploadEvaluationExcel(page);
    await expect(page.getByTestId("evaluation-import-ready")).toBeVisible();
    // 案内すべき画面の列をプレビューに出す。
    await expect(page.getByTestId("evaluation-import-preview")).toContainText("設定画面A");

    // 検索・回答プロファイルでも流して比べる（オンにしてから選ぶ。未選択なら欄の直下に出す）。
    await page.getByRole("switch", { name: "別の検索・回答プロファイルでも評価して比較する" }).click();
    await page.getByRole("button", { name: "評価実行" }).click();
    await expect(page.getByText("比べる検索・回答プロファイルを選んでください。")).toBeVisible();
    await page.locator("#evaluation-compare-profile").click();
    await expect(page.getByRole("listbox").getByRole("option", { name: /プロファイル A/ })).toHaveCount(0);
    await page.getByRole("listbox").getByRole("option", { name: /プロファイル B/ }).click();
    await page.getByRole("button", { name: "評価実行" }).click();

    await expect.poll(() => jobs.runPayloads.length).toBe(1);
    const payload = jobs.runPayloads[0] as {
      compare_search_answer_profile_id: string;
      cases: Record<string, unknown>[];
    };
    expect(payload).toMatchObject({search_answer_profile_id: "profile-base", compare_search_answer_profile_id: "profile-1"});
    expect(payload.cases[0]).toMatchObject({ required_answer_points: ["設定画面A"] });

    // 比べる表: 差はポイントと文言（色だけに頼らない）。低いほどよい指標は向きを逆にする。
    const table = page.getByTestId("evaluation-compare-table");
    await expect(page.getByRole("heading", { name: "検索・回答プロファイルの比較" })).toBeVisible();
    const correctnessRow=table.getByRole("row",{name:/回答の正確性/});
    await expect(correctnessRow).toContainText("+100pt");
    await expect(correctnessRow).toContainText("改善");
    const coverageRow=table.getByRole("row",{name:/回答の網羅性/});
    await expect(coverageRow).toContainText("+100pt");
    await expect(coverageRow).toContainText("改善");
    await expect(table.getByRole("row",{name:/必要な根拠の取得率/})).toContainText("変化なし");
    await expect(table.getByRole("row")).toHaveCount(7);
    await expect(page.getByTestId("evaluation-perspective-guidance")).toHaveCount(0);
    await expect(page.getByTestId("evaluation-metric-answer_correctness")).toContainText("0%");
    await expect(page.getByTestId("evaluation-metric-requirement_coverage")).toContainText("0%");
    await page.getByRole("tab",{name:"検索・回答プロファイル: プロファイル B"}).click();
    await expect(page.getByTestId("evaluation-metric-answer_correctness")).toContainText("100%");
    await expect(page.getByTestId("evaluation-metric-requirement_coverage")).toContainText("100%");
    await expectNoPageOverflow(page);
  });
}

test("比べないときは比べる表とタブを出さない", async ({ page }) => {
  await mockEvaluationImport(page, () => importWithScreens());
  const jobs = await mockEvaluationJobs(page, {
    runResult: () => metrics({}, {}),
    autoComplete: true,
  });

  await page.goto("/evaluation");
  await chooseEvaluationProfile(page);
  await uploadEvaluationExcel(page);
  await page.getByRole("button", { name: "評価実行" }).click();

  await expect.poll(() => jobs.runPayloads.length).toBe(1);
  const payload = jobs.runPayloads[0] as Record<string, unknown>;
  expect(payload.search_answer_profile_id).toBe("profile-base");
  expect(payload).not.toHaveProperty("knowledge_base_ids");
  expect(payload).not.toHaveProperty("compare_search_answer_profile_id");
  await expect(page.getByTestId("evaluation-perspective-retrieval")).toBeVisible();
  await expect(page.getByTestId("evaluation-compare-table")).toHaveCount(0);
  // 比べる結果のタブは出さない。
  await expect(page.getByRole("tab")).toHaveCount(0);
});
