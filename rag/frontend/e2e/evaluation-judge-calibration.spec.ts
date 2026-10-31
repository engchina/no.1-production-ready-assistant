import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { expect, type Locator, type Page, test, type TestInfo } from "./fixtures/test";

import { jumpToPage } from "../../../platform/packages/ui/e2e/layout-checks";

import { mockEvaluationJobs, prepareEvaluationRun } from "./_evaluation-jobs";
import { expectNoPageOverflow, mockLocalAuth } from "./_helpers";
import { JUDGE_CALIBRATION_ROWS, mockJudgeCalibration } from "./_judge-calibration";

/**
 * 品質評価 › 判定の校正（#3578）。記入の集合を作る → blind の記入 → 一致と裁定 → 校正 → 品質評価の結果の
 * 「校正済み」の流れを、合成の内容の mock で desktop と 375px で確かめる。
 * `CALIBRATION_SHOTS_DIR` があれば、手順ごとの画面（本文だけ。サイドナビのアカウントを写さない）を保存する。
 */

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 375, height: 812 },
] as const;

async function shot(target: Locator, testInfo: TestInfo, step: string, viewport: string) {
  const dir = process.env.CALIBRATION_SHOTS_DIR;
  const name = step + "-" + viewport + ".png";
  if (dir) mkdirSync(dir, { recursive: true });
  // 本文は縦にスクロールする領域なので、要素の全体が入る高さに一時的に広げてから写す。
  const page = target.page();
  const size = page.viewportSize();
  const box = await target.boundingBox();
  if (size && box && box.height + 200 > size.height) {
    await page.setViewportSize({ width: size.width, height: Math.ceil(box.height + 240) });
  }
  await target.scrollIntoViewIfNeeded();
  // 通知（Toast）は写さない（手順の画面だけを残す）。
  await target.screenshot({
    path: dir ? join(dir, name) : testInfo.outputPath(name),
    animations: "disabled",
    style: "[data-toast-placement] { display: none !important; }",
  });
  if (size) await page.setViewportSize(size);
}

/** 表のセルの文字が 1 行に収まる（375px で列が細く潰れて折り返さない。#3583）。 */
async function expectOneLine(target: Locator) {
  await expect(target).toBeVisible();
  const { height, lineHeight } = await target.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const style = getComputedStyle(element);
    const fontSize = Number.parseFloat(style.fontSize);
    const parsed = Number.parseFloat(style.lineHeight);
    return {
      height: range.getBoundingClientRect().height,
      lineHeight: Number.isFinite(parsed) ? parsed : fontSize * 1.5,
    };
  });
  expect(height).toBeLessThan(lineHeight * 1.5);
}

