import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { dropFiles } from "./_helpers/file-dropzone";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import { findClippedPlaceholders, jumpToPage, measureHorizontalOverflow } from "../../../../platform/packages/ui/e2e/layout-checks";

const basePath = "/api/nl2sql/quality-evaluations";

test.beforeEach(async ({ page }) => {
  await mockDatabaseGateReady(page);
});

function envelope(route: Route, data: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

function createRequestGate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

async function visibleBox(locator: Locator) {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box!;
}

async function expectVerticalOrder(locators: Locator[]) {
  if (locators.length === 0) return;
  let previous = await visibleBox(locators[0]!);
  for (const locator of locators.slice(1)) {
    const current = await visibleBox(locator);
    expect(current.y).toBeGreaterThanOrEqual(previous.y + previous.height - 1);
    previous = current;
  }
}

const capabilities = {
  engines: [
    { engine: "select_ai", label: "Select AI", available: true, reason: "" },
    {
      engine: "select_ai_agent",
      label: "Select AI Agent",
      available: true,
      reason: "",
    },
    {
      engine: "enterprise_ai_direct",
      label: "Enterprise AI Direct",
      available: true,
      reason: "",
    },
  ],
  judge: { available: true, reason: "", provider: "OCI Enterprise AI" },
  limits: {
    max_file_bytes: 10 * 1024 * 1024,
    max_cases: 100,
    max_attempts: 1000,
    min_repeat_count: 1,
    max_repeat_count: 10,
    attempt_timeout_seconds: 300,
  },
};

const summary = [
  {
    engine: "select_ai",
    total_attempts: 2,
    generation_successes: 2,
    generation_success_rate: 1,
    correct: 1,
    incorrect: 1,
    uncertain: 0,
    not_analyzed: 0,
    normalized_sql_consistency: 0.5,
    error_count: 0,
  },
  {
    engine: "enterprise_ai_direct",
    total_attempts: 2,
    generation_successes: 1,
    generation_success_rate: 0.5,
    correct: 1,
    incorrect: 0,
    uncertain: 0,
    not_analyzed: 1,
    normalized_sql_consistency: 1,
    error_count: 1,
  },
];

const longEvaluationQuestion =
  '対象テーブル："部署情報を管理するテーブル" 抽出項目："DEPARTMENT_ID", "DEPARTMENT_NAME", "LOCATION", "CREATED_AT" 抽出条件：未入金の請求金額を確認し、VERY_LONG_UNBROKEN_EVALUATION_QUERY_IDENTIFIER_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ を含めて取得してください';

type MockQualityEvaluationStatus =
  | "pending"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelled";

function job(
  status: MockQualityEvaluationStatus,
  overrides: Partial<{
    job_id: string;
    profile_name: string;
    profile_category: string;
    created_at: string;
    updated_at: string;
    finished_at: string | null;
    current_attempt_started_at: string | null;
    current_attempt_deadline_at: string | null;
    heartbeat_at: string | null;
    lease_expires_at: string | null;
    attempt_no: number;
    attempt_timeout_seconds: number;
  }> = {}
) {
  const terminal =
    status === "completed" ||
    status === "completed_with_errors" ||
    status === "failed" ||
    status === "cancelled";
  const createdAt = overrides.created_at ?? "2026-07-22T08:00:00Z";
  const finishedAt =
    overrides.finished_at === undefined
      ? terminal
        ? "2026-07-22T08:00:04Z"
        : null
      : overrides.finished_at;
  return {
    job_id: overrides.job_id ?? "job-001",
    profile_id: "default",
    profile_name: overrides.profile_name ?? "標準プロファイル",
    profile_category: overrides.profile_category ?? "SQL生成評価",
    engines: ["select_ai", "enterprise_ai_direct"],
    repeat_count: 2,
    case_count: 1,
    total_attempts: 4,
    completed_attempts: terminal ? 4 : status === "running" ? 1 : 0,
    success_count: terminal ? 3 : status === "running" ? 1 : 0,
    error_count: terminal ? 1 : 0,
    status,
    current_case_id: status === "running" ? "CASE-001" : "",
    current_engine: status === "running" ? "select_ai" : null,
    current_repetition: status === "running" ? 2 : 0,
    current_attempt_started_at:
      overrides.current_attempt_started_at ??
      (status === "running" ? "2026-07-22T08:00:01Z" : null),
    current_attempt_deadline_at:
      overrides.current_attempt_deadline_at ??
      (status === "running" ? "2026-07-22T08:10:01Z" : null),
    // 試行は上限つきの並行で実行する（#2059）。current_* は最も前に始めた試行。
    running_attempts:
      status === "running"
        ? [
            {
              case_no: 1,
              case_id: "CASE-001",
              engine: "select_ai",
              repetition_no: 2,
              started_at: overrides.current_attempt_started_at ?? "2026-07-22T08:00:01Z",
              deadline_at: overrides.current_attempt_deadline_at ?? "2026-07-22T08:10:01Z",
            },
            {
              case_no: 1,
              case_id: "CASE-001",
              engine: "enterprise_ai_direct",
              repetition_no: 1,
              started_at: "2026-07-22T08:00:03Z",
              deadline_at: "2026-07-22T08:10:03Z",
            },
          ]
        : [],
    concurrency: 2,
    engine_summaries: terminal && status !== "failed" && status !== "cancelled" ? summary : [],
    error_message:
      status === "failed"
        ? "worker の初期化に失敗しました。"
        : status === "cancelled"
          ? "利用者の操作で SQL生成評価 job を中止しました。"
          : "",
    heartbeat_at:
      overrides.heartbeat_at ?? (status === "running" ? "2026-07-22T08:00:02Z" : null),
    lease_expires_at:
      overrides.lease_expires_at ?? (status === "running" ? "2099-07-22T08:05:31Z" : null),
    attempt_no: overrides.attempt_no ?? (status === "running" ? 1 : 0),
    attempt_timeout_seconds: overrides.attempt_timeout_seconds ?? 300,
    created_at: createdAt,
    started_at: "2026-07-22T08:00:01Z",
    finished_at: finishedAt,
    updated_at: overrides.updated_at ?? finishedAt ?? createdAt,
  };
}

const results = [
  {
    result_id: "result-1",
    job_id: "job-001",
    case_no: 1,
    case_id: "CASE-001",
    excel_row: 2,
    question: longEvaluationQuestion,
    expected_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE STATUS = 'UNPAID'",
    engine: "select_ai",
    repetition_no: 1,
    generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE STATUS = 'UNPAID'",
    normalized_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE STATUS = 'UNPAID'",
    deterministic_analysis: {
      is_safe: true,
      is_select_only: true,
      referenced_objects: ["APP.INVOICES"],
      structure_summary: "INVOICES を STATUS で絞り込み",
      risk_findings: [],
    },
    generation_elapsed_ms: 120,
    judge_elapsed_ms: 240,
    total_elapsed_ms: 360,
    verdict: "correct",
    judge: {
      verdict: "correct",
      confidence: 0.96,
      summary: "質問と期待 SQL の意味に一致します。",
      differences: ["期待 SQL と生成 SQL は同じ未入金条件を参照しています。"],
      risks: ["行数制限を追加すると応答時間が安定します。"],
      correction_suggestion: "必要に応じて FETCH FIRST 100 ROWS ONLY を追加してください。",
    },
    generation_error: "",
    judge_error: "",
    created_at: "2026-07-22T08:00:02Z",
  },
  {
    result_id: "result-2",
    job_id: "job-001",
    case_no: 1,
    case_id: "CASE-001",
    excel_row: 2,
    question: longEvaluationQuestion,
    expected_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE STATUS = 'UNPAID'",
    engine: "enterprise_ai_direct",
    repetition_no: 2,
    generated_sql: "",
    normalized_sql: "",
    deterministic_analysis: {
      is_safe: false,
      is_select_only: false,
      referenced_objects: [],
      structure_summary: "",
      risk_findings: [],
    },
    generation_elapsed_ms: 500,
    judge_elapsed_ms: 0,
    total_elapsed_ms: 500,
    verdict: "not_analyzed",
    judge: null,
    generation_error: "OCI Enterprise AI timeout",
    judge_error: "",
    created_at: "2026-07-22T08:00:03Z",
  },
];

function repeatedResults(count: number) {
  return Array.from({ length: count }, (_, index) => {
    const source = results[index % results.length]!;
    return {
      ...source,
      result_id: `result-${index + 1}`,
      case_no: index + 1,
      case_id: `CASE-${String(index + 1).padStart(3, "0")}`,
      excel_row: index + 2,
      repetition_no: (index % 2) + 1,
      created_at: `2026-07-22T08:${String(index).padStart(2, "0")}:03Z`,
    };
  });
}

function repeatedJobs(count: number) {
  return Array.from({ length: count }, (_, index) =>
    job(index % 2 === 0 ? "completed" : "completed_with_errors", {
      job_id: `job-${String(index + 1).padStart(3, "0")}`,
      created_at: `2026-07-22T08:${String(index).padStart(2, "0")}:00Z`,
    })
  );
}

async function mockQualityApi(
  page: Page,
  options: {
    judgeAvailable?: boolean;
    fixedStatus?: "completed" | "completed_with_errors" | "failed";
    engines?: typeof capabilities.engines;
    resultItems?: typeof results;
    currentJob?: ReturnType<typeof job>;
    recentJobs?: ReturnType<typeof job>[];
    recentStatusSequence?: MockQualityEvaluationStatus[];
    startValidationErrors?: string[];
  } = {}
) {
  let jobReads = 0;
  let recentReads = 0;
  let submittedBody = "";
  let recentJobs = options.recentJobs ? [...options.recentJobs] : null;
  const deletedJobIds: string[] = [];
  const cancelledJobIds: string[] = [];
  const cancelledJobs = new Map<string, ReturnType<typeof job>>();
  await page.route("**/api/nl2sql/profiles**", (route) =>
    envelope(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "SQL生成評価",
          description: "",
          allowed_tables: [],
          allowed_views: [],
          glossary: {},
          sql_rules: [],
          default_row_limit: 100,
          safety_policy: "select_only",
          few_shot_examples: [],
          select_ai_config: {
            profile_name: "",
            region: "",
            model: "",
            embedding_model: "",
            max_tokens: 32000,
            enforce_object_list: true,
            comments: true,
            annotations: false,
            constraints: false,
            role: "",
            additional_instructions: "",
          },
          archived: false,
        },
      ],
      next_cursor: null,
      total: 1,
    })
  );
  await page.route("**/api/nl2sql/quality-evaluations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith("/capabilities")) {
      return envelope(route, {
        ...capabilities,
        engines: options.engines ?? capabilities.engines,
        judge:
          options.judgeAvailable === false
            ? {
                available: false,
                reason: "OCI Enterprise AI Judge が未構成です。",
                provider: "OCI Enterprise AI",
              }
            : capabilities.judge,
      });
    }
    if (path.endsWith("/template.xlsx")) {
      return route.fulfill({
        status: 200,
        contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers: {
          "Content-Disposition": 'attachment; filename="nl2sql_quality_evaluation_template.xlsx"',
        },
        body: Buffer.from("template"),
      });
    }
    if (path.endsWith("/results.xlsx")) {
      return route.fulfill({
        status: 200,
        contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers: {
          "Content-Disposition":
            'attachment; filename="nl2sql_quality_evaluation_20260722_job-001.xlsx"',
        },
        body: Buffer.from("result workbook"),
      });
    }
    if (request.method() === "DELETE" && path.startsWith(`${basePath}/`)) {
      const jobId = decodeURIComponent(path.slice(basePath.length + 1));
      const target = recentJobs?.find((item) => item.job_id === jobId);
      if (!target) {
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "指定されたSQL生成評価 job が見つかりません。" }),
        });
      }
      deletedJobIds.push(jobId);
      recentJobs = recentJobs?.filter((item) => item.job_id !== jobId) ?? null;
      return envelope(route, target);
    }
    if (request.method() === "POST" && path.startsWith(`${basePath}/`) && path.endsWith("/cancel")) {
      const encodedId = path.slice(basePath.length + 1, -"/cancel".length);
      const jobId = decodeURIComponent(encodedId);
      const target =
        cancelledJobs.get(jobId) ??
        options.currentJob ??
        recentJobs?.find((item) => item.job_id === jobId) ??
        job("running", { job_id: jobId });
      const cancelled = {
        ...target,
        status: "cancelled" as const,
        current_case_id: "",
        current_engine: null,
        current_repetition: 0,
        current_attempt_started_at: null,
        heartbeat_at: "2026-07-22T08:06:01Z",
        lease_expires_at: null,
        finished_at: "2026-07-22T08:06:01Z",
        updated_at: "2026-07-22T08:06:01Z",
        error_message: "利用者の操作で SQL生成評価 job を中止しました。",
      };
      if (!cancelledJobIds.includes(jobId)) cancelledJobIds.push(jobId);
      cancelledJobs.set(jobId, cancelled);
      recentJobs =
        recentJobs?.map((item) => (item.job_id === jobId ? cancelled : item)) ?? null;
      return envelope(route, cancelled);
    }
    if (path.endsWith("/results")) {
      const items = options.resultItems ?? results;
      return envelope(route, { items, next_cursor: null, total: items.length });
    }
    if (path === basePath && request.method() === "POST") {
      submittedBody = request.postData() ?? "";
      if (options.startValidationErrors?.length) {
        return route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "QUALITY_EVALUATION_VALIDATION_ERROR",
              errors: options.startValidationErrors,
            },
          }),
        });
      }
      return envelope(route, job("pending"), 202);
    }
    if (path === basePath) {
      if (recentJobs) {
        return envelope(route, {
          items: recentJobs,
          next_cursor: null,
          total: recentJobs.length,
        });
      }
      if (options.recentStatusSequence?.length) {
        const status =
          options.recentStatusSequence[
            Math.min(recentReads, options.recentStatusSequence.length - 1)
          ]!;
        recentReads += 1;
        return envelope(route, { items: [job(status)], next_cursor: null, total: 1 });
      }
      const recent = options.fixedStatus ? job(options.fixedStatus) : job("completed_with_errors");
      return envelope(route, { items: [recent], next_cursor: null, total: 1 });
    }
    if (path.endsWith("/job-001")) {
      const cancelled = cancelledJobs.get("job-001");
      if (cancelled) return envelope(route, cancelled);
      if (options.currentJob) return envelope(route, options.currentJob);
      if (options.fixedStatus) return envelope(route, job(options.fixedStatus));
      jobReads += 1;
      return envelope(route, job(jobReads < 2 ? "running" : "completed_with_errors"));
    }
    return route.fallback();
  });
  return {
    submittedBody: () => submittedBody,
    deletedJobIds: () => deletedJobIds,
    cancelledJobIds: () => cancelledJobIds,
  };
}

