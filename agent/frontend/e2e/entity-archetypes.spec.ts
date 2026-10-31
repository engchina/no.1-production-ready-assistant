import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";
import { findFloatingFormActionBars } from "../../../platform/packages/ui/e2e/layout-checks";

// ページの型（platform UX 契約 page-archetypes.md）の Agent への適用（#137）。
// A 型 = 一覧 → 全画面エディタ（?id= が唯一の情報源、パンくず、行メニュー / ObjectActionBar）、
// B 型 = 一覧 + 詳細を FixedSplitPane で並べる。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800, layout: "split" },
  { name: "mobile-375", width: 375, height: 812, layout: "stacked" },
] as const;

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function breadcrumbs(page: Page) {
  return page.getByRole("navigation", { name: "パンくず" });
}

function seedRun(mockApi: MockApi, id: string, goal: string, approvals: Record<string, unknown>[] = []) {
  mockApi.state.runs.push({
    id,
    goal,
    agent_id: "default",
    runtime_id: "legacy-native",
    status: "completed",
    steps: [],
    events: [],
    approvals,
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

function approval(id: string, name: string, runId: string) {
  return {
    id,
    run_id: runId,
    step_id: `${id}-step`,
    tool_call: { name, arguments: { query: `${name} の引数` } },
    status: "pending",
    reason: "承認が必要です",
    decided_by: null,
    created_at: MOCK_NOW,
    decided_at: null,
  };
}

for (const viewport of VIEWPORTS) {
  test.describe(`A 型: 一覧 → 全画面エディタ (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    // #585 / #1786: 全画面のエディタは、欄に結び付かない保存の失敗を確定の行（フォームの末尾）の直上の 1 か所だけに出す。
    test("保存に失敗すると理由を確定の行の直上だけに出し、Toast に重ねない", async ({ page }) => {
      const message = "同じ名前の業務 Agent があります。別の名前にしてください。";
      await page.route("**/api/agents", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        await route.fulfill({
          status: 409,
          json: { data: null, error_messages: [message], warning_messages: [] },
        });
      });
      await page.goto("/agents?id=new");
      await page.locator("#new-agent-name").fill("重複する Agent");
      const actions = page.getByRole("group", { name: "確定の操作" });
      // 複数のカードを 1 つの保存で確定する画面も、確定の行は最後のカードの下のふつうの行で、画面に貼り付かず
      // PageHeader に重ならない（#1786 / #2715）。
      expect(await findFloatingFormActionBars(page)).toEqual([]);
      // ヘッダーには確定を置かない。
      await expect(page.locator("[data-page-header-actions]").getByRole("button", { name: "作成", exact: true })).toHaveCount(0);
      await actions.getByRole("button", { name: "作成", exact: true }).click();

      const banner = page.getByTestId("agent-save-error");
      await expect(banner.getByRole("alert")).toHaveText(message);
      await expect(banner).toBeInViewport();
      await expect(page.getByText(message)).toHaveCount(1);
      // 確定の行の直上に置く（ボタンと失敗が並んで見える）。
      const [bannerBottom, actionsTop] = await Promise.all([
        banner.evaluate((element) => element.getBoundingClientRect().bottom),
        actions.evaluate((element) => element.getBoundingClientRect().top),
      ]);
      expect(actionsTop).toBeGreaterThanOrEqual(bannerBottom);
      expect(actionsTop - bannerBottom).toBeLessThan(48);
      await expect(page).toHaveURL(/\/agents\?id=new$/);
      await expectNoHorizontalOverflow(page);
    });

    test("編集対象は URL で開き、再読込・戻る / 進む・左上の一覧へ戻るで行き来できる", async ({ page }) => {
      await page.goto("/skills");
      await expect(page.getByRole("heading", { name: "スキル", level: 1 })).toBeVisible();
      await expect(breadcrumbs(page)).toHaveCount(0);

      // 先頭セルの対象名のリンクで開く（URL を持つので新しいタブでも開ける。#583）。
      const skillLink = page.getByRole("link", { name: /業務 RAG 調査/ });
      await expect(skillLink).toHaveAttribute("href", "/skills?id=business-rag-research");
      await skillLink.click();
      await expect(page).toHaveURL(/\/skills\?id=business-rag-research$/);
      await expect(page.getByRole("heading", { name: "業務 RAG 調査", level: 1 })).toBeVisible();
      // 2 階層のパンくずは出さず、タイトルの上の左端に「一覧へ戻る」（#618）。
      await expect(breadcrumbs(page)).toHaveCount(0);
      await expect(page.getByTestId("editor-back")).toHaveText("一覧へ戻る");
      await expect(page.getByTestId("editor-back")).toHaveAccessibleName("スキルの一覧へ戻る");
      await expectNoHorizontalOverflow(page);

      await page.reload();
      await expect(page.getByRole("heading", { name: "業務 RAG 調査", level: 1 })).toBeVisible();

      await page.goBack();
      await expect(page).toHaveURL(/\/skills$/);
      await expect(page.getByRole("heading", { name: "スキル", level: 1 })).toBeVisible();
      await page.goForward();
      await expect(page).toHaveURL(/\/skills\?id=business-rag-research$/);
      await expect(page.getByRole("heading", { name: "業務 RAG 調査", level: 1 })).toBeVisible();

      await page.getByTestId("editor-back").click();
      await expect(page).toHaveURL(/\/skills$/);

      // 行の操作以外の領域（状態のセル）をクリックしても開く。
      await page.getByTestId("skill-row-structured-data-query").getByRole("cell").nth(2).click();
      await expect(page).toHaveURL(/\/skills\?id=structured-data-query$/);

      // ?id=new は新規作成のエディタ。
      await page.goto("/skills?id=new");
      await expect(page.getByRole("heading", { name: "スキルを追加", level: 1 })).toBeVisible();
      await expect(page.getByTestId("editor-back")).toBeVisible();
      await expect(page.locator("#skill-id")).toBeEditable();
    });

    test("一覧の行は操作メニュー 1 つだけを持ち、キーボードで開閉できる", async ({ page, mockApi }) => {
      mockApi.state.mcpConnections.connections.push({
        server_id: "crm",
        label: "CRM Gateway",
        base_url: "http://mcp.example.test/jsonrpc",
        auth_mode: "none",
        service_audience: null,
        timeout_seconds: 10,
        source: "runtime",
        removable: true,
        configured: true,
        api_key_configured: false,
        oauth_configured: false,
        session_configured: false,
        service_token_configured: false,
      });
      await page.goto("/settings/mcp-connections");
      const table = page.getByRole("table", { name: "MCP 接続" });
      // 見出しの行 + RAG / NL2SQL（標準の接続）+ CRM。
      await expect(table.getByRole("row")).toHaveCount(4);

      // 各行のボタンは「対象名」と「操作メニュー」だけ（文字ボタンを並べない）。
      // RAG / NL2SQL（標準の接続）は削除できず、使える操作が無いので行メニューを出さない。
      for (const row of await table.getByRole("row").all()) {
        if ((await row.getByRole("columnheader").count()) > 0) continue;
        const builtin = (await row.getByText("標準", { exact: true }).count()) > 0;
        await expect(row.locator('[aria-haspopup="menu"]')).toHaveCount(builtin ? 0 : 1);
        expect(await row.getByRole("button").count()).toBeLessThanOrEqual(2);
      }

      // Enter で開いて先頭の項目へフォーカス、矢印で移動、Esc で閉じて trigger へ戻る。
      const trigger = page.getByRole("button", { name: "crm の操作", exact: true });
      await trigger.focus();
      await page.keyboard.press("Enter");
      const menu = page.getByRole("menu");
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("menuitem", { name: "削除" })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(trigger).toBeFocused();
      await expect(trigger).toHaveAttribute("aria-expanded", "false");


      // 破壊的な操作は確認する。キャンセルでは消えない。
      await trigger.click();
      await page.getByRole("menuitem", { name: "削除" }).click();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("MCP 接続を削除しますか?")).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await dialog.getByRole("button", { name: "キャンセル" }).click();
      await expect(trigger).toBeVisible();
      expect(mockApi.lastRequest("DELETE", "/api/settings/mcp-connections/crm")).toBeUndefined();

      await trigger.click();
      await page.getByRole("menuitem", { name: "削除" }).click();
      await dialog.getByRole("button", { name: "削除", exact: true }).click();
      await expect(page.getByText("MCP 接続を削除しました")).toBeVisible();
      await expect(trigger).toHaveCount(0);
    });

    test("エディタの対象操作は ObjectActionBar に出し、削除後は一覧へ戻る", async ({ page, mockApi }) => {
      mockApi.state.skills.push({
        id: "e2e_runtime",
        name: "E2E 実行時スキル",
        description: "削除できるスキル",
        instructions: "",
        mcp_requirements: [],
        references: [],
        enabled: true,
        tags: [],
        source: "runtime",
        created_at: MOCK_NOW,
        updated_at: MOCK_NOW,
      });
      await page.goto("/skills");
      await page.getByRole("link", { name: "E2E 実行時スキル e2e_runtime", exact: true }).click();
      await expect(page).toHaveURL(/\/skills\?id=e2e_runtime$/);

      // 行と同じ定義。危険な操作は「その他の操作」メニューに入る。
      const bar = page.getByTestId("skill-object-actions");
      await expect(bar.getByRole("button", { name: "削除" })).toHaveCount(0);
      await page.getByTestId("skill-object-actions-more").click();
      await page.getByRole("menuitem", { name: "削除" }).click();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("スキルを削除しますか?")).toBeVisible();
      await dialog.getByRole("button", { name: "削除", exact: true }).click();
      await expect(page.getByText("スキルを削除しました")).toBeVisible();
      await expect(page).toHaveURL(/\/skills$/);
      await expect(page.getByRole("link", { name: "E2E 実行時スキル e2e_runtime", exact: true })).toHaveCount(0);
    });
  });

  test.describe(`B 型: 一覧 + 詳細の分割ペイン (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("ツールは FixedSplitPane で一覧と詳細を並べる", async ({ page, mockApi }) => {
      seedRun(mockApi, "run-e2e-1", "一つ目の目標");
      seedRun(mockApi, "run-e2e-2", "二つ目の目標", [approval("approval-e2e-1", "rag__rag_search", "run-e2e-2")]);
      for (const [path, splitId] of [
        ["/tools", "tools-list"],
      ] as const) {
        await page.goto(path);
        const pane = page.getByTestId(`fixed-split-pane-${splitId}`);
        await expect(pane).toBeVisible();
        await expect(pane).toHaveAttribute("data-split-layout", viewport.layout);
        if (viewport.layout === "split") {
          await expect(pane.getByRole("separator", { name: "一覧と詳細の表示比率" })).toBeVisible();
        }
        await expectNoHorizontalOverflow(page);
      }

      if (viewport.layout === "split") {
        // divider のキー操作で比率が変わり、Agent の保存 key で残る。
        await page.goto("/tools");
        const divider = page.getByTestId("fixed-split-pane-tools-list-divider");
        const before = await divider.getAttribute("aria-valuenow");
        await divider.focus();
        await page.keyboard.press("ArrowRight");
        await expect(divider).not.toHaveAttribute("aria-valuenow", before ?? "");
        await expect
          .poll(() =>
            page.evaluate(() => window.localStorage.getItem("production-ready-agent.fixedSplitPane.tools-list"))
          )
          .not.toBeNull();
      }
    });

    test("Run の行を選ぶと詳細が変わり、チャットの Run の詳細は「会話を開く」を出して再実行を出さない（#2343）", async ({ page, mockApi }) => {
      seedRun(mockApi, "run-e2e-1", "一つ目の目標");
      seedRun(mockApi, "run-e2e-2", "二つ目の目標");
      // チャットの画面から送った Run（会話あり）。
      Object.assign(mockApi.state.runs[1] as Record<string, unknown>, { thread_id: "thread-e2e-2", owned_by_me: true });
      await page.goto("/runs");
      const detail = page.getByRole("region", { name: "実行の詳細" });
      await page.locator('a[data-run-id="run-e2e-1"]').click();
      await expect(detail.getByText("run-e2e-1", { exact: true }).first()).toBeVisible();
      // 会話の無い Run は開く元の会話が無いので出さない。
      await expect(page.getByTestId("run-open-chat")).toHaveCount(0);
      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();

      await page.getByRole("link", { name: "二つ目の目標（詳細を開く）" }).click();
      await expect(detail.getByText("run-e2e-2", { exact: true }).first()).toBeVisible();
      const openChat = page.getByTestId("run-open-chat");
      await expect(openChat).toHaveText("会話を開く");
      await expect(openChat).toHaveAttribute("href", /\/chat\?agent_id=default&thread_id=thread-e2e-2$/);
      // 再実行は実行の詳細・一覧の行に置かない（3 製品の実行の詳細を RAG と同じ「会話を開く」にそろえる）。
      await expect(page.getByRole("button", { name: "再実行" })).toHaveCount(0);

      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
      await expect(page.getByTestId("run-row-run-e2e-2")).toHaveAttribute("aria-current", "true");
      await expect(page.getByRole("menuitem", { name: "再実行" })).toHaveCount(0);
    });

    test("行の題名のリンク（共有 RowTitleButton）はキーボードで選べ、選んだ行と題名を aria-current で伝える", async ({ page, mockApi }, testInfo) => {
      // #421: Agent の EntityLayout の RowTitleButton を packages/ui の共有部品へ移した。
      seedRun(mockApi, "run-e2e-1", "一つ目の目標");
      seedRun(mockApi, "run-e2e-2", "二つ目の目標");
      await page.goto("/runs");
      const first = page.getByRole("link", { name: "一つ目の目標（詳細を開く）" });
      const second = page.getByRole("link", { name: "二つ目の目標（詳細を開く）" });
      await expect(first).toHaveAttribute("data-row-title-button", "");
      // 一覧では自動で詳細を開かず、選んだ履歴だけを現在の項目にする。
      await expect(first).not.toHaveAttribute("aria-current", /.*/);
      await expect(second).not.toHaveAttribute("aria-current", /.*/);

      await second.focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/id=run-e2e-2$/);
      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
      await expect(second).toHaveAttribute("aria-current", "true");
      await expect(page.getByTestId("run-row-run-e2e-2")).toHaveAttribute("aria-current", "true");
      await expect(first).not.toHaveAttribute("aria-current", /.*/);
      await first.focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/id=run-e2e-1$/);
      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
      await expect(first).toHaveAttribute("aria-current", "true");
      await expect(page.getByTestId("run-row-run-e2e-1")).toHaveAttribute("aria-current", "true");

      // 長い目標は 2 行で切り詰め、キーボードのフォーカスで Tooltip を出す
      // （Tooltip は短い文の部品なので先頭 120 文字まで。全文は全幅の詳細）。
      const longContent = `長い目標${"の本文".repeat(60)}`;
      seedRun(mockApi, "run-e2e-long", longContent);
      await page.goto("/runs");
      const longTitleButton = page.locator("a[data-row-title-button]").filter({ hasText: "長い目標" });
      await expect(longTitleButton).toBeVisible();
      const clamp = await longTitleButton.locator("span").first().evaluate((node) => ({
        clamp: getComputedStyle(node).webkitLineClamp,
        clamped: node.scrollHeight - node.clientHeight > 1,
      }));
      expect(clamp).toEqual({ clamp: "2", clamped: true });
      // キーボードのフォーカス（:focus-visible）で出すため、Tab で題名へ移る。
      await longTitleButton.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(longTitleButton).toBeFocused();
      const tooltip = page.locator('[role="tooltip"]:not([hidden])');
      await expect(tooltip).toBeVisible();
      await expect(tooltip).toHaveText(`${Array.from(longContent).slice(0, 120).join("")}…`);
      await expect(tooltip).toHaveAttribute("aria-hidden", "true");
      await expect(longTitleButton).not.toHaveAttribute("aria-describedby", /.*/);
      await page.keyboard.press("Escape");
      await expect(tooltip).toBeHidden();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`run-row-title-${viewport.name}.png`), fullPage: true });
    });

    test("承認は行メニューから確認して判断し、詳細に引数を出す", async ({ page, mockApi }) => {
      seedRun(mockApi, "run-e2e-2", "二つ目の目標", [
        approval("approval-e2e-1", "rag__rag_search", "run-e2e-2"),
        approval("approval-e2e-2", "crm__update_order", "run-e2e-2"),
      ]);
      await page.goto("/approvals");
      const detail = page.getByRole("region", { name: "承認の詳細" });
      await page.locator('a[data-approval-id="approval-e2e-1"]').click();
      await expect(detail.getByText("rag__rag_search の引数")).toBeVisible();
      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();

      await page.locator('a[data-approval-id="approval-e2e-2"]').click();
      await expect(detail.getByText("crm__update_order の引数")).toBeVisible();

      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
      await page.getByRole("button", { name: "rag__rag_search の操作", exact: true }).click();
      await page.getByRole("menuitem", { name: "却下" }).click();
      const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
      await expect(dialog.getByText("ツールの実行を却下しますか?")).toBeVisible();
      await dialog.getByRole("button", { name: "却下", exact: true }).click();
      await expect
        .poll(() => mockApi.lastRequest("POST", "/api/approvals/approval-e2e-1/decision")?.body)
        .toMatchObject({ approved: false });
      // 判断済みの承認は行メニューを持たない。
      await expect(page.getByRole("button", { name: "rag__rag_search の操作" })).toHaveCount(0);
      await expectNoHorizontalOverflow(page);
    });
  });
}
