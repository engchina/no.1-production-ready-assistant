import type { Page } from "@playwright/test";

import { dbUser, type CurrentUserPayload } from "./fixtures/auth";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";
import { openSidebarNav } from "./fixtures/nav";

// 権限によるナビ・ルート・ページ内の操作の出し分け（#215）。
// メニュー権限は画面の表示、capability（agent.runs.view / operate / approvals.decide / audit.view / admin）は
// 実データの閲覧と操作を許可する。backend が同じ規則で 403 を返すため、画面は出し分けるだけで認可の境界ではない。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

const VIEWER = dbUser({
  login_user_id: "viewer.user",
  display_name: "閲覧 次郎",
  permissions: ["agent.runs.view"],
  allowed_agent_ids: ["default"],
});

function seedRun(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.runs.push({
    id: "run-running",
    goal: "受注状況を確認する",
    agent_id: "default",
    runtime_id: "legacy-native",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  });
}

function pendingApproval(id: string) {
  return {
    id,
    run_id: "run-running",
    step_id: `${id}-step`,
    tool_call: { name: "rag__rag_search", arguments: { query: "受注" } },
    status: "pending",
    reason: "承認が必要です",
    decided_by: null,
    created_at: MOCK_NOW,
    decided_at: null,
  };
}

function sidebar(page: Page) {
  return page.getByRole("complementary", { name: "サイドナビゲーション" });
}

async function sidebarHrefs(page: Page): Promise<string[]> {
  // 375px ではナビがドロワー（#367）。開いてから読む。
  await openSidebarNav(page);
  await expect(sidebar(page).locator("a[href]").first()).toBeVisible();
  return sidebar(page)
    .locator("nav a[href]")
    .evaluateAll((links) => links.map((link) => link.getAttribute("href") ?? ""));
}

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

function signIn(mockApi: MockApi, user: CurrentUserPayload) {
  mockApi.setCurrentUser(user);
}

