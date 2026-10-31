import type { Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #2515: 業務 Agent の編集画面の「試す」。保存した下書きで会話し、下書きの会話はチャットの会話と分ける。

const AGENT_ID = "agent-2515";

function seedAgent(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.agents.push({
    id: AGENT_ID,
    name: "Agent A",
    description: "資料を調べて答える",
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

async function openTryTab(page: Page) {
  await page.goto(`/agents?id=${AGENT_ID}`);
  await expect(page.getByRole("heading", { name: "Agent A", level: 1 })).toBeVisible();
  await page.getByRole("tab", { name: "試す" }).click();
  await expect(page.getByRole("tab", { name: "試す" })).toHaveAttribute("aria-selected", "true");
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`公開していない業務 Agent を、保存した下書きで続けて質問して試せる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedAgent(mockApi);
    await openTryTab(page);

    await expect(page.getByRole("heading", { name: "下書きで試す" })).toBeVisible();
    await expect(page.getByText("質問を入力して、保存した下書きを試します")).toBeVisible();
    // 試すときは業務 Agent を選ばない（編集中の業務 Agent だけ）。
    await expect(page.getByLabel("業務 Agent", { exact: true })).toHaveCount(0);

    const composer = page.getByRole("textbox", { name: "質問" });
    await composer.fill("最初の質問");
    await page.getByTestId("chat-send").click();
    await expect(page.getByText("「最初の質問」への回答です。")).toBeVisible();
    const first = mockApi.lastRequest("POST", "/api/runs");
    expect(first?.body).toMatchObject({ agent_id: AGENT_ID, goal: "最初の質問", draft: true });

    // 同じ会話の続きも下書きで実行する。
    await composer.fill("続きの質問");
    await page.getByTestId("chat-send").click();
    await expect(page.getByText("「続きの質問」への回答です。")).toBeVisible();
    const second = mockApi.lastRequest("POST", "/api/runs");
    expect(second?.body).toMatchObject({ draft: true, thread_id: (first?.body as { thread_id?: string }).thread_id ?? expect.any(String) });

    // 試した会話は下書きの会話の一覧から読む。
    expect(mockApi.lastRequest("GET", "/api/threads")?.searchParams.get("draft")).toBe("true");

    // 「設定」に戻ってもフォームの入力は残り、「試す」に戻ると会話も残る。
    await page.getByRole("tab", { name: "設定" }).click();
    await expect(page.getByLabel("指示")).toHaveValue("v1 の指示");
    await page.getByRole("tab", { name: "試す" }).click();
    await expect(page.getByText("「続きの質問」への回答です。")).toBeVisible();

    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-editor-try-${viewport.name}.png`), fullPage: true });
  });
}

test("未保存の変更があると試せず、保存すると変更を反映した下書きで試せる", async ({ page, mockApi }) => {
  seedAgent(mockApi);
  await page.goto(`/agents?id=${AGENT_ID}`);
  await page.getByLabel("指示").fill("v2 の指示");
  await page.getByRole("tab", { name: "試す" }).click();

  const blocked = page.getByTestId("agent-try-blocked");
  await expect(blocked).toContainText("保存していない変更があります。");
  await page.getByRole("textbox", { name: "質問" }).fill("変更を確かめる");
  await expect(page.getByTestId("chat-send")).toBeDisabled();

  await blocked.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  await expect(blocked).toHaveCount(0);
  expect(mockApi.state.agents.find((agent) => agent.id === AGENT_ID)?.instructions).toBe("v2 の指示");

  await page.getByTestId("chat-send").click();
  await expect(page.getByText("「変更を確かめる」への回答です。")).toBeVisible();
});

test("無効の業務 Agent は試せないことを示し、送信しない", async ({ page, mockApi }) => {
  seedAgent(mockApi, { enabled: false });
  await openTryTab(page);

  await expect(page.getByTestId("agent-try-blocked")).toContainText("無効の業務 Agent は試せません。");
  await expect(page.getByTestId("agent-try-blocked").getByRole("button")).toHaveCount(0);
  await page.getByRole("textbox", { name: "質問" }).fill("質問");
  await expect(page.getByTestId("chat-send")).toBeDisabled();
});

test("下書きで試した会話は、チャットの会話の履歴に出ない", async ({ page, mockApi }) => {
  seedAgent(mockApi, { published_version: 1, unpublished_changes: false });
  await openTryTab(page);
  await page.getByRole("textbox", { name: "質問" }).fill("下書きの質問");
  await page.getByTestId("chat-send").click();
  await expect(page.getByText("「下書きの質問」への回答です。")).toBeVisible();

  await page.goto("/chat");
  await page.getByTestId("chat-history-toggle").click();
  await expect(page.getByText("下書きの質問")).toHaveCount(0);
  expect(mockApi.lastRequest("GET", "/api/threads")?.searchParams.get("draft")).toBeNull();
});

test("変更の権限が無い利用者には「試す」のタブを出さない", async ({ page, mockApi }) => {
  seedAgent(mockApi);
  mockApi.setCurrentUser(
    dbUser({ permissions: ["menu.agents", "menu.chat", "agent.runs.operate"], allowed_agent_ids: [AGENT_ID] })
  );
  await page.goto(`/agents?id=${AGENT_ID}`);

  await expect(page.getByRole("heading", { name: "Agent A", level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "試す" })).toHaveCount(0);
  await expect(page.getByLabel("指示")).toBeDisabled();
});
