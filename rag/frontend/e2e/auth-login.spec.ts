import { expect, test, type Page, type Route } from "./fixtures/test";

import {
  ALL_PERMISSION_CODES,
  apiEnvelope,
  dbUser,
  expectNoPageOverflow,
  mockDatabaseReady,
  type CurrentUserPayload,
  expandSidebarSections,
  openSidebarNav,
} from "./_helpers";

// 共通認証のログイン・パスワード変更・セッション切れ（#214）。
// DB ユーザーでログインし、失敗・強制パスワード変更・401 でログインへ戻る流れを確かめる。

const unauthorized = { data: null, error_messages: ["ログインが必要です。"], warning_messages: [] };

/** ログイン状態を持つ `/api/auth/*` の mock。null は未ログイン（me が 401）。 */
async function mockAuthApi(page: Page, initial: CurrentUserPayload | null) {
  const state = {
    current: initial,
    loginCalls: [] as Record<string, unknown>[],
    passwordCalls: [] as { body: Record<string, unknown>; csrf: string | null }[],
    logoutCalls: 0,
  };
  await page.route("**/api/auth/**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/auth/me") {
      await (state.current
        ? route.fulfill({ json: apiEnvelope(state.current) })
        : route.fulfill({ status: 401, json: unauthorized }));
      return;
    }
    if (path === "/api/auth/login") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.loginCalls.push(body);
      if (body.password === "TooMany!123") {
        // 試行の回数の上限（#1087）。backend は 429 と Retry-After を返す。
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
        return;
      }
      if (body.password === "WrongPass!123") {
        await route.fulfill({
          status: 401,
          json: {
            data: null,
            error_messages: ["ログインユーザーIDまたはパスワードを確認してください。"],
            warning_messages: [],
            error_code: "SECURITY_AUTHENTICATION_REQUIRED",
          },
        });
        return;
      }
      state.current = loginTarget(String(body.login_user_id));
      await route.fulfill({ json: apiEnvelope(state.current) });
      return;
    }
    if (path === "/api/auth/password/change") {
      state.passwordCalls.push({
        body: request.postDataJSON() as Record<string, unknown>,
        csrf: request.headers()["x-csrf-token"] ?? null,
      });
      // 変更後はセッションを失効させ、新しいパスワードでのログインを求める。
      state.current = null;
      await route.fulfill({ json: apiEnvelope({ changed: true }) });
      return;
    }
    if (path === "/api/auth/logout") {
      state.logoutCalls += 1;
      state.current = null;
      await route.fulfill({ json: apiEnvelope({ logged_out: true }) });
      return;
    }
    await route.fulfill({ status: 404, json: unauthorized });
  });
  return state;
}

function loginTarget(loginUserId: string): CurrentUserPayload {
  if (loginUserId === "first.user") {
    return dbUser({
      login_user_id: "first.user",
      display_name: "初回 利用者",
      force_password_change: true,
      permissions: ["menu.search"],
    });
  }
  return dbUser({
    login_user_id: loginUserId,
    display_name: "管理 太郎",
    role_codes: ["RAG_ADMIN"],
    permissions: ALL_PERMISSION_CODES,
    allowed_search_answer_profile_ids: null,
    allowed_knowledge_base_ids: null,
  });
}

test.beforeEach(async ({ page }) => {
  // 未モックの API はログインを求めない 200 の空応答にし、画面の描画だけを確かめる。
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } })
  );
  await mockDatabaseReady(page);
});

test("未ログインで開くとログイン画面へ移り、DB ユーザーでログインすると元の画面を開く", async ({ page }) => {
  const auth = await mockAuthApi(page, null);

  await page.goto("/file-list");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "システムにログイン" })).toBeVisible();
  // wordmark は RAG。
  await expect(page.getByText("Production Ready", { exact: true })).toBeVisible();
  await expect(page.getByText("RAG", { exact: true })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
  await expectNoPageOverflow(page);

  await page.getByLabel("ログインユーザーID").fill("admin.user");
  await page.getByLabel("パスワード").fill("CorrectPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();

  await expect(page).toHaveURL(/\/file-list$/);
  expect(auth.loginCalls).toEqual([{ login_user_id: "admin.user", password: "CorrectPass!123" }]);
  await openSidebarNav(page);
});

