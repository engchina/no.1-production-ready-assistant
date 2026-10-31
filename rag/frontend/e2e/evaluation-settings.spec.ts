import { FLOW_STAGES } from "./_prompt-stage-fixtures";
import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`評価の基準は品質評価の基準を表示する (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockEvaluation(page, "standard");

    await page.goto("/settings/evaluation");
    await expect(page.getByText(/ほかの五指標は 100%/)).toBeVisible();

    await expect(page.getByRole("heading", { name: "評価の基準", exact: true, level: 1 })).toBeVisible();
    // 基準は標準・厳格の 2 つだけ(#591)。
    await expect(page.getByRole("radio")).toHaveCount(2);
    await expect(page.getByRole("radio", { name: /標準/ })).toBeChecked();
    await expect(page.getByRole("radio", { name: /厳格/ })).toBeVisible();
    // 選んだ基準の閾値を、検索・回答・応答の判断の観点ごとに指標の意味と一緒に出す。
    const metrics = page.getByTestId("settings-evaluation-metrics");
    await expect(metrics.getByRole("heading", { name: "標準の指標と閾値" })).toBeVisible();
    for (const perspective of ["検索", "回答", "応答の判断"]) {
      await expect(metrics.getByRole("heading", { name: perspective, exact: true })).toBeVisible();
    }
    const search = await metrics.getByRole("heading", { name: "検索", exact: true }).locator("..").boundingBox();
    const answer = await metrics.getByRole("heading", { name: "回答", exact: true }).locator("..").boundingBox();
    const decision = await metrics.getByRole("heading", { name: "応答の判断", exact: true }).locator("..").boundingBox();
    expect(search).not.toBeNull();
    expect(answer).not.toBeNull();
    expect(decision).not.toBeNull();
    expect(decision!.x).toBe(answer!.x);
    expect(decision!.y).toBeGreaterThanOrEqual(answer!.y + answer!.height);
    if (viewport.name === "desktop") {
      expect(answer!.x).toBeGreaterThan(search!.x);
      expect(answer!.y).toBe(search!.y);
    } else {
      expect(answer!.x).toBe(search!.x);
      expect(answer!.y).toBeGreaterThan(search!.y);
    }
    await metrics.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("evaluation-decision-under-answer.png") });
    await expect(metrics.locator("[data-testid^='settings-evaluation-metric-']")).toHaveCount(6);
    await expect(page.getByTestId("settings-evaluation-metric-required_evidence_recall")).toContainText(
      "閾値 90%"
    );
    await expect(
      page.getByTestId("settings-evaluation-metric-answer_correctness")
    ).toContainText("標準回答または必須要点が必要");
    // 変更が実行済みの評価に効かないことを案内する（#1029）。
    await expect(page.getByRole("main")).toContainText("実行済みの評価の合否は変わりません");
    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    await expect(
      (await openSidebarNav(page))
        .locator("#nav-section-nav-section-pipeline")
        .getByRole("link", { name: "評価の基準" })
    ).toHaveAttribute("aria-current", "page");
    await expectNoHorizontalOverflow(page);
  });
}

test("評価の基準は厳格を選んで閾値を表示し保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let saved: unknown = null;
  await page.route("**/api/settings/evaluation-suite", async (route) => {
    if (route.request().method() === "PATCH") {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: evaluationEnvelope("strict") });
      return;
    }
    await route.fulfill({ json: evaluationEnvelope("standard") });
  });

  await page.goto("/settings/evaluation");

  const strict = page.getByRole("radio", { name: /厳格/ });
  await strict.click();
  await expect(strict).toBeChecked();
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();
  await expect(page.getByTestId("settings-evaluation-metric-claim_support_rate")).toContainText(
    "閾値 100%"
  );

  const actions = page.getByRole("group", { name: "評価の基準の操作" });
  await actions.getByRole("button", { name: "保存" }).click();

  // 保存の成功は Toast（messaging.md §10.2）。操作の行に常設の成功の表示を残さない。
  await expect(page.getByText("評価の基準を保存しました。")).toBeVisible();
  await expect(actions).not.toContainText("保存しました");
  await expect(actions.getByRole("button", { name: "変更を破棄" })).toBeDisabled();
  expect(saved).toEqual({ suite: "strict" });
  await expectNoHorizontalOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
]) {
  test(`評価の基準の保存に失敗しても選択を残して操作の行に出し、変更を破棄で戻せる（#1029, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route("**/api/settings/evaluation-suite", async (route) => {
      if (route.request().method() === "PATCH") {
        await route.fulfill({
          status: 500,
          json: {
            data: null,
            error_messages: ["品質評価設定を backend/.env へ保存できませんでした。"],
            warning_messages: [],
          },
        });
        return;
      }
      await route.fulfill({ json: evaluationEnvelope("standard") });
    });

    await page.goto("/settings/evaluation");
    const strict = page.getByRole("radio", { name: /厳格/ });
    await strict.click();
    const actions = page.getByRole("group", { name: "評価の基準の操作" });
    await actions.getByRole("button", { name: "保存" }).click();

    await expect(actions).toContainText("品質評価設定を backend/.env へ保存できませんでした。");
    await expect(strict).toBeChecked();
    await expect(actions.getByRole("button", { name: "保存" })).toBeEnabled();
    await expectNoHorizontalOverflow(page);
    await actions.getByRole("button", { name: "変更を破棄" }).click();
    await expect(page.getByRole("radio", { name: /標準/ })).toBeChecked();
    await expect(actions.getByRole("button", { name: "保存" })).toBeDisabled();
  });
}

