import { MOCK_NOW, expect, test } from "./fixtures/mock-api";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./fixtures/select-field";

// #1839: 業務 Agent の「回答前の確認」（曖昧な質問の扱い・確認の上限（往復））。既定は「確認してから回答する」・3 回。
// 「前提を示して回答する」では上限を使わない（選べなくする）。保存の本文と、公開していない変更になることを確かめる。

const AGENT_ID = "agent-1839";

test("業務 Agent の回答前の確認の方針と上限を保存できる", async ({ page, mockApi }, testInfo) => {
  mockApi.state.agents.push({
    id: AGENT_ID,
    name: "売上分析の Agent",
    description: "",
    instructions: "",
    skill_ids: [],
    model_id: "",
    data_scopes: {},
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [],
    published_version: null,
    unpublished_changes: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
  await page.goto(`/agents?id=${AGENT_ID}`);
  await expect(page.getByRole("heading", { name: "回答前の確認", level: 2 })).toBeVisible();

  const policy = page.locator(`#${AGENT_ID}-agent-clarification-policy`);
  const rounds = page.locator(`#${AGENT_ID}-agent-clarification-max-rounds`);
  // 項目の無い業務 Agent は既定（確認してから回答する・上限は既定の 3 回）。
  await expectSelectFieldValue(policy, "confirm");
  await expectSelectFieldValue(rounds, "0");
  await expect(rounds).toHaveText("既定（3 回）");
  await chooseSelectFieldOption(rounds, "3");
  await chooseSelectFieldOption(policy, "assume");
  // 前提を示して回答するときは確認しないので、上限は選べない。
  await expect(rounds).toBeDisabled();
  await page.getByTestId("agent-clarification").screenshot({ path: testInfo.outputPath("agent-clarification.png") });

  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  expect(mockApi.lastRequest("PATCH", `/api/agents/${AGENT_ID}`)?.body).toMatchObject({
    clarification_policy: "assume",
    clarification_max_rounds: 3,
  });
  await expect(page.getByRole("button", { name: "変更を破棄" })).toBeDisabled();

  // 変更を破棄で、保存した内容に戻る。
  await chooseSelectFieldOption(policy, "confirm");
  await expect(rounds).toBeEnabled();
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expectSelectFieldValue(policy, "assume");
});