test("SQL生成評価の読込状態は利用者向けの日本語ラベルを表示する", async ({ page }) => {
  await mockQualityApi(page);
  const capabilitiesGate = createRequestGate();
  await page.route("**/api/nl2sql/quality-evaluations/capabilities**", async (route) => {
    await capabilitiesGate.promise;
    return envelope(route, capabilities);
  });

  await page.goto("/evaluation");

  const loading = page.getByRole("region", { name: "読み込んでいます", exact: true }).first();
  await expect(loading).toBeVisible();
  await expect(page.getByText("common.loading", { exact: true })).toHaveCount(0);

  capabilitiesGate.release();
  await expect(loading).toBeHidden();
});

test("desktop executes two engines twice, restores the job URL and downloads Excel", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop flow");
  const state = await mockQualityApi(page);
  await page.goto("/evaluation");

  await expect(page.getByRole("heading", { name: "評価条件" })).toBeVisible();
  const inputRow = page.getByTestId("quality-evaluation-input-row");
  const profileField = page.getByTestId("quality-evaluation-profile-field");
  const fileField = page.getByTestId("quality-evaluation-file");
  const engineFieldset = page.getByTestId("quality-evaluation-engine-fieldset");
  const repeatLabel = page.getByTestId("quality-evaluation-repeat-field").locator("label");
  const repeatInput = page.getByLabel("繰り返し回数");
  const estimateSummary = page.getByTestId("quality-evaluation-estimate-summary");
  const estimateLabel = page.getByTestId("quality-evaluation-estimate-label");
  const estimateValue = page.getByTestId("quality-evaluation-estimate-value");
  const actionFooter = page.getByTestId("quality-evaluation-action-footer");
  const [
    inputRowBox,
    profileBox,
    fileBox,
    engineBox,
    repeatLabelBox,
    repeatInputBox,
    estimateBox,
    estimateLabelBox,
    estimateValueBox,
    footerBox,
  ] =
    await Promise.all([
      visibleBox(inputRow),
      visibleBox(profileField),
      visibleBox(fileField),
      visibleBox(engineFieldset),
      visibleBox(repeatLabel),
      visibleBox(repeatInput),
      visibleBox(estimateSummary),
      visibleBox(estimateLabel),
      visibleBox(estimateValue),
      visibleBox(actionFooter),
    ]);
  expect(Math.abs(profileBox.y - fileBox.y)).toBeLessThanOrEqual(3);
  expect(engineBox.y).toBeGreaterThanOrEqual(inputRowBox.y + inputRowBox.height - 1);
  expect(Math.abs(repeatLabelBox.y - estimateLabelBox.y)).toBeLessThanOrEqual(3);
  expect(Math.abs(repeatInputBox.y - estimateValueBox.y)).toBeLessThanOrEqual(3);
  expect(estimateValueBox.height).toBeLessThanOrEqual(repeatInputBox.height + 8);
  expect(estimateBox.height).toBeLessThanOrEqual(repeatInputBox.height + repeatLabelBox.height + 16);
  const estimateSurface = await estimateSummary.evaluate((element) => {
    const styles = window.getComputedStyle(element);
    return {
      backgroundColor: styles.backgroundColor,
      borderTopWidth: styles.borderTopWidth,
      borderRightWidth: styles.borderRightWidth,
      borderBottomWidth: styles.borderBottomWidth,
      borderLeftWidth: styles.borderLeftWidth,
    };
  });
  expect(estimateSurface.backgroundColor).toBe("rgba(0, 0, 0, 0)");
  expect(estimateSurface.borderTopWidth).toBe("0px");
  expect(estimateSurface.borderRightWidth).toBe("0px");
  expect(estimateSurface.borderBottomWidth).toBe("0px");
  expect(estimateSurface.borderLeftWidth).toBe("0px");
  expect(footerBox.y).toBeGreaterThanOrEqual(estimateBox.y + estimateBox.height - 1);
  const profileSelect = page.locator("#quality-evaluation-profile");
  await expectSelectFieldValue(profileSelect, "default");
  await expect(profileSelect).toContainText("標準プロファイル（SQL生成評価）");
  await expect(
    page.getByText("標準プロファイル（SQL生成評価）").filter({ visible: true }).first()
  ).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
  const checkboxes = page.getByRole("checkbox");
  const engineBulkActions = page.getByTestId("quality-evaluation-engine-selection-actions");
  const engineGrid = page.getByTestId("quality-evaluation-engine-grid");
  await expect
    .poll(async () => {
      const [actionsBox, gridBox] = await Promise.all([
        engineBulkActions.boundingBox(),
        engineGrid.boundingBox(),
      ]);
      if (!actionsBox || !gridBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - gridBox.x);
    })
    .toBeLessThanOrEqual(1);
  await expect(engineBulkActions.getByRole("button", { name: "すべて選択" })).toBeEnabled();
  await expect(engineBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeDisabled();
  await engineBulkActions.getByRole("button", { name: "すべて選択" }).click();
  await expect(checkboxes.nth(0)).toBeChecked();
  await expect(checkboxes.nth(1)).toBeEnabled();
  await expect(checkboxes.nth(1)).toBeChecked();
  await expect(checkboxes.nth(2)).toBeChecked();
  await expect(engineBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeEnabled();
  await engineBulkActions.getByRole("button", { name: "選択をすべて解除" }).click();
  await expect(checkboxes.nth(0)).not.toBeChecked();
  await expect(checkboxes.nth(1)).not.toBeChecked();
  await expect(checkboxes.nth(2)).not.toBeChecked();
  await checkboxes.nth(0).focus();
  await page.keyboard.press("Space");
  await checkboxes.nth(2).check();
  await expect(checkboxes.nth(1)).toBeEnabled();
  await page.getByLabel("繰り返し回数").fill("2");
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [
    {
      name: "quality-cases.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  await page.getByRole("button", { name: "評価を開始" }).click();

  await expect(page).toHaveURL(/\?job=job-001/);
  await expect(page.getByText("CASE-001").first()).toBeVisible();
  await expect(page.getByText("一部エラーで完了").first()).toBeVisible({ timeout: 8_000 });
  await expect(page.getByRole("heading", { name: "評価概要" })).toBeVisible();
  await expect(
    page.getByTestId("quality-evaluation-timing").getByRole("timer")
  ).toHaveAccessibleName("処理時間 00:03");
  await expect(page.getByRole("progressbar", { name: "実行状況" })).toHaveAttribute(
    "aria-valuemax",
    "4",
  );
  await expect(
    page.getByText("質問と期待 SQL の意味に一致します。").filter({ visible: true }).first()
  ).toBeVisible();
  const analysisToggle = page
    .getByTestId("quality-evaluation-analysis-toggle")
    .filter({ visible: true })
    .first();
  const analysisChevron = analysisToggle.locator("svg");
  await expect.poll(() => analysisChevron.evaluate((icon) => getComputedStyle(icon).rotate)).toBe("-90deg");
  await analysisToggle.click();
  await expect.poll(() => analysisChevron.evaluate((icon) => getComputedStyle(icon).rotate)).toBe("0deg");
  const tableBox = await visibleBox(page.getByTestId("quality-evaluation-results-table"));
  const analysisCellBox = await visibleBox(analysisToggle.locator("xpath=ancestor::td[1]"));
  const analysisToggleBox = await visibleBox(analysisToggle);
  const analysisBox = await visibleBox(
    page.getByTestId("quality-evaluation-analysis-detail").filter({ visible: true }).first()
  );
  expect(analysisToggleBox.y - analysisCellBox.y).toBeGreaterThanOrEqual(6);
  expect(analysisBox.width).toBeGreaterThan(tableBox.width * 0.85);
  expect(analysisBox.x).toBeLessThanOrEqual(tableBox.x + 24);
  expect(analysisBox.x + analysisBox.width).toBeGreaterThanOrEqual(
    tableBox.x + tableBox.width - 40
  );
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  ).toBe(true);
  expect(state.submittedBody()).toContain("select_ai");
  expect(state.submittedBody()).toContain("enterprise_ai_direct");
  expect(state.submittedBody()).toContain("repeat_count");

  // テンプレートのダウンロード中は、押したテンプレートのボタンだけが回り、結果のボタンは無効にするだけ。
  // 結果のダウンロードの処理中の表示も出さない（押したボタンだけが loading を持つ。#819）。
  const templateGate = createRequestGate();
  await page.route("**/api/nl2sql/quality-evaluations/template.xlsx", async (route) => {
    await templateGate.promise;
    await route.fallback();
  });
  const templateButton = page.getByRole("button", { name: "テンプレートをダウンロード" });
  const resultsButton = page.getByRole("button", { name: "結果 Excel をダウンロード" });
  const templateDownload = page.waitForEvent("download");
  await templateButton.click();
  await expect(templateButton).toHaveAttribute("aria-busy", "true");
  await expect(resultsButton).toBeDisabled();
  await expect(resultsButton).not.toHaveAttribute("aria-busy", /.*/);
  await expect(page.getByTestId("quality-evaluation-download-processing")).toHaveCount(0);
  templateGate.release();
  await templateDownload;
  await expect(templateButton).not.toHaveAttribute("aria-busy", /.*/);
  await expect(resultsButton).toBeEnabled();

  const downloadPromise = page.waitForEvent("download");
  await resultsButton.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(
    "nl2sql_quality_evaluation_20260722_job-001.xlsx"
  );
});

test("結果明細と最近の job は、offset 型の API を共通の Pagination で送り、読み込み中は経過時間と Skeleton を出す", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockQualityApi(page, { fixedStatus: "completed_with_errors", recentJobs: repeatedJobs(3) });
  // offset 型の API（offset / limit / total。#1891）を返す。後から登録した route が優先される。
  const allResults = repeatedResults(53);
  const allJobs = repeatedJobs(27);
  const resultGate = createRequestGate();
  const jobGate = createRequestGate();
  const requestedResultLimits: string[] = [];
  const offsetPage = <T,>(items: T[], url: URL) => {
    const limit = Number(url.searchParams.get("limit") ?? 25);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const page = items.slice(offset, offset + limit);
    return { items: page, total: items.length, limit, offset, has_next: offset + page.length < items.length };
  };
  await page.route(`**${basePath}/job-001/results**`, async (route) => {
    const url = new URL(route.request().url());
    requestedResultLimits.push(url.searchParams.get("limit") ?? "");
    await resultGate.promise;
    return envelope(route, offsetPage(allResults, url));
  });
  await page.route(`**${basePath}?**`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await jobGate.promise;
    return envelope(route, offsetPage(allJobs, new URL(route.request().url())));
  });

  await page.goto("/evaluation?job=job-001");

  const recentLoading = page.getByTestId("quality-evaluation-recent-jobs-loading");
  await expect(recentLoading).toContainText("最近の SQL生成評価 job を読み込んでいます");
  await expect(recentLoading.getByRole("timer")).toBeVisible();
  await expect(recentLoading.locator('[data-skeleton="list"]')).toBeVisible();
  const resultsLoading = page.getByTestId("quality-evaluation-results-loading");
  await expect(resultsLoading).toContainText("結果明細を読み込んでいます");
  await expect(resultsLoading.getByRole("timer")).toBeVisible();
  resultGate.release();
  jobGate.release();
  await expect(recentLoading).toHaveCount(0);
  await expect(resultsLoading).toHaveCount(0);
  expect(requestedResultLimits[0]).toBe("25");

  const results = page.getByTestId("quality-evaluation-results-pagination");
  await expect(results).toHaveAttribute("aria-label", "結果明細のページ切替");
  await expect(results).toContainText("1–25 / 53 件");
  await expect(results).toContainText("1 / 3 ページ");
  await expect(results.getByRole("button", { name: "前へ" })).toBeDisabled();
  await results.getByRole("button", { name: "次へ" }).click();
  await expect(results).toContainText("26–50 / 53 件");
  await results.getByRole("button", { name: "次へ" }).click();
  await expect(results).toContainText("51–53 / 53 件");
  await expect(results).toContainText("3 / 3 ページ");
  await expect(results.getByRole("button", { name: "次へ" })).toBeDisabled();
  await results.getByRole("button", { name: "前へ" }).click();
  await expect(results).toContainText("26–50 / 53 件");
  // ページ番号を入れて移り、件数は一覧の上の 1 か所（ListCount。#1891）。
  await jumpToPage(results, 3);
  await expect(results).toContainText("51–53 / 53 件");
  await expect(page.getByTestId("quality-evaluation-results-count")).toHaveText("全 53 件");
  // 表示件数を変えると 1 ページ目に戻る。
  await chooseSelectFieldOption(results.getByRole("combobox", { name: "表示件数" }), "100");
  await expect(results).toContainText("1–53 / 53 件");
  expect(requestedResultLimits.at(-1)).toBe("100");
  // 結果表（min-w 74rem）があっても、ページ送りはセクションの枠の中に収まる（横に押し出さない）。
  const contained = await results.evaluate((nav) => {
    const section = nav.closest("section")!;
    const sectionBox = section.getBoundingClientRect();
    const navBox = nav.getBoundingClientRect();
    return navBox.left >= sectionBox.left - 1 && navBox.right <= sectionBox.right + 1 && section.scrollWidth <= section.clientWidth + 1;
  });
  expect(contained).toBe(true);

  const jobs = page.getByTestId("quality-evaluation-recent-jobs-pagination");
  await expect(jobs).toContainText("1–25 / 27 件");
  await expect(page.getByTestId("quality-evaluation-recent-jobs-count")).toHaveText("全 27 件");
  await jobs.getByRole("button", { name: "次へ" }).click();
  await expect(jobs).toContainText("26–27 / 27 件");
  await expect(page.getByTestId("quality-evaluation-recent-jobs-scroll-region").locator("article")).toHaveCount(2);
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  expect(await findClippedPlaceholders(page)).toEqual([]);
});