test("品質評価設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/evaluation-suite", async (route) => {
    await route.fulfill({
      status: 503,
      json: { data: null, error_messages: ["品質評価設定を取得できませんでした。"], warning_messages: [] },
    });
  });

  await page.goto("/settings/evaluation");

  await expect(page.getByRole("alert")).toContainText("品質評価設定を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
});

async function collapseSidebar(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
}

function evaluationEnvelope(suite: string) {
  const thresholds = (strict: boolean) => ({
 required_evidence_recall:strict?1:0.9,mrr:strict?0.8:0.6,answer_correctness:strict?1:0.9,
 requirement_coverage:strict?1:0.9,claim_support_rate:strict?1:0.95,response_decision_accuracy:strict?1:0.9,
  });
  const specs = [
    { name: "standard", thresholds: thresholds(false) },
    { name: "strict", thresholds: thresholds(true) },
  ];
  const selected = specs.find((s) => s.name === suite) ?? specs[0];
  return {
    data: {
      suite,
      thresholds: selected.thresholds,
      suites: specs.map((s) => ({
        ...s,
        origin: "x",
        recommended_for: ["general"],
        selected: s.name === suite,
      })),
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

async function mockEvaluation(page: Page, suite: string) {
  await page.route("**/api/settings/evaluation-suite", async (route) => {
    await route.fulfill({ json: evaluationEnvelope(suite) });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}


test("回答の品質評価だけを評価の基準で読み取れる", { tag: "@responsive" }, async ({ page }) => {
  await mockEvaluation(page, "standard");
  await page.route("**/api/settings/answer-prompts**", (route) => route.fulfill({
    json: { data: { prompts: [], stages: FLOW_STAGES }, error_messages: [], warning_messages: [] },
  }));
  await page.goto("/settings/evaluation");
  const stage = page.getByTestId("answer-prompt-stage-evaluation");
  await expect(stage).toContainText("回答の品質評価");
  await expect(page.getByTestId("answer-prompt-stages-card")).toBeVisible();
  expect(await page.getByTestId("answer-prompt-stages-card").evaluate(node => parseFloat(getComputedStyle(node).borderTopWidth))).toBeGreaterThan(0);

  await expect(page.getByTestId("answer-prompt-stage-answer_validation")).toHaveCount(0);
  await stage.locator("summary").click();
  const fields = stage.getByRole("textbox");
  await expect(fields).toHaveCount(3);
  for (const field of await fields.all()) await expect(field).not.toBeEditable();
  expect(await fields.evaluateAll((elements) => elements.map((element) => (element as HTMLTextAreaElement).value)))
    .toEqual(["範囲の指示", "網羅性・忠実性の指示", "正確性の指示"]);
  await stage.getByRole("textbox").first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath("quality-evaluation-prompt.png") });
  await expectNoPageOverflow(page);
});

test("評価の基準の指示の編集内容を保存できる", { tag: "@responsive" }, async ({ page }) => {
  await mockEvaluation(page, "standard");
  const { mockEditableStage } = await import("./_feature-prompt-helpers");
  const requests = await mockEditableStage(page, "evaluation", "evaluation", "coverage");
  await page.goto("/settings/evaluation");
  const stage = page.getByTestId("answer-prompt-stage-evaluation");
  await stage.locator("summary").click();
  await expect(stage.getByRole("group", { name: "編集内容の操作" }).getByRole("button"))
    .toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  await stage.getByRole("textbox").fill("## Step 1: 合成の評価指示");
  await stage.getByRole("button", { name: "プロンプトを保存" }).click();
  await expect(stage.getByRole("button", { name: "プロンプトを保存" })).toBeDisabled();
  expect(requests).toEqual([{ key: "evaluation.coverage", method: "PUT", content: "## Step 1: 合成の評価指示" }]);
  await expectNoHorizontalOverflow(page);
});
