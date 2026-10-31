import type { Page } from "@playwright/test";

import { ALL_PERMISSION_CODES, dbUser } from "./fixtures/auth";
import { expect, test, type MockApi } from "./fixtures/mock-api";
import { expandSidebarSections, openSidebarNav } from "./fixtures/nav";

// 共通認証のログイン・パスワード変更・セッション切れ（#215）。
// production（`PLATFORM_AUTH_MODE=authenticated`）の DB ユーザーでログインし、失敗・強制パスワード変更・
// 401 でログインへ戻る流れと、状態を変える API に CSRF header（共有の Cookie `pr_csrf`。#1565）を付けることを確かめる。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

const CSRF_TOKEN = "csrf-e2e-token";

const ADMIN_USER = dbUser({
  login_user_id: "admin.user",
  display_name: "管理 太郎",
  role_codes: ["AGENT_ADMIN"],
  permissions: ALL_PERMISSION_CODES,
  allowed_agent_ids: null,
});

const FIRST_USER = dbUser({
  login_user_id: "first.user",
  display_name: "初回 利用者",
  force_password_change: true,
  permissions: ["agent.runs.view"],
  allowed_agent_ids: ["default"],
});

function registerAccounts(mockApi: MockApi) {
  mockApi.state.auth.accounts["admin.user"] = { password: "CorrectPass!123", user: ADMIN_USER };
  mockApi.state.auth.accounts["first.user"] = { password: "TemporaryPass!123", user: FIRST_USER };
}

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

function sidebar(page: Page) {
  return page.getByRole("complementary", { name: "サイドナビゲーション" });
}

/** 375px ではナビがドロワー（#367）。開いて文言付きの操作を使う。 */
async function expandSidebarOnMobile(page: Page) {
  await openSidebarNav(page);
}