test("desktop constrains result details and recent jobs to internal scroll regions", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop scroll regions");
  await mockQualityApi(page, {
    fixedStatus: "completed_with_errors",
    resultItems: repeatedResults(12),
    recentJobs: repeatedJobs(5),
  });
  await page.goto("/evaluation?job=job-001");

  const resultRegion = page.getByTestId("quality-evaluation-results-table");
  await expect(resultRegion).toHaveAttribute("role", "region");
  await expect(resultRegion).toHaveAttribute(
    "aria-label",
    "結果明細一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(resultRegion.locator("tbody tr").first()).toBeVisible();
  const resultMetrics = await resultRegion.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
    const computed = window.getComputedStyle(node);
    const rootFontSize = Number.parseFloat(
      window.getComputedStyle(document.documentElement).fontSize
    );
    const header = node.querySelector("thead");
    if (!header) throw new Error("result table header is missing");
    const regionRect = node.getBoundingClientRect();
    const headerRect = header.getBoundingClientRect();
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      scrollTop: node.scrollTop,
      maxHeight: Number.parseFloat(computed.maxHeight),
      overflowX: computed.overflowX,
      overflowY: computed.overflowY,
      expectedMaxHeight: rootFontSize * 30.5,
      headerOffset: Math.abs(headerRect.top - regionRect.top),
      headerPosition: window.getComputedStyle(header).position,
    };
  });
  expect(resultMetrics.maxHeight).toBeGreaterThanOrEqual(
    resultMetrics.expectedMaxHeight - 2
  );
  expect(resultMetrics.maxHeight).toBeLessThanOrEqual(resultMetrics.expectedMaxHeight + 2);
  expect(resultMetrics.scrollHeight).toBeGreaterThan(resultMetrics.clientHeight);
  expect(resultMetrics.scrollTop).toBeGreaterThan(0);
  expect(resultMetrics.overflowX).toBe("auto");
  expect(resultMetrics.overflowY).toBe("auto");
  expect(resultMetrics.headerPosition).toBe("sticky");
  expect(resultMetrics.headerOffset).toBeLessThanOrEqual(1);

  const recentRegion = page.getByTestId("quality-evaluation-recent-jobs-scroll-region");
  await expect(recentRegion).toHaveAttribute("role", "region");
  await expect(recentRegion).toHaveAttribute(
    "aria-label",
    "最近の SQL生成評価 job 一覧。必要に応じて縦方向にスクロールできます。"
  );
  await expect(recentRegion.locator("article")).toHaveCount(5);
  const recentMetrics = await recentRegion.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    const rootFontSize = Number.parseFloat(
      window.getComputedStyle(document.documentElement).fontSize
    );
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      maxHeight: Number.parseFloat(computed.maxHeight),
      overflowY: computed.overflowY,
      expectedMaxHeight: rootFontSize * 17.5,
    };
  });
  expect(recentMetrics.maxHeight).toBeGreaterThanOrEqual(
    recentMetrics.expectedMaxHeight - 2
  );
  expect(recentMetrics.maxHeight).toBeLessThanOrEqual(recentMetrics.expectedMaxHeight + 2);
  expect(recentMetrics.scrollHeight).toBeGreaterThan(recentMetrics.clientHeight);
  expect(recentMetrics.overflowY).toBe("auto");

  await recentRegion.focus();
  await expect(recentRegion).toBeFocused();
  await page.keyboard.press("PageDown");
  await expect.poll(() => recentRegion.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
});

