import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #925: 業務 Agent のエディタ。登録から消えたスキルを外せること、未保存の変更があるあいだは公開できないこと。

const AGENT_ID = "agent-925";

function seedAgent(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.agents.push({
    id: AGENT_ID,
    name: "経理の Agent",
    description: "経理の質問に答える",
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
    ...overrides,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("登録から消えたスキルが「見つかりません」として出て、外して保存できる", async ({ page, mockApi }) => {
  const knownSkill = String(mockApi.state.skills[0]?.id);
  seedAgent(mockApi, { skill_ids: ["deleted_skill_925", knownSkill].sort() });
  await page.goto(`/agents?id=${AGENT_ID}`);

  await expect(page.getByRole("heading", { name: "経理の Agent", level: 1 })).toBeVisible();
  await expect(
    page.getByText("登録されていないスキルが割り当てられています（deleted_skill_925）", { exact: false })
  ).toBeVisible();
  const missing = page.getByRole("option", { name: /deleted_skill_925/ });
  await expect(missing).toHaveAttribute("aria-checked", "true");
  await expect(missing).toContainText("見つかりません");
  await expect(page.getByTestId("agent-skill-picker")).toContainText("選択 2 件");
  await expectNoHorizontalOverflow(page);

  // 外さずに保存すると backend と同じく断られる（外す手段が画面にあることを確かめる前提）。
  await page.getByLabel("指示").fill("v2 の指示");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByTestId("agent-save-error")).toContainText("登録されていないスキルがあります: deleted_skill_925");

  await missing.click();
  await expect(missing).toHaveCount(0);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  const saved = mockApi.lastRequest("PATCH", `/api/agents/${AGENT_ID}`);
  expect(saved?.body).toMatchObject({ skill_ids: [knownSkill], instructions: "v2 の指示" });
  await expect(page.getByText("登録されていないスキルが割り当てられています", { exact: false })).toHaveCount(0);
});

test("保存していない変更があるあいだは公開できない", async ({ page, mockApi }) => {
  seedAgent(mockApi);
  await page.goto(`/agents?id=${AGENT_ID}`);
  await expect(page.getByRole("heading", { name: "経理の Agent", level: 1 })).toBeVisible();

  const actions = page.getByTestId("agent-object-actions");
  const publish = actions.getByRole("button", { name: "公開" });
  await expect(publish).toBeEnabled();
  await expect(page.getByTestId("agent-save-before-publish")).toHaveCount(0);

  await page.getByLabel("指示").fill("保存していない指示");
  await expect(publish).toBeDisabled();
  await expect(page.getByTestId("agent-save-before-publish")).toHaveText(
    "保存していない変更があります。公開するには先に保存してください（公開するのは保存した下書きです）。"
  );

  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  await expect(publish).toBeEnabled();
  await publish.click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await dialog.getByRole("button", { name: "公開", exact: true }).click();
  await expect(page.getByText("v1 として公開しました")).toBeVisible();
  const agent = mockApi.state.agents.find((item) => item.id === AGENT_ID);
  expect((agent?.versions as Array<Record<string, unknown>>)[0]?.instructions).toBe("保存していない指示");
});