for (const viewport of VIEWPORTS) {
  test.describe(`ログインとセッション (${viewport.name})`, () => {
    test.beforeEach(async ({ page, mockApi }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      registerAccounts(mockApi);
    });

    test("未ログインで開くとログイン画面へ移り、DB ユーザーでログインすると元の画面を開く", async ({ page, mockApi }) => {
      mockApi.setCurrentUser(null);

      await page.goto("/runs");
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole("heading", { name: "システムにログイン" })).toBeVisible();
      // wordmark は Agent。ログイン画面は AppShell の外（サイドナビなし）。
      await expect(page.getByText("Production Ready", { exact: true })).toBeVisible();
      await expect(page.getByText("Agent", { exact: true })).toBeVisible();
      await expect(sidebar(page)).toHaveCount(0);
      await expectNoPageOverflow(page);

      await page.getByLabel("ログインユーザーID").fill("admin.user");
      await page.getByLabel("パスワード").fill("CorrectPass!123");
      await page.getByRole("button", { name: "ログイン" }).click();

      await expect(page).toHaveURL(/\/runs$/);
      expect(mockApi.lastRequest("POST", "/api/auth/login")?.body).toEqual({
        login_user_id: "admin.user",
        password: "CorrectPass!123",
      });
      await openSidebarNav(page);
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
    });

    test("ログイン失敗は入力ミスの文言だけを出し、ログイン画面に留まる", async ({ page, mockApi }) => {
      mockApi.setCurrentUser(null);

      await page.goto("/login");
      // 未入力の送信は API を呼ばずに案内する。
      await page.getByRole("button", { name: "ログイン" }).click();
      await expect(page.getByText("ログインユーザーIDとパスワードを入力してください。")).toBeVisible();
      expect(mockApi.lastRequest("POST", "/api/auth/login")).toBeUndefined();

      await page.getByLabel("ログインユーザーID").fill("admin.user");
      await page.getByLabel("パスワード").fill("WrongPass!123");
      await page.getByRole("button", { name: "ログイン" }).click();

      await expect(page.getByText("ログインユーザーIDまたはパスワードを確認してください。")).toBeVisible();
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByLabel("ログインユーザーID")).toHaveValue("admin.user");
    });

    test("ログインの試行が多すぎるときは 429 の文をそのまま出し、再送しない", async ({ page, mockApi }) => {
      mockApi.setCurrentUser(null);
      // 試行の回数の上限（#1087）。backend は 429 と Retry-After を返す。
      let loginCalls = 0;
      await page.route("**/api/auth/login", async (route) => {
        loginCalls += 1;
        await route.fulfill({
          status: 429,
          headers: { "Retry-After": "900", "X-Request-ID": "login-rate-limited" },
          json: {
            data: null,
            error_messages: ["ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。"],
            warning_messages: [],
            error_code: "SECURITY_RATE_LIMITED",
          },
        });
      });

      await page.goto("/login");
      await page.getByLabel("ログインユーザーID").fill("system_admin");
      await page.getByLabel("パスワード").fill("WrongPass!123");
      await page.getByRole("button", { name: "ログイン" }).click();

      const error = page.getByText("ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。", { exact: true });
      await expect(error).toBeVisible();
      await expect(error).not.toContainText("login-rate-limited");
      await expect(page).toHaveURL(/\/login$/);
      expect(loginCalls).toBe(1);
      await expectNoPageOverflow(page);
    });

    test("初回ログインは強制パスワード変更へ移り、変更は CSRF header 付きで送ってログインへ戻る", async ({
      page,
      context,
      baseURL,
      mockApi,
    }) => {
      mockApi.setCurrentUser(null);
      await context.addCookies([{ name: "pr_csrf", value: CSRF_TOKEN, url: baseURL ?? "http://127.0.0.1:3142" }]);

      await page.goto("/login");
      await page.getByLabel("ログインユーザーID").fill("first.user");
      await page.getByLabel("パスワード").fill("TemporaryPass!123");
      await page.getByRole("button", { name: "ログイン" }).click();

      await expect(page).toHaveURL(/\/password\/change$/);
      await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
      await expect(sidebar(page)).toHaveCount(0);
      // 強制変更中は他の画面を開いてもパスワード変更へ戻す。
      await page.goto("/runs");
      await expect(page).toHaveURL(/\/password\/change$/);
      await expectNoPageOverflow(page);

      await page.getByLabel("現在のパスワード").fill("TemporaryPass!123");
      await page.locator("#auth-password-new").fill("IndependentPass!456");
      await page.getByLabel("新しいパスワード（確認）").fill("IndependentPass!456");
      await page.getByRole("button", { name: "パスワードを変更" }).click();

      await expect(page.getByText("パスワードを変更しました。新しいパスワードでログインしてください。")).toBeVisible();
      await expect(page).toHaveURL(/\/login$/);
      const change = mockApi.lastRequest("POST", "/api/auth/password/change");
      expect(change?.body).toEqual({ current_password: "TemporaryPass!123", new_password: "IndependentPass!456" });
      expect(change?.headers["x-csrf-token"]).toBe(CSRF_TOKEN);
      // 参照（GET）には CSRF header を付けない。
      expect(mockApi.lastRequest("GET", "/api/auth/me")?.headers["x-csrf-token"]).toBeUndefined();
    });

    test("ログイン中に API が 401 を返したらログイン画面へ戻す", async ({ page, mockApi }) => {
      mockApi.setCurrentUser(ADMIN_USER);

      await page.goto("/runs");
      await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();

      // セッションが切れた（backend が 401 を返す）状態で別の画面を開く。
      mockApi.setCurrentUser(null);
      await page.route("**/api/skills", (route) =>
        route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ data: null, error_messages: ["ログインが必要です。"], warning_messages: [] }),
        })
      );
      await expandSidebarOnMobile(page);
      // スキルは既定で閉じている「Agent 構築」にある（#1563）。
      await expandSidebarSections(sidebar(page), "Agent 構築");
      await sidebar(page).locator('a[href="/skills"]').click();

      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole("heading", { name: "システムにログイン" })).toBeVisible();
    });

    test("アカウントの欄からアカウントの画面を開き、API キーとパスワード変更のタブを使う（#1873）", async ({
      page,
      context,
      baseURL,
      mockApi,
    }) => {
      mockApi.setCurrentUser(ADMIN_USER);
      await context.addCookies([{ name: "pr_csrf", value: CSRF_TOKEN, url: baseURL ?? "http://127.0.0.1:3142" }]);

      await page.goto("/runs");
      await expandSidebarOnMobile(page);
      // アカウント欄の 1 行目はログイン ID。表示名はほかの画面で使い、アカウント欄には出さない（#1696）。
      await expect(sidebar(page).getByText("admin.user", { exact: true })).toBeVisible();
      await expect(sidebar(page).getByText("管理 太郎")).toHaveCount(0);
      // 個人の操作（API キー・パスワード変更）はサイドナビの行に出さず、欄（行全体のリンク）からアカウントの画面へ移る。
      await expect(sidebar(page).getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
      const accountLink = sidebar(page).getByRole("link", { name: "アカウント設定を開く（admin.user）" });
      expect((await accountLink.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
      await accountLink.click();
      await expect(page).toHaveURL(/\/account$/);
      await expect(page.getByRole("heading", { name: "アカウント", level: 1 })).toBeVisible();

      // 「API キー」のタブ: Agent でも自分のキーを発行でき（#1874）、業務 Agent を絞る選択肢を出す。
      const apiKeysTab = page.getByRole("tab", { name: "API キー" });
      await expect(apiKeysTab).toHaveAttribute("aria-selected", "true");
      await expect(page.getByTestId("api-keys-endpoint-agent")).toContainText("/api/mcp");
      await expect(page.getByTestId("api-key-agent-scope-all")).toBeChecked();
      await page.getByLabel("名前（用途）").fill("会社の PC の Claude Code");
      await page.getByTestId("api-key-create").click();
      await expect(page.getByTestId("api-key-token")).toHaveValue(/^prak_/);
      const created = mockApi.lastRequest("POST", "/api/account/api-keys");
      expect(created?.headers["x-csrf-token"]).toBe(CSRF_TOKEN);
      expect((created?.body as { products: string[] }).products).toEqual(["rag", "nl2sql", "agent"]);
      await expectNoPageOverflow(page);

      // 「パスワード変更」のタブ（← → で移り、URL に残す）。
      await apiKeysTab.focus();
      await page.keyboard.press("ArrowRight");
      await expect(page.getByRole("tab", { name: "パスワード変更" })).toHaveAttribute("aria-selected", "true");
      await expect(page).toHaveURL(/\/account\?tab=password$/);
      await expect(page.getByLabel("現在のパスワード")).toBeVisible();
      await expect(page.getByTestId("account-password-submit")).toBeVisible();
      await expectNoPageOverflow(page);
    });

    test("アカウント欄のログアウト（CSRF header 付き）", async ({ page, context, baseURL, mockApi }) => {
      mockApi.setCurrentUser(ADMIN_USER);
      await context.addCookies([{ name: "pr_csrf", value: CSRF_TOKEN, url: baseURL ?? "http://127.0.0.1:3142" }]);

      await page.goto("/runs");
      await expandSidebarOnMobile(page);
      await sidebar(page).getByRole("button", { name: "ログアウト" }).click();
      await expect(page).toHaveURL(/\/login$/);
      expect(mockApi.lastRequest("POST", "/api/auth/logout")?.headers["x-csrf-token"]).toBe(CSRF_TOKEN);
    });

    test("ローカル（PLATFORM_AUTH_MODE=local）はログインせずに全画面を使え、アカウント欄にログイン省略を示す（#307）", async ({ page }) => {
      // fixture の既定はローカルの全権限の利用者。`/` はナビの最初の画面（チャット。#768）へ移す（#262）。
      await page.goto("/");
      await expect(page).toHaveURL(/\/chat$/);
      await expect(page.getByRole("heading", { name: "チャット", level: 1 })).toBeVisible();
      await expandSidebarOnMobile(page);
      // NL2SQL と同じく、利用者名とログイン省略の表示を出し、パスワード変更・ログアウトは出さない。
      await expect(sidebar(page).getByText("ローカル利用者")).toBeVisible();
      await expect(sidebar(page).getByRole("status", { name: "ログイン省略" })).toBeVisible();
      await expect(sidebar(page).getByRole("button", { name: "ログアウト" })).toHaveCount(0);
      await expect(sidebar(page).getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
      // アカウントの画面のパスワード変更のタブは、ローカルの認証ではパスワードを使わない説明だけ（#1873）。
      await page.goto("/account?tab=password");
      await expect(page.getByText("ローカルの認証（ログインの省略）ではパスワードを使いません。")).toBeVisible();

      // ログイン画面を開いても既定の入口（ナビの先頭のチャット。RAG と同じ。#791）へ戻す。
      await page.goto("/login");
      await expect(page).toHaveURL(/\/chat$/);
      await expect(page.getByRole("heading", { name: "チャット", level: 1 })).toBeVisible();
    });
  });
}