test("ログイン失敗は入力ミスの文言だけを出し、ログイン画面に留まる", async ({ page }) => {
  const auth = await mockAuthApi(page, null);

  await page.goto("/login");
  // 未入力の送信は API を呼ばずに案内する。
  await page.getByRole("button", { name: "ログイン" }).click();
  await expect(page.getByText("ログインユーザーIDとパスワードを入力してください。")).toBeVisible();
  expect(auth.loginCalls).toHaveLength(0);

  await page.getByLabel("ログインユーザーID").fill("admin.user");
  await page.getByLabel("パスワード").fill("WrongPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();

  await expect(page.getByText("ログインユーザーIDまたはパスワードを確認してください。")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByLabel("ログインユーザーID")).toHaveValue("admin.user");
});

// desktop と mobile（375px）の project で実行する。
test("ログインの試行が多すぎるときは 429 の文をそのまま出し、再送しない", async ({ page }) => {
  const auth = await mockAuthApi(page, null);

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("system_admin");
  await page.getByLabel("パスワード").fill("TooMany!123");
  await page.getByRole("button", { name: "ログイン" }).click();

  const error = page.getByText("ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。", { exact: true });
  await expect(error).toBeVisible();
  await expect(error).not.toContainText("login-rate-limited");
  await expect(page).toHaveURL(/\/login$/);
  expect(auth.loginCalls).toHaveLength(1);
  await expectNoPageOverflow(page);
});

test("初回ログインは強制パスワード変更へ移り、変更後は CSRF 付きで送ってログインへ戻る", async ({
  page,
  context,
  baseURL,
}) => {
  const auth = await mockAuthApi(page, null);
  await context.addCookies([{ name: "pr_csrf", value: "csrf-e2e-token", url: baseURL ?? "http://127.0.0.1:3100" }]);

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("first.user");
  await page.getByLabel("パスワード").fill("TemporaryPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();

  await expect(page).toHaveURL(/\/password\/change$/);
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ログインへ戻る" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
  // 強制変更中は他の画面を開いてもパスワード変更へ戻す。
  await page.goto("/search");
  await expect(page).toHaveURL(/\/password\/change$/);
  await expectNoPageOverflow(page);

  await page.getByLabel("現在のパスワード").fill("TemporaryPass!123");
  await page.locator("#auth-password-new").fill("IndependentPass!456");
  await page.getByLabel("新しいパスワード（確認）").fill("IndependentPass!456");
  await page.getByRole("button", { name: "パスワードを変更" }).click();

  await expect(page.getByText("パスワードを変更しました。新しいパスワードでログインしてください。")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
  expect(auth.passwordCalls).toEqual([
    {
      body: { current_password: "TemporaryPass!123", new_password: "IndependentPass!456" },
      csrf: "csrf-e2e-token",
    },
  ]);
});

test("ログイン中に API が 401 を返したらログイン画面へ戻す", async ({ page }) => {
  await mockAuthApi(page, loginTarget("admin.user"));
  let expired = false;
  await page.route("**/api/documents**", (route) =>
    expired
      ? route.fulfill({ status: 401, json: unauthorized })
      : route.fulfill({
          json: apiEnvelope({ items: [], total: 0, limit: 50, offset: 0, has_next: false }),
        })
  );

  await page.goto("/settings/appearance");
  const sidebar = await openSidebarNav(page);
  // 文書インデックスは既定で閉じている「ナレッジ構築」にある（#1563）。
  await expandSidebarSections(sidebar, "ナレッジ構築");

  expired = true;
  await sidebar.getByRole("link", { name: "文書インデックス" }).click();

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "システムにログイン" })).toBeVisible();
});

test("起動時のログインの確認が 503 ならログイン画面へ移さず、再試行で元の画面を開く（#1061）", async ({ page }) => {
  await mockAuthApi(page, loginTarget("admin.user"));
  let unavailable = true;
  // mockAuthApi の後に登録した route が優先される。
  await page.route("**/api/auth/me", (route) =>
    unavailable
      ? route.fulfill({
          status: 503,
          json: { data: null, error_messages: ["一時的に利用できません。"], warning_messages: [] },
        })
      : route.fallback()
  );

  await page.goto("/settings/appearance");
  const error = page.getByTestId("auth-session-error");
  await expect(error).toContainText("ログインの状態を確認できませんでした。");
  await expect(error).toContainText("一時的に利用できません。");
  await expect(page).toHaveURL(/\/settings\/appearance$/);
  await expectNoPageOverflow(page);

  unavailable = false;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByRole("heading", { name: "外観と証明書", level: 1 })).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/appearance$/);
});

