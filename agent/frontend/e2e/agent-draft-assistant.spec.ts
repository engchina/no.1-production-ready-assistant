import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #2519: 業務の説明から業務 Agent の下書きを作る・公開前の評価の門で断られたときの表示。

const SUGGESTION = {
  name: "Agent B",
  description: "資料を調べて答える",
  instructions: "根拠の箇所を示して答える。範囲の外の質問には答えられないと伝える。",
  skill_ids: ["business-rag-research"],
  data_scopes: { rag: { profile_ids: ["bv-legal"], default_profile_id: "bv-legal" } },
  // #2561: 決まった項目で結果を返す業務なら、出力の項目も提案する。
  output_fields: [
    { key: "item_name", label: "項目", type: "text", required: true, description: "" },
    { key: "amount", label: "金額", type: "number", required: false, description: "円" },
  ],
  evaluation_cases: [
    { question: "質問 A", expected: "根拠の箇所を示して答える" },
    { question: "質問 B", expected: "範囲の外であることを伝える" },
    { question: "質問 C", expected: "必要な条件を確かめてから答える" },
  ],
  notices: [
    "データ問い合わせ（NL2SQL）の候補を取得できません。",
    "出力の項目を 2 件入れました。回答を文だけで返す業務なら、「出力の項目」から外してください。",
  ],
};

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function suggest(page: Page, description: string) {
  await page.getByLabel("業務の説明").fill(description);
  await page.getByTestId("agent-draft-suggest").click();
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`業務の説明から下書きを作ってフォームに入れ、評価の問いと一緒に作成できる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    mockApi.state.draftSuggestion = SUGGESTION;
    await page.goto("/agents?id=new");
    await expect(page.getByRole("heading", { name: "説明から下書きを作る" })).toBeVisible();

    await suggest(page, "社内の手続きの質問に、規程の根拠を示して答える");
    await expect(page.getByText("提案をフォームに入れました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/agents/draft-suggestion")?.body).toEqual({
      description: "社内の手続きの質問に、規程の根拠を示して答える",
    });
    await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("Agent B");
    await expect(page.getByLabel("指示")).toHaveValue(SUGGESTION.instructions);
    await expect(page.getByTestId("agent-skill-picker")).toContainText("選択 1 件");
    const outputFields = page.getByTestId("agent-output-fields");
    await expect(outputFields.getByRole("textbox", { name: "表示名" }).first()).toHaveValue("項目");
    await expect(outputFields.getByRole("textbox", { name: "key" }).nth(1)).toHaveValue("amount");
    await expect(page.getByTestId("agent-draft-notices")).toContainText("NL2SQL");
    await expect(page.getByTestId("agent-draft-notices")).toContainText("出力の項目を 2 件入れました");
    const cases = page.getByTestId("agent-draft-cases");
    await expect(page.getByRole("heading", { name: "評価の問い（3 件）" })).toBeVisible();
    await expect(cases).toContainText("質問 A");
    await expect(cases.getByRole("checkbox")).toBeChecked();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-draft-assistant-${viewport.name}.png`), fullPage: true });

    await page.getByRole("button", { name: "作成", exact: true }).click();
    await expect(page.getByText("Agent を作成しました")).toBeVisible();
    const created = mockApi.state.agents.find((agent) => agent.name === "Agent B");
    expect(created?.skill_ids).toEqual(["business-rag-research"]);
    expect(created?.data_scopes).toEqual(SUGGESTION.data_scopes);
    // 下書きは評価の比べ方を提案しないので、画面が既定（完全に一致）を入れて保存する（#2571）。
    expect(created?.output_fields).toEqual(SUGGESTION.output_fields.map((field) => ({ ...field, match: "exact" })));
    const evaluationSet = mockApi.lastRequest("POST", "/api/evaluation-sets")?.body as
      | { agent_id: string; name: string; cases: { question: string }[] }
      | undefined;
    expect(evaluationSet?.agent_id).toBe(created?.id);
    expect(evaluationSet?.name).toBe("Agent B の評価");
    expect(evaluationSet?.cases.map((item) => item.question)).toEqual(["質問 A", "質問 B", "質問 C"]);
  });
}

test("入力済みのフォームは、確認してから提案で置き換える", async ({ page, mockApi }) => {
  mockApi.state.draftSuggestion = SUGGESTION;
  await page.goto("/agents?id=new");
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("入力中の名前");

  await suggest(page, "業務の説明");
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog.getByText("入力中の内容を置き換えますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("入力中の名前");

  await page.getByTestId("agent-draft-suggest").click();
  await dialog.getByRole("button", { name: "置き換える" }).click();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("Agent B");
});

test("説明が空なら送らず、欄に理由を出す", async ({ page, mockApi }) => {
  await page.goto("/agents?id=new");
  await page.getByTestId("agent-draft-suggest").click();
  await expect(page.getByText("業務の説明を入力してください。")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/agents/draft-suggestion")).toBeUndefined();
});

function seedDraftAgent(mockApi: MockApi) {
  mockApi.state.agents.push({
    id: "agent-2519",
    name: "Agent C",
    description: "",
    instructions: "v1 の指示",
    skill_ids: [],
    model_id: "",
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [],
    published_version: null,
    unpublished_changes: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

test("公開前の評価の門で断られると、理由を出して公開しない", async ({ page, mockApi }) => {
  seedDraftAgent(mockApi);
  const refusal = "公開するには、今の下書きを品質評価で確かめ、合格率 80% 以上にしてください（下書きを変えた後の評価がありません）。";
  mockApi.state.publishEvaluationRefusal = refusal;
  await page.goto("/agents?id=agent-2519");
  await expect(page.getByRole("heading", { name: "Agent C", level: 1 })).toBeVisible();

  const actions = page.getByTestId("agent-object-actions");
  const publish = actions.getByRole("button", { name: "公開" });
  if (await publish.count()) {
    await publish.click();
  } else {
    await page.getByTestId("agent-object-actions-more").click();
    await page.getByRole("menuitem", { name: "公開" }).click();
  }
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await dialog.getByRole("button", { name: "公開", exact: true }).click();

  await expect(page.getByText(refusal)).toBeVisible();
  expect(mockApi.state.agents.find((agent) => agent.id === "agent-2519")?.published_version).toBeNull();
});
