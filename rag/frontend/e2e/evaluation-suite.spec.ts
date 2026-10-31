import { expect, type Locator, type Page, test } from "./fixtures/test";

import { measureShellHiddenOverflow } from "../../../platform/packages/ui/e2e/layout-checks";
import {
  chooseEvaluationProfile,
  mockEvaluationProfiles,
  evaluationImportResult,
  mockEvaluationImport,
  mockEvaluationJobs,
  prepareEvaluationRun,
  uploadEvaluationExcel,
} from "./_evaluation-jobs";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockKnowledgeBases(page);
  await mockEvaluationProfiles(page);
  await mockEvaluationSuiteSettings(page, "standard");
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(
    `評価ページは評価の基準を表示だけし、プロファイルと評価リクエストの Excel を必須にする (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });

      await page.goto("/evaluation");

      await expect(page.getByRole("heading", { name: "品質評価", level: 1 })).toBeVisible();
      // 評価の実行と判定の校正（#3578）のタブ。既定は評価の実行。
      await expect(page.getByRole("tab", { name: "評価の実行" })).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("tab", { name: "判定の校正" })).toHaveAttribute("aria-selected", "false");
      // 評価の基準は表示だけ（#2065）。選択欄・ラベルは無く、設定の既定と閾値と設定へのリンクを出す。
      const suite = page.getByTestId("evaluation-suite");
      await expect(suite.getByRole("combobox")).toHaveCount(0);
      await expect(suite.getByTestId("evaluation-suite-current")).toHaveText("設定の既定: 標準");
      const preview = page.getByTestId("evaluation-suite-thresholds");
      await expect(preview.getByText("必要な根拠の取得率")).toBeVisible();
      await expect(preview.getByText("90%").first()).toBeVisible();
      await expect(preview.getByText("回答の正確性")).toBeVisible();
      await expect(page.getByRole("link", { name: "設定で既定の基準を変更" })).toHaveAttribute(
        "href",
        "/settings/evaluation"
      );
      // ナレッジベースと評価リクエストの Excel は必須（部品の「必須」のタグ）。
      const conditions = page.getByRole("form", { name: "評価の条件" });
      await expect(conditions.getByText("必須", { exact: true })).toHaveCount(2);
      await expect(page.getByRole("button", { name: "テンプレートをダウンロード" })).toBeVisible();
      await expect(conditions).toContainText("評価に必要な入力や根拠が不足する指標は「未評価」となり、合格を意味しません。");
      await expect(conditions).toContainText("同じ六指標・閾値で比較します。");
      await expect(conditions).toContainText("所要時間は設定や処理状況によって変わります。");
      await expect(conditions).not.toContainText("約 2 倍");
      await expect(conditions.getByRole("switch", { name: "別の検索・回答プロファイルでも評価して比較する" })).toBeVisible();
      await conditions.screenshot({ path: testInfo.outputPath("evaluation-guidance.png") });
      // JSON の入力・設定比較・サンプルは無い。
      await expect(page.getByLabel("Golden set JSON")).toHaveCount(0);
      await expect(page.getByRole("button", { name: "比較実行" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "サンプルを読み込む" })).toHaveCount(0);
      await expectNoPageOverflow(page);

      // 未選択のまま実行すると、ナレッジベースの欄の直下に出してフォーカスを移す。
      const run = page.getByRole("button", { name: "評価実行" });
      await expect(run).toBeEnabled();
      await run.click();
      const knowledgeBase = page.getByTestId("evaluation-profile");
      await expect(knowledgeBase.getByRole("alert")).toHaveText("評価する検索・回答プロファイルを 1 つ選んでください。");
      await expect(page.locator("#evaluation-profile")).toBeFocused();

      // ナレッジベースを選ぶとエラーは消え、次は Excel の欄に出す。
      await chooseEvaluationProfile(page);
      await expect(knowledgeBase.getByRole("alert")).toHaveCount(0);
      await run.click();
      const dropzone = page.getByTestId("evaluation-request-dropzone");
      await expect(dropzone).toHaveAccessibleDescription(/評価リクエストの Excel をアップロードしてください。/);
      await expect(dropzone).toHaveAttribute("aria-invalid", "true");
      await expect(dropzone).toBeFocused();
      await expectNoPageOverflow(page);
    }
  );

  test(
    `Excel の警告は表で出し、評価は実行できる (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const jobs = await mockEvaluationJobs(page, { autoComplete: true });
      await mockEvaluationImport(page, () =>
        evaluationImportResult({
          warning_count: 2,
          warnings: [
            {
              row: 2,
              column: "期待する根拠",
              message: "1 行目: 「料金表.xlsx」は表計算の文書で、ページがありません。「シート: 名前 | 行: 10-12」か「抜粋:」で指定してください。",
            },
            {
              row: 2,
              column: "期待する根拠",
              message: "2 行目: 「手順書.pdf」という名前の文書が 2 件あります。どれか 1 つが引用されれば一致とみなします。",
            },
          ],
        })
      );

      await page.goto("/evaluation");
      await chooseEvaluationProfile(page);
      await uploadEvaluationExcel(page);

      const ready = page.getByTestId("evaluation-import-ready");
      await expect(ready).toContainText("cases.xlsx: 2 件のケースを評価できます。");
      const warnings = page.getByTestId("evaluation-import-warnings");
      await expect(warnings).toContainText("cases.xlsx に 2 件の警告があります。");
      await expect(warnings).toContainText("評価は実行できます。");
      const table = warnings.getByRole("table", { name: "Excel の警告。スクロールできます。" });
      await expect(table.getByRole("row", { name: /2 行目・期待する根拠/ })).toHaveCount(2);
      await expect(table).toContainText("表計算の文書で、ページがありません");
      await expectNoPageOverflow(page);

      await page.getByRole("button", { name: "評価実行" }).click();
      await expect.poll(() => jobs.runPayloads.length).toBe(1);
    }
  );

  test(
    `Excel の誤りは行・列・内容の表で出し、評価を実行しない (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const jobs = await mockEvaluationJobs(page, { autoComplete: true });
      await mockEvaluationImport(page, () =>
        evaluationImportResult({
          cases: [],
          rows: [],
          error_count: 3,
          errors: [
            { row: null, column: null, message: "評価のケースは 200 行までです。" },
            { row: 3, column: "期待する根拠", message: "1 行目: 「無い規程.pdf」は選んだナレッジベースの文書にありません。" },
            { row: 4, column: "質問", message: "質問を入力してください。" },
          ],
        })
      );

      await page.goto("/evaluation");
      await chooseEvaluationProfile(page);
      await uploadEvaluationExcel(page);

      const errors = page.getByTestId("evaluation-import-errors");
      await expect(errors).toContainText("cases.xlsx に 3 件の誤りがあります。");
      const table = errors.getByRole("table", { name: "Excel の誤り。スクロールできます。" });
      await expect(table.getByRole("row", { name: /3 行目・期待する根拠/ })).toContainText("無い規程.pdf");
      await expect(table.getByRole("row", { name: /4 行目・質問/ })).toContainText("質問を入力してください。");
      await expect(table.getByRole("row", { name: /ファイル全体/ })).toBeVisible();

      await page.getByRole("button", { name: "評価実行" }).click();
      await expect(page.getByTestId("evaluation-request-dropzone")).toHaveAccessibleDescription(
        /Excel の誤りを直して、もう一度アップロードしてください。/
      );
      expect(jobs.runPayloads).toHaveLength(0);
      await expectNoPageOverflow(page);
    }
  );
}

for (const viewport of [
  { name: "desktop 1280", width: 1280, height: 760, maxChipRows: 2 },
  { name: "desktop 1920", width: 1920, height: 1080, maxChipRows: 1 },
  { name: "mobile 375", width: 375, height: 812, maxChipRows: null },
]) {
  test(`評価の基準は評価の条件の上に全幅で置き、閾値は横に並べる (#3295, ${viewport.name})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/evaluation");

    const suite = page.getByTestId("evaluation-suite");
    const conditions = page.getByTestId("evaluation-conditions");
    await expect(suite.getByTestId("evaluation-suite-current")).toBeVisible();
    await expect(conditions.getByRole("form", { name: "評価の条件" })).toBeVisible();

    // DOM の順（読み上げ・Tab の順）でも、基準が条件の前。
    const suiteFirst = await suite.evaluate((node) => {
      const other = document.querySelector('[data-testid="evaluation-conditions"]');
      return Boolean(other && node.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(suiteFirst).toBe(true);

    // 見た目の順も、基準の下端が条件の上端より上。どちらも同じ左端と幅（全幅）。
    const suiteBox = await suite.boundingBox();
    const conditionsBox = await conditions.boundingBox();
    expect(suiteBox).not.toBeNull();
    expect(conditionsBox).not.toBeNull();
    expect(suiteBox!.y + suiteBox!.height).toBeLessThanOrEqual(conditionsBox!.y);
    expect(Math.abs(suiteBox!.x - conditionsBox!.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(suiteBox!.width - conditionsBox!.width)).toBeLessThanOrEqual(1);

    await page.screenshot({ path: testInfo.outputPath("evaluation-layout.png"), fullPage: true });

    // 全幅の desktop では、閾値の chip は横に並べて折り返す（1 行に 1 つずつ縦に積まない。375px は幅の分だけ折り返す）。
    const chipTops = await page
      .getByTestId("evaluation-suite-thresholds")
      .locator("li")
      .evaluateAll((items) => items.map((item) => Math.round(item.getBoundingClientRect().top)));
    expect(chipTops).toHaveLength(6);
    if (viewport.maxChipRows !== null) {
      expect(new Set(chipTops).size).toBeLessThanOrEqual(viewport.maxChipRows);
    }

    await expectNoPageOverflow(page);
  });
}

test("テンプレートをダウンロードでき、選んだナレッジベースの文書の一覧を求める", async ({ page }) => {
  const urls: string[] = [];
  await page.route("**/api/evaluation/cases/template**", (route) => {
    urls.push(route.request().url());
    return route.fulfill({
      status: 200,
      headers: {
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="evaluation_request_template.xlsx"',
      },
      body: "xlsx",
    });
  });

  await page.goto("/evaluation");
  await expect(page.getByText(/検索・回答プロファイルを選んでからダウンロードすると、参照する文書の一覧/)).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "テンプレートをダウンロード" }).click();

  expect((await download).suggestedFilename()).toBe("evaluation_request_template.xlsx");
  await expect(page.getByText("テンプレートをダウンロードしました。")).toBeVisible();
  expect(new URL(urls[0] ?? "").searchParams.getAll("knowledge_base_ids")).toEqual([]);

  // ナレッジベースを選ぶと、その文書（ファイル名・文書 ID）を「文書一覧」のシートに入れるよう求める（#2086）。
  await chooseEvaluationProfile(page);
  const second = page.waitForEvent("download");
  await page.getByRole("button", { name: "テンプレートをダウンロード" }).click();
  await second;
  expect(new URL(urls[1] ?? "").searchParams.getAll("search_answer_profile_id")).toEqual(["profile-base"]);
});

test("Excel の形式でないファイルは検証に送らず欄の直下に出す", async ({ page }) => {
  const imports = await mockEvaluationImport(page);
  await page.goto("/evaluation");
  await chooseEvaluationProfile(page);

  await uploadEvaluationExcel(page, "cases.csv");

  await expect(page.getByTestId("evaluation-request-dropzone")).toHaveAccessibleDescription(
    /Excel（.xlsx）のファイルを選んでください。/
  );
  expect(imports.requests).toHaveLength(0);
});

test("プロファイルより先に Excel を選ぶと、選んだ時点で検証し、変えたら検証し直す", async ({ page }) => {
  const imports = await mockEvaluationImport(page);
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({ json: knowledgeBasesEnvelope(["社内規程", "人事規程"]) });
  });
  await page.goto("/evaluation");

  await uploadEvaluationExcel(page);
  await expect(page.getByText("検索・回答プロファイルを選ぶと、参照する文書で Excel", { exact: false })).toBeVisible();
  expect(imports.requests).toHaveLength(0);

  await chooseEvaluationProfile(page);
  await expect(page.getByTestId("evaluation-import-ready")).toContainText("cases.xlsx: 2 件のケースを評価できます。");
  expect(imports.requests).toEqual([["profile-base"]]);

  // 期待する根拠のファイル名は KB で照合するので、KB を変えたら同じファイルで検証し直す。
  await chooseEvaluationProfile(page, /プロファイル B/);
  await expect.poll(() => imports.requests.length).toBe(2);
  expect(imports.requests[1]).toEqual(["profile-1"]);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(
    `検証した Excel は件数とプレビューを出し、評価の job は進捗・今の行・経過時間を出す (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const jobs = await mockEvaluationJobs(page, {
        runResult: () => evaluationMetrics("standard"),
      });

      await page.goto("/evaluation");
      await prepareEvaluationRun(page);

      const ready = page.getByTestId("evaluation-import-ready");
      await expect(ready).toContainText("cases.xlsx: 2 件のケースを評価できます。");
      await expect(ready).toContainText("回答を期待 1 件・保留を期待 1 件・標準回答あり 1 件");
      const preview = page.getByTestId("evaluation-import-preview");
      await expect(preview.getByRole("row", { name: /経費精算の締め日/ })).toContainText("経費精算規程.pdf | p.3");
      await expect(preview.getByRole("row", { name: /社員食堂/ })).toContainText("必要な根拠の取得率は未評価");
      await expectNoPageOverflow(page);

      await page.getByRole("button", { name: "評価実行" }).click();
      // 評価の基準は設定の既定（suite を送らない）、検索の対象は選んだ KB、ケースは検証の結果。
      await expect.poll(() => jobs.runPayloads.length).toBe(1);
      expect(jobs.runPayloads[0]).toEqual({
        cases: evaluationImportResult().cases,
        search_answer_profile_id: "profile-base",
      });

      // 実行状況（#390）: 状態・件数・今の行・経過時間（placement="job"）と取り消し。
      const panel = page.getByTestId("evaluation-run-job");
      await expect(panel.locator("[data-status-variant]")).toHaveText("実行中");
      await expect(page.getByTestId("evaluation-run-job-count")).toHaveText("0 / 2 件（0%）");
      await expect(page.getByTestId("evaluation-run-job-current-case")).toHaveText("実行中のケース: 2 行目");
      const timing = page.getByTestId("evaluation-run-job-timing");
      await expect(timing).toHaveAttribute("data-processing-placement", "job");
      await expect(timing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
      await expect(panel.getByRole("button", { name: "取り消し" })).toBeVisible();
      // 実行中は同じ評価を重ねて投入しない。
      await expect(page.getByRole("button", { name: "評価実行" })).toBeDisabled();
      await expect(page.getByTestId("evaluation-result-loading")).toBeVisible();
      await expectNoPageOverflow(page);

      jobs.complete();
      await expect(page.getByText("評価の基準: 標準")).toBeVisible();
      await expect(panel.locator("[data-status-variant]")).toHaveText("完了");
      await expect(page.getByTestId("evaluation-run-job-count")).toHaveText("2 / 2 件（100%）");
      await expect(timing.getByRole("timer")).toHaveAccessibleName(/処理時間 \d{2}:\d{2}/);
      await expect(panel.getByRole("button", { name: "取り消し" })).toHaveCount(0);
      await expect(page.getByTestId("evaluation-result-loading")).toHaveCount(0);
      await expect(page.getByRole("button", { name: "評価実行" })).toBeEnabled();

      // 結果の表は Excel の行と質問、期待する根拠の照合の結果（見つかった / 見つからない）を出す。
      const table = page.getByTestId("evaluation-case-scroll-region");
      const first = table.getByRole("row", { name: /経費精算の締め日/ });
      await expect(first.getByTestId("evaluation-case-question")).toContainText("2 行目");
      const evidence = first.getByTestId("evaluation-case-evidence");
      await expect(evidence).toContainText("1 / 2 件");
      await expect(evidence.getByRole("listitem").filter({ hasText: "経費精算規程.pdf | p.3" })).toContainText(
        "見つかった"
      );
      await expect(evidence.getByRole("listitem").filter({ hasText: "出張旅費規程.docx" })).toContainText(
        "見つからない"
      );
      await expect(table.getByRole("columnheader", {name:/応答の判断/})).toBeVisible();
      await expectNoPageOverflow(page);
    }
  );

  test(`失敗したケースは理由と時間切れになった工程を表に出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockEvaluationJobs(page, {
      runResult: () => evaluationMetricsWithTimedOutCase(),
      autoComplete: true,
    });

    await page.goto("/evaluation");
    await prepareEvaluationRun(page);
    await page.getByRole("button", { name: "評価実行" }).click();

    const error = page.getByTestId("evaluation-case-error");
    await expect(page.getByTestId("evaluation-case-question").first()).toContainText("2 行目");
    await expect(error).toContainText("エラー");
    await expect(error).toContainText("工程: 質問の条件を整理して回答を作る");
    await expect(error).toContainText(
      "評価ケースの回答生成が上限の 10 分以内に終わりませんでした（時間切れになった工程: 質問の理解から回答の作成まで）。"
    );
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "1280px", width: 1280, height: 800 },
  { name: "1920px", width: 1920, height: 1080 },
  { name: "375px", width: 375, height: 812 },
]) {
  test(`実行中の評価を最下部までスクロールしても、本文の下端と下の余白が画面の中に見える (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockEvaluationJobs(page, { runResult: () => evaluationMetrics("standard") });

    await page.goto("/evaluation");
    await prepareEvaluationRun(page);
    await page.getByRole("button", { name: "評価実行" }).click();
    await expect(page.getByTestId("evaluation-run-job-timing")).toBeVisible();
    await expect(page.getByTestId("evaluation-result-loading")).toBeVisible();

    // 実行状況の読み上げ専用の文（sr-only = absolute）が main の外を包含ブロックにすると、AppShell の外枠
    // （overflow: hidden）が隠れてスクロールでき、見える位置へ移す操作で外枠ごとずれて本文の下端が切れた。
    expect(await measureShellHiddenOverflow(page)).toEqual({ vertical: 0, horizontal: 0 });

    const main = page.locator("main#pr-main");
    const measure = () =>
      main.evaluate((element) => {
        element.scrollTo({ top: element.scrollHeight });
        // 支援技術・ページ内検索と同じく、読み上げ専用の文を見える位置へ移す。外枠は動かない。
        for (const live of Array.from(element.querySelectorAll(".sr-only"))) live.scrollIntoView({ block: "nearest" });
        element.scrollTo({ top: element.scrollHeight });
        // 評価の実行のタブ（#3578）の中にあるので、タブの外の PageBody の余白を測る。
        const panel = element.querySelector<HTMLElement>("[data-testid=evaluation-result-loading]")!.parentElement!;
        const body = panel.closest<HTMLElement>("[role=tabpanel]")?.parentElement ?? panel;
        const last = body.lastElementChild!.getBoundingClientRect();
        const mainRect = element.getBoundingClientRect();
        return {
          shellScrollTop: element.parentElement!.scrollTop,
          mainBottomToViewport: Math.round(window.innerHeight - mainRect.bottom),
          bottomGap: Math.round(mainRect.bottom - last.bottom),
          bodyPaddingBottom: Math.round(Number.parseFloat(getComputedStyle(body).paddingBottom)),
        };
      });
    const result = await measure();
    expect(result.shellScrollTop).toBe(0);
    expect(result.mainBottomToViewport).toBe(0);
    // 最後の Skeleton の下に PageBody の下の余白（1.5rem）が残る。
    expect(Math.abs(result.bottomGap - result.bodyPaddingBottom)).toBeLessThanOrEqual(1);
    await expectNoPageOverflow(page);
  });
}

test("実行中の評価は確認してから取り消せる", async ({ page }) => {
  const jobs = await mockEvaluationJobs(page, { runResult: () => evaluationMetrics("standard") });

  await page.goto("/evaluation");
  await prepareEvaluationRun(page);
  await page.getByRole("button", { name: "評価実行" }).click();
  const panel = page.getByTestId("evaluation-run-job");
  await panel.getByRole("button", { name: "取り消し" }).click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("評価を取り消しますか？");
  await dialog.getByRole("button", { name: "取り消す" }).click();

  await expect.poll(() => jobs.cancelled.length).toBe(1);
  await expect(panel.locator("[data-status-variant]")).toHaveText("取り消し済み");
  await expect(panel.getByText("評価を取り消しました。")).toBeVisible();
  await expect(page.getByTestId("evaluation-result-loading")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "評価実行" })).toBeEnabled();
});

test("再読込しても検証した Excel と実行中の評価の job の状態を表示し、失敗の理由を出す", async ({ page }) => {
  const jobs = await mockEvaluationJobs(page, { runResult: () => evaluationMetrics("standard") });

  await page.goto("/evaluation");
  const imports = await prepareEvaluationRun(page);
  await page.getByRole("button", { name: "評価実行" }).click();
  await expect(page.getByTestId("evaluation-run-job-count")).toHaveText("0 / 2 件（0%）");

  // 戻っただけで評価を送り直さず、保存した job id でサーバーの状態を確かめる（workspace-state.md）。
  // 検証した Excel（ケース）も残し、送り直さない。
  await page.reload();
  await expect(page.getByTestId("evaluation-run-job-count")).toHaveText("0 / 2 件（0%）");
  await expect(page.getByTestId("evaluation-import-ready")).toContainText("cases.xlsx: 2 件のケースを評価できます。");
  expect(jobs.runPayloads).toHaveLength(1);
  expect(imports.requests).toHaveLength(1);

  jobs.fail(
    "品質評価の実行中に予期しないエラーが発生したため、評価を中断しました。サーバーのログを確認して、もう一度実行してください。 エラー種別: RuntimeError"
  );
  const panel = page.getByTestId("evaluation-run-job");
  await expect(panel.locator("[data-status-variant]")).toHaveText("失敗");
  await expect(panel.getByText("評価を最後まで実行できませんでした。")).toBeVisible();
  // 本文は原因と対処だけにし、例外の種別は開いた「詳細」に出す（messaging.md §10.3）。
  await expect(
    panel.getByText(
      "品質評価の実行中に予期しないエラーが発生したため、評価を中断しました。サーバーのログを確認して、もう一度実行してください。",
      { exact: true }
    )
  ).toBeVisible();
  const details = panel.locator("details").filter({ hasText: "詳細" });
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText("エラー種別");
  await expect(details).toContainText("RuntimeError");
});

// #977: 状態の取得が retry を使い切って失敗しても、実行中の job の状態の取得を止めない（止めると「実行中」のまま）。
test("実行中の評価の状態の取得が一時的に失敗しても、取得を続けて完了を出す", async ({ page }) => {
  test.setTimeout(60_000);
  const jobs = await mockEvaluationJobs(page, { runResult: () => evaluationMetrics("standard") });
  await page.goto("/evaluation");
  await prepareEvaluationRun(page);
  await page.getByRole("button", { name: "評価実行" }).click();
  await expect(page.getByTestId("evaluation-run-job-count")).toHaveText("0 / 2 件（0%）");

  let failing = true;
  await page.route("**/api/evaluation/jobs/jobrun*", async (route) => {
    if (failing && route.request().method() === "GET") {
      await route.fulfill({ status: 503, json: { data: null, error_messages: ["一時的に利用できません。"] } });
      return;
    }
    await route.fallback();
  });
  // 1 回の取得の retry（3 回）を使い切ると、パネルの直下に取得できていないことを出す。
  const warning = page.getByText("評価の実行状況を取得できません。自動で取得し直しています。", { exact: false });
  await expect(warning).toBeVisible({ timeout: 20_000 });
  const panel = page.getByTestId("evaluation-run-job");
  await expect(panel.locator("[data-status-variant]")).toHaveText("実行中");
  await expectNoPageOverflow(page);

  failing = false;
  jobs.complete();
  await expect(panel.locator("[data-status-variant]")).toHaveText("完了", { timeout: 15_000 });
  await expect(warning).toHaveCount(0);
  await expect(page.getByTestId("evaluation-perspective-retrieval")).toBeVisible();
});

function knowledgeBasesEnvelope(names: string[]) {
  return {
    data: {
      items: names.map((name, index) => ({
        id: `kb-${index + 1}`,
        name,
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
      })),
      total: names.length,
      limit: 50,
      offset: 0,
      has_next: false,
    },
    error_messages: [],
    warning_messages: [],
  };
}

async function mockKnowledgeBases(page: Page) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({ json: knowledgeBasesEnvelope(["社内規程"]) });
  });
}

async function mockEvaluationSuiteSettings(page: Page, suite: string) {
  await page.route("**/api/settings/evaluation-suite", async (route) => {
    await route.fulfill({ json: evaluationSuiteEnvelope(suite) });
  });
}

function evaluationSuiteEnvelope(suite: string) {
  const thresholds=(strict:boolean)=>({required_evidence_recall:strict?1:0.9,mrr:strict?0.8:0.6,answer_correctness:strict?1:0.9,requirement_coverage:strict?1:0.9,claim_support_rate:strict?1:0.95,response_decision_accuracy:strict?1:0.9});
  const specs = [
    { name: "standard", thresholds: thresholds(false) },
    { name: "strict", thresholds: thresholds(true) },
  ];
  const selected = specs.find((item) => item.name === suite) ?? specs[0];
  return {
    data: {
      suite,
      thresholds: selected.thresholds,
      suites: specs.map((item) => ({
        ...item,
        origin: "x",
        recommended_for: ["general"],
        selected: item.name === suite,
      })),
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

function evaluationMetrics(suite:string) {
 return {
 case_count:2,error_count:0,evaluation_suite:suite,passed:null,
 required_evidence_recall:1,mrr:0.5,answer_correctness:null,requirement_coverage:null,claim_support_rate:0.9,response_decision_accuracy:1,
 metric_case_counts:{required_evidence_recall:1,mrr:1,answer_correctness:0,requirement_coverage:0,claim_support_rate:1,response_decision_accuracy:2},
 threshold_failures:[],failure_reason_counts:{},category_breakdown:{},
 decision_breakdown:{clarify:{case_count:1,error_count:0,metrics:{response_decision_accuracy:1},metric_case_counts:{response_decision_accuracy:1}}},
 case_results:[{case_id:"row-2",trace_id:"trace-1",status:"success",retrieved_document_ids:["doc-1"],relevant_document_ids:["doc-1"],hit_document_ids:["doc-1"],required_evidence_recall:1,mrr:1,answer_correctness:null,requirement_coverage:null,claim_support_rate:0.9,response_decision_accuracy:1,guardrail_warnings:[],missing_evidence:["evidence-2"],failure_reasons:[],diagnostics:{},elapsed_ms:1,error_type:null,error_message:null}],
 };
}

function evaluationMetricsWithTimedOutCase() {
  const metrics = evaluationMetrics("standard");
  const [success] = metrics.case_results;
  const refusal={...success,case_id:"row-3",trace_id:"trace-hold",required_evidence_recall:null,mrr:null,response_decision_accuracy:1};
  return {
    ...metrics,
    case_count: 2,
    error_count: 1,
    passed: false,
    failure_reason_counts: { case_error: 1 },
    case_results: [
      {
        ...success,
        case_id: "row-2",
        trace_id: "trace-slow",
        status: "error",
        retrieved_document_ids: [],
        hit_document_ids: [],
        context_recall: null,
        reciprocal_rank: null,
        faithfulness: null,
        answer_keyword_hit: null,
        abstained: null,
        refusal_correct: null,
        answer_evaluation: null,
        failure_reasons: ["case_error"],
        elapsed_ms: 300000,
        error_type: "TimeoutError",
        error_stage: "answer",
        error_message:
          "評価ケースの回答生成が上限の 10 分以内に終わりませんでした（時間切れになった工程: 質問の理解から回答の作成まで）。trace_id で監査ログを確認してください。",
      },
      refusal,
    ],
  };
}

for (const viewport of [{name:"desktop",width:1280,height:900},{name:"mobile",width:375,height:812}]) {
 test(`六指標は検索二・回答三・応答判断一で結果とケースに表示する (${viewport.name})`, async ({page}, testInfo)=>{
  await page.setViewportSize({width:viewport.width,height:viewport.height});
  await mockEvaluationJobs(page,{runResult:()=>evaluationMetrics("standard"),autoComplete:true});
  await page.goto("/evaluation");await prepareEvaluationRun(page);
  await page.getByRole("button",{name:"評価実行"}).click();
  for (const [id,label,count] of [["retrieval","検索",2],["answer","回答",3],["decision","応答の判断",1]] as const) {
   const group=page.getByTestId(`evaluation-perspective-${id}`);
   await expect(group.getByRole("heading",{name:label,exact:true})).toBeVisible();
   await expect(group.locator("[data-testid^='evaluation-metric-']")).toHaveCount(count);
   await group.scrollIntoViewIfNeeded();
   await group.screenshot({path:testInfo.outputPath(`six-${id}.png`)});
  }
  await expect(page.getByTestId("evaluation-metric-answer_correctness")).toContainText("未評価");
  await expect(page.getByTestId("evaluation-metric-answer_correctness")).toContainText("対象 0 件");
  await expect(page.getByTestId("evaluation-metric-claim_support_rate")).toContainText("90%");
  await expect(page.getByTestId("evaluation-perspective-grounding")).toHaveCount(0);
  await expect(page.getByTestId("evaluation-perspective-guidance")).toHaveCount(0);
  await expect(page.getByTestId("evaluation-decision-breakdown")).toContainText("確認");
  await expectNoPageOverflow(page);
  await page.screenshot({path:testInfo.outputPath("six-metrics.png"),fullPage:true});
 });
}

for (const viewport of [{name:"desktop",width:1280,height:900},{name:"mobile",width:375,height:812}]) {
 test(`六指標の閾値未達・対象件数・分類別内訳を表示する (${viewport.name})`,async({page})=>{
  await page.setViewportSize({width:viewport.width,height:viewport.height});
  const base=evaluationMetrics("standard");
  const values={required_evidence_recall:0.5,mrr:0.5,answer_correctness:0,requirement_coverage:0.5,claim_support_rate:1,response_decision_accuracy:0};
  const counts=Object.fromEntries(Object.keys(values).map(key=>[key,1]));
  await mockEvaluationJobs(page,{autoComplete:true,runResult:()=>({...base,...values,passed:false,metric_case_counts:counts,threshold_failures:[{metric:"mrr",actual:0.5,threshold:0.6}],failure_reason_counts:{unexpected_handling:1},category_breakdown:{clarification_required:{case_count:1,error_count:0,metrics:values,metric_case_counts:counts}}})});
  await page.goto("/evaluation");await prepareEvaluationRun(page);await page.getByRole("button",{name:"評価実行"}).click();
  const mrr=page.getByTestId("evaluation-metric-mrr");
  await expect(page.getByTestId("evaluation-threshold-mrr")).toHaveText(" / 60%");
  await expect(page.getByRole("alert").filter({hasText:"閾値未達"})).toHaveCount(0);
  await expect(mrr).toContainText("50%");await expect(mrr).toContainText("対象 1 件");await expect(mrr.locator("[data-status-variant]")).toHaveText("閾値未達");
  await expect(page.getByTestId("evaluation-metric-answer_correctness")).toContainText("0%");
  await expect(page.getByTestId("evaluation-category-breakdown").getByRole("row",{name:/条件の確認が要る/})).toContainText("50%");
  await expect(page.getByTestId("evaluation-failure-reasons")).toContainText("1");
  await expectNoPageOverflow(page);
 });
}


test("Excel の六列を全幅で表示し、長い 1 行も表の中で縦横スクロールする", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const evidence = Array.from({length:30}, (_, i) => ({id:`e-${i}`,document_id:"doc-a",label:`資料 A.pdf | p.${i+1}`,page_start:i+1}));
  const standard = "合成の標準回答です。\n".repeat(60) + "標準回答の末尾";
  const process = "合成の回答手順です。\n".repeat(30) + "回答の過程の末尾";
  await mockEvaluationImport(page, () => evaluationImportResult({
    row_count:1,
    cases:[{id:"row-2",query:"合成の質問",relevant_document_ids:["doc-a"],required_evidence:evidence,standard_answer:standard}],
    rows:[{row:2,case_id:"row-2",question:"合成の質問",evidence:evidence.map(e=>e.label),standard_answer_preview:"短いプレビュー",has_standard_answer:true,required_answer_points:["合成の要点"],expected_response_decision:"answer",answer_process:process}],
    document_names:{"doc-a":"資料 A.pdf"},
  }));
  const metrics = evaluationMetrics("standard");
  await mockEvaluationJobs(page, {autoComplete:true,runResult:()=>({...metrics,case_count:1,case_results:[{...metrics.case_results[0],case_id:"row-2"}]})});
  await page.goto("/evaluation");
  await chooseEvaluationProfile(page);
  await uploadEvaluationExcel(page);
  const preview=page.getByTestId("evaluation-import-preview");
  await expect(preview).toBeVisible();
  for(const name of ["質問","期待する根拠","標準回答","必須の回答要点","期待する応答の判断","回答の過程"]) {
    await expect(preview.getByRole("columnheader",{name,exact:true})).toBeAttached();
  }
  await expect(preview).toContainText("標準回答の末尾");
  await expect(preview).toContainText("回答の過程の末尾");
  const uploadBox=await page.getByTestId("evaluation-request-upload").boundingBox();
  const formBox=await page.getByRole("form",{name:"評価の条件"}).boundingBox();
  expect(Math.abs((uploadBox?.width ?? 0)-(formBox?.width ?? 0))).toBeLessThanOrEqual(1);
  const checkBounds = async (region: Locator) => {
    const box=await region.evaluate(node=>({height:node.clientHeight,content:node.scrollHeight,
      cap:parseFloat(getComputedStyle(document.documentElement).fontSize)*(matchMedia("(min-width: 48rem)").matches?28:17.5)}));
    expect(Math.abs(box.height-box.cap)).toBeLessThanOrEqual(2);
    expect(box.content).toBeGreaterThan(box.height);
    await region.evaluate(node=>node.scrollTo({top:node.scrollHeight,left:node.scrollWidth}));
    await expect.poll(()=>region.evaluate(node=>node.scrollHeight-node.scrollTop-node.clientHeight)).toBeLessThanOrEqual(1);
  };
  await checkBounds(preview);
  await expectNoPageOverflow(page);
  await preview.screenshot({path:testInfo.outputPath("excel-six-columns.png")});
  await page.getByRole("button",{name:"評価実行"}).click();
  const result=page.getByTestId("evaluation-case-scroll-region");
  await expect(result).toBeVisible();
  await checkBounds(result);
  expect(await result.locator("thead").evaluate(node=>getComputedStyle(node).position)).toBe("sticky");
  await expectNoPageOverflow(page);
  const exportRequests: Record<string,unknown>[]=[];
  await page.route("**/api/evaluation/jobs/*/results.xlsx", async route=>{
    exportRequests.push(route.request().postDataJSON());
    await route.fulfill({status:200,headers:{"Content-Type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "Content-Disposition":'attachment; filename="evaluation_results.xlsx"'},body:"xlsx"});
  });
  await mockEvaluationImport(page,()=>evaluationImportResult({file_name:"replacement.xlsx"}));
  await uploadEvaluationExcel(page,"replacement.xlsx");
  await expect(page.getByTestId("evaluation-import-ready")).toContainText("replacement.xlsx");
  const downloaded=page.waitForEvent("download");
  await page.getByRole("button",{name:"Excel をダウンロード",exact:true}).click();
  expect((await downloaded).suggestedFilename()).toBe("evaluation_results.xlsx");
  expect(exportRequests[0]).toMatchObject({cases:[{query:"合成の質問",standard_answer:standard}],
    rows:[{question:"合成の質問",answer_process:process}],profile_name:"プロファイル A"});

});
