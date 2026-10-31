import type { Page } from "@playwright/test";

import { LOCAL_CURRENT_USER, dbUser } from "./fixtures/auth";
import { MOCK_NOW, RUNTIME_STORAGE_MEMORY, expect, test, type MockApi } from "./fixtures/mock-api";
import { chooseSelectFieldOption } from "./fixtures/select-field";

// #784: 業務 Agent の自動実行（スケジュール。トリガーはスケジュールだけ。#1799）。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function seedAutomation(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.automations.push({
    id: "auto-seeded",
    agent_id: "default",
    name: "毎朝の売上の要約",
    goal: "昨日の売上を要約してください。",
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
    ...overrides,
  });
}

for (const viewport of VIEWPORTS) {
  test(`スケジュールの自動実行を作り、一覧に次回とスケジュールを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // 既定の時間帯はブラウザではなくシステムの設定の時間帯（/api/auth/me の timezone。#3265）。
    mockApi.setCurrentUser({ ...LOCAL_CURRENT_USER, timezone: "UTC" });
    await page.goto("/automations");
    await expect(page.getByRole("heading", { name: "自動実行", level: 1 })).toBeVisible();
    await expect(page.getByText("自動実行はまだありません")).toBeVisible();

    await page.getByRole("button", { name: "自動実行を作成" }).first().click();
    await expect(page).toHaveURL(/\/automations\?id=new$/);
    // トリガーはスケジュールだけなので、種類の選択は出さず、スケジュールの設定だけを出す（#1799）。
    await expect(page.getByRole("heading", { name: "スケジュール", level: 2 })).toBeVisible();
    await expect(page.getByRole("radio")).toHaveCount(0);
    // 必須の入力が無ければ保存しない。
    await page.getByRole("button", { name: "作成", exact: true }).first().click();
    await expect(page.getByText("名前を入力してください。")).toBeVisible();
    await expect(page.getByText("指示を入力してください。")).toBeVisible();

    await page.getByLabel("名前").fill("週次の品質の点検");
    await page.locator("#automation-goal").fill("先週の不良率を確認し、基準を超えたラインを報告してください。");
    await chooseSelectFieldOption(page.locator("#automation-frequency"), "weekly");
    await page.getByRole("checkbox", { name: "水" }).check();
    await page.getByLabel("時刻").fill("08:30");
    await page.screenshot({ path: testInfo.outputPath(`automation-editor-${viewport.name}.png`), fullPage: true });
    await page.getByRole("button", { name: "作成", exact: true }).first().click();

    await expect(page.getByText("自動実行を作成しました")).toBeVisible();
    await expect(page).toHaveURL(/\/automations\?id=auto-1$/);
    const body = mockApi.lastRequest("POST", "/api/automations")?.body as Record<string, unknown>;
    expect(body).toMatchObject({
      agent_id: "default",
      name: "週次の品質の点検",
      enabled: true,
      schedule: { frequency: "weekly", time: "08:30", weekdays: [0, 2], minute: 0, timezone: "UTC" },
    });
    expect(body).not.toHaveProperty("trigger");
    await expect(page.getByTestId("automation-next")).toContainText("次回の実行");

    await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
    const row = page.getByRole("table", { name: "自動実行の一覧" }).getByRole("row", { name: /週次の品質の点検/ });
    await expect(row).toContainText("毎週 月・水 08:30");
    await expect(row).toContainText("有効");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`automations-${viewport.name}.png`), fullPage: true });
  });
}

test("保存したスケジュールを編集し、次回と一覧のスケジュールを更新する", async ({ page, mockApi }) => {
  seedAutomation(mockApi);
  await page.goto("/automations?id=auto-seeded");
  await expect(page.getByRole("heading", { name: "毎朝の売上の要約", level: 1 })).toBeVisible();
  await expect(page.getByTestId("automation-schedule")).toBeVisible();
  await expect(page.getByTestId("automation-next")).toContainText("次回の実行");

  await chooseSelectFieldOption(page.locator("#automation-frequency"), "hourly");
  await page.getByLabel("分", { exact: true }).fill("15");
  // 未保存の変更があるあいだは、保存済みの次回を出さない。
  await expect(page.getByTestId("automation-next")).toHaveCount(0);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("自動実行を保存しました")).toBeVisible();
  const body = mockApi.lastRequest("PUT", "/api/automations/auto-seeded")?.body as Record<string, unknown>;
  expect(body).toMatchObject({ schedule: { frequency: "hourly", minute: 15 } });
  expect(body).not.toHaveProperty("trigger");
  await expect(page.getByTestId("automation-next")).toContainText("次回の実行");

  await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
  const row = page.getByRole("table", { name: "自動実行の一覧" }).getByRole("row", { name: /毎朝の売上の要約/ });
  await expect(row).toContainText("毎時 15 分");
  await expect(page.getByRole("columnheader", { name: "スケジュール" })).toBeVisible();
});

test("一覧から今すぐ実行し、実行履歴に出す。未保存の離脱を確かめ、削除できる", async ({ page, mockApi }) => {
  seedAutomation(mockApi);
  await page.goto("/automations");
  await page.getByTestId("automation-row-actions-auto-seeded").click();
  await page.getByRole("menuitem", { name: "今すぐ実行" }).click();
  await expect(page.getByText("実行を開始しました")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/automations/auto-seeded/run")).toBeDefined();

  await page.getByRole("table", { name: "自動実行の一覧" }).getByRole("link", { name: /毎朝の売上の要約/ }).click();
  // 前回は「実行を作った」ことではなく、前回の Run の今の状態（#1812）。
  await expect(page.getByTestId("automation-last")).toContainText("待機中");
  await expect(page.getByTestId("automation-last")).toContainText("前回の実行");
  await expect(page.getByTestId("automation-last")).not.toContainText("実行を作りました。");
  const history = page.getByRole("table", { name: "実行履歴" });
  await expect(history).toContainText("今すぐ実行");
  await expect(history).toContainText("run-auto-1");

  await page.getByLabel("名前").fill("毎朝の売上の要約（改）");
  await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog.getByText("変更を破棄しますか")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expect(page.getByLabel("名前")).toHaveValue("毎朝の売上の要約");

  const actions = page.getByTestId("automation-actions");
  await expect(actions).toBeVisible();
  const deleteButton = actions.getByRole("button", { name: "削除" });
  if (await deleteButton.count()) {
    await deleteButton.click();
  } else {
    await page.getByTestId("automation-actions-more").click();
    await page.getByRole("menuitem", { name: "削除" }).click();
  }
  await expect(dialog.getByText("「毎朝の売上の要約」を削除しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByText("自動実行を削除しました")).toBeVisible();
  await expect(page).toHaveURL(/\/automations$/);
});

test("Agent 管理の権限が無い利用者は閲覧だけ", async ({ page, mockApi }) => {
  seedAutomation(mockApi, { last_result: "skipped", last_run_at: MOCK_NOW, last_message: "前回の実行が終わっていないため、この回は実行しませんでした。" });
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.automations"] }));
  // 自動実行の一覧は利用者の対象範囲の業務 Agent のものだけ（backend の list_automations。#1113）。
  mockApi.state.auth.currentUser!.allowed_agent_ids = ["default"];
  await page.goto("/automations");
  await expect(page.getByRole("button", { name: "自動実行を作成" })).toHaveCount(0);
  const row = page.getByRole("table", { name: "自動実行の一覧" }).getByRole("row", { name: /毎朝の売上の要約/ });
  await expect(row).toContainText("飛ばした");
  await expect(page.getByTestId("automation-row-actions-auto-seeded")).toHaveCount(0);

  await row.getByRole("link", { name: /毎朝の売上の要約/ }).click();
  await expect(page.getByLabel("名前")).toBeDisabled();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toHaveCount(0);
});

test("保存先が無いときは知らせ、読み込み中は経過時間を出す", async ({ page, mockApi }) => {
  mockApi.state.automationsPersistent = false;
  mockApi.state.runtimeStorage = { ...RUNTIME_STORAGE_MEMORY };
  await page.route("**/api/automations", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await page.goto("/automations");
  await expect(page.getByTestId("automations-loading")).toContainText("自動実行を読み込んでいます");
  await expect(page.getByTestId("storage-not-persistent-notice")).toContainText(
    "作成・変更した内容は、バックエンドの再起動で消えます"
  );
});

// #1812: 前回は Run を作ったことではなく、前回の Run の今の状態（実行履歴と同じ語）を出し、Run の詳細へ移れる。
const LAST_RESULTS = [
  { id: "auto-completed", name: "完了した自動実行", fields: { last_result: "created", last_run_id: "run-1812-completed", last_run_status: "completed" }, label: "完了" },
  { id: "auto-failed", name: "失敗した自動実行", fields: { last_result: "created", last_run_id: "run-1812-failed", last_run_status: "failed" }, label: "失敗" },
  { id: "auto-waiting", name: "承認待ちの自動実行", fields: { last_result: "created", last_run_id: "run-1812-waiting", last_run_status: "waiting_approval" }, label: "承認待ち" },
  { id: "auto-not-started", name: "開始できなかった自動実行", fields: { last_result: "failed_to_start", last_message: "実行する利用者に実行の権限がありません。" }, label: "開始できず" },
  { id: "auto-gone", name: "記録の無い自動実行", fields: { last_result: "created", last_run_id: "run-1812-gone", last_run_status: null }, label: "記録なし" },
];

for (const viewport of VIEWPORTS) {
  test(`前回に Run の最終の状態を出し、前回の実行を開ける (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const item of LAST_RESULTS) {
      seedAutomation(mockApi, {
        id: item.id,
        name: item.name,
        last_run_at: MOCK_NOW,
        last_trigger: "schedule",
        last_message: "実行を作りました。",
        ...item.fields,
      });
    }
    await page.goto("/automations");
    await expect(page.getByRole("table", { name: "自動実行の一覧" })).toBeVisible();
    for (const item of LAST_RESULTS) {
      // 状態は色だけでなく語で出す（実行履歴と同じ語）。
      await expect(page.getByTestId(`automation-last-${item.id}`)).toContainText(item.label);
    }
    await expectNoHorizontalOverflow(page);

    await page.goto("/automations?id=auto-failed");
    const last = page.getByTestId("automation-last");
    await expect(last).toContainText("失敗");
    await expect(last).toContainText("前回の実行");
    await expect(last).not.toContainText("実行を作りました。");
    const link = page.getByTestId("automation-last-run-link");
    await expect(link).toHaveAttribute("href", /\/runs\?id=run-1812-failed$/);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`automation-last-${viewport.name}.png`), fullPage: true });
    await link.click();
    await expect(page).toHaveURL(/\/runs\?id=run-1812-failed$/);

    // Run を作れなかった回は理由を出し、開く実行は無い。
    await page.goto("/automations?id=auto-not-started");
    await expect(page.getByTestId("automation-last")).toContainText("開始できず");
    await expect(page.getByTestId("automation-last")).toContainText("実行する利用者に実行の権限がありません。");
    await expect(page.getByTestId("automation-last-run-link")).toHaveCount(0);

    // Run の記録が無い（保存期間を過ぎた）回は「記録なし」で、開く実行は無い。
    await page.goto("/automations?id=auto-gone");
    await expect(page.getByTestId("automation-last")).toContainText("記録なし");
    await expect(page.getByTestId("automation-last-run-link")).toHaveCount(0);
  });
}