for (const viewport of VIEWPORTS) {
  test.describe(`権限によるナビとルート (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("実行履歴の参照だけの利用者は、実行履歴だけをナビに出し、他の URL は権限なしの画面へ移す", async ({
      page,
      mockApi,
    }) => {
      signIn(mockApi, VIEWER);
      seedRun(mockApi, { status: "running" });

      await page.goto("/runs");
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
      // agent.runs.view は menu.runs を含む（implies）。項目が 0 件のセクションは出さない。
      expect(await sidebarHrefs(page)).toEqual(["/runs"]);

      // Run の作成と取消・再開は Run の実行・操作の権限が必要。
      await expect(page.getByRole("button", { name: "実行を作成" })).toHaveCount(0);
      await expect(page.getByTestId("run-row-actions-run-running")).toHaveCount(0);
      await expect(page.getByTestId("run-object-actions")).toHaveCount(0);
      // Run の監査記録は監査の閲覧の権限が必要なので出さない（GET /runs/{id}/audit を呼ばない）。
      await expect(page.getByText("受注状況を確認する").first()).toBeVisible();
      expect(mockApi.requests.some((request) => request.path.endsWith("/audit"))).toBe(false);
      await expectNoPageOverflow(page);

      // ナビにない画面を URL で直接開くと権限なしの画面へ移す。
      for (const path of ["/settings/mcp-connections", "/audit", "/settings/security/permissions", "/tools"]) {
        await page.goto(path);
        await expect(page).toHaveURL(/\/forbidden$/);
        await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
      }
      await expectNoPageOverflow(page);
      // 「利用可能な画面へ戻る」は既定の入口（チャット）を開けないので、ナビの最初の画面（実行履歴）へ。
      await page.getByRole("button", { name: "利用可能な画面へ戻る" }).click();
      await expect(page).toHaveURL(/\/runs$/);
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();

      // `/` と未知の URL も Run へ振り分ける（ダッシュボードは廃止。#262）。
      await page.goto("/");
      await expect(page).toHaveURL(/\/runs$/);
      await page.goto("/no-such-page");
      await expect(page).toHaveURL(/\/runs$/);
    });

    test("Run の実行権限があれば Run の作成と取消を出す", async ({ page, mockApi }) => {
      signIn(
        mockApi,
        dbUser({ permissions: ["agent.runs.operate"], allowed_agent_ids: ["default"] })
      );
      seedRun(mockApi, { status: "running" });

      await page.goto("/runs");
      await expect(page.getByRole("button", { name: "実行を作成" })).toBeVisible();
      await expect(page.getByTestId("run-row-actions-run-running")).toHaveCount(1);
      await expectNoPageOverflow(page);
    });

    test("承認の判断は approver だけに出し、決定者は送らない", async ({ page, mockApi }) => {
      seedRun(mockApi, { status: "waiting_approval", approvals: [pendingApproval("approval-1")] });

      // 承認の画面を開ける（menu.approvals）が判断の権限がない利用者。
      signIn(mockApi, dbUser({ permissions: ["menu.approvals", "agent.runs.view"], allowed_agent_ids: ["default"] }));
      await page.goto("/approvals");
      await page.locator('a[data-approval-id="approval-1"]').click();
      await expect(page.getByRole("heading", { name: "承認の詳細", level: 1 })).toBeVisible();
      await expect(page.getByText("rag__rag_search").first()).toBeVisible();
      await expect(page.getByTestId("approval-object-actions")).toHaveCount(0);

      // 承認の判断の権限（agent.approvals.decide）がある利用者。
      signIn(mockApi, dbUser({ permissions: ["agent.approvals.decide"], allowed_agent_ids: ["default"] }));
      await page.reload();
      const actions = page.getByTestId("approval-object-actions");
      await expect(actions.getByRole("button", { name: "承認" })).toBeVisible();
      await actions.getByRole("button", { name: "承認" }).click();
      await page.getByRole("alertdialog").getByRole("button", { name: "承認" }).click();
      await expect.poll(() => mockApi.lastRequest("POST", "/api/approvals/approval-1/decision")?.body).toEqual({
        approved: true,
      });
    });

    test("画面は開けても実データの閲覧権限がなければ、データを取らずに理由を示す", async ({ page, mockApi }) => {
      signIn(mockApi, dbUser({ permissions: ["menu.runs"], allowed_agent_ids: [] }));

      await page.goto("/runs");
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
      await expect(page.getByTestId("capability-required")).toContainText("実行履歴の参照");
      expect(mockApi.requests.some((request) => request.path === "/api/runs")).toBe(false);
      await expectNoPageOverflow(page);
    });

    test("Run を開けない利用者は、/ と未知の URL で開ける最初の画面へ移る", async ({ page, mockApi }) => {
      signIn(mockApi, dbUser({ permissions: ["menu.security_users"], allowed_agent_ids: [] }));
      mockApi.state.security.users = [];

      await page.goto("/");
      await expect(page).toHaveURL(/\/settings\/security\/users$/);
      await expect(page.getByRole("heading", { name: "ユーザー管理", level: 1 })).toBeVisible();
      expect(await sidebarHrefs(page)).toEqual(["/settings/security/users"]);

      await page.goto("/runs/unknown");
      await expect(page).toHaveURL(/\/settings\/security\/users$/);
    });

    test("/ と未知の URL はナビの並び順で最初に開ける画面へ移り、履歴に / を残さない", async ({
      page,
      mockApi,
    }) => {
      // ダッシュボード（#262 で廃止）の項目・セクションはナビに出さない。
      signIn(mockApi, dbUser({ permissions: ["menu.agents", "agent.runs.view"], allowed_agent_ids: ["default"] }));

      await page.goto("/agents");
      await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();
      // 上に利用者の画面（AI 活用の実行履歴）、下に管理者の画面（Agent 構築の業務 Agent）。#791
      expect(await sidebarHrefs(page)).toEqual(["/runs", "/agents"]);
      await expect(sidebar(page).getByText("ダッシュボード")).toHaveCount(0);
      await expect(sidebar(page).locator('a[href="/"]')).toHaveCount(0);

      // `/` はナビの並び順で最初に開ける画面（実行履歴）へ置き換えで移る。戻ると `/` ではなく元の画面。
      await page.goto("/");
      await expect(page).toHaveURL(/\/runs$/);
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
      await page.goBack();
      await expect(page).toHaveURL(/\/agents$/);

      // 未知の URL は既定の入口（チャット）を開けないので、ナビの最初の画面（実行履歴）へ。
      await page.goto("/no-such-page");
      await expect(page).toHaveURL(/\/runs$/);
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
      await expectNoPageOverflow(page);
    });

    test("どの画面も開けない利用者は、/ で権限なしの画面へ移る", async ({ page, mockApi }) => {
      signIn(mockApi, dbUser({ permissions: [], allowed_agent_ids: [] }));

      await page.goto("/");
      await expect(page).toHaveURL(/\/forbidden$/);
      await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
      await expectNoPageOverflow(page);
    });

    test("Agent 管理の権限がなければ業務 Agent は閲覧だけ（作成・保存を出さない）", async ({
      page,
      mockApi,
    }) => {
      signIn(mockApi, dbUser({ permissions: ["menu.agents", "agent.runs.view"], allowed_agent_ids: ["default"] }));

      await page.goto("/agents");
      await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();
      await expect(page.getByRole("button", { name: "業務 Agent を作成" })).toHaveCount(0);
      await expect(page.getByTestId("agent-row-actions-default")).toHaveCount(0);

      // 作成の URL を直接開いても一覧を出す。
      await page.goto("/agents?id=new");
      await expect(page.getByRole("table", { name: "業務 Agent 一覧" })).toBeVisible();

      await page.goto("/agents?id=default");
      await expect(page.getByRole("heading", { name: "資料・データ横断アシスタント", level: 1 })).toBeVisible();
      await expect(page.getByRole("button", { name: "保存" })).toHaveCount(0);
      await expect(page.locator("#default-agent-name")).toBeDisabled();
      await expect(page.locator("#default-agent-model")).toBeDisabled();
      await expect(page.getByTestId("agent-object-actions")).toHaveCount(0);
      await expectNoPageOverflow(page);
    });
  });
}
