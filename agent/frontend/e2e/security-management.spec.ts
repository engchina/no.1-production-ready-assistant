import type { Page } from "@playwright/test";
import { closeSidebarNav, openSidebarNav } from "./fixtures/nav";

import { ALL_PERMISSION_CODES, dbUser } from "./fixtures/auth";
import { expect, test } from "./fixtures/mock-api";

// ユーザー管理・ロール管理（3製品共通の画面）と Agent の権限管理（#215）。
// 権限管理は機能権限に加えて、利用できる業務 Agentを PUT /api/security/roles/{id}/access で保存する。
// 検索・回答プロファイルの判定は RAG が Run の利用者のサービストークンで行うため、Agent の権限管理には出さない（#750）。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

const CSRF_TOKEN = "csrf-security-token";

const SECURITY_ADMIN = dbUser({
  login_user_id: "admin",
  display_name: "管理 太郎",
  role_codes: ["SYSTEM_ADMIN"],
  role_display_names: ["システム管理者"],
  is_system_admin: true,
  permissions: ALL_PERMISSION_CODES,
  allowed_agent_ids: null,
});

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

/** 編集の画面の保存はフォームの末尾の確定の行（#1786。#618 の PageHeader の右端から移した）。 */
function editActions(page: Page) {
  return page.locator("[data-form-action-bar][data-testid$='-form-actions']");
}

for (const viewport of VIEWPORTS) {
  test.describe(`ユーザー・ロール・権限管理 (${viewport.name})`, () => {
    test.beforeEach(async ({ page, mockApi, context, baseURL }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      mockApi.setCurrentUser(SECURITY_ADMIN);
      await context.addCookies([{ name: "pr_csrf", value: CSRF_TOKEN, url: baseURL ?? "http://127.0.0.1:3042" }]);
    });

    test("ユーザー管理とロール管理を開け、ロールの詳細から権限管理へ移れる", async ({ page }) => {
      await page.goto("/settings/security/users");
      await expect(page.getByRole("heading", { name: "ユーザー管理", level: 1 })).toBeVisible();
      const users = page.getByTestId("security-users-grid");
      await expect(users.getByText("実行 花子", { exact: true })).toBeVisible();
      await expect(users.getByText("管理 太郎")).toBeVisible();
      await expectNoPageOverflow(page);

      await page.goto("/settings/security/roles");
      await expect(page.getByRole("heading", { name: "ロール管理", level: 1 })).toBeVisible();
      await page
        .getByTestId("security-roles-grid")
        .locator("tbody tr")
        .filter({ hasText: "Agent 実行担当" })
        .locator("td")
        .first()
        .click();
      const summary = page.getByTestId("security-roles-permission-summary");
      await expect(summary).toContainText("付与している機能権限: 2 件");
      await expectNoPageOverflow(page);
      await summary.getByTestId("security-roles-open-permissions").click();

      await expect(page).toHaveURL(/\/settings\/security\/permissions\?role=role-operator$/);
      await expect(page.getByRole("heading", { name: "権限管理", level: 1 })).toBeVisible();
      await expectNoPageOverflow(page);
    });

    test("権限管理で業務 Agentを保存し、Agent 管理では全件が対象になる。検索・回答プロファイルは出さない", async ({
      page,
      mockApi,
    }) => {
      await page.goto("/settings/security/permissions?role=role-operator");
      await expect(page.getByRole("heading", { name: "権限管理", level: 1 })).toBeVisible();
      await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();

      const agents = page.getByTestId("security-roles-agent-access-list");
      await expect(agents).toHaveAccessibleName("利用できる業務 Agent");
      await expect(page.getByTestId("security-roles-search-answer-profile-access-list")).toHaveCount(0);
      await expect(page.getByText("利用できる検索・回答プロファイル")).toHaveCount(0);
      // 無効な業務 Agentは状態を示す。保存済みの対象は選択済み。
      await expect(agents.getByText("無効")).toBeVisible();
      await expect(agents.getByRole("option", { name: /資料・データ横断アシスタント/ })).toBeChecked();

      await agents.getByRole("option", { name: /経理 Agent/ }).check();
      expect(mockApi.lastRequest("PUT", "/api/security/roles/role-operator/access")).toBeUndefined();
      await expectNoPageOverflow(page);

      await editActions(page).getByRole("button", { name: "保存" }).click();
      await expect(page.getByText("変更を保存しました。")).toBeVisible();
      const first = mockApi.lastRequest("PUT", "/api/security/roles/role-operator/access");
      expect(first?.headers["x-csrf-token"]).toBe(CSRF_TOKEN);
      expect(first?.body).toMatchObject({
        version: 3,
        agent_ids: expect.arrayContaining(["default", "finance"]),
      });
      const firstBody = first?.body as Record<string, string[]>;
      expect(firstBody.agent_ids).toHaveLength(2);
      expect(firstBody).not.toHaveProperty("search_answer_profile_ids");
      expect([...firstBody.permissions].sort()).toEqual(["agent.runs.operate", "menu.agents"]);
      // 共通のロール API には送らない（権限は Agent の保存 API だけが変える）。
      expect(mockApi.requests.some((request) => request.method === "PATCH" && request.path.startsWith("/api/security/roles"))).toBe(false);

      // 保存後も編集を続けられる。Agent 管理を付けると業務 Agentは全件が対象になり、空の一覧を送る。
      await page.getByRole("checkbox", { name: /^Agent 管理/ }).check();
      await expect(page.getByText("Agent 管理の権限により、すべての業務 Agentを利用できます。個別選択は不要です。")).toBeVisible();
      await expect(page.getByTestId("security-roles-agent-access-list")).toHaveCount(0);
      await editActions(page).getByRole("button", { name: "保存" }).click();

      await expect
        .poll(() => mockApi.requests.filter((request) => request.method === "PUT").length)
        .toBe(2);
      const second = mockApi.lastRequest("PUT", "/api/security/roles/role-operator/access")?.body as Record<
        string,
        unknown
      >;
      expect(second).toMatchObject({ version: 4, agent_ids: [] });
      // implies で付くメニュー権限は送らない（backend が展開する）。
      expect([...(second.permissions as string[])].sort()).toEqual(["agent.admin", "agent.runs.operate", "menu.agents"]);
    });

    test("業務 Agentが大量でも全件を読まず、サーバー側で検索して 50 件ずつ読む（#608）", async ({ page, mockApi }) => {
      mockApi.state.security.accessTargets.agents = Array.from({ length: 180 }, (_, index) => ({
        id: `agent-${String(index + 1).padStart(3, "0")}`,
        name: `業務 Agent ${String(index + 1).padStart(3, "0")}`,
        description: null,
        status: "enabled",
      }));

      await page.goto("/settings/security/permissions?role=role-operator");
      await expect(page.getByRole("heading", { name: "権限管理", level: 1 })).toBeVisible();
      await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
      const agents = page.getByTestId("security-roles-agent-access-list");
      await expect(agents.getByRole("option")).toHaveCount(50);
      await expect(agents).toContainText("50 / 180 件、選択 1 件");
      await agents.getByRole("button", { name: "さらに読み込む" }).click();
      await expect(agents.getByRole("option")).toHaveCount(100);
      await agents.getByRole("searchbox").fill("177");
      await expect(agents.getByRole("option")).toHaveCount(1);
      const searched = mockApi.requests.filter((request) => request.path === "/api/security/access-targets/agents").at(-1);
      expect(searched?.searchParams.get("q")).toBe("177");
      expect(searched?.searchParams.get("offset")).toBe("0");
      await agents.getByRole("option", { name: /業務 Agent 177/ }).click();
      await editActions(page).getByRole("button", { name: "保存" }).click();
      await expect
        .poll(() => (mockApi.lastRequest("PUT", "/api/security/roles/role-operator/access")?.body as { agent_ids?: string[] })?.agent_ids)
        .toEqual(["default", "agent-177"]);
      await expectNoPageOverflow(page);
    });
  });
}


