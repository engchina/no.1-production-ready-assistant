import { expect, test, type Page } from "./fixtures/test";
import { mockEditableStage } from "./_feature-prompt-helpers";
import { DEFAULT_STAGES, FLOW_STAGES, type StageFixture } from "./_prompt-stage-fixtures";
import { expectNoPageOverflow, mockLocalAuth, openSidebarNav } from "./_helpers";

// 回答プロンプトの画面（#595）。回答を作る指示のテンプレートと、回答の各工程（読み取り専用）だけを持つ。
// 以前の system prompt の版（作成・有効化）は削除した。

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

/** TanStack Query の window focus 再取得を起こす(タブへ戻った操作に相当)。 */
async function refetchOnFocus(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`回答プロンプトの画面はテンプレートだけを出し、版の一覧と作成は無い (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await page.route("**/api/settings/answer-prompts**", (route) =>
      route.fulfill({ json: answerPromptsEnvelope("既定のテンプレート {{question}} {{answer_context}}", false) })
    );

    await page.goto("/settings/prompts");

    await expect(page.getByRole("heading", { name: "回答プロンプト", exact: true, level: 1 })).toBeVisible();
    await expect(page.getByText("回答生成のプロンプト", { exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "テンプレート", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "版を作成" })).toHaveCount(0);
    await expect(page.getByTestId("prompt-version-list")).toHaveCount(0);
    await expect(page.getByTestId("answer-prompt-unused-note")).toHaveCount(0);
    await expect(page.locator("main")).not.toContainText("DocRAG");
    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    await expect((await openSidebarNav(page)).getByRole("link", { name: "回答プロンプト" })).toHaveAttribute(
      "aria-current",
      "page"
    );
    await expectNoPageOverflow(page);
  });

  test(`回答プロンプトの読み込み中は経過時間と形の Skeleton を出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/api/settings/answer-prompts**", async (route) => {
      await gate;
      await route.fulfill({ json: answerPromptsEnvelope("既定のテンプレート {{question}} {{answer_context}}", false) });
    });

    await page.goto("/settings/prompts");

    const loading = page.getByTestId("settings-prompts-loading");
    await expect(loading).toBeVisible();
    release();
    await expect(loading).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "テンプレート", exact: true })).toBeVisible();
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
]) {
  test(`回答生成のプロンプトを検証・保存・既定に戻せて、各段は Markdown 原文で見られる (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const requests: { method: string; body: unknown }[] = [];
    await page.route("**/api/settings/answer-prompts**", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        await route.fulfill({ json: answerPromptsEnvelope("既定のテンプレート {{question}} {{answer_context}}", false) });
        return;
      }
      const body = method === "PUT" ? route.request().postDataJSON() : null;
      requests.push({ method, body });
      if (method === "PUT" && !String((body as { content: string }).content).includes("{{answer_context}}")) {
        await route.fulfill({
          status: 422,
          json: { data: null, error_messages: ["必須の placeholder がありません: {{answer_context}}"], warning_messages: [] },
        });
        return;
      }
      await route.fulfill({
        json:
          method === "PUT"
            ? answerPromptsEnvelope((body as { content: string }).content, true)
            : answerPromptsEnvelope("既定のテンプレート {{question}} {{answer_context}}", false),
      });
    });

    await page.goto("/settings/prompts");

    const field = page.getByLabel("テンプレート");
    const save = page.getByRole("button", { name: "プロンプトを保存" });
    await expect(page.getByText("既定値", { exact: true })).toHaveCount(0);
    const actions = page.getByRole("group", { name: "編集内容の操作" });
    await expect(actions.getByRole("button")).toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
    await expect(actions.getByRole("button", { name: "既定に戻す" })).toBeDisabled();
    const fieldBox = await field.boundingBox();
    const actionBox = await actions.boundingBox();
    expect(actionBox!.y).toBeGreaterThan(fieldBox!.y + fieldBox!.height);
    await field.fill("質問だけ {{question}}");
    await save.click();
    // 必須の placeholder の欠けは送る前に欄の下へ出し、欄へフォーカスを移す（#1010）。
    await expect(page.getByText("必須の placeholder がありません: {{answer_context}}")).toBeVisible();
    await expect(field).toBeFocused();

    await field.fill("独自 {{question}} {{answer_context}}");
    await actions.scrollIntoViewIfNeeded();
    const buttonBoxes = await actions.getByRole("button").evaluateAll((buttons) =>
      buttons.map((button) => { const box = button.getBoundingClientRect(); return { x: box.x, y: box.y, right: box.right, bottom: box.bottom }; })
    );
    if (viewport.name === "desktop") {
      expect(buttonBoxes[0].right).toBeLessThanOrEqual(buttonBoxes[1].x);
      expect(buttonBoxes[1].right).toBeLessThanOrEqual(buttonBoxes[2].x);
      expect(buttonBoxes.map((box) => box.y)).toEqual([buttonBoxes[0].y, buttonBoxes[0].y, buttonBoxes[0].y]);
    } else {
      expect(buttonBoxes[0].bottom).toBeLessThanOrEqual(buttonBoxes[1].y);
      expect(buttonBoxes[1].bottom).toBeLessThanOrEqual(buttonBoxes[2].y);
    }
    await page.screenshot({ path: testInfo.outputPath("prompt-actions-bottom.png") });
    await save.click();
    await expect(page.getByText("プロンプトを保存しました。").last()).toBeVisible();
    await expect(page.getByText(/^編集済み/)).toBeVisible();

    await page.getByRole("button", { name: "既定に戻す" }).click();
    await expect(page.getByText("既定の内容に戻しました。保存すると反映されます。")).toBeVisible();
    expect(requests.map((request) => request.method)).toEqual(["PUT"]);
    await page.getByRole("button", { name: "変更を破棄" }).click();
    await expect(field).toHaveValue("独自 {{question}} {{answer_context}}");
    await page.getByRole("button", { name: "既定に戻す" }).click();
    await save.click();
    await expect(page.getByText("プロンプトを保存しました。").last()).toBeVisible();
    await expect(field).toHaveValue("既定のテンプレート {{question}} {{answer_context}}");
    expect(requests.map((request) => request.method)).toEqual(["PUT", "DELETE"]);

    await page.getByTestId("answer-prompt-stage-audit").locator("summary").click();
    const audit = page.getByTestId("answer-prompt-stage-audit");
    const source = audit.getByRole("textbox");
    await expect(source).toHaveValue(DEFAULT_STAGES.find((stage) => stage.id === "audit")!.prompts[0].content);
    await expect(source).toHaveAttribute("readonly", "");
    await expect(source).toBeEnabled();
    await expect(source).toHaveClass(/font-mono/);
    await source.focus();
    await source.evaluate((element: HTMLTextAreaElement) => element.select());
    expect(await source.evaluate((element: HTMLTextAreaElement) =>
      element.value.slice(element.selectionStart, element.selectionEnd)
    )).toContain("## 手順");
    await expect(audit.locator("h6, ol, code, script")).toHaveCount(0);
    expect(await page.evaluate(() => "promptInjected" in window)).toBe(false);
    await audit.screenshot({ path: testInfo.outputPath("markdown-prompt.png") });
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
]) {
  test(`各段のプロンプトは処理の順の区分の見出しの下に並び、閉じたまま使われる条件が分かる（#2054, ${viewport.name}）`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route("**/api/settings/answer-prompts**", (route) =>
      route.fulfill({ json: answerPromptsEnvelope("既定のテンプレート {{question}} {{answer_context}}", false, FLOW_STAGES) })
    );

    await page.goto("/settings/prompts");

    const stages = page.getByRole("region", { name: "処理の各段のプロンプト" });
    await expect(stages).toBeVisible();
    // 取込は解析と索引に分け、独立した準備の後に検索・回答・評価を並べる。
    await expect(stages.getByRole("heading", { level: 3 })).toHaveText([
      "業務ガイドの準備",
      "質問の解釈",
      "回答の生成",
      "監査",
    ]);
    const editor = stages.getByRole("textbox", { name: "テンプレート", exact: true });
    await expect(editor).toHaveCount(1);
    await expect(stages.getByRole("region", { name: "回答の生成" }).getByRole("textbox", { name: "テンプレート", exact: true })).toBeEditable();
    expect(await stages.getByTestId("answer-prompt-stage-generation").evaluate((element) =>
      Boolean(element.compareDocumentPosition(document.getElementById("answer-prompt-vlm_answer")!) & Node.DOCUMENT_POSITION_FOLLOWING)
    )).toBe(true);
    await expect(stages.getByTestId("answer-prompt-stage-audit")).toContainText("生成後の LLM 監査を有効にしたとき（既定は無効）");
    await expect(stages.getByTestId("answer-prompt-stage-application")).toContainText("生成後の LLM 監査が有効で");
    for (const phase of ["検索", "根拠の判定"]) {
      await expect(stages.getByRole("region", { name: phase, exact: true })).toHaveCount(0);
    }
    const conditions = stages.getByTestId("answer-prompt-stage-support_guide_conditions");
    await expect(conditions).toHaveCount(1);
    await conditions.locator("summary").click();
    const conditionSource = conditions.getByRole("textbox");
    await expect(conditionSource).toHaveAttribute("readonly", "");
    await expect(conditionSource).toHaveValue(FLOW_STAGES.find((stage) => stage.id === "support_guide_conditions")!.prompts[0].content);
    for (const id of ["history_rewrite", "field_filter", "routing", "expansion"]) {
      await expect(stages.getByTestId(`answer-prompt-stage-${id}`)).toHaveCount(0);
    }
    await expect(stages.getByTestId("answer-prompt-stage-faq_answer")).toContainText("検索しない");
    await expectNoPageOverflow(page);
    await stages.getByRole("heading", { name: "業務ガイドの準備", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("pipeline-prompt-order.png") });
    await editor.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("generation-template-position.png") });
    // 段は区分の中に入り、開かなくても使われる条件を 1 行で示す。
    await expect(stages.getByRole("region", { name: "文書解析" })).toHaveCount(0);
    await expect(stages.getByRole("region", { name: "文書分割・索引" })).toHaveCount(0);
    await expect(stages.getByRole("region", { name: "回答の生成" }).getByTestId("answer-prompt-stage-generation")).toContainText(
      "使われる条件: 検索した根拠から回答を生成するとき"
    );
    const generation = stages.getByTestId("answer-prompt-stage-generation");
    await expect(page.getByTestId("answer-prompt-stages-card")).toBeVisible();
    expect(await page.getByTestId("answer-prompt-stages-card").evaluate(node => parseFloat(getComputedStyle(node).borderTopWidth))).toBeGreaterThan(0);

    await generation.locator("summary").click();
    await expect(generation).toContainText("「前提を示して回答する」のときに追加する システムプロンプト");
    await expect(generation).toContainText("未確定の条件を明示する。");
    await expect(generation).toContainText("小問の根拠計画を使う回答生成のシステムプロンプト");
    await expect(generation).toContainText("計画を作り直さず原文から回答する。");
    await expect(generation).toContainText("システムプロンプト");
    await expect(generation).not.toContainText("system プロンプト");
    await generation.screenshot({ path: testInfo.outputPath("assumption-system-instructions.png") });
    await expect(stages.getByTestId("answer-prompt-stage-evaluation")).toHaveCount(0);
    await expect(stages.getByTestId("answer-prompt-stage-answer_validation")).toContainText("資料との照合");

    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
]) {
  test(`回答生成のプロンプトは空を送る前に止め、変更を破棄で保存値へ戻せる（#1010, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const requests: string[] = [];
    await page.route("**/api/settings/answer-prompts**", async (route) => {
      if (route.request().method() !== "GET") requests.push(route.request().method());
      await route.fulfill({ json: answerPromptsEnvelope("保存版 {{question}} {{answer_context}}", true) });
    });

    await page.goto("/settings/prompts");
    const field = page.getByRole("textbox", { name: "テンプレート", exact: true });
    const actions = page.getByRole("group", { name: "編集内容の操作" });
    const save = actions.getByRole("button", { name: "プロンプトを保存" });
    const discard = actions.getByRole("button", { name: "変更を破棄" });
    await expect(discard).toBeDisabled();

    await field.fill("   ");
    await expect(actions).toContainText("未保存の変更があります。");
    await save.click();
    await expect(page.getByText("テンプレートを入力してください。")).toBeVisible();
    await expect(field).toBeFocused();
    expect(requests).toEqual([]);

    await discard.click();
    await expect(field).toHaveValue("保存版 {{question}} {{answer_context}}");
    await expect(page.getByText("テンプレートを入力してください。")).toHaveCount(0);
    await expect(save).toBeDisabled();
    await expect(actions.getByText(/^編集済み/)).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

test("回答生成のプロンプトは編集中の内容を背景の再取得で上書きしない", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  let content = "保存版 A {{question}} {{answer_context}}";
  await page.route("**/api/settings/answer-prompts**", (route) =>
    route.fulfill({ json: answerPromptsEnvelope(content, true) })
  );

  await page.goto("/settings/prompts");
  const editor = page.getByRole("textbox", { name: "テンプレート", exact: true });
  await expect(editor).toHaveValue(content);

  // 未編集なら保存値の変更へ追従する。初回の取得（StrictMode の再購読による取り直しを含む）が
  // まだ途中だと、フォーカスの再取得はその取得（保存版 A）にまとめられるため、B が届くまで再取得を促す（#833）。
  content = "保存版 B {{question}} {{answer_context}}";
  await expect
    .poll(async () => {
      await refetchOnFocus(page);
      return editor.inputValue();
    })
    .toBe(content);

  // 編集中は、保存値が変わっても編集内容を残す。
  await editor.fill("編集中 {{question}} {{answer_context}}");
  content = "保存版 C {{question}} {{answer_context}}";
  const refetched = page.waitForResponse("**/api/settings/answer-prompts**");
  await refetchOnFocus(page);
  await refetched;
  await expect(editor).toHaveValue("編集中 {{question}} {{answer_context}}");
});

async function collapseSidebar(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
}

function answerPromptsEnvelope(content: string, customized: boolean, stages: StageFixture[] = DEFAULT_STAGES) {
  return {
    data: {
      prompts: [
        {
          key: "vlm_answer",
          content,
          default_content: "既定のテンプレート {{question}} {{answer_context}}",
          customized,
          required_placeholders: ["question", "answer_context"],
          updated_at: customized ? "2026-09-26T01:00:00Z" : null,
        },
      ],
      stages,
    },
    error_messages: [],
    warning_messages: [],
  };
}


for (const width of [1280, 375]) {
  test(`回答プロンプトの取得失敗後は再試行で工程と編集欄へ戻れる (${width}px)`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    let failed = true;
    await page.route("**/api/settings/answer-prompts**", (route) => failed
      ? route.fulfill({ status: 503, json: { data: null, error_messages: ["取得できませんでした"], warning_messages: [] } })
      : route.fulfill({ json: answerPromptsEnvelope("既定 {{question}} {{answer_context}}", false) }));
    await page.goto("/settings/prompts");
    await expect(page.getByText("プロンプトを読み込めませんでした。", { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole("textbox", { name: "テンプレート", exact: true })).toHaveCount(0);
    failed = false;
    await page.getByRole("button", { name: "再試行", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "テンプレート", exact: true })).toBeEditable();
    await expectNoPageOverflow(page);
  });
}

// 新しい工程別の編集欄もテンプレートと同じ保存・破棄・復帰の契約を使う。
test("資料との照合の指示を保存でき、既定への復帰は保存まで取り消せる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const key = "answer_validation.system";
  const builtin = "## Step 1: 資料と説明を確認する\n合成の指示。";
  let custom: string | null = null;
  const methods: string[] = [];
  await page.route("**/api/settings/answer-prompts**", async (route) => {
    const method = route.request().method();
    if (method !== "GET") {
      methods.push(method);
      custom = method === "DELETE" ? null : route.request().postDataJSON().content;
    }
    const envelope = answerPromptsEnvelope("既定 {{question}} {{answer_context}}", false, [
      { id: "answer_validation", phase: "audit", prompts: [{ id: "system", content: custom ?? builtin, prompt_key: key }] },
    ]);
    envelope.data.prompts.push({ key, content: custom ?? builtin, default_content: builtin,
      customized: custom !== null, required_placeholders: [], updated_at: null });
    await route.fulfill({ json: envelope });
  });
  await page.goto("/settings/prompts");
  const stage = page.getByTestId("answer-prompt-stage-answer_validation");
  await stage.locator("summary").click();
  const field = stage.getByRole("textbox");
  const save = stage.getByRole("button", { name: "プロンプトを保存" });
  const reset = stage.getByRole("button", { name: "既定に戻す" });
  await expect(field).toBeEditable();
  await field.fill("## Step 1: 合成の確認を追加する");
  await save.click();
  await expect(save).toBeDisabled();
  await reset.click();
  await expect(field).toHaveValue(builtin);
  expect(methods).toEqual(["PUT"]);
  await stage.getByRole("button", { name: "変更を破棄" }).click();
  await expect(field).toHaveValue("## Step 1: 合成の確認を追加する");
  await reset.click();
  await save.click();
  await expect(save).toBeDisabled();
  expect(methods).toEqual(["PUT", "DELETE"]);
  await expect(page.getByText("既定値", { exact: true })).toHaveCount(0);
  const actions = stage.getByRole("group", { name: "編集内容の操作" });
  await expect(actions.getByRole("button")).toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  const resetBox = await reset.boundingBox();
  const fieldBox = await field.boundingBox();
  expect(resetBox!.y).toBeGreaterThan(fieldBox!.y + fieldBox!.height);
  await expectNoPageOverflow(page);
  await stage.screenshot({ path: testInfo.outputPath("editable-stage.png") });
});


test("引用範囲の修復指示を保存・再読込でき、既定への復帰は保存まで取り消せる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const key = "generation.citationRepair";
  const label = "引用範囲の修復（同じ根拠・版を特定でき、行範囲が未指定・不正・範囲外で、是正の予算が残るとき）";
  const requests = await mockEditableStage(page, "generation", "generation", "citationRepair");
  await page.goto("/settings/prompts");
  const stage = page.getByTestId("answer-prompt-stage-generation");
  await stage.locator("summary").click();
  const field = stage.getByRole("textbox", { name: label, exact: true });
  const actions = stage.getByRole("group", { name: "編集内容の操作" });
  const save = actions.getByRole("button", { name: "プロンプトを保存", exact: true });
  const discard = actions.getByRole("button", { name: "変更を破棄", exact: true });
  const reset = actions.getByRole("button", { name: "既定に戻す", exact: true });
  const builtin = "## Step 1: 合成の既定指示";
  const custom = "## Step 1: 合成の引用範囲を確認する";
  await expect(field).toBeEditable();
  await expect(field).toHaveValue(builtin);
  await field.fill(custom);
  await save.click();
  await expect(save).toBeDisabled();
  expect(requests).toEqual([{ key, method: "PUT", content: custom }]);
  await page.reload();
  await stage.locator("summary").click();
  await expect(field).toHaveValue(custom);
  await field.fill("## Step 1: 破棄する変更");
  await discard.click();
  await expect(field).toHaveValue(custom);
  await reset.click();
  await expect(field).toHaveValue(builtin);
  expect(requests).toHaveLength(1);
  await discard.click();
  await expect(field).toHaveValue(custom);
  await reset.click();
  await save.click();
  await expect(save).toBeDisabled();
  expect(requests).toEqual([{ key, method: "PUT", content: custom }, { key, method: "DELETE", content: null }]);
  await page.reload();
  await stage.locator("summary").click();
  await expect(field).toHaveValue(builtin);
  await expect(actions.getByRole("button")).toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  await expect(stage.getByText("既定値", { exact: true })).toHaveCount(0);
  await actions.scrollIntoViewIfNeeded();
  const boxes = await actions.getByRole("button").evaluateAll((buttons) => buttons.map((button) => {
    const box = button.getBoundingClientRect();
    return { x: box.x, y: box.y, right: box.right, bottom: box.bottom };
  }));
  const fieldBox = await field.boundingBox();
  expect(boxes[2].y).toBeGreaterThanOrEqual(fieldBox!.y + fieldBox!.height);
  if (testInfo.project.name === "mobile") {
    expect(boxes[0].bottom).toBeLessThanOrEqual(boxes[1].y);
    expect(boxes[1].bottom).toBeLessThanOrEqual(boxes[2].y);
  } else {
    expect(boxes[0].right).toBeLessThanOrEqual(boxes[1].x);
    expect(boxes[1].right).toBeLessThanOrEqual(boxes[2].x);
  }
  await expectNoPageOverflow(page);
  await stage.screenshot({ path: testInfo.outputPath("citation-range-prompt-editor.png") });
});