test("recent active quality evaluation jobs keep polling until terminal", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop polling");
  await mockQualityApi(page, { recentStatusSequence: ["pending", "completed"] });
  await page.goto("/evaluation");

  const recentRegion = page.getByTestId("quality-evaluation-recent-jobs-scroll-region");
  await expect(recentRegion.getByText("待機中")).toBeVisible();
  await expect(recentRegion.getByText("完了")).toBeVisible({ timeout: 6_000 });
});

test("quality evaluation job detail stops polling after the job is deleted", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop polling");
  await mockQualityApi(page);
  let jobReads = 0;
  await page.route("**/api/nl2sql/quality-evaluations/job-001", (route) => {
    jobReads += 1;
    if (jobReads === 1) return envelope(route, job("running"));
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "指定されたSQL生成評価 job が見つかりません。" }),
    });
  });

  await page.goto("/evaluation?job=job-001");

  await expect(page.getByText("実行中", { exact: true })).toBeVisible();
  await expect.poll(() => jobReads, { timeout: 6_000 }).toBe(2);
  await expect(page.getByText("SQL生成評価データを読み込めませんでした。")).toBeVisible();
  await page.waitForTimeout(2_200);
  expect(jobReads).toBe(2);
});

test("desktop shows stale attempt diagnostics and cancels a running job", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop stale cancel flow");
  const staleJob = job("running", {
    job_id: "job-001",
    profile_name: "Agent停滞",
    current_attempt_started_at: "2026-07-22T08:00:00Z",
    heartbeat_at: "2026-07-22T08:00:02Z",
    lease_expires_at: "2026-07-22T08:05:30Z",
    attempt_timeout_seconds: 300,
  });
  const state = await mockQualityApi(page, {
    currentJob: staleJob,
    recentJobs: [staleJob],
  });
  await page.goto("/evaluation?job=job-001");

  await expect(page.getByText("試行 timeout を検知しました")).toBeVisible();
  await expect(page.getByText("実行中の試行が 300 秒を超過しています。")).toBeVisible();
  await expect(page.getByTestId("quality-evaluation-job-diagnostics")).toContainText("300秒");
  // 並行の試行の数と上限（#2059）。
  await expect(page.getByTestId("quality-evaluation-running-attempts")).toHaveText(
    "同時に実行中 2 / 2 試行"
  );
  await page
    .getByRole("button", { name: "中止" })
    .first()
    .click();
  const dialog = page.getByRole("alertdialog", { name: "SQL生成評価 job を中止しますか" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "job を中止" }).click();

  await expect.poll(() => state.cancelledJobIds()).toEqual(["job-001"]);
  await expect(page.getByText("SQL生成評価 job を中止しました。", { exact: true })).toBeVisible();
  await expect(
    page.getByText("利用者の操作で SQL生成評価 job を中止しました。", { exact: true }).first()
  ).toBeVisible();
  await expect(page.getByText("中止").first()).toBeVisible();
});

