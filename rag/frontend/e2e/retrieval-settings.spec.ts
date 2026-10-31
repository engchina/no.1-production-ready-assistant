import { expect, test, type Page } from "./fixtures/test";
import { FLOW_STAGES } from "./_prompt-stage-fixtures";
import { apiEnvelope, expectNoPageOverflow, mockAuthUser, mockLocalAuth, openSidebarNav } from "./_helpers";

// 検索方法の画面。検索・回答の既定、検索関連プロンプトを持つ。
// 以前の検索モード・検索オプションと、根拠確認・回答スタイル・高度な検索の画面は削除した。

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`検索方法の画面は回答の設定と検索プロンプトを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockRetrievalCards(page);
    const removedSettingRequests: string[] = [];
    page.on("request", (request) => { if (/\/api\/settings\/(answer-records|query-history)$/.test(request.url())) removedSettingRequests.push(request.method()); });

    await page.goto("/settings/retrieval");

    await expect(page.getByRole("heading", { name: "検索方法", exact: true, level: 1 })).toBeVisible();
    await expect(page.getByTestId("answering-settings-card")).toBeVisible();
    await expect(page.getByRole("button", { name: "回答の設定を保存", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "保存期間を保存" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "質問履歴の設定を保存" })).toHaveCount(0);
    await expect(page.getByText("回答の記録の保存期間", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "保存期間", exact: true })).toHaveCount(0);
    await expect(page.getByText("質問履歴", { exact: true })).toHaveCount(0);
    expect(removedSettingRequests).toEqual([]);
    await page.getByTestId("answer-prompt-stages-card").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("retrieval-without-history-settings.png") });
    // 削除した検索モード・検索オプション（#595）は出さない。
    await expect(page.getByRole("radiogroup", { name: "検索モード" })).toHaveCount(0);
    await expect(page.getByRole("switch", { name: "クエリ拡張" })).toHaveCount(0);
    await expect(page.getByTestId("answer-prompt-unused-note")).toHaveCount(0);
    // 画面の文言に移植元の呼び名（DocRAG）を出さない。
    await expect(page.locator("main")).not.toContainText("DocRAG");
    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。削除した画面はナビに出さない。
    const nav = await openSidebarNav(page);
    await expect(nav.getByRole("link", { name: "検索方法" })).toHaveAttribute("aria-current", "page");
    for (const removed of ["根拠確認", "回答スタイル", "高度な検索"]) {
      await expect(nav.getByRole("link", { name: removed })).toHaveCount(0);
    }
    await expectNoPageOverflow(page);
  });

  test(`検索方法の画面は読み込み中は経過時間を先頭の 1 か所に出し、カードは形を保つ (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await mockRetrievalCards(page, gate);

    await page.goto("/settings/retrieval");

    const loading = page.getByTestId("settings-retrieval-loading");
    await expect(loading).toBeVisible();
    // カードは読み込み中も枠を出し、保存のボタンはまだ出さない（寸法を予約する）。
    await expect(page.getByTestId("answering-settings-card")).toBeVisible();
    await expect(page.getByRole("button", { name: "回答の設定を保存", exact: true })).toHaveCount(0);
    release();
    await expect(loading).toHaveCount(0);
    await expect(page.getByRole("button", { name: "回答の設定を保存", exact: true })).toBeVisible();
    await expectNoPageOverflow(page);
  });

  test(`削除した設定の画面の URL は検索方法へ移す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockRetrievalCards(page);

    for (const removed of ["/settings/grounding", "/settings/generation", "/settings/agentic"]) {
      await page.goto(removed);
      await expect(page).toHaveURL(/\/settings\/retrieval$/);
      await expect(page.getByRole("heading", { name: "検索方法", exact: true, level: 1 })).toBeVisible();
    }
  });
}

test("検索方法の権限が無い利用者は、削除した画面の URL から権限なしの画面へ移る", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockAuthUser(page, { permissions: ["menu.chat"] });

  await page.goto("/settings/grounding");

  await expect(page).toHaveURL(/\/forbidden/);
});




const ANSWERING_SETTINGS = {
  query_strategy: "auto_routing",
  answer_flow: "standard_rag",
  answer_audit_mode: "none",
  answer_evidence_refinement_mode: "legacy",
  neighbor_child_count: 3,
  rerank_enabled: true,
  screen_linking_enabled: false,
  request_coverage_retrieval_enabled: false,
  auto_field_filter_enabled: false,
  graph_expansion_enabled: false,
  config_source: "runtime",
  entity_expansion_enabled: true,
  entity_expansion_max_chunks: 6,
  section_expansion_enabled: true,
  section_expansion_max_levels: 4,
  section_expansion_input_token_percent: 50,
};

async function mockAnsweringSettings(page: Page, saved: unknown[] = [], initial: Record<string, unknown> = {}) {
  let current = { ...ANSWERING_SETTINGS, ...initial };
  await page.route("**/api/settings/answering", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      saved.push(body);
      current = { ...current, ...body };
      await route.fulfill({
        json: { data: current, error_messages: [], warning_messages: [] },
      });
      return;
    }
    await route.fulfill({ json: { data: current, error_messages: [], warning_messages: [] } });
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900, collapse: false },
  { name: "mobile", width: 375, height: 900, collapse: true },
]) {
  for (const theme of ["light", "dark"] as const) {
    test(`検索方法の画面で回答の検索と生成の既定を変えて保存できる (#593, ${viewport.name}, ${theme})`, { tag: "@responsive" }, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      // 外観（テーマ）とサイドバーの折りたたみは共有 UI の ui-store の保存値で決まる。
      await page.addInitScript(
        (state) => window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state, version: 0 })),
        { theme, sidebarCollapsed: viewport.collapse }
      );
      await mockRetrievalCards(page);
      const saved: unknown[] = [];
      await mockAnsweringSettings(page, saved);

      await page.goto("/settings/retrieval");
      await expect
        .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
        .toBe(theme === "dark");

      await expect(page.getByText("回答の検索と生成", { exact: true })).toBeVisible();
      // 画面の文言に移植元の呼び名（DocRAG）を出さない（#593）。
      const card = page.getByTestId("answering-settings-card");
      await expect(card).not.toContainText("DocRAG");
      await expect(card.getByRole("switch")).toHaveCount(7);
      expect(await card.getByRole("switch").evaluateAll((elements) => elements.map((element) => element.getAttribute("aria-label"))))
        .toEqual(["質問から項目の条件を読み取る", "実体・別名による検索拡張", "文書構造による検索拡張", "画面目録で操作画面を探す", "Rerank で検索候補を並べ替える", "章節で広げる根拠", "根拠の無い要求を探し直す"]);
      const strategy = page.getByRole("combobox", { name: "質問の拡張", exact: true });
      await expect(strategy).toContainText("自動ルーティング");
      // 回答の生成方式の既定は標準 RAG。先頭に「（既定）」付きで並べ、CRAG は任意で AI の呼び出しが増えると説明する（#3509）。
      const answerFlow = page.getByRole("combobox", { name: "回答の生成方式", exact: true });
      await expect(answerFlow).toContainText("標準 RAG（既定）");
      await expect(answerFlow).toHaveAccessibleDescription(/CRAG は必要なときだけ選ぶ方式.*回答生成前.*AI の呼び出しが増え/);
      await expect(answerFlow).toHaveAccessibleDescription(/生成後の再検索・監査とは別の設定.*自動では有効にしません/);
      await expect(page.getByRole("switch", { name: "Rerank で検索候補を並べ替える" })).toBeChecked();
      await page.getByRole("switch", { name: "質問から項目の条件を読み取る" }).scrollIntoViewIfNeeded();
      await page.screenshot({ path: test.info().outputPath("retrieval-option-order.png") });
      const screenLinking = page.getByRole("switch", { name: "画面目録で操作画面を探す" });
      await expect(screenLinking).not.toBeChecked();
      // 根拠の無い要求を探し直す（#1279）は既定で無効。
      const requestCoverage = page.getByRole("switch", { name: "根拠の無い要求を探し直す" });
      await expect(requestCoverage).not.toBeChecked();
      await expect(requestCoverage).toHaveAccessibleDescription(/回答生成が明示した根拠不足.*回数・時間の予算内/);
      await expect(requestCoverage).toHaveAccessibleDescription(/既にある根拠で説明が抜けた場合.*材料不足として検索し直す設定ではありません/);
      await expect(requestCoverage).toHaveAccessibleDescription(/未校正の意味監査の結果だけでは、追加検索や再生成を行いません/);
      await requestCoverage.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath(`settings-${viewport.name}-${theme}.png`) });
      await expect(answerFlow).toHaveAccessibleDescription(/初回を含め最大 3 段.*書き換えは最大 2 回/);
      await expect(screenLinking).toHaveAccessibleDescription(/目録がない場合.*省略/);
      const autoFieldFilter = page.getByRole("switch", { name: "質問から項目の条件を読み取る" });
      await expect(autoFieldFilter).not.toBeChecked();
      // 文書構造による検索拡張（#2055）は既定で無効。AI の呼び出しが増えないことを説明に出す。
      const graphExpansion = page.getByRole("switch", { name: "文書構造による検索拡張" });
      await expect(graphExpansion).not.toBeChecked();
      await graphExpansion.scrollIntoViewIfNeeded();
      await page.screenshot({ path: test.info().outputPath("entity-structure-expansion.png") });
      await expect(graphExpansion).toHaveAccessibleDescription(/AI の呼び出しは増えません/);
      await expect(graphExpansion).toHaveAccessibleDescription(/文書構造索引.*併用でき.*重複して加えません/);
      await expect(page.getByRole("switch", { name: "実体・別名による検索拡張" })).toHaveAccessibleDescription(/名称・ID・別名.*1 段.*文書構造による検索拡張と併用/);
      const save = page.getByRole("button", { name: "回答の設定を保存", exact: true });
      await expect(save).toBeDisabled();

      await strategy.click();
      await page.getByRole("option", { name: /RAG フュージョン/ }).click();
      await answerFlow.click();
      await expect(page.getByRole("option")).toHaveText(["標準 RAG（既定）", "CRAG（根拠を評価し、必要なら補正検索）"]);
      await page.getByRole("option", { name: /^CRAG/ }).click();
      await expect(answerFlow).toContainText("CRAG");
      await page.getByRole("combobox", { name: "根拠の前後から加える数", exact: true }).click();
      await page.getByRole("option", { name: "5", exact: true }).click();
      await screenLinking.click();
      await autoFieldFilter.click();
      await requestCoverage.click();
      const auditMode = page.getByRole("combobox", { name: "AIによる確認", exact: true });
      await expect(auditMode).toContainText("実行しない");
      await auditMode.click();
      await page.getByRole("option", { name: "詳細", exact: true }).click();
      await graphExpansion.click();
      await expect(save).toBeEnabled();
      await save.click();

      await expect(page.getByText("回答の検索と生成の設定を保存しました。")).toBeVisible();
      expect(saved).toEqual([
        {
          query_strategy: "rag_fusion",
          answer_flow: "crag",
          answer_audit_mode: "detailed",
          answer_evidence_refinement_mode: "legacy",
          neighbor_child_count: 5,
          rerank_enabled: true,
          screen_linking_enabled: true,
          request_coverage_retrieval_enabled: true,
          auto_field_filter_enabled: true,
          graph_expansion_enabled: true,
          entity_expansion_enabled: true,
          entity_expansion_max_chunks: 6,
          section_expansion_enabled: true,
          section_expansion_max_levels: 4,
          section_expansion_input_token_percent: 50,
        },
      ]);
      await expect(save).toBeDisabled();
      await expect(screenLinking).toBeChecked();
      await expectNoPageOverflow(page);
    });
  }
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`生成後の回答確認は独立して保存・破棄し再読込できる (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockRetrievalCards(page);
    const saved: unknown[] = [];
    await mockAnsweringSettings(page, saved);
    await page.goto("/settings/retrieval");
    const card = page.getByTestId("answering-settings-card");
    const audit = card.getByRole("combobox", { name: "AIによる確認", exact: true });
    const save = card.getByRole("button", { name: "回答の設定を保存", exact: true });
    const discard = card.getByRole("button", { name: "変更を破棄", exact: true });
    await expect(card.getByRole("heading", { name: "生成後の回答確認", exact: true })).toBeVisible();
    await expect(page.getByTestId("answering-audit-mode")).toContainText("引用ID・引用範囲の基本確認は、設定に関わらず実行します。");
    await expect(card).toContainText("回答生成が明示した根拠不足の要求は、回数・時間の予算内");
    await expect(card).toContainText("未校正の意味監査の結果だけでは、追加検索や再生成を行いません。");
    await expect(audit).toContainText("実行しない");
    await expect(audit).toHaveAccessibleDescription(/自動の生成後の LLM 監査.*行いません.*引用の ID・行範囲・原文との照合/);
    const options = card.getByRole("group", { name: "回答の検索のオプション", exact: true });
    expect(await options.locator("button[role=switch],button[role=combobox]").evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("id") || element.getAttribute("aria-label"))
    )).toContain("answering-answer-audit-mode");
    const section = card.getByRole("switch", { name: "章節で広げる根拠", exact: true });
    const coverage = card.getByRole("switch", { name: "根拠の無い要求を探し直す", exact: true });
    expect(await section.evaluate((element) => Boolean(element.compareDocumentPosition(document.getElementById("answering-answer-audit-mode")!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    expect(await audit.evaluate((element) => Boolean(element.compareDocumentPosition(document.querySelector('[aria-label="根拠の無い要求を探し直す"]')!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    await audit.click();
    await expect(page.getByRole("option")).toHaveText(["実行しない", "標準", "詳細"]);
    await page.getByRole("option", { name: "標準", exact: true }).click();
    await expect(audit).toHaveAccessibleDescription(/未校正の意味判定による自動是正は行いません.*是正予算は最大 1 回.*初回を含め最大 2 回/);
    await expect(save).toBeEnabled();
    await discard.click();
    await expect(audit).toContainText("実行しない");
    await expect(save).toBeDisabled();
    expect(saved).toEqual([]);
    for (const label of ["詳細", "実行しない", "標準"]) {
      await audit.click();
      await page.getByRole("option", { name: label, exact: true }).click();
      if (label === "詳細") await expect(audit).toHaveAccessibleDescription(/意味判定や説明の抜けの診断だけでは作り直しません.*是正予算は最大 2 回.*合計最大 3 回/);
      await save.click();
      await expect(save).toBeDisabled();
      await page.reload();
      await expect(audit).toContainText(label);
      await expect(card.getByRole("combobox", { name: "回答の生成方式", exact: true })).toContainText("標準 RAG（既定）");
      await expect(coverage).not.toBeChecked();
      await expect(page.getByTestId("answering-audit-mode")).toContainText("独立した設定");
      await expect(page.getByTestId("answering-audit-mode")).toContainText("共有の時間予算");
    }
    expect(saved).toHaveLength(3);
    expect(saved).toEqual(expect.arrayContaining([
      expect.objectContaining({ answer_audit_mode: "none", answer_flow: "standard_rag", request_coverage_retrieval_enabled: false }),
      expect.objectContaining({ answer_audit_mode: "standard", answer_flow: "standard_rag", request_coverage_retrieval_enabled: false }),
      expect.objectContaining({ answer_audit_mode: "detailed", answer_flow: "standard_rag", request_coverage_retrieval_enabled: false }),
    ]));
    await page.getByTestId("answering-audit-mode").scrollIntoViewIfNeeded();
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-audit-mode-${viewport.name}.png`) });
  });
}

