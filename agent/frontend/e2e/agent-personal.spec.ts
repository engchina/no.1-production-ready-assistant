import type { Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #2523: 個人の業務 Agent。自分の業務 Agent の作成の権限だけを持つ利用者が作って使い、Agent 管理の承認で
// 組織の業務 Agent として共有する。

const BUILDER = dbUser({
  user_uuid: "22222222-2222-2222-2222-222222222222",
  login_user_id: "builder01",
  display_name: "担当者 A",
  permissions: ["agent.agents.build"],
  allowed_agent_ids: ["default"],
});

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function seedPersonalAgent(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.agents.push({
    id: "personal-2523",
    name: "Agent P",
    description: "資料を調べて答える",
    instructions: "v1 の指示",
    skill_ids: [],
    model_id: "",
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [{ version: 1, name: "Agent P", description: "", instructions: "v1 の指示", skill_ids: [], model_id: "", data_scopes: {}, note: "", published_at: MOCK_NOW, published_by: "builder01" }],
    published_version: 1,
    unpublished_changes: false,
    owner_user_uuid: BUILDER.user_uuid,
    share_request: null,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  });
  mockApi.state.creatorDisplayNames[BUILDER.user_uuid] = BUILDER.display_name;
}

async function openObjectAction(page: Page, name: string) {
  const actions = page.getByTestId("agent-object-actions");
  const button = actions.getByRole("button", { name });
  if (await button.count()) {
    await button.click();
  } else {
    await page.getByTestId("agent-object-actions-more").click();
    await page.getByRole("menuitem", { name }).click();
  }
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`作成の権限だけの利用者が自分の業務 Agent を作り、公開して共有を申請できる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    mockApi.setCurrentUser(BUILDER);
    await page.goto("/agents");
    await page.getByRole("button", { name: "業務 Agent を作成" }).first().click();

    await page.getByRole("textbox", { name: "名前", exact: true }).fill("Agent Q");
    await page.getByRole("button", { name: "作成", exact: true }).click();
    await expect(page.getByText("Agent を作成しました")).toBeVisible();
    const created = mockApi.state.agents.find((agent) => agent.name === "Agent Q");
    expect(created?.owner_user_uuid).toBe(BUILDER.user_uuid);

    // 持ち主は編集・試す・公開ができる。共有は公開してから申請する。
    await expect(page.getByText("個人", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("tab", { name: "試す" })).toBeVisible();
    const share = page.getByTestId("agent-share");
    await expect(share).toContainText("公開してから申請できます。");
    await expect(share.getByRole("button", { name: "共有を申請" })).toBeDisabled();

    await openObjectAction(page, "公開");
    const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
    await dialog.getByRole("button", { name: "公開", exact: true }).click();
    await expect(page.getByText("v1 として公開しました")).toBeVisible();

    await share.getByLabel("Agent 管理への一言").fill("部署で使いたい");
    await share.getByRole("button", { name: "共有を申請" }).click();
    await expect(page.getByText("共有を申請しました")).toBeVisible();
    await expect(share).toContainText("共有を申請しています");
    expect((created?.share_request as { note: string } | null)?.note).toBe("部署で使いたい");
    await expect(page.getByText("共有を申請中").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-personal-${viewport.name}.png`), fullPage: true });
  });
}

test("作成の権限だけの利用者は、組織の業務 Agent を変更できない", async ({ page, mockApi }) => {
  mockApi.setCurrentUser(BUILDER);
  await page.goto("/agents?id=default");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "試す" })).toHaveCount(0);
  await expect(page.getByLabel("指示")).toBeDisabled();
  await expect(page.getByTestId("agent-share")).toHaveCount(0);
});

test("Agent 管理は共有の申請を承認し、組織の業務 Agent にする", async ({ page, mockApi }) => {
  seedPersonalAgent(mockApi, {
    share_request: { status: "requested", note: "部署で使いたい", requested_at: MOCK_NOW, reason: "", decided_at: null },
  });
  await page.goto("/agents");
  const row = page.getByRole("row", { name: /Agent P/ });
  await expect(row).toContainText("個人");
  await expect(row).toContainText("共有を申請中");

  await page.goto("/agents?id=personal-2523");
  const review = page.getByTestId("agent-share-review");
  await expect(review).toContainText("担当者 A さんが共有を申請しています");
  await expect(review).toContainText("部署で使いたい");
  await review.getByRole("button", { name: "承認" }).click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog).toContainText("ロールに割り当てるまで、申請した本人も使えなくなります");
  await dialog.getByRole("button", { name: "承認", exact: true }).click();
  await expect(page.getByText("共有を承認しました。権限管理でロールに割り当ててください", { exact: true })).toBeVisible();

  const agent = mockApi.state.agents.find((item) => item.id === "personal-2523");
  expect(agent?.owner_user_uuid).toBeNull();
  await expect(page.getByTestId("agent-share-review")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "権限管理でロールに割り当てる" })).toBeVisible();
  await expect(page.getByText("個人", { exact: true })).toHaveCount(0);
});

test("Agent 管理は理由を添えて却下し、持ち主に理由が見える", async ({ page, mockApi }) => {
  seedPersonalAgent(mockApi, {
    share_request: { status: "requested", note: "", requested_at: MOCK_NOW, reason: "", decided_at: null },
  });
  await page.goto("/agents?id=personal-2523");
  const review = page.getByTestId("agent-share-review");
  await review.getByRole("button", { name: "却下" }).click();
  await expect(review.getByText("却下の理由を入力してください。")).toBeVisible();
  await review.getByLabel("却下の理由").fill("評価の問いを足してください");
  await review.getByRole("button", { name: "却下" }).click();
  await expect(page.getByText("共有の申請を却下しました")).toBeVisible();

  mockApi.setCurrentUser(BUILDER);
  await page.goto("/agents?id=personal-2523");
  await expect(page.getByTestId("agent-share-rejected")).toContainText("評価の問いを足してください");
  await expect(page.getByTestId("agent-share").getByRole("button", { name: "共有を申請" })).toBeEnabled();
});


test("共有の承認後は申請した本人に割り当て待ちを案内する", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  seedPersonalAgent(mockApi, { owner_user_uuid: null, share_request: {
    status: "approved", requested_by_user_uuid: BUILDER.user_uuid, approved_name: "Agent P",
    note: "", reason: "", requested_at: MOCK_NOW, decided_at: MOCK_NOW,
  } });
  mockApi.setCurrentUser(BUILDER);
  await page.goto("/agents");
  await expect(page.getByTestId("agent-share-approved-notice")).toContainText("使うには管理者によるロールへの割り当てが必要です");
  await page.goto("/agents?id=personal-2523");
  await expect(page.getByTestId("agent-share-approved-notice")).toContainText("Agent P");
  await expect(page.getByText("削除されたか、存在しません", { exact: false })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("share-approved-notice.png") });
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByRole("heading", { name: "業務 Agent", exact: true }).first()).toBeVisible();
  mockApi.setCurrentUser({ ...BUILDER, allowed_agent_ids: ["personal-2523"] });
  await page.reload();
  await expect(page.getByTestId("agent-share-approved-notice")).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Agent P/ })).toBeVisible();
});