async function openJobActions(page: Page, jobId: string) {
  // 中止・削除は行内に並べず、行メニュー（その他の操作）にまとめる（platform/docs/ux-contracts/buttons.md §5.1）。
  await page.getByTestId(`quality-evaluation-job-actions-${jobId}-trigger`).click();
  return page.getByRole("menu");
}

test("desktop keeps view inline, puts cancel/delete in the row menu and cancels without calling the API", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop delete action layout");
  const state = await mockQualityApi(page, {
    recentJobs: [
      job("completed", {
        job_id: "delete-me",
        profile_name: "削除対象",
        created_at: "2026-07-22T08:01:00Z",
      }),
      job("running", {
        job_id: "running-job",
        profile_name: "実行中対象",
        created_at: "2026-07-22T08:00:00Z",
      }),
    ],
  });
  await page.goto("/evaluation");

  const recentRegion = page.getByTestId("quality-evaluation-recent-jobs-scroll-region");
  const completedRow = recentRegion.locator("article").filter({ hasText: "削除対象" });
  const runningRow = recentRegion.locator("article").filter({ hasText: "実行中対象" });
  const viewButton = completedRow.getByRole("button", { name: "結果を表示" });
  const trigger = page.getByTestId("quality-evaluation-job-actions-delete-me-trigger");
  const viewBox = await visibleBox(viewButton);
  const triggerBox = await visibleBox(trigger);
  expect(triggerBox.x).toBeGreaterThanOrEqual(viewBox.x + viewBox.width - 1);
  await expect(completedRow.getByRole("button", { name: /を削除$|^削除$/ })).toHaveCount(0);
  await expect(runningRow.getByRole("button", { name: /を中止$|^中止$/ })).toHaveCount(0);

  const runningMenu = await openJobActions(page, "running-job");
  await expect(runningMenu.getByRole("menuitem", { name: /実行中対象.*を中止/ })).toBeEnabled();
  await expect(
    runningMenu.getByRole("menuitem", { name: "実行中または待機中の job は完了後に削除できます。" })
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(runningMenu).toHaveCount(0);

  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = page.getByRole("menu");
  await expect(menu.getByRole("menuitem", { name: /削除対象.*を削除/ })).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("alertdialog", { name: "SQL生成評価 job を削除しますか" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("削除対象");
  await dialog.getByRole("button", { name: "キャンセル" }).click();

  await expect(dialog).toBeHidden();
  expect(state.deletedJobIds()).toEqual([]);
  await expect(completedRow).toHaveCount(1);
});

test("desktop confirms delete, removes the current job URL and clears the page state", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop delete confirmation");
  const state = await mockQualityApi(page, {
    fixedStatus: "completed",
    recentJobs: [
      job("completed", {
        job_id: "job-001",
        profile_name: "現在 job",
        created_at: "2026-07-22T08:02:00Z",
      }),
    ],
  });
  await page.goto("/evaluation?job=job-001");

  const recentRegion = page.getByTestId("quality-evaluation-recent-jobs-scroll-region");
  const currentRow = recentRegion.locator("article").filter({ hasText: "現在 job" });
  await (await openJobActions(page, "job-001")).getByRole("menuitem", { name: /現在 job.*を削除/ }).click();
  const dialog = page.getByRole("alertdialog", { name: "SQL生成評価 job を削除しますか" });
  await dialog.getByRole("button", { name: "削除" }).click();

  await expect.poll(() => state.deletedJobIds()).toEqual(["job-001"]);
  await expect(page.getByText("SQL生成評価 job を削除しました。")).toBeVisible();
  await expect(page).not.toHaveURL(/\?job=/);
  await expect(currentRow).toHaveCount(0);
  await expect(page.getByText("SQL生成評価 job はまだありません")).toBeVisible();
  await expect(page.getByText("結果明細はまだありません")).toBeVisible();
});

test("mobile recent job delete actions do not create horizontal overflow", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-375", "mobile delete actions");
  await mockQualityApi(page, {
    recentJobs: [
      job("completed", {
        job_id: "mobile-delete",
        profile_name: "モバイル削除",
      }),
      job("pending", {
        job_id: "mobile-pending",
        profile_name: "モバイル待機",
      }),
    ],
  });
  await page.goto("/evaluation");

  const recentRegion = page.getByTestId("quality-evaluation-recent-jobs-scroll-region");
  const completedRow = recentRegion.locator("article").filter({ hasText: "モバイル削除" });
  const rowBox = await visibleBox(completedRow);
  const viewBox = await visibleBox(completedRow.getByRole("button", { name: "結果を表示" }));
  const triggerBox = await visibleBox(page.getByTestId("quality-evaluation-job-actions-mobile-delete-trigger"));
  expect(viewBox.x).toBeGreaterThanOrEqual(rowBox.x - 1);
  expect(triggerBox.x + triggerBox.width).toBeLessThanOrEqual(rowBox.x + rowBox.width + 1);
  const pendingMenu = await openJobActions(page, "mobile-pending");
  await expect(
    pendingMenu.getByRole("menuitem", { name: "実行中または待機中の job は完了後に削除できます。" })
  ).toBeDisabled();
  const menuBox = await visibleBox(pendingMenu);
  const viewport = page.viewportSize()!;
  expect(menuBox.x).toBeGreaterThanOrEqual(0);
  expect(menuBox.x + menuBox.width).toBeLessThanOrEqual(viewport.width + 1);
  await page.keyboard.press("Escape");
  const noHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth
  );
  expect(noHorizontalOverflow).toBe(true);
});