test("回答の検索と生成の既定を読み込めないときは、その欄だけに理由を出す (#593)", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockRetrievalCards(page);
  await page.route("**/api/settings/answering", (route) =>
    route.fulfill({
      status: 503,
      json: { data: null, error_messages: ["読み込めません"], warning_messages: [] },
    })
  );

  await page.goto("/settings/retrieval");

  // 取得の失敗は API の理由と再試行をそのカードに出す（ApiErrorState。#906 / #1002）。
  const card = page.getByTestId("answering-settings-card");
  await expect(card.getByRole("alert")).toContainText("読み込めません");
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "回答の設定を保存" })).toHaveCount(0);
  // 検索プロンプトの欄はそのまま使える。
  await expect(page.getByRole("button", { name: "保存期間を保存" })).toHaveCount(0);
  await expect(page.getByTestId("answer-prompt-stages-card")).toBeVisible();
});



test("回答の検索と生成の保存に失敗しても入力を残して操作の行に出し、変更を破棄で保存値へ戻せる（#1002）", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockRetrievalCards(page);
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/settings/answering", async (route) => {
    if (route.request().method() === "PATCH") {
      await gate;
      await route.fulfill({
        status: 500,
        json: {
          data: null,
          error_messages: ["回答の検索と生成の設定を backend/.env へ保存できませんでした。"],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({ json: apiEnvelope(ANSWERING_SETTINGS) });
  });

  await page.goto("/settings/retrieval");
  const actions = page.getByRole("group", { name: "回答の検索と生成の設定の操作" });
  const strategy = page.getByRole("combobox", { name: "質問の拡張", exact: true });
  await strategy.click();
  await page.getByRole("option", { name: /RAG フュージョン/ }).click();
  await actions.getByRole("button", { name: "回答の設定を保存" }).click();
  // 保存中は欄を変えられない（保存の後に保存値で作り直すため、保存中の変更は消える）。
  await expect(strategy).toBeDisabled();
  release();

  await expect(actions).toContainText("回答の検索と生成の設定を backend/.env へ保存できませんでした。");
  await expect(strategy).toContainText("RAG フュージョン");
  await expect(strategy).toBeEnabled();
  await actions.getByRole("button", { name: "変更を破棄" }).click();
  await expect(strategy).toContainText("自動ルーティング");
  await expect(actions.getByRole("button", { name: "回答の設定を保存" })).toBeDisabled();
});


async function collapseSidebar(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
}



/** 設定とプロンプトの取得（GET）を mock する。`gate` を渡すと、その間は応答を止める（読み込み中の確認）。 */
async function mockRetrievalCards(page: Page, gate?: Promise<void>, stages = FLOW_STAGES) {
  const cards: [string, unknown][] = [
    ["**/api/settings/answer-prompts", { prompts: [], stages }],
    ["**/api/settings/answering", ANSWERING_SETTINGS],
  ];
  for (const [url, data] of cards) {
    await page.route(url, async (route) => {
      if (gate) await gate;
      await route.fulfill({ json: apiEnvelope(data) });
    });
  }
}

for (const width of [1280, 375]) {
  test(`検索プロンプトは処理順・使用条件・読み取り専用で確認できる (${width}px)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await mockAuthUser(page, { permissions: ["menu.settings_retrieval"] });
    const displayStages = [{ id: "rule_selection", phase: "question", prompts: [{ id: "system", content: "## Step 1: 選択を確かめる\n合成の条件。\n\n## Step 2: 結果を返す\nJSON のみ。" }] }, ...FLOW_STAGES].map((stage) => stage.id === "history_rewrite" ? {
      ...stage, prompts: [{ id: "system", content: "## 目的と手順\n\n- 合成の対象名を補います。\n- 質問だけを 1 行で返します。" }],
    } : stage);
    await mockRetrievalCards(page, undefined, displayStages);
    await page.goto("/settings/retrieval");
    const prompts = page.getByRole("region", { name: "検索に使うプロンプト", exact: true });
    const card = page.getByTestId("answer-prompt-stages-card");
    await expect(card).toBeVisible();
    const bounds = await card.boundingBox();
    const reference = await page.getByTestId("answering-settings-card").boundingBox();
    expect(Math.abs(bounds!.width-reference!.width)).toBeLessThanOrEqual(1);
    expect(await card.evaluate(node => parseFloat(getComputedStyle(node).borderTopWidth))).toBeGreaterThan(0);

    await expect(prompts.getByRole("heading", { level: 3 })).toHaveText(["質問の解釈", "検索", "根拠の判定"]);
    const expected = displayStages.filter((stage) => stage.id !== "support_guide_conditions" && ["question", "retrieval", "grounding"].includes(stage.phase));
    await expect(prompts.locator("details")).toHaveCount(expected.length);
    for (const stage of expected) {
      const panel = prompts.getByTestId(`answer-prompt-stage-${stage.id}`);
      await expect(panel).toContainText("使われる条件:");
      await panel.locator("summary").click();
      const fields = panel.getByRole("textbox");
      expect(await fields.evaluateAll((elements) => elements.map((element) => (element as HTMLTextAreaElement).value))).toEqual(stage.prompts.map((part) => part.content));
      for (const field of await fields.all()) await expect(field).toHaveAttribute("readonly", "");
      await panel.locator("summary").click();
    }
    await expect(prompts.getByRole("button", { name: "プロンプトを保存" })).toHaveCount(0);
    await expect(prompts.getByTestId("answer-prompt-stage-generation")).toHaveCount(0);
    await expect(prompts.getByTestId("answer-prompt-stage-support_guide_conditions")).toHaveCount(0);
    await expect(prompts.getByTestId("answer-prompt-stage-history_rewrite")).toContainText("書き換えを有効にしたとき");
    await expect(prompts.getByTestId("answer-prompt-stage-section_extraction")).toContainText("原文抽出は既定で無効");
    await expect(prompts.getByTestId("answer-prompt-stage-rule_selection")).toContainText("曖昧な条件は確認に残します");
    const rewrite = prompts.getByTestId("answer-prompt-stage-history_rewrite");
    await rewrite.locator("summary").click();
    await expect(rewrite.getByRole("textbox")).toHaveValue(/\n- .+\n- /);
    await rewrite.getByRole("textbox").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("history-rewrite-bullets.png") });
    await rewrite.locator("summary").click();
    await prompts.getByRole("heading", { level: 2 }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("retrieval-prompts.png") });
    await expectNoPageOverflow(page);
  });

  test(`検索プロンプトの取得失敗は他の設定を妨げず再試行できる (${width}px)`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockRetrievalCards(page);
    let failed = true;
    await page.route("**/api/settings/answer-prompts", (route) => failed
      ? route.fulfill({ status: 503, json: { data: null, error_messages: ["取得できませんでした"], warning_messages: [] } })
      : route.fulfill({ json: apiEnvelope({ prompts: [], stages: FLOW_STAGES }) }));
    await page.goto("/settings/retrieval");
    await expect(page.getByText("プロンプトを読み込めませんでした。", { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole("button", { name: "回答の設定を保存", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "保存期間を保存" })).toHaveCount(0);
    failed = false;
    await page.getByRole("button", { name: "再試行", exact: true }).click();
    await expect(page.getByRole("region", { name: "検索に使うプロンプト", exact: true })).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

 test("根拠の前後から加える数に不要なゼロの説明を表示しない", {tag: "@responsive"}, async ({page}) => {
  await mockLocalAuth(page);
  await mockRetrievalCards(page);
  await page.goto("/settings/retrieval");
  await expect(page.getByText("当たった根拠の前と後から、それぞれ最大でこの数のチャンクを文脈に加えます。同じ親やページの範囲、取得できる根拠、入力上限によって実際の数は減ります。", {exact:true})).toBeVisible();
  await expect(page.getByText("0 は加えません。", {exact:false})).toHaveCount(0);
 });

test("検索方法の指示の編集内容を保存できる", { tag: "@responsive" }, async ({ page }) => {
  await mockRetrievalCards(page);
  const { mockEditableStage } = await import("./_feature-prompt-helpers");
  const requests = await mockEditableStage(page, "routing", "question", "system");
  await page.goto("/settings/retrieval");
  const stage = page.getByTestId("answer-prompt-stage-routing");
  await stage.locator("summary").click();
  await expect(stage.getByRole("group", { name: "編集内容の操作" }).getByRole("button"))
    .toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  await stage.getByRole("textbox").fill("## Step 1: 合成の検索指示");
  await stage.getByRole("button", { name: "プロンプトを保存" }).click();
  await expect(stage.getByRole("button", { name: "プロンプトを保存" })).toBeDisabled();
  expect(requests).toEqual([{ key: "routing.system", method: "PUT", content: "## Step 1: 合成の検索指示" }]);
  await expectNoPageOverflow(page);
});


// 選別は回答の全体設定で、監査・補検索を自動で有効にしない。
test("生成後の根拠選別を独立して保存・破棄し再読込できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockRetrievalCards(page);
  const saved: unknown[] = [];
  await mockAnsweringSettings(page, saved);
  await page.goto("/settings/retrieval");
  const card = page.getByTestId("answering-settings-card");
  const select = card.getByRole("combobox", { name: "生成後の根拠選別", exact: true });
  const save = card.getByRole("button", { name: "回答の設定を保存", exact: true });
  const discard = card.getByRole("button", { name: "変更を破棄", exact: true });
  const audit = card.getByRole("combobox", { name: "AIによる確認", exact: true });
  const coverage = card.getByRole("switch", { name: "根拠の無い要求を探し直す", exact: true });
  await expect(select).toContainText("既存方式を維持");
  await expect(save).toBeDisabled();
  await select.click();
  await expect(page.getByRole("option")).toHaveText([
    "既存方式を維持", "引用済みの根拠だけ（比較用）", "引用と必須の補完根拠",
  ]);
  await page.getByRole("option", { name: "引用済みの根拠だけ（比較用）", exact: true }).click();
  await expect(select).toHaveAccessibleDescription(/重要な根拠を落とすおそれ/);
  await discard.click();
  await expect(select).toContainText("既存方式を維持");
  expect(saved).toEqual([]);
  for (const label of ["引用済みの根拠だけ（比較用）", "引用と必須の補完根拠", "既存方式を維持"]) {
    await select.click();
    await page.getByRole("option", { name: label, exact: true }).click();
    await save.click();
    await expect(save).toBeDisabled();
    await page.reload();
    await expect(select).toContainText(label);
    await expect(audit).toContainText("実行しない");
    await expect(coverage).not.toBeChecked();
    if (label === "引用と必須の補完根拠") {
      await expect(select).toHaveAccessibleDescription(/意味の支持を保証しません/);
      await page.getByTestId("answering-evidence-refinement").scrollIntoViewIfNeeded();
      await expectNoPageOverflow(page);
      await page.screenshot({ path: testInfo.outputPath("evidence-refinement-enabled.png") });
    }
  }
  expect(saved).toEqual([
    expect.objectContaining({ answer_evidence_refinement_mode: "cited_only", answer_audit_mode: "none", request_coverage_retrieval_enabled: false }),
    expect.objectContaining({ answer_evidence_refinement_mode: "cited_required", answer_audit_mode: "none", request_coverage_retrieval_enabled: false }),
    expect.objectContaining({ answer_evidence_refinement_mode: "legacy", answer_audit_mode: "none", request_coverage_retrieval_enabled: false }),
  ]);
  const section = page.getByTestId("answering-evidence-refinement");
  await expect(section).toContainText("最大 1 回再生成");
  await expect(section).toContainText("既存の是正回数・時間予算");
  await expect(section).toContainText("意味の確認を暗黙に追加しません");
  await section.scrollIntoViewIfNeeded();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("evidence-refinement-settings.png") });
});
