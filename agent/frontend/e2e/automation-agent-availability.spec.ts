import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #927: 自動実行の業務 Agent が使えなくなったとき・業務 Agent の未選択・タイムゾーンの誤り。

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function seedAgent(mockApi: MockApi, overrides: Record<string, unknown>) {
  mockApi.state.agents.push({
    id: "agent-927",
    name: "経理の Agent",
    description: "",
    instructions: "",
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

function seedAutomation(mockApi: MockApi, agentId: string) {
  mockApi.state.automations.push({
    id: "auto-927",
    agent_id: agentId,
    name: "毎朝の経理の要約",
    goal: "昨日の仕訳を要約してください。",
    enabled: true,
    schedule: { frequency: "daily", time: "09:00", weekdays: [0], minute: 0, timezone: "Asia/Tokyo" },
    run_as_user_uuid: "local",
    created_by_user_uuid: "local",
    next_run_at: MOCK_NOW,
    last_run_at: null,
    last_run_id: null,
    last_trigger: null,
    last_result: null,
    last_message: null,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

test("業務 Agent が実行できなくなった自動実行は、業務 Agent を見せたまま無効にして保存できる", async ({ page, mockApi }) => {
  seedAgent(mockApi, { enabled: false, published_version: 1, versions: [{ version: 1, name: "経理の Agent" }] });
  seedAutomation(mockApi, "agent-927");
  await page.goto("/automations?id=auto-927");
  await expect(page.getByRole("heading", { name: "毎朝の経理の要約", level: 1 })).toBeVisible();

  const agentSelect = page.locator("#automation-agent");
  await expect(agentSelect).toHaveText(/経理の Agent（実行できません）/);
  await expect(
    page.getByText("この業務 Agent は無効・未公開か、削除されています。", { exact: false })
  ).toBeVisible();

  // 有効のまま保存すると断られる（理由はヘッダーの直下）。
  await page.locator("#automation-goal").fill("昨日の仕訳を要約し、差異を報告してください。");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByTestId("automation-save-error")).toContainText("この業務 Agent は実行できない状態です。");

  // 無効にすれば保存できる。
  await page.getByRole("switch", { name: "有効にする" }).click();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("自動実行を保存しました")).toBeVisible();
  const body = mockApi.lastRequest("PUT", "/api/automations/auto-927")?.body as Record<string, unknown>;
  expect(body).toMatchObject({ agent_id: "agent-927", enabled: false });
  await expectNoHorizontalOverflow(page);
});

test("消えた業務 Agent の自動実行は ID と「見つかりません」を出す", async ({ page, mockApi }) => {
  seedAutomation(mockApi, "deleted-agent-927");
  await page.goto("/automations?id=auto-927");
  await expect(page.locator("#automation-agent")).toHaveText(/deleted-agent-927（見つかりません）/);
});

test("新規を直接開いても、業務 Agent の一覧が届いたら先頭の業務 Agent を選んで作成できる", async ({ page, mockApi }) => {
  await page.goto("/automations?id=new");
  await expect(page.locator("#automation-agent")).toHaveText(/資料・データ横断アシスタント/);
  await page.getByLabel("名前").fill("受注の通知");
  await page.locator("#automation-goal").fill("受け取った受注を確認してください。");
  // 業務 Agent を選んだだけでは未保存の変更にしない（一覧へ戻るで確認を出さない）。
  await page.getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("自動実行を作成しました")).toBeVisible();
  expect((mockApi.lastRequest("POST", "/api/automations")?.body as { agent_id: string }).agent_id).toBe("default");
});

test("使える業務 Agent が無いときと、タイムゾーンの誤りを欄の下に出して送らない", async ({ page, mockApi }) => {
  for (const agent of mockApi.state.agents) agent.published_version = null;
  await page.goto("/automations?id=new");
  await expect(page.getByText("使える業務 Agent がありません。業務 Agent を公開してから作成してください。")).toBeVisible();
  await page.getByLabel("名前").fill("受注の通知");
  await page.locator("#automation-goal").fill("受け取った受注を確認してください。");
  await page.getByLabel("タイムゾーン").fill("Mars/Olympus");
  await page.getByRole("button", { name: "作成", exact: true }).click();

  await expect(page.getByText("業務 Agent を選んでください。")).toBeVisible();
  await expect(page.locator("#automation-agent")).toBeFocused();
  await expect(page.getByText("タイムゾーンが正しくありません。", { exact: false })).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/automations")).toBeUndefined();
  await expectNoHorizontalOverflow(page);
});