test("start validation errors stay in the action footer", { tag: "@responsive" }, async ({ page }) => {
  const validationError = "行 3: ケースID「CASE-001」が重複しています。";
  await mockQualityApi(page, { startValidationErrors: [validationError] });
  await page.goto("/evaluation");

  await page.getByRole("checkbox").nth(0).check();
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [
    {
      name: "quality-cases.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  await page.getByRole("button", { name: "評価を開始" }).click();

  const actionFooter = page.getByTestId("quality-evaluation-action-footer");
  const actionError = actionFooter.getByRole("alert").filter({ hasText: validationError });
  await expect(actionError).toBeVisible();
  await expect(actionError).not.toContainText("QUALITY_EVALUATION_VALIDATION_ERROR");
  const footerBox = await visibleBox(actionFooter);
  const errorBox = await visibleBox(actionError);
  expect(errorBox.y).toBeGreaterThanOrEqual(footerBox.y);
  await expect(
    page.locator("main > [role='alert']").filter({ hasText: validationError })
  ).toHaveCount(0);
  await expect(page).not.toHaveURL(/\?job=/);
  const noHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth
  );
  expect(noHorizontalOverflow).toBe(true);
});

test("unavailable engines remain disabled when capabilities mark them unavailable", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop unavailable engine state");
  await mockQualityApi(page, {
    engines: capabilities.engines.map((engine) =>
      engine.engine === "select_ai_agent"
        ? { ...engine, available: false, reason: "Agent team が未構成です。" }
        : engine
    ),
  });
  await page.goto("/evaluation");

  const checkboxes = page.getByRole("checkbox");
  await expect(checkboxes.nth(0)).toBeEnabled();
  await expect(checkboxes.nth(1)).toBeDisabled();
  await expect(checkboxes.nth(2)).toBeEnabled();
  await expect(page.getByText("Agent team が未構成です。")).toBeVisible();
  await expect(page.getByText("利用不可")).toBeVisible();
});