test("API キーの管理は専用権限で開け、削除を確定できる", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.security_api_keys"] }));
  const key = {
    id: "key-a", name: "検証キー A", token_prefix: "prak_example", products: ["agent"],
    owner_user_uuid: "user-a", owner_display_name: "利用者 A", created_at: "2026-01-01T00:00:00Z",
    expires_at: null, last_used_at: null, expired: false, resource_scopes: {},
  };
  let keys = [key];
  const revoked: string[] = [];
  await page.route("**/api/security/api-keys**", async (route) => {
    if (route.request().method() === "DELETE") {
      revoked.push(route.request().url().split("/").pop() ?? "");
      keys = [];
      await route.fulfill({ json: { data: { status: "revoked" } } });
    } else await route.fulfill({ json: { data: { keys } } });
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/settings\/security\/api-keys$/);
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "API キー", exact: true })).toHaveAttribute("aria-current", "page");
  await closeSidebarNav(page);
  await expect(page.getByRole("table", { name: "API キーの一覧" })).toContainText("利用者 A");
  await expect(page.getByTestId("api-key-create")).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => {}))));
  await page.screenshot({ path: testInfo.outputPath("api-keys-admin.png"), fullPage: true });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("api-keys-admin-dark.png"), fullPage: true });
  await page.getByTestId("api-key-row-actions-key-a-trigger").click();
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("利用者 A");
  await dialog.getByRole("button", { name: "削除", exact: true }).click();
  await expect.poll(() => revoked).toEqual(["key-a"]);
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.chat", "agent.runs.operate"] }));
  await page.goto("/settings/security/api-keys");
  await expect(page).toHaveURL(/\/forbidden$/);
});


test("管理画面の読込表示は対象を短く示して一覧の幅に収まる", { tag: "@responsive" }, async ({ page, mockApi }) => {
  mockApi.setCurrentUser(SECURITY_ADMIN);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/security/**", async route => { await pending; await route.fallback(); });
  try {
    for (const [kind, label] of [["users", "ユーザーを読込中"], ["roles", "ロールを読込中"], ["permissions", "権限を読込中"]]) {
      await page.goto(`/settings/security/${kind}`);
      const loading = page.getByTestId(`security-${kind}-loading`);
      await expect(loading.locator("[data-processing-label]")).toHaveText(label);
      await expect(loading.locator("[data-processing-label]")).toBeVisible();
      await expect(loading).toContainText("経過時間");
      const size = await loading.locator("[data-processing-label]").evaluate(node => ({height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight)}));
      expect(size.height).toBeLessThanOrEqual(size.line * 2 + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    }
  } finally { release(); }
});