test("アカウントの欄からアカウントの画面を開き、タブを切り替えてパスワードを変える（#1873）", { tag: "@responsive" }, async ({
  page,
  context,
  baseURL,
}) => {
  const auth = await mockAuthApi(page, loginTarget("admin.user"));
  await context.addCookies([{ name: "pr_csrf", value: "csrf-e2e-token", url: baseURL ?? "http://127.0.0.1:3100" }]);

  await page.goto("/settings/appearance");
  // 375px ではナビがドロワー（#367）。開いて文言付きの操作を使う。
  const sidebar = await openSidebarNav(page);
  // アカウント欄の 1 行目はログイン ID。表示名はほかの画面で使い、アカウント欄には出さない（#1696）。
  await expect(sidebar.getByText("admin.user", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("管理 太郎")).toHaveCount(0);
  // 個人の操作（API キー・パスワード変更）はサイドナビの行に出さず、欄からアカウントの画面へ移る。
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  await expect(sidebar.getByRole("button", { name: "API キー" })).toHaveCount(0);
  const accountLink = sidebar.getByRole("link", { name: "アカウント設定を開く（admin.user）" });
  expect((await accountLink.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await accountLink.click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByRole("heading", { name: "アカウント", level: 1 })).toBeVisible();
  await expect(page.getByText("admin.user（ロール: ")).toBeVisible();
  // 375px のドロワーは押すと閉じて移る。開き直すと欄が現在地。
  const reopened = await openSidebarNav(page);
  await expect(reopened.getByTestId("sidebar-account")).toHaveAttribute("aria-current", "page");
  if (await page.getByTestId("nav-drawer-trigger").isVisible()) await page.keyboard.press("Escape");

  // タブ（API キー / パスワード変更）は ← → / Home / End で移り、URL に残す。
  const apiKeysTab = page.getByRole("tab", { name: "API キー" });
  const passwordTab = page.getByRole("tab", { name: "パスワード変更" });
  await expect(apiKeysTab).toHaveAttribute("aria-selected", "true");
  await apiKeysTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(passwordTab).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\/account\?tab=password$/);
  await page.keyboard.press("Home");
  await expect(apiKeysTab).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(passwordTab).toHaveAttribute("aria-selected", "true");
  await page.reload();
  await expect(page.getByRole("tab", { name: "パスワード変更" })).toHaveAttribute("aria-selected", "true");
  await expectNoPageOverflow(page);

  // パスワードの変更は CSRF 付きで送り、新しいパスワードでのログインへ移る。
  await page.getByLabel("現在のパスワード").fill("Current!Pass123");
  await page.locator("#auth-password-new").fill("NewPass!Word456");
  await page.getByLabel("新しいパスワード（確認）").fill("NewPass!Word456");
  await page.getByTestId("account-password-submit").click();
  await expect(page).toHaveURL(/\/login$/);
  expect(auth.passwordCalls).toHaveLength(1);
  expect(auth.passwordCalls[0].body).toEqual({ current_password: "Current!Pass123", new_password: "NewPass!Word456" });
  expect(auth.passwordCalls[0].csrf).toBe("csrf-e2e-token");
});

test("アカウント欄のログアウト", async ({ page }) => {
  const auth = await mockAuthApi(page, loginTarget("admin.user"));

  await page.goto("/settings/appearance");
  const sidebar = await openSidebarNav(page);
  await sidebar.getByRole("button", { name: "ログアウト" }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(auth.logoutCalls).toBe(1);
});

test("ログイン画面の「パスワードを忘れた場合」は管理者への依頼を案内する（#1873。メールの送信は無い）", { tag: "@responsive" }, async ({ page }) => {
  await mockAuthApi(page, null);

  await page.goto("/login");
  const help = page.getByTestId("auth-login-forgot-password-help");
  await expect(help).toBeHidden();
  await page.getByText("パスワードを忘れた場合").click();
  await expect(help).toBeVisible();
  await expect(help).toContainText("管理者にパスワードのリセット（一時パスワードの発行）を依頼してください。");
  await expectNoPageOverflow(page);
});

/** 修正前（#1644）のアカウント欄の名前・ロールの行の高さ（px）。欄を今より高くしない。 */
const ACCOUNT_ROW_HEIGHT_BEFORE = 50;

/** アカウント欄（名前とロールの 2 行）の各行の行数・省略・サイドナビの内側に収まるかを測る。 */
async function measureAccountFooter(page: Page) {
  return page.getByTestId("sidebar-account").evaluate((row) => {
    const sidebar = row.closest("aside")!.getBoundingClientRect();
    const footer = row.closest("aside > div")!.getBoundingClientRect();
    const lines = Array.from(row.querySelectorAll<HTMLElement>("[title]")).map((element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      // 見える文字（読み上げだけの sr-only を除く）。
      const visible = Array.from(element.childNodes)
        .filter((node) => !(node instanceof HTMLElement && node.classList.contains("sr-only")))
        .map((node) => node.textContent)
        .join("");
      return {
        text: visible,
        title: element.getAttribute("title"),
        lines: Math.round(rect.height / Number.parseFloat(style.lineHeight)),
        truncated: element.scrollWidth > element.clientWidth + 1,
        ellipsis: style.textOverflow === "ellipsis",
        insideSidebar: rect.left >= sidebar.left && rect.right <= sidebar.right + 0.5,
      };
    });
    return { height: row.getBoundingClientRect().height, footerHeight: footer.height, lines };
  });
}

/** アカウント欄の操作（ログアウトなど）と名前の行が、サイドナビの右端からはみ出していないか。 */
async function expectAccountFooterInsideSidebar(page: Page) {
  const overflow = await page.getByRole("complementary", { name: "サイドナビゲーション" }).evaluate((sidebar) => {
    const right = sidebar.getBoundingClientRect().right;
    const footer = sidebar.querySelector(":scope > div:last-child")!;
    return Array.from(footer.querySelectorAll("button, [data-testid='sidebar-account']")).map(
      (element) => element.getBoundingClientRect().right - right
    );
  });
  for (const value of overflow) expect(value).toBeLessThanOrEqual(0.5);
}

for (const theme of ["light", "dark"] as const) {
  test(`アカウント欄はログイン ID とロールの表示名を 1 行ずつ出し、長い行だけ省略記号で切って title に全文を出す（${theme}。#1644・#1696）`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    // 外観の選好（共有 UI の ui-store が保存する値）を読み込みの前に入れておく。
    await page.addInitScript((value) => {
      window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
    }, theme);
    const user = (loginUserId: string, displayName: string, roles: [code: string, name: string][]) =>
      dbUser({
        login_user_id: loginUserId,
        display_name: displayName,
        role_codes: roles.map(([code]) => code),
        role_display_names: roles.map(([, name]) => name),
        is_system_admin: true,
        permissions: ALL_PERMISSION_CODES,
        allowed_search_answer_profile_ids: null,
        allowed_knowledge_base_ids: null,
      });
    // 構成のシステム管理者: 1 行目はログイン ID（表示名「システム管理者」ではない。#1696）、2 行目はロールの表示名（code ではない）。
    const auth = await mockAuthApi(page, user("system_admin", "システム管理者", [["SYSTEM_ADMIN", "システム管理者"]]));

    await page.goto("/settings/appearance");
    await expect.poll(() => page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(theme === "dark");
    const sidebar = await openSidebarNav(page);
    await expect(sidebar.getByText("system_admin", { exact: true })).toBeVisible();
    await expect(sidebar.getByText("システム管理者", { exact: true })).toBeVisible();
    // ロールの code（SYSTEM_ADMIN）は出さない。
    await expect(sidebar.getByText(/SYSTEM_ADMIN/)).toHaveCount(0);
    const short = await measureAccountFooter(page);
    await page.getByTestId("sidebar-account").screenshot({ path: testInfo.outputPath(`sidebar-account-login-id-${theme}.png`) });
    expect(short.lines).toEqual([
      expect.objectContaining({ text: "system_admin", title: "system_admin", lines: 1, truncated: false, insideSidebar: true }),
      expect.objectContaining({ text: "システム管理者", title: "ロール: システム管理者", lines: 1, truncated: false, insideSidebar: true }),
    ]);
    // 読み上げはロールの行も「ロール: …」の全文。
    await expect(page.getByTestId("sidebar-account")).toContainText("ロール: システム管理者");
    // 欄は修正前より高くしない。
    expect(short.height).toBeLessThanOrEqual(ACCOUNT_ROW_HEIGHT_BEFORE);
    await expectAccountFooterInsideSidebar(page);

    // 1 行に入らない名前・ロールは折り返さず、各行の末尾を省略記号で切る。全文は title と DOM の文字（読み上げ）に残り、
    // 欄の高さは変わらず、右へはみ出さない。複数のロールは表示名を「、」でつなぐ。
    // 1 行目はログイン ID。表示名（長い日本語の名前）はアカウント欄に出さない（#1696）。
    const longDisplayName = "山田 太郎 ナレッジベース運用責任者 品質評価と監査の担当 第二事業部";
    const longName = "yamada.taro.knowledge-base-operations-lead.quality-audit.div2";
    const longRoles: [string, string][] = [
      ["SYSTEM_ADMIN", "システム管理者"],
      ["RAG_KNOWLEDGE_ADMIN", "ナレッジベース管理者"],
      ["RAG_EVALUATION_REVIEWER", "品質評価の確認者"],
    ];
    auth.current = user(longName, longDisplayName, longRoles);
    await page.reload();
    const reloaded = await openSidebarNav(page);
    await expect(reloaded.getByText(longName, { exact: true })).toBeVisible();
    await expect(reloaded.getByText(longDisplayName)).toHaveCount(0);
    const long = await measureAccountFooter(page);
    const longRoleNames = "システム管理者、ナレッジベース管理者、品質評価の確認者";
    expect(long.lines).toEqual([
      { text: longName, title: longName, lines: 1, truncated: true, ellipsis: true, insideSidebar: true },
      { text: longRoleNames, title: `ロール: ${longRoleNames}`, lines: 1, truncated: true, ellipsis: true, insideSidebar: true },
    ]);
    expect(Math.abs(long.height - short.height)).toBeLessThanOrEqual(0.5);
    expect(Math.abs(long.footerHeight - short.footerHeight)).toBeLessThanOrEqual(0.5);
    await expectAccountFooterInsideSidebar(page);
    await expectNoPageOverflow(page);

    if (!(await page.getByTestId("nav-drawer-trigger").isVisible())) {
      // 折りたたみ（アイコンだけ）のサイドナビは名前を出さず（アイコンの行のリンクだけ。#1873）、操作はアイコンの列に収まる。
      await reloaded.getByRole("button", { name: "サイドバーを折りたたむ" }).click();
      await expect(reloaded).toHaveAttribute("data-state", "collapsed");
      await expect(reloaded.getByTestId("sidebar-account")).toHaveAttribute("aria-label", `アカウント設定を開く（${longName}）`);
      await expect(reloaded.getByText(longRoleNames)).toHaveCount(0);
      await expectAccountFooterInsideSidebar(page);
    }
  });
}

test("ローカル DEBUG はログインせずに全画面を使え、アカウント欄にログイン省略を示す（#307）", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: apiEnvelope({
        ...dbUser({
          user_uuid: "00000000-0000-0000-0000-000000000000",
          login_user_id: "local",
          display_name: "ローカル利用者",
          role_codes: ["SYSTEM_ADMIN"],
          role_display_names: ["システム管理者"],
          is_system_admin: true,
          permissions: ALL_PERMISSION_CODES,
          allowed_search_answer_profile_ids: null,
          allowed_knowledge_base_ids: null,
          password_change_allowed: false,
        }),
        debug_mode: true,
      }),
    })
  );

  // `/` はナビの並びで最初に開ける画面（チャット。#399 でナビの先頭をチャットにした）へ。
  await page.goto("/");
  await expect(page).toHaveURL(/\/chat$/);
  const sidebar = await openSidebarNav(page);
  // NL2SQL と同じく、利用者名とログイン省略の表示を出し、パスワード変更・ログアウトは出さない。
  await expect(sidebar.getByText("ローカル利用者")).toBeVisible();
  await expect(sidebar.getByRole("status", { name: "ログイン省略" })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "ログアウト" })).toHaveCount(0);
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  // アカウントの画面のパスワード変更のタブは、ローカルの認証ではパスワードを使わない説明だけ（#1873）。
  await page.goto("/account?tab=password");
  await expect(page.getByText("ローカルの認証（ログインの省略）ではパスワードを使いません。")).toBeVisible();
  await expect(page.getByLabel("現在のパスワード")).toHaveCount(0);

  // ログイン画面を開いても既定の入口（チャット）へ戻す。
  await page.goto("/login");
  await expect(page).toHaveURL(/\/chat$/);
});