test("mobile restores completed results as cards without page overflow", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-375", "mobile flow");
  await mockQualityApi(page, { fixedStatus: "completed_with_errors" });
  await page.goto("/evaluation?job=job-001");

  const engineBulkActions = page.getByTestId("quality-evaluation-engine-selection-actions");
  const engineGrid = page.getByTestId("quality-evaluation-engine-grid");
  await expect
    .poll(async () => {
      const [actionsBox, gridBox] = await Promise.all([
        engineBulkActions.boundingBox(),
        engineGrid.boundingBox(),
      ]);
      if (!actionsBox || !gridBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - gridBox.x);
    })
    .toBeLessThanOrEqual(1);

  await expectVerticalOrder([
    page.getByTestId("quality-evaluation-profile-field"),
    page.getByTestId("quality-evaluation-file"),
    page.getByTestId("quality-evaluation-engine-fieldset"),
    page.getByTestId("quality-evaluation-repeat-field"),
    page.getByTestId("quality-evaluation-estimate-summary"),
    page.getByTestId("quality-evaluation-judge-note"),
    page.getByTestId("quality-evaluation-action-footer"),
  ]);
  await expect(
    page
      .locator("article")
      .filter({ hasText: "対象テーブル", visible: true })
      .first()
  ).toBeVisible();
  await expect(page.locator("table")).toBeHidden();
  await expect(
    page.getByText("OCI Enterprise AI timeout").filter({ visible: true }).first()
  ).toBeVisible();
  const mobileAnalysisToggle = page
    .getByTestId("quality-evaluation-analysis-toggle")
    .filter({ visible: true })
    .first();
  const mobileAnalysisChevron = mobileAnalysisToggle.locator("svg");
  await expect.poll(() => mobileAnalysisChevron.evaluate((icon) => getComputedStyle(icon).rotate)).toBe("-90deg");
  await mobileAnalysisToggle.click();
  await expect.poll(() => mobileAnalysisChevron.evaluate((icon) => getComputedStyle(icon).rotate)).toBe("0deg");
  await expect(
    page.getByTestId("quality-evaluation-analysis-detail").filter({ visible: true }).first()
  ).toBeVisible();
  const noHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth
  );
  expect(noHorizontalOverflow).toBe(true);
});

test("mobile constrains result cards to an internal vertical scroll region", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-375", "mobile scroll region");
  await mockQualityApi(page, {
    fixedStatus: "completed_with_errors",
    resultItems: repeatedResults(8),
  });
  await page.goto("/evaluation?job=job-001");

  await expect(page.getByTestId("quality-evaluation-results-table")).toBeHidden();
  const cardRegion = page.getByTestId("quality-evaluation-results-cards-scroll-region");
  await expect(cardRegion).toHaveAttribute("role", "region");
  await expect(cardRegion).toHaveAttribute(
    "aria-label",
    "結果明細一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(cardRegion.locator("article")).toHaveCount(8);
  const cardMetrics = await cardRegion.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
    const computed = window.getComputedStyle(node);
    const rootFontSize = Number.parseFloat(
      window.getComputedStyle(document.documentElement).fontSize
    );
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      scrollTop: node.scrollTop,
      maxHeight: Number.parseFloat(computed.maxHeight),
      overflowY: computed.overflowY,
      expectedMaxHeight: rootFontSize * 37.5,
    };
  });
  expect(cardMetrics.maxHeight).toBeGreaterThanOrEqual(cardMetrics.expectedMaxHeight - 2);
  expect(cardMetrics.maxHeight).toBeLessThanOrEqual(cardMetrics.expectedMaxHeight + 2);
  expect(cardMetrics.scrollHeight).toBeGreaterThan(cardMetrics.clientHeight);
  expect(cardMetrics.scrollTop).toBeGreaterThan(0);
  expect(cardMetrics.overflowY).toBe("auto");
  const noHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth
  );
  expect(noHorizontalOverflow).toBe(true);
});

test("form errors, unavailable engines, Judge readiness and failed jobs are explicit", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop validation states");
  await mockQualityApi(page);
  await page.goto("/evaluation");
  await page.getByRole("button", { name: "評価を開始" }).click();
  await expect(page.getByText(".xlsx ファイルを選択してください。")).toBeVisible();
  await expect(page.getByText("利用可能な実行エンジンを1つ以上選択してください。")).toBeVisible();
  await expect(page.getByTestId("quality-evaluation-file-input")).toHaveAttribute(
    "accept",
    ".xlsx"
  );
  await expect(page.getByText(".XLSX", { exact: true })).toBeVisible();
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [
    {
      name: "invalid.csv",
      type: "text/csv",
      content: "QUESTION,EXPECTED_SQL",
    },
  ]);
  await expect(page.getByText(".xlsx ファイルのみ使用できます。")).toBeVisible();

  await page.unrouteAll({ behavior: "wait" });
  await mockDatabaseGateReady(page);
  await mockQualityApi(page, { judgeAvailable: false, fixedStatus: "failed" });
  await page.goto("/evaluation?job=job-001");
  await expect(page.getByText("LLM Judge を利用できません", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "評価を開始" })).toBeDisabled();
  await expect(
    page.getByText("worker の初期化に失敗しました。").filter({ visible: true }).first()
  ).toBeVisible();
});

test("evaluation submission locks conditions and preserves them after rejection", async ({ page }, testInfo) => {
  await mockQualityApi(page);
  const gate = createRequestGate();
  let calls = 0;
  await page.route(`**${basePath}`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    calls += 1;
    await gate.promise;
    await route.fulfill({ status: 503, json: { detail: "評価の開始に失敗しました" } });
  });
  await page.goto("/evaluation");
  const repeat = page.getByLabel("繰り返し回数");
  await repeat.fill("2");
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [{
    name: "pending-cases.xlsx", type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    content: "mock xlsx",
  }]);
  await page.getByTestId("quality-evaluation-engine-fieldset").getByRole("checkbox").first().check();
  const start = page.getByRole("button", { name: "評価を開始", exact: true });
  await start.click();
  await expect(repeat).toBeDisabled();
  await expect(page.getByTestId("quality-evaluation-profile-field").getByRole("combobox")).toBeDisabled();
  await expect(page.getByTestId("quality-evaluation-engine-fieldset").getByRole("checkbox").first()).toBeDisabled();
  await expect(page.getByTestId("quality-evaluation-file-input")).toBeDisabled();
  await expect(start).toBeDisabled();
  gate.release();
  await expect(start).toBeEnabled();
  await expect(repeat).toBeEnabled();
  await expect(repeat).toHaveValue("2");
  await expect(page.getByText("pending-cases.xlsx", { exact: false }).first()).toBeVisible();
  expect(calls).toBe(1);
  await start.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => calls).toBe(2);
  await expect(start).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("evaluation-submission-recovery.png"), fullPage: true });
});

