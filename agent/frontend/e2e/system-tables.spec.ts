import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";
import { openSidebarNav } from "./fixtures/nav";

// 運用設定 > システムテーブル（#751。RAG / NL2SQL と同じ共通のカード）。
// 旧版の DB（検索・回答プロファイルの割り当ての表が残る）は、削除される内容を確認ダイアログで承認してから更新する。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

test(
  "全再作成の削除範囲と保持範囲を操作前と最終確認で示し、キャンセルでは削除しない",
  { tag: "@responsive" },
  async ({ page, mockApi }, testInfo) => {
    await page.goto("/settings/system-tables");
    const recreateSection = page.locator('section[aria-labelledby="recreate-system-tables-title"]');
    const deletedData = [
      "実行履歴",
      "保存済みの定義（業務 Agent・スキル・MCP 接続など）",
      "利用状況の集計データ",
      "ロールの権限と利用できる業務 Agent の割り当て",
    ];
    const preservedData = "3 製品共通のユーザー・ロール（PLATFORM_*）と、RAG / NL2SQL のテーブルは削除しません。";
    for (const data of deletedData) await expect(recreateSection).toContainText(data);
    await expect(recreateSection).toContainText(preservedData);

    const confirmation = recreateSection.getByRole("textbox");
    const recreate = recreateSection.getByRole("button", { name: "すべて再作成", exact: true });
    await confirmation.fill("架空検証用・再作成しない");
    await expect(recreateSection.getByText("不一致", { exact: true })).toBeVisible();
    await expect(recreate).toBeDisabled();
    expect(mockApi.lastRequest("POST", "/api/settings/database/system-tables/initialize")).toBeUndefined();

    await confirmation.fill("RECREATE_AGENT_SYSTEM_TABLES");
    await expect(recreateSection.getByText("確認済み", { exact: true })).toBeVisible();
    await expect(recreate).toBeEnabled();
    await expectNoPageOverflow(page);
    await recreateSection.screenshot({ path: testInfo.outputPath("recreate-warning.png") });
    await recreate.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByRole("heading")).toHaveText("Agent の実行履歴・保存済みの定義・権限の割り当てを削除しますか？");
    for (const data of deletedData) await expect(dialog).toContainText(data);
    await expect(dialog).toContainText(preservedData);
    await expect(dialog).toContainText("復元できません");
    await expect(dialog.getByRole("button", { name: "キャンセル", exact: true })).toBeInViewport();
    await expect(dialog.getByRole("button", { name: "すべて再作成", exact: true })).toBeInViewport();
    await expectNoPageOverflow(page);
    await dialog.screenshot({ path: testInfo.outputPath("recreate-confirmation.png") });
    expect(mockApi.lastRequest("POST", "/api/settings/database/system-tables/initialize")).toBeUndefined();
    await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(mockApi.lastRequest("POST", "/api/settings/database/system-tables/initialize")).toBeUndefined();
  },
);