// #1813: 自動実行は無人なので承認が要るツールを呼ばない。編集画面で使えないツールを警告し、ツール権限へ案内する。
for (const viewport of VIEWPORTS) {
  test(`承認が要るツールを自動実行で使えないと警告し、ツール権限へ案内する (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    mockApi.state.automationApprovalTools.default = ["crm__update_order"];
    await page.goto("/automations?id=new");
    const notice = page.getByTestId("automation-approval-tools");
    await expect(notice).toContainText("自動実行ではこのツールは使えません");
    await expect(notice.getByRole("list", { name: "承認が要るツール" })).toContainText("crm__update_order");
    await expect(notice).toContainText("確かめられなかった点として回答します");
    const open = page.getByTestId("automation-open-tool-policy");
    await expect(open).toHaveAttribute("href", /\/settings\/tool-policy$/);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`automation-approval-tools-${viewport.name}.png`), fullPage: true });
    await open.click();
    await expect(page).toHaveURL(/\/settings\/tool-policy$/);
  });
}

test("承認が要るツールが無い業務 Agent では警告を出さず、確かめられない接続は理由を出す", async ({ page, mockApi }) => {
  seedAutomation(mockApi);
  await page.goto("/automations?id=auto-seeded");
  await expect(page.getByLabel("名前")).toHaveValue("毎朝の売上の要約");
  expect(mockApi.lastRequest("GET", "/api/automations/agents/default/approval-tools")).toBeDefined();
  await expect(page.getByTestId("automation-approval-tools")).toHaveCount(0);

  mockApi.state.automationApprovalWarnings.default = ["MCP 接続「データ問い合わせ（NL2SQL）」のツールを取得できません: 接続できません"];
  await page.reload();
  const notice = page.getByTestId("automation-approval-tools");
  await expect(notice).toContainText("ツールを確かめられませんでした");
  await expect(notice).toContainText("MCP 接続「データ問い合わせ（NL2SQL）」のツールを取得できません");
  await expect(page.getByTestId("automation-open-tool-policy")).toHaveCount(0);
});

test("Agent 管理の権限が無い利用者には、ツール権限の変更を管理者に頼むよう案内する", async ({ page, mockApi }) => {
  seedAutomation(mockApi);
  mockApi.state.automationApprovalTools.default = ["crm__update_order"];
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.automations"] }));
  mockApi.state.auth.currentUser!.allowed_agent_ids = ["default"];
  await page.goto("/automations?id=auto-seeded");
  const notice = page.getByTestId("automation-approval-tools");
  await expect(notice).toContainText("crm__update_order");
  await expect(notice).toContainText("管理者が変更できます");
  await expect(page.getByTestId("automation-open-tool-policy")).toHaveCount(0);
});