test("業務プロファイルを切り替えても評価条件のフォームを出したままにし、取り直しの間は開始させない (#995)", async ({
  page,
}) => {
  await mockQualityApi(page);
  const profile = (id: string, name: string) => ({ id, name, category: "SQL生成評価", description: "", archived: false });
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    envelope(route, {
      items: [profile("default", "標準プロファイル"), profile("sales", "売上プロファイル")],
      next_cursor: null,
      total: 2,
    })
  );
  const salesGate = createRequestGate();
  await page.route("**/api/nl2sql/quality-evaluations/capabilities**", async (route) => {
    if (new URL(route.request().url()).searchParams.get("profile_id") === "sales") await salesGate.promise;
    return envelope(route, capabilities);
  });
  await page.goto("/evaluation");

  const inputRow = page.getByTestId("quality-evaluation-input-row");
  const profileSelect = page.getByTestId("quality-evaluation-profile-field").getByRole("combobox");
  await expect(inputRow).toBeVisible();
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [{
    name: "cases.xlsx",
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    content: "mock xlsx",
  }]);
  await page.getByTestId("quality-evaluation-engine-fieldset").getByRole("checkbox").first().check();
  const start = page.getByRole("button", { name: "評価を開始", exact: true });
  await expect(start).toBeEnabled();

  await profileSelect.click();
  await page.getByRole("option", { name: /売上プロファイル/ }).click();
  // 取り直しの間もフォームを出したまま（読み込み中の表示に置き換えない）。前の profile の実行可否では開始させない。
  await expect(inputRow).toBeVisible();
  await expect(page.getByTestId("quality-evaluation-conditions-loading")).toHaveCount(0);
  await expect(profileSelect).toBeFocused();
  await expect(start).toBeDisabled();

  salesGate.release();
  await expect(start).toBeEnabled();
  await expect(page.getByText("cases.xlsx", { exact: false }).first()).toBeVisible();
  await expect(page.getByTestId("quality-evaluation-engine-fieldset").getByRole("checkbox").first()).toBeChecked();
});

test("job の削除・中止の失敗は backend の理由を起点の近くに残す (#995)", async ({ page }, testInfo) => {
  await mockQualityApi(page, {
    currentJob: job("running", { job_id: "job-001", profile_name: "実行中の job" }),
    recentJobs: [
      job("completed", {
        job_id: "other-user-job",
        profile_name: "他の利用者の job",
        created_at: "2026-07-22T08:01:00Z",
      }),
    ],
  });
  // backend の problem 応答（app/api/problems.py）と同じ形。403 は経路の権限拒否ではない error_code。
  const apiFailure = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify({ data: null, error_messages: [message], warning_messages: [], error_code: code }),
    });
  await page.route("**/api/nl2sql/quality-evaluations/other-user-job", (route) =>
    route.request().method() === "DELETE"
      ? apiFailure(route, 403, "FORBIDDEN", "他のユーザーのSQL生成評価 job を操作する権限がありません。")
      : route.fallback()
  );
  await page.route("**/api/nl2sql/quality-evaluations/job-001/cancel", (route) =>
    apiFailure(route, 409, "CONFLICT", "SQL生成評価 job はすでに完了しています。")
  );
  await page.goto("/evaluation?job=job-001");

  await (await openJobActions(page, "other-user-job"))
    .getByRole("menuitem", { name: /他の利用者の job.*を削除/ })
    .click();
  await page
    .getByRole("alertdialog", { name: "SQL生成評価 job を削除しますか" })
    .getByRole("button", { name: "削除" })
    .click();
  const recentSection = page.locator("section[aria-labelledby='quality-evaluation-recent']");
  await expect(
    recentSection.getByText("他のユーザーのSQL生成評価 job を操作する権限がありません。")
  ).toBeVisible();
  await expect(page.getByText("SQL生成評価 job を削除できませんでした。", { exact: false })).toHaveCount(0);

  const progress = page.locator("section[aria-labelledby='quality-evaluation-progress']");
  await progress.getByRole("button", { name: "中止" }).click();
  await page
    .getByRole("alertdialog", { name: "SQL生成評価 job を中止しますか" })
    .getByRole("button", { name: "job を中止" })
    .click();
  await expect(progress.getByText("SQL生成評価 job はすでに完了しています。")).toBeVisible();
  // 次の操作を始めたので、一覧の削除の失敗は消す。
  await expect(page.getByText("他のユーザーのSQL生成評価 job を操作する権限がありません。")).toHaveCount(0);
  await progress.screenshot({ path: testInfo.outputPath("evaluation-cancel-failure.png") });
});

test("Select AI Credential が無いと Select AI の評価の開始を止めて事前条件を示し、Enterprise AI だけなら開始できる (#1688)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await mockQualityApi(page);
  const readiness = { exists: false };
  await page.route("**/api/nl2sql/select-ai/credential-readiness", (route) =>
    envelope(route, {
      credential_required: true,
      credential_name: "NL2SQL_OCI_CRED",
      credential_exists: readiness.exists,
    })
  );
  await page.goto("/evaluation");
  await dropFiles(page, page.getByTestId("quality-evaluation-file-dropzone"), [
    {
      name: "quality-cases.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  const precondition = page.getByTestId("quality-evaluation-credential-precondition");
  const start = page.getByRole("button", { name: "評価を開始" });
  // Select AI を選ぶまでは出さない（選んだ方式が Credential を使うときだけ）。
  await expect(precondition).toHaveCount(0);
  await page.getByRole("checkbox").nth(0).check();
  await expect(precondition).toContainText("Select AI Credential が作成されていません");
  await expect(precondition).toContainText("作成するまで評価を開始できません。");
  await expect(start).toBeDisabled();
  await expect(start).toHaveAttribute("aria-describedby", "quality-evaluation-credential-precondition");
  // 開始の操作の行（区切り線の上）の直前に出す。
  const noticeBox = await visibleBox(precondition);
  const footerBox = await visibleBox(page.getByTestId("quality-evaluation-action-footer"));
  expect(noticeBox.y + noticeBox.height).toBeLessThanOrEqual(footerBox.y);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  ).toBe(true);
  await precondition.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("evaluation-credential-missing.png") });

  // Enterprise AI だけなら Credential を使わないので開始できる。
  await page.getByRole("checkbox").nth(0).uncheck();
  await page.getByRole("checkbox").nth(2).check();
  await expect(precondition).toHaveCount(0);
  await expect(start).toBeEnabled();
  await start.click();
  await expect.poll(() => state.submittedBody()).toContain("enterprise_ai_direct");
  expect(state.submittedBody()).not.toContain('name="engines"\r\n\r\nselect_ai\r\n');
});