for (const viewport of VIEWPORTS) {
  test.describe(`システムテーブル (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("運用設定の先頭にあり、データを消す更新は確認ダイアログで承認してから実行する", async ({ page, mockApi }) => {
      await page.goto("/settings/system-tables");
      await expect(page.getByRole("heading", { name: "システムテーブル管理", level: 1 })).toBeVisible();
      await expect(page.getByText("一部不足").first()).toBeVisible();
      await expect(page.getByText("データを削除する更新があります")).toBeVisible();
      await expect(page.getByText("AGENT_ROLE_BUSINESS_VIEWS").first()).toBeVisible();
      await expectNoPageOverflow(page);

      await page.getByRole("button", { name: "作成・更新" }).click();
      const dialog = page.getByRole("alertdialog");
      await expect(dialog.getByText("データを削除する更新を実行しますか？")).toBeVisible();
      await dialog.getByRole("button", { name: "削除して更新" }).click();

      await expect(page.getByText("初期化済み").first()).toBeVisible();
      const request = mockApi.lastRequest("POST", "/api/settings/database/system-tables/initialize");
      expect(request?.body).toMatchObject({ recreate: false, allow_destructive: true });
      await expectNoPageOverflow(page);
    });
  });
}

test(
  "保存済みのデータを書き換えた後に再起動が要るプロセスは、作成・更新の直下に残す（#1844）",
  { tag: "@responsive" },
  async ({ page, mockApi }) => {
    const dispatcher = "実行の dispatcher（python -m app.features.agent.runtime_dispatcher）";
    mockApi.state.systemTablesRestartRequired = {
      message: "保存済みのデータを書き換える migration を適用しました。次のプロセスは再起動してください。",
      processes: [dispatcher],
    };
    await page.goto("/settings/system-tables");
    const initialize = page.getByRole("button", { name: "作成・更新" });
    await initialize.click();
    await page.getByRole("alertdialog").getByRole("button", { name: "削除して更新" }).click();

    const notice = page.getByRole("status").filter({ has: page.getByTestId("system-tables-restart-required") });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("Agent のシステムテーブルを更新しました。再起動が必要なプロセスがあります");
    await expect(notice.getByRole("listitem")).toHaveText([dispatcher]);
    // 結果は操作の直下の Banner 1 か所だけ（Toast と重ねない。messaging.md §10.1）。
    await expect(page.getByRole("region", { name: "通知" }).getByRole("status")).toHaveCount(0);
    const actionBox = await initialize.boundingBox();
    const noticeBox = await notice.boundingBox();
    expect(actionBox && noticeBox && noticeBox.y >= actionBox.y + actionBox.height).toBeTruthy();
    await expectNoPageOverflow(page);

    // 状態を取り直したら消す（次の操作まで残す。messaging.md §10.4）。
    await page.getByRole("button", { name: "最新情報を取得" }).click();
    await expect(notice).toHaveCount(0);
  },
);

test("サイドナビの運用設定は、システムテーブルから始まる（RAG / NL2SQL と同じ。#658）", async ({ page }) => {
  await page.goto("/settings/system-tables");
  // 375px（mobile-375 の project）ではナビはドロワーなので、開いてから見る（#823）。
  const nav = await openSidebarNav(page);
  const operations = nav.getByRole("link", { name: "システムテーブル" });
  await expect(operations).toBeVisible();
  await expect(operations).toHaveAttribute("aria-current", "page");
});

test(
  "台帳が最新でも既存の表に無い列を更新必要として並べ、更新で直らなければ列の名前で止める（#2458）",
  { tag: "@responsive" },
  async ({ page, mockApi }, testInfo) => {
    const missingColumns = [
      { table_name: "AGENT_RUN_FACTS", column_name: "DATA_TOOL_CALLS" },
      { table_name: "AGENT_RUNTIME_CHECKPOINTS", column_name: "WRITER_ID" },
    ];
    // 既定の旧版の DB の応答から、object がそろい台帳も最新の状態を作る。
    const legacy = mockApi.state.systemTables as Record<string, unknown>;
    const present = (rows: unknown) =>
      (rows as Array<Record<string, unknown>>).map((row) => ({ ...row, exists: true }));
    mockApi.state.systemTables = {
      ...legacy,
      status: "outdated",
      applied_versions: legacy.pending_versions,
      pending_versions: [],
      existing_object_count: legacy.expected_object_count,
      existing_table_count: legacy.expected_table_count,
      tables: present(legacy.tables),
      objects: present(legacy.objects),
      pending_destructive_migrations: [],
      missing_objects: [],
      retired_objects: [],
      missing_columns: missingColumns,
    };
    mockApi.state.systemTablesInitializeError = {
      status: 500,
      message:
        "システムテーブル操作後も必須の列が不足しています（AGENT_RUN_FACTS.DATA_TOOL_CALLS, " +
        "AGENT_RUNTIME_CHECKPOINTS.WRITER_ID）。更新を再試行しても直りません。" +
        "配備の手順の既存環境の更新手順を確認してください。",
      errorCode: "SCHEMA_POSTCONDITION_FAILED",
    };

    await page.goto("/settings/system-tables");
    const card = page.locator("#system-tables");
    await expect(card.getByText("更新必要", { exact: true }).first()).toBeVisible();
    const missing = card.getByTestId("system-tables-missing-columns");
    await expect(missing).toContainText("既存のテーブルに必須の列が 2 件ありません");
    await expect(missing.getByRole("listitem")).toHaveText([
      "AGENT_RUN_FACTS.DATA_TOOL_CALLS",
      "AGENT_RUNTIME_CHECKPOINTS.WRITER_ID",
    ]);
    await expectNoPageOverflow(page);
    await missing.screenshot({
      path: testInfo.outputPath(`system-tables-missing-columns-${testInfo.project.name}.png`),
    });

    await card.getByRole("button", { name: "作成・更新" }).click();
    const operationAlert = card.getByRole("alert");
    await expect(operationAlert).toHaveCount(1);
    await expect(operationAlert).toContainText("AGENT_RUN_FACTS.DATA_TOOL_CALLS");
    await expect(operationAlert).toContainText("更新を再試行しても直りません");
    // 列は戻っていないので、一覧は残す。
    await expect(missing).toBeVisible();
    await expectNoPageOverflow(page);
  },
);
