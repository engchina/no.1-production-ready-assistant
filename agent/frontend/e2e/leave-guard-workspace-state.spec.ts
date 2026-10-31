import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";
import { closeSidebarNav, expandSidebarSections, openSidebarNav } from "./fixtures/nav";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./fixtures/select-field";

// 未保存変更の離脱ガードと、一覧の作業状態の保持（platform UX 契約 workspace-state.md。#87）。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
];

/** サイドナビのリンク。375px ではナビがドロワー（#367）のため、先に「メニュー」で開く。 */
async function sidebarLink(page: Page, href: string): Promise<Locator> {
  const sidebar = await openSidebarNav(page);
  // 既定では「AI 活用」と今のページのセクションだけが開いているため、閉じたセクションを開いてから選ぶ（#1563）。
  await expandSidebarSections(sidebar);
  return sidebar.locator(`a[href="${href}"]`);
}

/** beforeunload の handler が登録されていれば、cancelable な event が preventDefault される。 */
async function beforeUnloadBlocks(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const viewport of VIEWPORTS) {
  test.describe(`離脱ガード (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("未保存の Agent 作成フォームはブラウザの戻るでも確認する（#138）", async ({ page }) => {
      await page.goto("/agents");
      // 一覧 → 作成エディタ（?id=new）は SPA 内で履歴を 1 つ積む。
      await page.getByRole("button", { name: "業務 Agent を作成" }).click();
      await expect(page).toHaveURL(/\/agents\?id=new$/);
      await page.locator("#new-agent-name").fill("下書きの Agent");
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(true);

      // 戻る（?id=new → 一覧）→ 確認。キャンセルすると URL と入力が残る。
      await page.goBack();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("変更を破棄しますか")).toBeVisible();
      await dialog.getByRole("button", { name: "キャンセル" }).click();
      await expect(page).toHaveURL(/\/agents\?id=new$/);
      await expect(page.locator("#new-agent-name")).toHaveValue("下書きの Agent");

      // もう一度戻る → 破棄して移動。
      await page.goBack();
      await dialog.getByRole("button", { name: "破棄して移動" }).click();
      await expect(page).toHaveURL(/\/agents$/);
      await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();
    });

    test("未保存の Agent 作成フォームはサイドナビの移動を確認し、破棄を選ぶと移動する", async ({ page }) => {
      await page.goto("/agents?id=new");
      await expect(page.getByRole("heading", { name: "業務 Agent を作成", level: 1 })).toBeVisible();
      expect(await beforeUnloadBlocks(page)).toBe(false);

      await page.locator("#new-agent-name").fill("下書きの Agent");
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(true);

      await (await sidebarLink(page, "/runs")).click();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("変更を破棄しますか")).toBeVisible();
      await expectNoHorizontalOverflow(page);

      // キャンセルすると移動せず、入力も残る。
      await dialog.getByRole("button", { name: "キャンセル" }).click();
      await expect(page).toHaveURL(/\/agents\?id=new$/);
      await expect(page.locator("#new-agent-name")).toHaveValue("下書きの Agent");
      // 375px ではサイドナビのドロワーが開いたまま（確認をキャンセルしたため）。閉じてから本文を操作する。
      await closeSidebarNav(page);

      // 左上の「一覧へ戻る」も破棄を確認する（#618）。
      await page.getByTestId("editor-back").click();
      const leaveDialog = page.getByRole("alertdialog");
      await expect(leaveDialog).toBeVisible();
      await leaveDialog.getByRole("button", { name: "キャンセル" }).click();
      await expect(page).toHaveURL(/\/agents\?id=new$/);

      await (await sidebarLink(page, "/runs")).click();
      await dialog.getByRole("button", { name: "破棄して移動" }).click();
      await expect(page).toHaveURL(/\/runs$/);
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
      expect(await beforeUnloadBlocks(page)).toBe(false);
    });

    test("Agent を作成すると作成した Agent のエディタへ移り、確認なしで移動できる", async ({ page }) => {
      await page.goto("/agents");
      await page.getByRole("button", { name: "業務 Agent を作成" }).click();
      await page.locator("#new-agent-name").fill("作成する Agent");
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(true);
      await page.getByRole("button", { name: "作成", exact: true }).first().click();
      await expect(page.getByText("Agent を作成しました")).toBeVisible();
      await expect(page).toHaveURL(/\/agents\?id=agent-4$/);
      await expect(page.getByRole("heading", { name: "作成する Agent", level: 1 })).toBeVisible();
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(false);
      // 新規フォームは履歴に残さない（戻るで空の作成フォームへ戻らない）。
      await page.goBack();
      await expect(page).toHaveURL(/\/agents$/);
      await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();

      await (await sidebarLink(page, "/runs")).click();
      await expect(page).toHaveURL(/\/runs$/);
    });

    test("設定を保存すると基準が更新され、確認なしで移動できる", async ({ page }) => {
      await page.goto("/settings/tool-policy");
      await expect(page.getByRole("heading", { name: "ツール権限", level: 1 })).toBeVisible();
      await chooseSelectFieldOption(page.locator("#tool-policy-default-mode"), "deny");
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(true);

      await page.getByRole("button", { name: "保存", exact: true }).click();
      await expect(page.getByText("設定を保存しました")).toBeVisible();
      await expect.poll(() => beforeUnloadBlocks(page)).toBe(false);

      await (await sidebarLink(page, "/runs")).click();
      await expect(page).toHaveURL(/\/runs$/);
      await expect(page.getByText("変更を破棄しますか")).toHaveCount(0);
    });

    test("変更していなければ確認なしで移動できる", async ({ page }) => {
      await page.goto("/settings/mcp-connections");
      await expect(page.getByRole("heading", { name: "MCP 接続", level: 1 })).toBeVisible();
      expect(await beforeUnloadBlocks(page)).toBe(false);

      await (await sidebarLink(page, "/agents")).click();
      await expect(page).toHaveURL(/\/agents$/);
      await expect(page.getByText("変更を破棄しますか")).toHaveCount(0);
    });

    test("Skill のエディタは左上の「一覧へ戻る」でも破棄を確認する", async ({ page }) => {
      await page.goto("/skills");
      await page.getByRole("button", { name: "スキルを追加" }).click();
      await page.locator("#skill-id").fill("draft_skill");

      // 「一覧へ戻る」はタイトルの上の左端（375px でもメニューに畳まない。#618）。
      await page.getByTestId("editor-back").click();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("閉じると編集内容は破棄されます")).toBeVisible();
      await dialog.getByRole("button", { name: "破棄して閉じる" }).click();
      await expect(page).toHaveURL(/\/skills$/);
      await expect(page.locator("#skill-id")).toHaveCount(0);
      expect(await beforeUnloadBlocks(page)).toBe(false);
    });
  });

  test.describe(`作業状態の保持 (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("監査の絞り込み条件は移動して戻っても、再読込しても残る", async ({ page, mockApi }) => {
      await page.goto("/audit");
      await expect(page.getByRole("heading", { name: "監査ログ", level: 1 })).toBeVisible();
      await page.locator("#audit-run-id").fill("run-e2e-1");
      await chooseSelectFieldOption(page.locator("#audit-tool-name"), "echo");
      await chooseSelectFieldOption(page.locator("#audit-warning-filter"), "true");
      await page.getByRole("button", { name: "フィルター適用" }).click();
      await expect
        .poll(() => mockApi.lastRequest("GET", "/api/audit/tool-calls")?.searchParams.get("run_id"))
        .toBe("run-e2e-1");
      // 適用していない入力も下書きとして残す。
      await page.locator("#audit-error-code").fill("E_DRAFT");

      await (await sidebarLink(page, "/runs")).click();
      await expect(page).toHaveURL(/\/runs$/);
      await (await sidebarLink(page, "/audit")).click();
      await expect(page.locator("#audit-run-id")).toHaveValue("run-e2e-1");
      await expectSelectFieldValue(page.locator("#audit-tool-name"), "echo");
      await expectSelectFieldValue(page.locator("#audit-warning-filter"), "true");
      await expect(page.locator("#audit-error-code")).toHaveValue("E_DRAFT");

      await page.reload();
      await expect(page.locator("#audit-run-id")).toHaveValue("run-e2e-1");
      await expect(page.locator("#audit-error-code")).toHaveValue("E_DRAFT");
      // 再読込後の一覧は、適用済みの条件（run_id / tool_name / 警告あり）で取り直す。
      await expect
        .poll(() => {
          const params = mockApi.lastRequest("GET", "/api/audit/tool-calls")?.searchParams;
          return params ? `${params.get("run_id")}|${params.get("tool_name")}|${params.get("has_guardrail_warnings")}|${params.get("error_code")}` : null;
        })
        .toBe("run-e2e-1|echo|true|null");
      await expectNoHorizontalOverflow(page);
    });

    test("Skill の編集対象は URL が唯一の情報源で、消えた対象は説明して一覧へ戻す", async ({ page }) => {
      await page.goto("/skills");
      await page.getByRole("link", { name: /業務 RAG 調査/ }).click();
      await expect(page).toHaveURL(/\/skills\?id=business-rag-research$/);
      // 詳細だけが出す読み取り専用の案内で、対象が開いていることを確かめる。
      const detail = page.getByText("このスキルは読み取り専用です", { exact: false });
      await expect(detail).toBeVisible();

      await page.reload();
      await expect(detail).toBeVisible();

      // 編集対象は sessionStorage ではなく URL に持つ（#137）。
      const stored = await page.evaluate(() =>
        Object.keys(window.sessionStorage).filter((key) => key.startsWith("production-ready-agent.workspace.v1:skills"))
      );
      expect(stored).toEqual([]);

      // URL の対象が一覧にない場合は、黙って別の対象に置き換えず説明する。
      await page.goto("/skills?id=deleted_skill");
      await expect(page.getByText("対象が見つかりません")).toBeVisible();
      await expect(page.getByText("「deleted_skill」は削除されたか、存在しません", { exact: false })).toBeVisible();
      await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
      await expect(page).toHaveURL(/\/skills$/);
      await expectNoHorizontalOverflow(page);
    });

    test("Run の目標は残る", async ({ page }) => {
      await page.goto("/runs?id=new");
      await page.locator("#run-goal").fill("下書きの目標");
      await page.goto("/skills");
      await (await sidebarLink(page, "/runs")).click();
      await page.getByRole("button", { name: "実行を作成", exact: true }).click();
      await expect(page.locator("#run-goal")).toHaveValue("下書きの目標");
      await page.reload();
      await expect(page.locator("#run-goal")).toHaveValue("下書きの目標");

      const stored = await page.evaluate(() =>
        Object.keys(window.sessionStorage).filter((key) => key.startsWith("production-ready-agent.workspace.v1:"))
      );
      // owner は作業状態の持ち主（ログイン中の利用者）。利用者が変わったら作業状態を消すために使う（#215）。
      expect(stored.sort()).toEqual([
        "production-ready-agent.workspace.v1:owner",
        "production-ready-agent.workspace.v1:runs.goal",
      ]);
    });
  });
}