async function mockEvaluationBasics(page: Page) {
  await mockLocalAuth(page);
  await page.route("**/api/settings/evaluation-suite", (route) =>
    route.fulfill({
      json: {
        data: {
          suite: "standard",
          thresholds: { answer_correctness: 0.9, requirement_coverage: 0.9, claim_support_rate: 0.95 },
          suites: [],
          config_source: "runtime",
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: {
        data: {
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
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
}

function answerMetrics() {
  return {
    case_count: 2,
    error_count: 0,
    evaluation_suite: "standard",
    passed: null,
    required_evidence_recall: 1,
    mrr: 1,
    answer_correctness: 0.85,
    requirement_coverage: 0.8,
    claim_support_rate: 0.92,
    response_decision_accuracy: 1,
    metric_case_counts: {
      required_evidence_recall: 1,
      mrr: 1,
      answer_correctness: 2,
      requirement_coverage: 2,
      claim_support_rate: 2,
      response_decision_accuracy: 2,
    },
    threshold_failures: [],
    failure_reason_counts: {},
    category_breakdown: {},
    decision_breakdown: {},
    case_results: [],
  };
}

for (const viewport of VIEWPORTS) {
  test("判定の校正は集合を作り、blind で記入し、裁定し、校正して結果に校正済みを出す (" + viewport.name + ")", { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockEvaluationBasics(page);
    await mockEvaluationJobs(page, { runResult: () => answerMetrics(), autoComplete: true });
    const calibration = await mockJudgeCalibration(page, { withSet: true });

    await page.goto("/evaluation?tab=calibration");
    await expect(page.getByRole("tab", { name: "判定の校正" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("judge-calibration-steps")).toBeVisible();
    await expect(page.getByTestId("calibration-label-sets").getByText("校正の集合 A")).toBeVisible();
    await expectOneLine(page.getByTestId("calibration-label-sets").getByRole("rowheader", { name: "校正の集合 A" }));

    // 1. 記入の集合を作る（終わった品質評価を選ぶ）。
    await page.getByRole("button", { name: "記入の集合を作る" }).click();
    const create = page.getByTestId("calibration-create");
    await create.getByRole("button", { name: "作成" }).click();
    await expect(create.getByText("名前を入力してください。")).toBeVisible();
    await create.getByLabel("名前").fill("校正の集合 B");
    await create.getByRole("button", { name: "作成" }).click();
    await expect(create.getByRole("alert")).toHaveText("品質評価を 1 つ以上選んでください。");
    await page.getByTestId("calibration-sources").getByRole("checkbox").first().check();
    await expect(create.getByRole("switch", { name: "合成の対照を含める" })).toBeChecked();
    await expectNoPageOverflow(page);
    await shot(create, testInfo, "01-build-set", viewport.name);
    await create.getByRole("button", { name: "作成" }).click();
    await expect(create).toHaveCount(0);
    const detail = page.getByTestId("calibration-detail");
    await expect(detail.getByRole("heading", { name: "校正の集合 B" })).toBeVisible();

    // 2. 記入（自分の判定だけ。他の記入者・LLM の判定は出さない）。
    const annotator = page.getByTestId("calibration-annotator");
    await expect(annotator.getByRole("status")).toHaveText("1 / " + JUDGE_CALIBRATION_ROWS + " 行");
    await expect(annotator.getByTestId("calibration-criterion")).toBeVisible();
    await expectOneLine(page.getByTestId("calibration-my-progress").getByRole("rowheader", { name: "正確性" }));
    await expect(annotator).not.toContainText("a2");
    const judgement = annotator.getByTestId("calibration-annotate-label-pass").locator("xpath=ancestor::fieldset");
    const spacing = await judgement.evaluate(element => {
      const legend = element.querySelector("legend")!;
      const button = element.querySelector("button")!;
      return {
        gap: button.getBoundingClientRect().top - legend.getBoundingClientRect().bottom,
        standard: Number.parseFloat(getComputedStyle(document.documentElement).fontSize) * 0.5,
      };
    });
    expect(spacing.gap).toBeGreaterThanOrEqual(spacing.standard - 0.5);
    await annotator.getByTestId("calibration-annotate-save").click();
    await expect(annotator.getByRole("alert")).toHaveText("判定を選んでください。");
    await annotator.getByTestId("calibration-annotate-label-fail").click();
    await expect(annotator.getByTestId("calibration-annotate-label-fail")).toHaveAttribute("aria-pressed", "true");
    await annotator.getByLabel("メモ").fill("期限の例外の条件が足りない");
    await expectNoPageOverflow(page);
    await shot(annotator, testInfo, "02-annotate", viewport.name);
    await annotator.getByTestId("calibration-annotate-save").click();
    await expect(annotator.getByRole("status")).toHaveText("1 / " + (JUDGE_CALIBRATION_ROWS - 1) + " 行");

    // 残りの行は合成の記入で埋める（a2 と 2 行だけ食い違う）。
    calibration.fillMine();
    // 合成の記入を画面に反映する（選んだ集合と手順は作業状態に残る）。
    await page.reload();
    await expect(detail.getByRole("heading", { name: "校正の集合 B" })).toBeVisible();

    // 3. 一致と裁定。
    await detail.getByRole("tab", { name: "2. 一致と裁定" }).click();
    const agreement = page.getByTestId("calibration-aspect-agreement");
    await expect(agreement).toContainText("a1–a2");
    await expectOneLine(agreement.getByRole("rowheader", { name: "正確性" }));
    const rows = page.getByTestId("calibration-agreement-rows");
    await expect(rows.getByRole("row")).toHaveCount(3);
    await rows.getByRole("button").first().click();
    const editor = page.getByTestId("calibration-adjudication-editor");
    await expect(editor).toContainText("根拠に無い断定がある");
    await editor.getByTestId("calibration-adjudicate-label-fail").click();
    await expectNoPageOverflow(page);
    await shot(page.getByTestId("calibration-agreement"), testInfo, "03-agreement", viewport.name);
    await editor.getByTestId("calibration-adjudicate-save").click();
    await expect(rows.getByRole("row")).toHaveCount(2);
    await rows.getByRole("button").first().click();
    await page.getByTestId("calibration-adjudicate-label-fail").click();
    await page.getByTestId("calibration-adjudicate-save").click();
    await expect(rows.getByText("この条件の行はありません")).toBeVisible();

    // 4. 校正（job で動き、観点 × 区分の κ・判定率・誤合格の率と合否、モデル・rubric の版・sha256）。
    await detail.getByRole("tab", { name: "3. 校正" }).click();
    const run = page.getByRole("button", { name: "校正を実行" });
    await expect(run).toBeEnabled();
    await run.click();
    const result = page.getByTestId("calibration-result");
    await expect(result.getByTestId("calibration-provenance")).toContainText("judge-model-a");
    await expect(result.getByTestId("calibration-provenance")).toContainText("5f2c0a9e1b7d");
    await expect(page.getByTestId("calibration-job")).toContainText("36 / 36 件");
    await expect(result.getByTestId("calibration-result-table")).toContainText("0.74");
    await expect(result.getByTestId("calibration-failures")).toContainText("κ 0.52 < 0.6");
    await expectNoPageOverflow(page);
    await shot(page.getByTestId("calibration-run"), testInfo, "04-calibrate", viewport.name);

    // 5. 品質評価の結果に、観点ごとの校正の状態を出す（指標の値は変えない）。
    await page.getByRole("tab", { name: "評価の実行" }).click();
    await expect(page).not.toHaveURL(/tab=calibration/);
    await prepareEvaluationRun(page);
    await page.getByRole("button", { name: "評価実行" }).click();
    const answer = page.getByTestId("evaluation-perspective-answer");
    await expect(answer.getByTestId("evaluation-metric-calibration-claim_support_rate")).toHaveText("校正済み κ=0.74");
    await expect(answer.getByTestId("evaluation-metric-calibration-answer_correctness")).toHaveText("未校正");
    await expect(answer.getByTestId("evaluation-metric-claim_support_rate")).toContainText("92");
    await expectNoPageOverflow(page);
    await shot(answer, testInfo, "05-results-badge", viewport.name);
  });
}

test("判定の校正の管理の権限が無い記入者には、記入だけを出す", async ({ page }) => {
  await mockEvaluationBasics(page);
  await mockJudgeCalibration(page, { withSet: true, canManage: false });
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: {
        data: {
          user_uuid: "22222222-2222-2222-2222-222222222222",
          login_user_id: "annotator",
          display_name: "記入者",
          status: "ACTIVE",
          force_password_change: false,
          role_codes: ["RAG_USER"],
          role_display_names: ["RAG_USER"],
          is_system_admin: false,
          permissions: ["menu.evaluation"],
          allowed_search_answer_profile_ids: [],
          allowed_knowledge_base_ids: [],
          debug_mode: false,
          password_change_allowed: true,
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.goto("/evaluation?tab=calibration");
  await expect(page.getByRole("button", { name: "記入の集合を作る" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "JSON を取り込む" })).toHaveCount(0);
  await page.getByTestId("calibration-label-sets").getByRole("button", { name: "校正の集合 A" }).click();
  await expect(page.getByTestId("calibration-annotator")).toBeVisible();
  await expect(page.getByRole("tab", { name: "2. 一致と裁定" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "JSON を書き出す" })).toHaveCount(0);
});

test("記入の集合を削除しても、消した集合の詳細と行を取り直さない (#3587)", async ({ page }) => {
  await mockEvaluationBasics(page);
  await mockEvaluationJobs(page, { runResult: () => answerMetrics(), autoComplete: true });
  await mockJudgeCalibration(page, { withSet: true });
  await page.goto("/evaluation?tab=calibration");
  await page.getByTestId("calibration-label-sets").getByRole("button", { name: "校正の集合 A" }).click();
  const detail = page.getByTestId("calibration-detail");
  await expect(detail.getByRole("heading", { name: "校正の集合 A" })).toBeVisible();
  await expect(page.getByTestId("calibration-annotator").getByRole("status")).toBeVisible();

  const afterDelete: string[] = [];
  let deleted = false;
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (!url.pathname.includes("/api/evaluation/calibration/label-sets/")) return;
    if (response.request().method() === "DELETE") {
      deleted = true;
      return;
    }
    if (deleted) afterDelete.push(response.request().method() + " " + url.pathname + " " + response.status());
  });
  await detail.getByRole("button", { name: "削除" }).click();
  const dialog = page.getByRole("alertdialog");
  await dialog.getByRole("button", { name: "削除する" }).click();
  await expect(detail).toHaveCount(0);
  await expect(page.getByTestId("calibration-label-sets").getByRole("button", { name: "校正の集合 A" })).toHaveCount(0);
  expect(deleted).toBe(true);
  expect(afterDelete).toEqual([]);
});


test("根拠は共通の高さで止まり、最後まで内部スクロールできる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockEvaluationBasics(page);
  await mockJudgeCalibration(page, { withSet: true, evidence: Array.from({ length: 15 }, (_, i) => ({
    id: `evidence-${i + 1}`, text: (`合成の根拠 ${i + 1} の説明です。\n`).repeat(10),
  })) });
  await page.goto("/evaluation?tab=calibration");
  await page.getByTestId("calibration-label-sets").getByRole("button", { name: "校正の集合 A" }).click();
  const annotator = page.getByTestId("calibration-annotator");
  await annotator.locator("summary").filter({ hasText: "根拠（評価器に渡した 15 件）" }).click();
  const region = annotator.getByTestId("calibration-evidence-scroll");
  await expect(region).toBeVisible();
  const dimensions = await region.evaluate(node => {
    const rootSize = parseFloat(getComputedStyle(document.documentElement).fontSize);
    return {height: node.clientHeight, content: node.scrollHeight,
      expected: rootSize * (matchMedia("(min-width: 48rem)").matches ? 28 : 17.5)};
  });
  expect(Math.abs(dimensions.height - dimensions.expected)).toBeLessThanOrEqual(1);
  expect(dimensions.content).toBeGreaterThan(dimensions.height);
  await region.focus();
  await expect(region).toBeFocused();
  await region.press("End");
  await expect.poll(() => region.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  await region.evaluate(node => node.scrollTo({top: node.scrollHeight}));
  await expect(region.locator("li")).toHaveCount(15);
  await expect.poll(() => region.evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(1);
  await expectNoPageOverflow(page);
  await region.screenshot({path:testInfo.outputPath("evidence-scroll.png")});
});


test("集合一覧は標準のページングと内部スクロールを持ち、作業状態を保つ", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockEvaluationBasics(page);
  await mockJudgeCalibration(page, { withSet: true, setCount: 60 });
  await page.goto("/evaluation?tab=calibration");
  const table = page.getByTestId("calibration-label-sets");
  const pager = page.getByTestId("calibration-label-sets-pagination");
  await expect(table.getByRole("row")).toHaveCount(26);
  await expect(page.getByTestId("calibration-label-sets-toolbar")).toContainText("全 60 件");
  await expect(pager).toContainText("1–25 / 60 件");
  const scroller = table.locator("xpath=ancestor::div[1]");
  await expect.poll(() => scroller.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await expect(table.locator("thead")).toHaveCSS("position", "sticky");
  await table.getByRole("button", { name: "校正の集合 A", exact: true }).click();
  const detail = page.getByTestId("calibration-detail");
  await expect(detail.getByRole("heading", { name: "校正の集合 A" })).toBeVisible();
  await jumpToPage(pager, 2);
  await expect(table).toContainText("校正の集合 26");
  await expect(detail.getByRole("heading", { name: "校正の集合 A" })).toBeVisible();
  await pager.getByRole("combobox", { name: "表示件数" }).click();
  await page.getByRole("option", { name: "10 件", exact: true }).click();
  await expect(table.getByRole("row")).toHaveCount(11);
  await expect(pager).toContainText("1–10 / 60 件");
  await jumpToPage(pager, 3);
  await expect(table).toContainText("校正の集合 21");
  await page.reload();
  await expect(pager).toContainText("21–30 / 60 件");
  await expect(detail.getByRole("heading", { name: "校正の集合 A" })).toBeVisible();
  await expectNoPageOverflow(page);
  await table.screenshot({ path: testInfo.outputPath("label-sets-paging.png") });
});


test("合成の対照は共通の最大表示行数を超えると表内スクロールし、除外を保持する", {tag: "@responsive"}, async ({page}, testInfo) => {
  await mockEvaluationBasics(page);
  await mockJudgeCalibration(page, {withSet: true, syntheticCount: 40});
  await page.goto("/evaluation?tab=calibration");
  await page.getByTestId("calibration-label-sets").getByRole("button", {name: "校正の集合 A", exact:true}).click();
  const detail = page.getByTestId("calibration-detail");
  await detail.getByRole("tab", {name: "2. 一致と裁定"}).click();
  const table = page.getByTestId("calibration-synthetic-table");
  const scroll = page.getByTestId("calibration-synthetic-scroll");
  await scroll.scrollIntoViewIfNeeded();
  await expect(table.getByRole("row")).toHaveCount(41);
  await expect.poll(() => scroll.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  const header = table.getByRole("columnheader").first();
  const firstTop = (await header.boundingBox())!.y;
  await scroll.evaluate(node => {node.scrollTop = node.scrollHeight;});
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => Math.abs((await header.boundingBox())!.y-firstTop)).toBeLessThanOrEqual(1);
  const toggle = table.getByRole("switch").last();
  await toggle.click();
  await expect(toggle).toBeChecked();
  await page.reload();
  await detail.getByRole("tab", {name: "2. 一致と裁定"}).click();
  await expect(table.getByRole("switch").last()).toBeChecked();
  await expectNoPageOverflow(page);
  await page.getByTestId("calibration-synthetic").screenshot({path:testInfo.outputPath("synthetic-scroll.png")});
});
