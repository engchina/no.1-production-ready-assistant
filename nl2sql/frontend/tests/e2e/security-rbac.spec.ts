import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { mockDatabaseGateReady, profileAccessPage, systemAdminMe } from "./_helpers/database-gate";

function envelope(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

function problemEnvelope({
  status,
  code,
  title,
  detail,
  requestId,
  retryable = false,
  fieldErrors = [],
}: {
  status: number;
  code: string;
  title: string;
  detail: string;
  requestId: string;
  retryable?: boolean;
  fieldErrors?: Array<{ pointer: string; code: string; message: string }>;
}) {
  return {
    data: null,
    error_messages: [detail],
    warning_messages: [],
    error_code: code,
    problem: {
      type: `urn:nl2sql:problem:${code.toLowerCase().replaceAll("_", "-")}`,
      title,
      status,
      detail,
      code,
      request_id: requestId,
      retryable,
      field_errors: fieldErrors,
    },
  };
}

async function fulfill(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(status >= 400 ? { detail: data } : envelope(data)),
  });
}

async function topLevelPanelStyle(page: Page, id: string, prefix: "security-users" | "security-roles") {
  const panel = page.locator(`#${prefix}-panel-${id}`);
  await expect(panel).toBeVisible();
  return panel.evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      backgroundColor: style.backgroundColor,
      borderTopWidth: style.borderTopWidth,
      borderRadius: style.borderRadius,
      paddingTop: style.paddingTop,
      boxShadow: style.boxShadow,
    };
  });
}

async function expectNoPageHorizontalScroll(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1 &&
          document.body.scrollWidth <= document.body.clientWidth + 1
      )
    )
    .toBeTruthy();
}

async function expectNoElementHorizontalOverflow(locator: Locator) {
  await expect
    .poll(() =>
      locator.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)
    )
    .toBeTruthy();
}

async function expectBoundedSecurityTableScroll(page: Page, scrollRegion: Locator) {
  // 一覧 + 詳細（ListDetailLayout。#2282）の一覧の列は sticky で画面の高さの中に収まり、行は列の中の表で
  // 縦にスクロールする（visibleRows の 8 行の固定の高さにしない。表頭は固定）。
  const metrics = await scrollRegion.evaluate((node) => {
    const header = node.querySelector("thead");
    const column = node.closest('[data-testid^="list-detail-"][data-testid$="-list"]');
    if (!header || !column) throw new Error("security table header or list column is missing");
    const computed = window.getComputedStyle(node);
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      columnHeight: column.getBoundingClientRect().height,
      viewportHeight: window.innerHeight,
      overflowY: computed.overflowY,
      headerPosition: window.getComputedStyle(header).position,
    };
  });
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  // 列の高さは画面の高さまで（sticky で本文をスクロールしても一覧が見え続ける）。
  expect(metrics.columnHeight).toBeLessThanOrEqual(metrics.viewportHeight);
  expect(metrics.headerPosition).toBe("sticky");
  expect(metrics.overflowY).toBe("auto");

  await scrollRegion.evaluate((node) => node.scrollTo({ top: 0, left: 0 }));
  await scrollRegion.focus();
  await expect(scrollRegion).toBeFocused();
  await scrollRegion.press("PageDown");
  await expect.poll(() => scrollRegion.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);

  await scrollRegion.evaluate((node) => node.scrollTo({ top: node.scrollHeight }));
  const bottomState = await scrollRegion.evaluate((node) => {
    const regionRect = node.getBoundingClientRect();
    const header = node.querySelector("thead");
    const lastRow = node.querySelector("tbody tr:last-child");
    if (!header || !lastRow) throw new Error("security table rows are missing");
    const headerRect = header.getBoundingClientRect();
    const lastRowRect = lastRow.getBoundingClientRect();
    return {
      headerOffset: Math.abs(headerRect.top - regionRect.top),
      lastRowVisible:
        lastRowRect.top >= headerRect.bottom - 1 &&
        lastRowRect.bottom <= regionRect.bottom + 1,
    };
  });
  expect(bottomState.headerOffset).toBeLessThanOrEqual(1);
  expect(bottomState.lastRowVisible).toBe(true);
  await expectNoPageHorizontalScroll(page);
}

async function expectFloatingMenuInsideViewport(page: Page, menu: Locator) {
  await expect(menu).toBeVisible();
  const [box, viewport] = await Promise.all([menu.boundingBox(), page.viewportSize()]);
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width + 1);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height + 1);
  await expect
    .poll(() =>
      menu.evaluate((node) => ({
        constrained: node.getAttribute("data-floating-menu-constrained"),
        fitsWithoutScrollbar: node.scrollHeight <= node.clientHeight + 1,
      }))
    )
    .toEqual({ constrained: null, fitsWithoutScrollbar: true });
}

async function sidebarComparableStyle(locator: Locator) {
  await expect(locator).toBeVisible();
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return {
      height: Math.round(rect.height),
      borderRadius: style.borderRadius,
      fontSize: style.fontSize,
      paddingLeft: style.paddingLeft,
      paddingRight: style.paddingRight,
      transitionProperty: style.transitionProperty,
    };
  });
}

async function expectSidebarActionMatchesMenuItem(action: Locator, menuItem: Locator) {
  const [actionStyle, menuStyle] = await Promise.all([
    sidebarComparableStyle(action),
    sidebarComparableStyle(menuItem),
  ]);
  expect(actionStyle).toEqual(menuStyle);
}

const systemRole = {
  role_id: "role-system",
  role_code: "SYSTEM_ADMIN",
  display_name: "システム管理者",
  description: "組み込み",
  is_built_in: true,
  archived: false,
  version: 1,
  permissions: [],
  data_entitlements: [],
  allowed_profile_ids: [],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), []))
  );
});

test("ユーザー管理の migration 未適用は初回 ErrorState、再取得失敗は既存データ保持で示す", async ({
  page,
}, testInfo) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole])
  );
  const recoveredUser = {
    user_uuid: "migration-recovered-user",
    login_user_id: "migration.recovered",
    display_name: "復旧確認ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [systemRole.role_id],
    is_bootstrap_admin: false,
  };
  let userRequests = 0;
  let allowUsersSuccess = false;
  let backgroundFailure = false;
  await page.route("**/api/security/users", async (route) => {
    userRequests += 1;
    if (allowUsersSuccess && !backgroundFailure) {
      await fulfill(route, [recoveredUser]);
      return;
    }
    const requestId = backgroundFailure
      ? "users-migration-refresh-request"
      : "users-migration-initial-request";
    const response = problemEnvelope({
      status: 409,
      code: "SECURITY_SCHEMA_MIGRATION_REQUIRED",
      title: "セキュリティ初期化が必要です",
      detail:
        "アプリケーション認証/RBAC の schema migration が未適用です。`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` を実行してから再試行してください。",
      requestId,
    });
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      headers: { "X-Request-ID": requestId },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/users");

  const initialError = page.locator("main").getByRole("alert");
  await expect(initialError).toHaveCount(1);
  await expect(initialError).toContainText(
    "アプリケーション認証/RBAC の schema migration が未適用です。"
  );
  await expect(initialError).toContainText("リクエストID: users-migration-initial-request");
  await expect(page.getByTestId("security-users-grid")).toHaveCount(0);
  await expect(page.getByText("データがありません", { exact: true })).toHaveCount(0);
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({
    path: testInfo.outputPath(`security-users-migration-error-${testInfo.project.name}.png`),
    fullPage: true,
  });

  const retry = initialError.getByRole("button", { name: "再試行" });
  await retry.focus();
  await expect(retry).toBeFocused();
  allowUsersSuccess = true;
  await retry.press("Enter");

  await expect(page.getByRole("button", { name: "復旧確認ユーザー を表示" })).toBeVisible();
  await expect(initialError).toHaveCount(0);
  expect(userRequests).toBeGreaterThanOrEqual(2);

  backgroundFailure = true;
  const userActions = page.getByTestId("security-users-actions");
  const directRefresh = userActions.getByRole("button", { name: "最新情報を取得" });
  if ((await directRefresh.count()) > 0) {
    await directRefresh.click();
  } else {
    await userActions.getByTestId("page-actions-more").click();
    await page.getByRole("menuitem", { name: "最新情報を取得" }).click();
  }
  const refreshError = page
    .locator("main")
    .getByRole("alert")
    .filter({ hasText: "users-migration-refresh-request" });
  await expect(refreshError).toBeVisible();
  await expect(page.getByRole("button", { name: "復旧確認ユーザー を表示" })).toBeVisible();
  await expect(page.getByTestId("security-users-grid")).toBeVisible();

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(refreshError).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー・ロール一覧は一覧の列（画面の高さまで）の中で固定表頭の内部スクロールになる", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await mockDatabaseGateReady(page);
  // 1 ページ（25 件）が一覧の列の高さを超える件数。
  const rowCount = 30;
  const singlePane = testInfo.project.name === "mobile-375";
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "SECURITY_VIEWER",
    display_name: "セキュリティ閲覧",
    description: "表示のみ",
    is_built_in: false,
    permissions: ["menu.security_users", "menu.security_roles"],
  };
  const users = Array.from({ length: rowCount }, (_, index) => {
    const sequence = String(index + 1).padStart(2, "0");
    return {
      user_uuid: `scroll-user-${sequence}`,
      login_user_id: `scroll.user.${sequence}`,
      display_name: `スクロールユーザー ${sequence}`,
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: [viewerRole.role_id],
      is_bootstrap_admin: false,
    };
  });
  const roles = Array.from({ length: rowCount }, (_, index) => {
    const sequence = String(index + 1).padStart(2, "0");
    return {
      ...viewerRole,
      role_id: `scroll-role-${sequence}`,
      role_code: `SCROLL_ROLE_${sequence}`,
      display_name: `スクロールロール ${sequence}`,
      version: index + 1,
    };
  });

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));

  await page.goto("/settings/security/users");
  const userScrollRegion = page.getByTestId("security-users-scroll-region");
  await expect(userScrollRegion).toHaveAttribute("role", "region");
  await expect(userScrollRegion).toHaveAttribute(
    "aria-label",
    "ユーザー一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(25);

  const lastUser = users.at(-1)!;
  await page.getByTestId("security-users-search").fill(lastUser.display_name);
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(1);
  await expect
    .poll(() =>
      userScrollRegion.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)
    )
    .toBeTruthy();
  await page.getByTestId("security-users-search").fill("");
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(25);
  await expectBoundedSecurityTableScroll(page, userScrollRegion);

  const lastUserRow = userScrollRegion.locator("tbody tr").last();
  await lastUserRow.locator("td").nth(1).click();
  await expect(page).toHaveURL(/[?&]user=scroll-user-25/u);
  await expect(
    page.getByRole("heading", { name: users[24].display_name, exact: true })
  ).toBeVisible();
  if (singlePane) {
    // xl 未満は 1 ペイン: 選ぶと詳細だけになり、一覧は隠れる。
    await expect(userScrollRegion).toBeHidden();
  } else {
    await expect(lastUserRow).toHaveAttribute("data-selected", "true");
  }
  await expectNoPageHorizontalScroll(page);

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, roles)
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  await page.goto("/settings/security/roles");
  const roleScrollRegion = page.getByTestId("security-roles-scroll-region");
  await expect(roleScrollRegion).toHaveAttribute("role", "region");
  await expect(roleScrollRegion).toHaveAttribute(
    "aria-label",
    "ロール一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(25);

  const lastRole = roles.at(-1)!;
  await page.getByTestId("security-roles-search").fill(lastRole.display_name);
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(1);
  await expect
    .poll(() =>
      roleScrollRegion.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)
    )
    .toBeTruthy();
  await page.getByTestId("security-roles-search").fill("");
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(25);
  await expectBoundedSecurityTableScroll(page, roleScrollRegion);

  const lastRoleRow = roleScrollRegion.locator("tbody tr").last();
  await lastRoleRow.locator("td").nth(1).click();
  await expect(page).toHaveURL(/[?&]role=scroll-role-25/u);
  // 詳細の見出しはロール名（コード・ID は出さない。#1682）。
  await expect(
    page.getByRole("heading", { name: roles[24].display_name, exact: true })
  ).toBeVisible();
  if (!singlePane) await expect(lastRoleRow).toHaveAttribute("data-selected", "true");
  await expectNoPageHorizontalScroll(page);
});

test("ローカル DEBUG はログインせず SYSTEM_ADMIN として入り、状態を明示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "00000000-0000-0000-0000-000000000000",
      login_user_id: "local-debug",
      display_name: "ローカル DEBUG 管理者",
      debug_mode: true,
    })
  );

  await page.goto("/settings/appearance");

  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(page).toHaveURL(/\/settings\/appearance$/);
  await expect(page.getByRole("heading", { name: "システムにログイン" })).toHaveCount(0);
  await expect(
    sidebar.getByRole("status", {
      name: "ログイン省略",
    })
  ).toBeVisible();
  const debugColors = await sidebar
    .getByRole("status", { name: "ログイン省略" })
    .evaluate((element) => {
      const style = getComputedStyle(element);
      return { backgroundColor: style.backgroundColor, color: style.color };
    });
  // 共有 Sidebar は常時暗いスラブ(data-surface="inverted" → color-scheme: dark)なので、
  // globals.css の warning トークンはダーク値に解決される(旧実装はライトの琥珀色を直書きしていた)。
  // --color-warning-subtle = light-dark(amber-200, amber-900 #3a2e13)
  // --color-warning-fg     = light-dark(amber-700, amber-300 #f4c95d)
  // (packages/ui/src/styles/tokens/colors.css, palette.css)
  expect(debugColors).toEqual({ backgroundColor: "rgb(58, 46, 19)", color: "rgb(244, 201, 93)" });
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  await expect(sidebar.getByRole("button", { name: "ログアウト" })).toHaveCount(0);
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});

test("外観と証明書のページは専用権限だけで表示でき、権限なしでは直達できない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "appearance-viewer",
      login_user_id: "appearance.viewer",
      display_name: "外観閲覧ユーザー",
      role_codes: ["APPEARANCE_VIEWER"],
      is_system_admin: false,
      permissions: ["menu.settings_appearance"],
      password_change_allowed: true,
    })
  );

  await page.goto("/");
  await expect(page).toHaveURL(/\/settings\/appearance$/);
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "外観と証明書" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);

  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "no-access",
      login_user_id: "no.access",
      display_name: "権限なしユーザー",
      role_codes: ["NO_ACCESS"],
      is_system_admin: false,
      permissions: [],
      password_change_allowed: true,
    })
  );

  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

test("ログイン失敗を一般化して表示し、初回パスワード変更へ誘導する", async ({ page }) => {
  let loginAttempts = 0;
  await page.route("**/api/auth/me", (route) => fulfill(route, "ログインしてください。", 401));
  await page.route("**/api/auth/login", async (route) => {
    loginAttempts += 1;
    if (loginAttempts === 1) {
      // 実バックエンドと同じく request id 付きの 401 を返す(ログイン画面では非表示にする)
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        headers: { "X-Request-ID": "login-failed-request" },
        body: JSON.stringify(
          problemEnvelope({
            status: 401,
            code: "SECURITY_LOGIN_FAILED",
            title: "ログインできません",
            detail: "ログインユーザーIDまたはパスワードを確認してください。",
            requestId: "login-failed-request",
          })
        ),
      });
      return;
    }
    await fulfill(route, { ...systemAdminMe, force_password_change: true, password_change_allowed: true });
  });
  await page.route("**/api/auth/password/change", (route) => fulfill(route, { changed: true }));

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("SYSTEM");
  await page.getByLabel("パスワード").fill("WrongPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  const loginError = page.getByText("ログインユーザーIDまたはパスワードを確認してください。", { exact: true });
  await expect(loginError).toBeVisible();
  await expect(loginError).not.toContainText("リクエストID");
  await expect(loginError).not.toContainText("login-failed-request");

  await page.getByLabel("パスワード").fill("BootstrapPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ログインへ戻る" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);

  await page.getByLabel("現在のパスワード").fill("BootstrapPass!123");
  await page.locator("#auth-password-new").fill("IndependentPass!456");
  await page.getByLabel("新しいパスワード（確認）").fill("IndependentPass!456");
  await page.getByRole("button", { name: "パスワードを変更" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("ログインの試行が多すぎるときは 429 の文をそのまま出し、再送しない", async ({ page }) => {
  let loginAttempts = 0;
  await page.route("**/api/auth/me", (route) => fulfill(route, "ログインしてください。", 401));
  await page.route("**/api/auth/login", async (route) => {
    loginAttempts += 1;
    // 試行の回数の上限（#1087）。backend は request id と Retry-After 付きの 429 を返す。
    await route.fulfill({
      status: 429,
      contentType: "application/json",
      headers: { "X-Request-ID": "login-rate-limited", "Retry-After": "900" },
      body: JSON.stringify(
        problemEnvelope({
          status: 429,
          code: "SECURITY_RATE_LIMITED",
          title: "リクエストが多すぎます",
          detail: "ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。",
          requestId: "login-rate-limited",
          retryable: true,
        })
      ),
    });
  });

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("system_admin");
  await page.getByLabel("パスワード").fill("WrongPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  const loginError = page.getByText("ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。", { exact: true });
  await expect(loginError).toBeVisible();
  await expect(loginError).not.toContainText("リクエストID");
  await expect(page).toHaveURL(/\/login$/);
  expect(loginAttempts).toBe(1);
});

test("構成管理者はアカウントの欄からアカウントの画面を開け、サイドバー操作は安定した高さを保つ", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.goto("/query");

  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  const menuItem = sidebar.getByRole("link", { name: "SQL 生成" });
  // パスワード変更はサイドナビの行に無く、アカウントの欄（行全体のリンク）から開くアカウントの画面のタブ（#1873）。
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  const accountLink = sidebar.getByRole("link", { name: /^アカウント設定を開く/ });
  const logoutButton = sidebar.getByRole("button", { name: "ログアウト" });
  await expect(accountLink).toBeVisible();
  expect((await accountLink.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await expectSidebarActionMatchesMenuItem(logoutButton, menuItem);

  // 初回・リセット後の強制の変更の単独の画面はそのまま。

  await page.goto("/password/change");
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByLabel("現在のパスワード")).toBeVisible();
  await expect(page.locator("#auth-password-new")).toBeVisible();
  await expect(page.getByLabel("新しいパスワード（確認）")).toBeVisible();
  await expect(page.getByRole("button", { name: "戻る" })).toBeVisible();
});

test("通常ユーザーはアカウントの欄からパスワード変更のタブを開き、元の画面へ戻れる", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "query-user",
      login_user_id: "query.user",
      display_name: "SQL 生成ユーザー",
      role_codes: ["QUERY_MENU"],
      is_system_admin: false,
      permissions: ["menu.query"],
      password_change_allowed: true,
    })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [],
      next_cursor: null,
      total: 0,
      table_count: 0,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: [],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    })
  );

  await page.goto("/query");
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  const menuItem = sidebar.getByRole("link", { name: "SQL 生成" });
  const accountLink = sidebar.getByRole("link", { name: "アカウント設定を開く（query.user）" });
  const logoutButton = sidebar.getByRole("button", { name: "ログアウト" });
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  await expect(accountLink).toBeVisible();
  await expect(logoutButton).toBeVisible();
  await expectSidebarActionMatchesMenuItem(logoutButton, menuItem);

  // アカウントの欄はログアウトの上に重ならずに並び、ログアウトの文言は折り返さない。
  const footerLayout = await Promise.all([accountLink, logoutButton].map((element) => element.boundingBox()));
  expect((footerLayout[1]?.y ?? 0) - ((footerLayout[0]?.y ?? 0) + (footerLayout[0]?.height ?? 0))).toBeGreaterThanOrEqual(0);
  await expect(logoutButton.locator("span").last()).toHaveCSS("white-space", "nowrap");

  // 375px のドロワーは押すと閉じて移る（#1873）。
  await accountLink.click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByRole("heading", { name: "アカウント", level: 1 })).toBeVisible();
  await page.getByRole("tab", { name: "パスワード変更" }).click();
  await expect(page).toHaveURL(/\/account\?tab=password$/);
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByLabel("現在のパスワード")).toBeVisible();
  await expect(page.getByTestId("account-password-submit")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1))
    .toBe(true);
  await page.goBack();
  await expect(page).toHaveURL(/\/query$/);
});

test("強制パスワード変更中の戻る操作はログインへ戻す", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "forced-user",
      login_user_id: "forced.user",
      display_name: "初回ユーザー",
      role_codes: ["QUERY_MENU"],
      is_system_admin: false,
      permissions: ["menu.query"],
      force_password_change: true,
      password_change_allowed: true,
    })
  );
  await page.route("**/api/auth/logout", (route) => fulfill(route, { logged_out: true }));

  await page.goto("/password/change");
  await expect(page.getByRole("button", { name: "ログインへ戻る" })).toBeVisible();
  await page.getByRole("button", { name: "ログインへ戻る" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("SQL 生成だけのユーザーは profile を利用できるが管理メニューには入れない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  const limited = {
    ...systemAdminMe,
    user_uuid: "limited",
    login_user_id: "limited.user",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: ["default"],
    password_change_allowed: true,
  };
  let usageContextRequested = false;
  let fullProfileRequested = false;
  let persistenceRequested = false;
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/persistence", (route) => {
    persistenceRequested = true;
    return fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-08-20T00:00:00Z",
    });
  });
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [
        {
          owner: "APP",
          object_name: "DEPARTMENT",
          object_type: "TABLE",
          logical_name: "部署",
          comment: "部署情報",
          row_count: 3,
          column_count: 2,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      table_count: 1,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) => {
    usageContextRequested = true;
    return fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: ["APP.DEPARTMENT"],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    });
  });
  await page.route("**/api/nl2sql/profiles/default", (route) => {
    fullProfileRequested = true;
    return fulfill(route, "この機能を利用する権限がありません。", 403);
  });
  await page.route("**/api/security/users", (route) =>
    fulfill(route, "この機能を利用する権限がありません。", 403)
  );
  await page.goto("/query");
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toBeVisible();
  await expect(page.getByRole("button", { name: "SQL を生成して実行" })).toBeVisible();
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await expect(page).toHaveURL(/\/query$/);
  await expect(sidebar.getByText("業務プロファイル", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("ユーザー管理", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("セキュリティ管理", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "SELECT SQL を実行" })).toHaveCount(0);
  await expect(sidebar.getByText("管理 SQL を実行", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "スキーマを更新" })).toHaveCount(0);
  await expect.poll(() => persistenceRequested).toBeTruthy();
  await expect.poll(() => usageContextRequested).toBeTruthy();
  expect(fullProfileRequested).toBe(false);

  await page.goto("/direct-sql");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  await page.goto("/admin-sql");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  await page.evaluate(() => {
    window.history.pushState({}, "", "/profiles");
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
  });
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  const apiStatus = await page.evaluate(async () => (await fetch("/api/security/users")).status);
  expect(apiStatus).toBe(403);
  const profileSearchStatus = await page.evaluate(
    async () => (await fetch("/api/nl2sql/profiles/search")).status
  );
  expect(profileSearchStatus).toBe(200);
  const fullProfileStatus = await page.evaluate(
    async () => (await fetch("/api/nl2sql/profiles/default")).status
  );
  expect(fullProfileStatus).toBe(403);

  await page.goto("/settings/security/users");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

test("SQL 生成だけのユーザーは結果接地で ontology 管理 API の 403 に遷移しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/rewrite", (route) => {
    // rewrite 既定 ON。未モックだと submit が最初の rewrite で失敗する。
    const body = route.request().postDataJSON() as { question?: string };
    const original = String(body?.question ?? "");
    return fulfill(route, {
      original_question: original,
      rewritten_question: original,
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  const limited = {
    ...systemAdminMe,
    user_uuid: "query-only-ontology-result",
    login_user_id: "query.only.ontology",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: ["default"],
    password_change_allowed: true,
  };
  const createdAt = "2026-08-20T00:00:00Z";
  const question = "部署ごとの人数を表示";
  const generatedSql =
    'SELECT "d"."DEPARTMENT_NAME", COUNT(*) AS "EMPLOYEE_COUNT" FROM "APP"."DEPARTMENT" "d" GROUP BY "d"."DEPARTMENT_NAME"';
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "sql-table-department",
        owner: "APP",
        name: "DEPARTMENT",
        alias: "d",
        qualified_name: "APP.DEPARTMENT",
        source_sql: '"APP"."DEPARTMENT" "d"',
      },
    ],
    columns: [
      {
        id: "sql-column-department-name",
        table: "d",
        name: "DEPARTMENT_NAME",
        expression_sql: '"d"."DEPARTMENT_NAME"',
      },
    ],
    joins: [],
    projections: [],
    filters: [],
    aggregates: [{ id: "sql-aggregate-count", expression_sql: "COUNT(*)" }],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    parse_warnings: [],
  };
  const ontologyGraph = {
    revision_id: "revision-query-result",
    revision: {
      id: "revision-query-result",
      version: 1,
      status: "published",
      schema_fingerprint: "schema-v1",
      etag: "revision-query-result-etag",
    },
    nodes: [
      {
        id: "department-table",
        kind: "table",
        technical_name: "APP.DEPARTMENT",
        business_name_ja: "部署情報",
        review_status: "approved",
        metadata: { owner: "APP", object_name: "DEPARTMENT" },
      },
      {
        id: "department-name",
        kind: "column",
        technical_name: "APP.DEPARTMENT.DEPARTMENT_NAME",
        business_name_ja: "部署名",
        review_status: "approved",
        metadata: { owner: "APP", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" },
      },
    ],
    edges: [
      {
        id: "department-name-column",
        kind: "column",
        source_node_id: "department-table",
        target_node_id: "department-name",
        relationship_name_ja: "列",
        review_status: "approved",
      },
    ],
  };
  let ontologyViewRequests = 0;

  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me**", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: createdAt,
    })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: createdAt,
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [
        {
          owner: "APP",
          object_name: "DEPARTMENT",
          object_type: "TABLE",
          logical_name: "部署",
          comment: "部署情報",
          row_count: 3,
          column_count: 2,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      table_count: 1,
      view_count: 0,
      counts_included: true,
      refreshed_at: createdAt,
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: createdAt,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: ["APP.DEPARTMENT"],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: createdAt,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) => {
    ontologyViewRequests += 1;
    return fulfill(route, "この機能を利用する権限がありません。", 403);
  });
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfill(route, {
      job_id: "job-query-ontology-403",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-query-ontology-403", (route) =>
    fulfill(route, {
      job_id: "job-query-ontology-403",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 40,
      error_message: null,
      warning_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 4 },
        { stage: "format_results", status: "done", elapsed_ms: 2 },
      ],
      timing: null,
      result: {
        history_id: "hist-query-ontology-403",
        engine: "select_ai",
        engine_meta: { profile: "NL2SQL_DEFAULT_PROFILE" },
        fallback_reason: "",
        original_question: question,
        rewritten_question: question,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "部署ごとの人数を集計します。",
        safety: {
          is_safe: true,
          is_select_only: true,
          row_limit_applied: null,
          blocked_reason: "",
          warnings: [],
          referenced_tables: ["APP.DEPARTMENT"],
          referenced_columns: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["DEPARTMENT_NAME", "EMPLOYEE_COUNT"],
          rows: [{ DEPARTMENT_NAME: "開発部", EMPLOYEE_COUNT: 3 }],
          total: 1,
        },
        timing: {
          created_at: createdAt,
          started_at: createdAt,
          finished_at: createdAt,
          elapsed_ms: 40,
          stage_timings: [],
        },
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: question,
            rewritten_question: question,
            profile_id: "default",
            profile_name: "標準プロファイル",
            profile_category: "",
            target_objects: ["APP.DEPARTMENT"],
            filters: [],
            group_by: ["DEPARTMENT_NAME"],
            order_by: [],
            aggregations: ["COUNT"],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "APP.DEPARTMENT を参照し、部署ごとに集計します。",
            statement_type: "SELECT",
            tables: ["APP.DEPARTMENT"],
            columns: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
            joins: [],
            filters: [],
            aggregations: ["COUNT"],
            group_by: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
            order_by: [],
            limit: null,
            semantic_graph: sqlGraph,
            warnings: [],
          },
          ontology_graph: ontologyGraph,
          warnings: [],
        },
        show_prompt: null,
      },
    })
  );

  await page.goto("/query");
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await page.locator("#nl2sql-question-input").fill(question);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByText("開発部")).toBeVisible();
  await expect(page.getByTestId("nl2sql-sql-grounding-panel")).toBeVisible();
  await expect(page).toHaveURL(/\/query$/);
  expect(ontologyViewRequests).toBe(0);
});

test("SQL 生成だけのユーザーは profile 未作成時に管理作成ボタンを表示しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  const limited = {
    ...systemAdminMe,
    user_uuid: "limited-empty-profile",
    login_user_id: "limited.empty",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: [],
    password_change_allowed: true,
  };
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 0,
      column_count: 0,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, { items: [], next_cursor: null, total: 0, change_token: 1 })
  );

  await page.goto("/query");

  await expect(
    page.getByText("利用可能な業務プロファイルがありません。管理者に権限付与を依頼してください。")
  ).toBeVisible({ timeout: 45_000 });
  await expect(page.getByRole("button", { name: "業務プロファイルを作成" })).toHaveCount(0);
});

test("SQL 生成だけのユーザーは空 schema 失敗時にサンプルデータ投入ボタンを表示しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/rewrite", (route) => {
    // rewrite 既定 ON。未モックだと submit が最初の rewrite で失敗する。
    const body = route.request().postDataJSON() as { question?: string };
    const original = String(body?.question ?? "");
    return fulfill(route, {
      original_question: original,
      rewritten_question: original,
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  const limited = {
    ...systemAdminMe,
    user_uuid: "query-only-sample-readonly",
    login_user_id: "query.only.sample",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    password_change_allowed: true,
  };
  let sampleImportRequested = false;
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog", (route) =>
    fulfill(route, { refreshed_at: "2026-08-20T00:00:00Z", tables: [] })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-empty",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 0,
      column_count: 0,
      change_token: 1,
      etag: "schema-empty",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [],
      next_cursor: null,
      total: 0,
      table_count: 0,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: [],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/sample-data/import", (route) => {
    sampleImportRequested = true;
    return fulfill(route, { executed: true });
  });

  const createdAt = "2026-08-20T00:00:00Z";
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfill(route, {
      job_id: "job-empty-readonly-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-empty-readonly-001", (route) =>
    fulfill(route, {
      job_id: "job-empty-readonly-001",
      status: "error",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 30,
      result: null,
      error_message:
        "NL2SQL ジョブに失敗しました: Schema catalog が空です。Oracle schema を refresh するか、Data Tools から sample data を明示的に import してください。",
      timing: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "error", elapsed_ms: 5 },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );

  await page.goto("/query");
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await page.locator("#nl2sql-question-input").fill("すべてプロジェクトを教えてください。");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "error");
  await expect(progress.getByRole("button", { name: "サンプルデータを投入" })).toHaveCount(0);
  await expect(
    progress.getByText("スキーマが空です。管理者にサンプルデータの投入またはスキーマ更新を依頼してください。")
  ).toBeVisible();
  expect(sampleImportRequested).toBe(false);
});

test("管理者がユーザーを作成して単一ロールを割り当て、一時パスワードを一度だけ確認する", async ({ page, context }) => {
  await mockDatabaseGateReady(page);
  await context.addCookies([{ name: "pr_csrf", value: "csrf-token", url: "http://127.0.0.1:3101" }]);
  await page.addInitScript(() => {
    const state = globalThis as typeof globalThis & {
      __copiedOneTimePassword?: string;
      __copyOneTimePasswordShouldFail?: boolean;
    };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          if (state.__copyOneTimePasswordShouldFail) {
            throw new Error("clipboard denied");
          }
          state.__copiedOneTimePassword = value;
        },
      },
    });
  });
  let csrfObserved = false;
  let createRequestCount = 0;
  let successfulCreateCount = 0;
  let failNextCreate = false;
  let holdNextCreate = false;
  let heldCreate = Promise.resolve();
  let releaseHeldCreate: () => void = () => undefined;
  const createdPayloads: Array<{ loginUserId: string; roleIds: string[] }> = [];
  const generatedPasswords = [
    "RandomStrong!Pass123-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
    "SecondStrong!Pass456-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
    "ThirdStrong!Pass789-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
  ];
  let users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
  ];
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const runnerRole = {
    ...systemRole,
    role_id: "role-runner",
    role_code: "QUERY_RUNNER",
    display_name: "検索実行",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole, runnerRole])
  );
  await page.route("**/api/security/users", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, users);
      return;
    }
    createRequestCount += 1;
    csrfObserved = route.request().headers()["x-csrf-token"] === "csrf-token";
    const payload = route.request().postDataJSON() as {
      login_user_id: string;
      display_name: string;
      role_ids: string[];
    };
    createdPayloads.push({
      loginUserId: payload.login_user_id,
      roleIds: payload.role_ids,
    });
    if (holdNextCreate) {
      holdNextCreate = false;
      await heldCreate;
    }
    if (failNextCreate) {
      failNextCreate = false;
      const response = problemEnvelope({
        status: 503,
        code: "SECURITY_SERVICE_UNAVAILABLE",
        title: "サービスを一時的に利用できません",
        detail: "ユーザーを作成できません。時間をおいて再試行してください。",
        requestId: "inline-password-create-failed",
        retryable: true,
      });
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "X-Request-ID": response.problem.request_id },
        body: JSON.stringify(response),
      });
      return;
    }
    successfulCreateCount += 1;
    const user = {
      user_uuid: `new-user-${successfulCreateCount}`,
      login_user_id: payload.login_user_id,
      display_name: payload.display_name,
      status: "ACTIVE",
      force_password_change: true,
      locked_until: null,
      version: 1,
      role_ids: payload.role_ids,
      is_bootstrap_admin: false,
    };
    users = [...users, user];
    await fulfill(route, {
      user,
      temporary_password: generatedPasswords[successfulCreateCount - 1],
    });
  });

  await page.goto("/settings/security/users");
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await page.getByLabel("ログインユーザーID").fill("001");
  await page.getByLabel("表示名").fill("短いログインユーザーIDユーザー");
  await expect(page.getByTestId("security-users-role-selection-actions")).toHaveCount(0);
  // システム管理者のロールは選択肢に出さず、理由を添える（#1792）。
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-role-system-admin-note")).toHaveText("システム管理者のロールは構成管理者（system_admin）だけが持つため、ここでは割り当てられません。");
  const createButton = page.getByTestId("security-users-submit");
  await createButton.click();
  await expect(page.getByText("ロールを選択してください。", { exact: true })).toBeVisible();
  expect(createRequestCount).toBe(0);

  const viewerRadio = page.getByRole("radio", { name: /検索閲覧/ });
  const runnerRadio = page.getByRole("radio", { name: /検索実行/ });
  await expect(viewerRadio).toHaveAttribute("type", "radio");
  await expect(runnerRadio).toHaveAttribute("type", "radio");
  await viewerRadio.check();
  await expect(viewerRadio).toBeChecked();
  await expect(runnerRadio).not.toBeChecked();
  await runnerRadio.check();
  await expect(runnerRadio).toBeChecked();
  await expect(viewerRadio).not.toBeChecked();
  await viewerRadio.check();
  heldCreate = new Promise<void>((resolve) => {
    releaseHeldCreate = resolve;
  });
  holdNextCreate = true;
  await createButton.click();
  await expect(createButton).toBeDisabled();
  await page.keyboard.press("Enter");
  expect(createRequestCount).toBe(1);
  releaseHeldCreate();

  const loginInput = page.getByLabel("ログインユーザーID");
  const displayNameInput = page.getByLabel("表示名");
  const temporaryPasswordInput = page.getByLabel("一時パスワード", { exact: true });
  const copyButton = page.getByRole("button", { name: "一時パスワードをコピー" });
  await expect(page.locator("#security-users-panel-create")).toHaveCount(0);
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByRole("heading", { name: "ユーザーを編集" })).toBeVisible();
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);
  await expect(loginInput).toHaveValue("001");
  await expect(loginInput).toBeDisabled();
  await expect(loginInput).toHaveClass(/disabled:bg-surface-disabled/u);
  await expect(displayNameInput).toHaveValue("短いログインユーザーIDユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[0]);
  await expect(temporaryPasswordInput).toHaveAttribute("readonly", "");
  await expect(temporaryPasswordInput).toHaveClass(/read-only:bg-surface-sunken/u);
  await expect(viewerRadio).toBeChecked();
  await expect(runnerRadio).not.toBeChecked();
  // 作成後は同じ PageHeader の右端のボタンが「保存」になる（#618）。
  await expect(createButton).toHaveText("保存");
  await expect(page.getByText("変更を保存しました。", { exact: true }).last()).toBeVisible();

  await copyButton.click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (globalThis as typeof globalThis & { __copiedOneTimePassword?: string })
            .__copiedOneTimePassword
      )
    )
    .toBe(generatedPasswords[0]);
  // 成功はボタンの Check と読み上げ（共有の CopyButton。#2099）。
  await expect(copyButton).toHaveAttribute("data-copy-state", "copied");
  await expect(page.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);

  await page.evaluate(() => {
    (
      globalThis as typeof globalThis & { __copyOneTimePasswordShouldFail?: boolean }
    ).__copyOneTimePasswordShouldFail = true;
  });
  await copyButton.click();
  await expect(page.getByTestId("security-user-temporary-password-error")).toContainText(
    "ブラウザのクリップボード権限を確認するか、表示内容を手動で選択してください。"
  );

  await page.evaluate(() => {
    (
      globalThis as typeof globalThis & { __copyOneTimePasswordShouldFail?: boolean }
    ).__copyOneTimePasswordShouldFail = false;
  });
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await loginInput.fill("002");
  await displayNameInput.fill("連続作成ユーザー");
  await viewerRadio.check();
  await createButton.click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(loginInput).toHaveValue("002");
  await expect(displayNameInput).toHaveValue("連続作成ユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[1]);

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await loginInput.fill("003");
  await displayNameInput.fill("再試行ユーザー");
  await viewerRadio.check();
  failNextCreate = true;
  await createButton.click();
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "ユーザーを作成できません。時間をおいて再試行してください。"
  );
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);
  await expect(page.getByTestId("security-user-temporary-password")).toHaveValue("");
  await expect(loginInput).toHaveValue("003");
  await expect(displayNameInput).toHaveValue("再試行ユーザー");

  await createButton.click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(loginInput).toHaveValue("003");
  await expect(displayNameInput).toHaveValue("再試行ユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[2]);
  await expect(temporaryPasswordInput).not.toHaveValue(generatedPasswords[1]);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-edit"));
  await expect(copyButton).toBeVisible();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeVisible();
  expect(csrfObserved).toBe(true);
  expect(createRequestCount).toBe(4);
  expect(successfulCreateCount).toBe(3);
  expect(createdPayloads[0]).toEqual({
    loginUserId: "001",
    roleIds: ["role-viewer"],
  });
  expect(createdPayloads.at(-1)).toEqual({
    loginUserId: "003",
    roleIds: ["role-viewer"],
  });
  expect(users.some((user) => user.login_user_id === "001")).toBe(true);
  expect(users.some((user) => user.login_user_id === "003")).toBe(true);
});

test("ユーザー作成の problem field error は入力直下だけに表示し、局所クリアと request ID を保つ", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  let submitCount = 0;
  await page.route("**/api/security/users", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, []);
      return;
    }
    submitCount += 1;
    const response =
      submitCount === 1
        ? problemEnvelope({
            status: 409,
            code: "SECURITY_USER_LOGIN_ID_CONFLICT",
            title: "ユーザーを作成できません",
            detail: "このログインユーザーIDは既に使用されています。別のIDを入力してください。",
            requestId: "user-conflict-request",
            fieldErrors: [
              {
                pointer: "/login_user_id",
                code: "already_exists",
                message: "別のログインユーザーIDを入力してください。",
              },
            ],
          })
        : submitCount === 2
          ? problemEnvelope({
              status: 422,
              code: "REQUEST_VALIDATION_FAILED",
              title: "入力内容を確認してください",
              detail: "入力内容に誤りがあります。該当項目を確認してください。",
              requestId: "user-validation-request",
              fieldErrors: [
                { pointer: "/login_user_id", code: "invalid", message: "IDを確認してください。" },
                { pointer: "/display_name", code: "invalid", message: "表示名を確認してください。" },
              ],
            })
          : problemEnvelope({
              status: 503,
              code: "SECURITY_SERVICE_UNAVAILABLE",
              title: "サービスを一時的に利用できません",
              detail: "ユーザーを作成できません。時間をおいて再試行してください。",
              requestId: "user-service-request",
              retryable: true,
            });
    await route.fulfill({
      status: response.problem.status,
      contentType: "application/json",
      headers: { "X-Request-ID": response.problem.request_id },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/users");
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  const loginInput = page.getByLabel("ログインユーザーID");
  const displayNameInput = page.getByLabel("表示名");
  await loginInput.fill("duplicate.user");
  await displayNameInput.fill("重複ユーザー");
  await page.getByRole("radio", { name: /検索閲覧/ }).check();
  const submit = page.getByTestId("security-users-submit");
  await submit.click();

  const conflictMessage = "このログインユーザーIDは既に使用されています。別のIDを入力してください。";
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(1);
  await expect(loginInput).toHaveAttribute("aria-invalid", "true");
  // エラーは共有の TextField が欄の下に出し、aria-describedby で結び付ける（id は useId を含む。#631）。
  await expect(loginInput).toHaveAccessibleDescription(/このログインユーザーIDは既に使用されています。/);
  await expect(loginInput).toBeFocused();
  await expect(page.getByTestId("security-users-save-error").getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-create"));
  await loginInput.fill("available.user");
  await expect(loginInput).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await submit.click();
  await expect(loginInput).toBeFocused();
  await expect(page.getByText("IDを確認してください。", { exact: true })).toBeVisible();
  await expect(page.getByText("表示名を確認してください。", { exact: true })).toBeVisible();
  await displayNameInput.fill("利用可能な表示名");
  await expect(page.getByText("表示名を確認してください。", { exact: true })).toHaveCount(0);
  await expect(page.getByText("IDを確認してください。", { exact: true })).toBeVisible();
  await loginInput.fill("final.user");
  await expect(page.getByText("IDを確認してください。", { exact: true })).toHaveCount(0);

  await submit.click();
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "ユーザーを作成できません。時間をおいて再試行してください。"
  );
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "リクエストID: user-service-request"
  );
  expect(submitCount).toBe(3);
});

test("ユーザー一覧と詳細のパスワードリセット結果を編集フォームに表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
    {
      user_uuid: "sales-user",
      login_user_id: "sales.user",
      display_name: "営業ユーザー",
      status: "ACTIVE",
      force_password_change: true,
      locked_until: null,
      version: 1,
      role_ids: ["role-viewer"],
      is_bootstrap_admin: false,
    },
  ];
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  let resetRequestCount = 0;
  await page.route("**/api/security/users/sales-user/reset-password", async (route) => {
    resetRequestCount += 1;
    await fulfill(route, {
      user: { ...users[1], force_password_change: true },
      temporary_password: `ListResetStrong!Pass-${resetRequestCount}`,
    });
  });

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users");
  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const adminRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  await adminRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-users-detail-actions");
  // 詳細の見出しは表示名（ログイン ID を出さない。#1696）。
  await expect(page.getByRole("heading", { name: "システム管理者", exact: true })).toBeVisible();
  await expect(detailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(
    detailActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  const adminMoreButton = detailActions.getByRole("button", { name: "その他の操作" });
  await expect(adminMoreButton).toBeVisible();
  await adminMoreButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(adminMoreButton).toBeFocused();
  await expectNoPageHorizontalScroll(page);

  const salesRow = grid.locator("tbody tr").filter({ hasText: "営業ユーザー" });
  await salesRow.locator("td").first().click();
  const salesDetailActions = page.getByTestId("security-users-detail-actions");
  const salesResetButton = salesDetailActions.getByRole("button", { name: "パスワードをリセット" });
  await expect(salesDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(salesResetButton).toBeVisible();
  await salesDetailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("Escape");
  // Escape の後、メニューは次の frame でトリガーへ focus を戻す（restoreMenuTriggerFocus）。
  // 戻る前に同じ操作の行の「パスワードをリセット」へ移すと、戻す処理に奪い返され、
  // 続く Enter が「その他の操作」のメニューを開き直す。戻ったのを待ってから移す（#1141）。
  await expect(salesDetailActions.getByRole("button", { name: "その他の操作" })).toBeFocused();
  await salesResetButton.focus();
  await page.keyboard.press("Enter");
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByLabel("ログインユーザーID")).toHaveValue("sales.user");
  await expect(page.getByLabel("ログインユーザーID")).toBeDisabled();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue(
    "ListResetStrong!Pass-1"
  );
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveAttribute(
    "readonly",
    ""
  );
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await salesRow.locator("td").first().click();
  await expect(salesDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(salesDetailActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expect(salesDetailActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await salesDetailActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue(
    "ListResetStrong!Pass-2"
  );
  expect(resetRequestCount).toBe(2);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-edit"));
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー編集はパスワードリセットと無効化・有効化をフォーム操作に統合する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.addInitScript(() => {
    const state = globalThis as typeof globalThis & { __copiedEditPassword?: string };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          state.__copiedEditPassword = value;
        },
      },
    });
  });

  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  let user = {
    user_uuid: "sales-user",
    login_user_id: "sales.user",
    display_name: "営業ユーザー",
    status: "ACTIVE" as "ACTIVE" | "DISABLED",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: ["role-viewer"],
    is_bootstrap_admin: false,
  };
  let resetRequestCount = 0;
  let resetPayload: unknown = null;
  let failNextReset = false;
  let updateRequestCount = 0;
  const disableVersions: number[] = [];
  const enableVersions: number[] = [];
  let failNextDisable = false;

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, [user]));
  await page.route("**/api/security/users/sales-user", async (route) => {
    updateRequestCount += 1;
    await fulfill(route, user);
  });
  await page.route("**/api/security/users/sales-user/*", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/reset-password")) {
      resetRequestCount += 1;
      resetPayload = route.request().postDataJSON();
      if (failNextReset) {
        failNextReset = false;
        await fulfill(
          route,
          "一時パスワードを再発行できませんでした。接続状態と権限を確認して再試行してください。",
          503
        );
        return;
      }
      user = { ...user, force_password_change: true };
      await fulfill(route, {
        user,
        temporary_password: "EditResetStrong!Pass-1",
      });
      return;
    }
    const payload = route.request().postDataJSON() as { version: number };
    if (pathname.endsWith("/disable")) {
      disableVersions.push(payload.version);
      if (failNextDisable) {
        failNextDisable = false;
        await fulfill(route, "最後のシステム管理者は無効化または権限解除できません。", 409);
        return;
      }
      user = { ...user, status: "DISABLED", version: user.version + 1 };
      await fulfill(route, user);
      return;
    }
    if (pathname.endsWith("/enable")) {
      enableVersions.push(payload.version);
      user = { ...user, status: "ACTIVE", version: user.version + 1 };
      await fulfill(route, user);
      return;
    }
    await fulfill(route, "未対応のユーザー操作です。", 404);
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");
  const salesRow = page
    .getByTestId("security-users-grid")
    .locator("tbody tr")
    .filter({ hasText: "営業ユーザー" });
  await salesRow.locator("td").first().click();
  await page
    .getByTestId("security-users-detail-actions")
    .getByRole("button", { name: "編集" })
    .click();

  const editPanel = page.locator("#security-users-panel-edit");
  const editActions = page.getByRole("group", { name: "ユーザー編集操作" });
  const displayName = page.getByLabel("表示名");
  const temporaryPassword = page.getByLabel("一時パスワード", { exact: true });
  const copyTemporaryPassword = page.getByRole("button", {
    name: "一時パスワードをコピー",
  });
  const assignedRole = page.getByLabel("検索閲覧");
  await expect(editPanel).toBeVisible();
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(editActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await expect(temporaryPassword).toHaveValue("");
  await expect(temporaryPassword).toHaveAttribute("readonly", "");
  await expect(temporaryPassword).toHaveClass(/read-only:bg-surface-sunken/u);
  await expect(copyTemporaryPassword).toBeDisabled();
  await displayName.fill("未保存の営業ユーザー");

  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "キャンセル" })
    .click();
  expect(resetRequestCount).toBe(0);
  await expect(displayName).toHaveValue("未保存の営業ユーザー");

  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.getByTestId("security-users-edit-reset-password-region")).toHaveCount(0);
  await expect(temporaryPassword).toHaveValue("EditResetStrong!Pass-1");
  await expect(page.getByText("パスワードをリセットしました。", { exact: true }).last()).toBeVisible();
  expect(resetPayload).toEqual({ temporary_password: null });
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(copyTemporaryPassword).toBeEnabled();
  await copyTemporaryPassword.click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (globalThis as typeof globalThis & { __copiedEditPassword?: string })
            .__copiedEditPassword
      )
    )
    .toBe("EditResetStrong!Pass-1");

  failNextReset = true;
  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.getByTestId("security-user-temporary-password-error")).toContainText(
    "接続状態と権限を確認して再試行してください。"
  );
  await expect(temporaryPassword).toHaveValue("EditResetStrong!Pass-1");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(page.locator("main > [role='alert']")).toHaveCount(0);

  const moreButton = editActions.getByRole("button", { name: "その他の操作" });
  await moreButton.focus();
  await page.keyboard.press("Enter");
  const disableItem = page.getByRole("menuitem", { name: "無効化" });
  await expect(disableItem).toBeFocused();
  await expect(disableItem).toHaveAttribute("data-entity-action-tone", "danger");
  await page.keyboard.press("Escape");
  await expect(moreButton).toBeFocused();

  await moreButton.click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "キャンセル" })
    .click();
  expect(disableVersions).toEqual([]);

  await moreButton.click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(editActions.getByRole("button", { name: "有効化" })).toBeVisible();
  await expect(page.getByTestId("security-users-submit")).toHaveCount(0);
  await expect(
    editActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(displayName).toBeDisabled();
  await expect(temporaryPassword).toBeDisabled();
  await expect(copyTemporaryPassword).toBeDisabled();
  // システム管理者のロールは選択肢に出さない（#1792）。
  await expect(page.getByRole("radio")).toHaveCount(1);
  await expect(assignedRole).toBeVisible();
  await expect(assignedRole).toBeChecked();
  await expect(assignedRole).toBeDisabled();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await page.getByRole("button", { name: "一覧へ戻る" }).focus();
  await page.keyboard.press("Tab");
  await expect(editActions.getByRole("button", { name: "有効化" })).toBeFocused();
  await editPanel.locator("form").evaluate((form) =>
    (form as HTMLFormElement).requestSubmit()
  );
  expect(updateRequestCount).toBe(0);
  expect(resetRequestCount).toBe(2);
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(page.getByText("ユーザーを無効化しました。既存セッションは失効しました。", { exact: true }).last()).toBeVisible();
  expect(disableVersions).toEqual([1]);

  await editActions.getByRole("button", { name: "有効化" }).click();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await expect(editActions.getByRole("button", { name: "有効化" })).toHaveCount(0);
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(
    editActions.getByRole("button", { name: "パスワードをリセット" })
  ).toBeVisible();
  await expect(displayName).toBeEnabled();
  await expect(temporaryPassword).toBeEnabled();
  await expect(temporaryPassword).toHaveAttribute("readonly", "");
  await expect(copyTemporaryPassword).toBeEnabled();
  await expect(assignedRole).toBeEnabled();
  await expect(assignedRole).toBeChecked();
  await expect(page.getByText("ユーザーを有効化しました。次回ログインから利用できます。", { exact: true }).last()).toBeVisible();
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  expect(enableVersions).toEqual([2]);

  failNextDisable = true;
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "実行" })
    .click();
  // 操作の失敗もヘッダーの直下の Banner の 1 か所（#585 / #618）。
  await expect(page.getByTestId("security-users-save-error")).toContainText("最後のシステム管理者は無効化または権限解除できません。");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  expect(disableVersions).toEqual([1, 3]);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(editActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("無効ユーザーの削除は確認・フォーカス復帰を保ち、削除後は選択を外す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-delete-viewer",
    role_code: "DELETE_VIEWER",
    display_name: "削除確認ロール",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  const activeUser = {
    user_uuid: "active-delete-user",
    login_user_id: "active.user",
    display_name: "有効ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 4,
    role_ids: [viewerRole.role_id],
    assigned_roles: [],
    is_bootstrap_admin: false,
  };
  const disabledUser = {
    ...activeUser,
    user_uuid: "disabled-delete-user",
    login_user_id: "disabled.user",
    display_name: "削除対象ユーザー",
    status: "DISABLED",
    version: 7,
  };
  let users = [activeUser, disabledUser];
  let deleteCount = 0;
  let observedIfMatch = "";

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  await page.route("**/api/security/users/disabled-delete-user", async (route) => {
    deleteCount += 1;
    observedIfMatch = route.request().headers()["if-match"] ?? "";
    users = users.filter((user) => user.user_uuid !== disabledUser.user_uuid);
    await fulfill(route, {
      deleted: true,
      user_uuid: disabledUser.user_uuid,
      login_user_id: disabledUser.login_user_id,
    });
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const activeRow = grid.locator("tbody tr").filter({ hasText: "有効ユーザー" });
  await activeRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-users-detail-actions");
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  const disabledRow = grid.locator("tbody tr").filter({ hasText: "削除対象ユーザー" });
  await disabledRow.locator("td").first().click();
  // 選択は URL を経て詳細に反映される。前の行（有効ユーザー）の詳細のメニューを開かないよう、
  // 削除対象ユーザーの詳細の操作に置き換わってからフォーカスする。
  const deleteTrigger = detailActions.getByRole("button", {
    name: "その他の操作（操作: 削除対象ユーザー）",
  });
  await expect(deleteTrigger).toBeVisible();
  await deleteTrigger.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("End");
  const deleteMenuItem = page.getByRole("menuitem", { name: "削除" });
  await expect(deleteMenuItem).toBeFocused();
  await page.keyboard.press("Enter");

  const deleteDialog = page.getByRole("alertdialog", { name: "削除" });
  await expect(deleteDialog).toContainText(
    "「削除対象ユーザー」を完全に削除します。"
  );
  await expect(deleteDialog).not.toContainText("disabled.user");
  await expect(deleteDialog).toContainText("割り当てロール、既存セッションは削除され");
  await page.mouse.click(4, 4);
  await expect(deleteDialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(deleteDialog).toHaveCount(0);
  await expect(deleteTrigger).toBeFocused();
  expect(deleteCount).toBe(0);

  await deleteTrigger.click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(deleteTrigger).toBeFocused();
  expect(deleteCount).toBe(0);

  await deleteTrigger.click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ユーザー", { exact: true })).toHaveCount(0);
  // 削除した対象の選択を外し、ほかの行へ置き換えない（page-archetypes.md §1 B。#2282）。
  await expect(page).not.toHaveURL(/[?&]user=/u);
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-users-detail")).toContainText("ユーザーを選択してください");
  await expect(page.getByText("削除対象ユーザー を完全に削除しました。", { exact: true }).last()).toBeVisible();
  expect(observedIfMatch).toBe('"7"');
  expect(deleteCount).toBe(1);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("詳細アクションメニューを閉じた直後に別要素へフォーカスしても奪い返さない", async ({
  page,
}) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-focus-viewer",
    role_code: "FOCUS_VIEWER",
    display_name: "フォーカス確認ロール",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  const firstUser = {
    user_uuid: "focus-first-user",
    login_user_id: "focus.first",
    display_name: "先のユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [viewerRole.role_id],
    assigned_roles: [],
    is_bootstrap_admin: false,
  };
  const secondUser = {
    ...firstUser,
    user_uuid: "focus-second-user",
    login_user_id: "focus.second",
    display_name: "後のユーザー",
    version: 2,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, [firstUser, secondUser]));

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const firstRow = grid.locator("tbody tr").filter({ hasText: "先のユーザー" });
  await firstRow.locator("td").first().click();
  const search = page.getByTestId("security-users-search");
  const trigger = page
    .getByTestId("security-users-detail-actions")
    .getByRole("button", { name: "その他の操作" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();

  // Escape で閉じた直後（フォーカス復帰の rAF が走る前）に検索欄へフォーカスを移す。
  await page.evaluate(() => {
    document.activeElement?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    document.querySelector<HTMLInputElement>('[data-testid="security-users-search"]')?.focus();
  });

  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(search).toBeFocused();
  await expect(trigger).not.toBeFocused();
});

test("ユーザー管理は一覧 + 詳細（狭い一覧・広い詳細）で、作成・編集は同じパネルにする", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  const users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
    {
      user_uuid: "sales-user",
      login_user_id: "sales.user",
      display_name: "営業ユーザー",
      status: "DISABLED",
      force_password_change: true,
      locked_until: "2026-07-21T03:00:00Z",
      version: 2,
      role_ids: ["role-viewer"],
      is_bootstrap_admin: false,
    },
  ];
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [
      systemRole,
      {
        ...systemRole,
        role_id: "role-viewer",
        role_code: "QUERY_VIEWER",
        display_name: "検索閲覧",
        is_built_in: false,
        permissions: ["menu.query"],
        data_entitlements: [],
      },
    ])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));

  await page.setViewportSize({ width: 2048, height: 1000 });
  await page.goto("/settings/security/users");

  // 一覧 + 詳細（ListDetailLayout。#2282）: xl 以上は 2 ペイン、一覧の列は既定 20rem。
  const usersLayout = page.getByTestId("list-detail-security-users");
  await expect(usersLayout).toHaveAttribute("data-list-detail-layout", "split");
  const usersListWidth = await page.getByTestId("list-detail-security-users-list").evaluate((node) =>
    node.getBoundingClientRect().width / Number.parseFloat(getComputedStyle(document.documentElement).fontSize)
  );
  expect(Math.abs(usersListWidth - 20)).toBeLessThanOrEqual(0.1);
  await expect(page.getByTestId("security-users-grid")).toBeVisible();
  const grid = page.getByTestId("security-users-grid");
  await expect(grid.locator("tbody tr")).toHaveCount(2);
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const salesUserRow = grid.locator("tbody tr").filter({ hasText: "営業ユーザー" });
  const adminUserRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  // 先頭の行を自動で選ばず、詳細は空の状態（#2282）。
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-users-detail")).toContainText("ユーザーを選択してください");
  await expect(grid).not.toContainText("sales.user");
  await expect(grid.getByRole("button", { name: "編集" })).toHaveCount(0);
  await adminUserRow.locator("td").first().click();
  // 一覧は表示名の順（システム管理者 < 営業ユーザー）、詳細見出しは表示名で、ログイン ID は出さない（#1696）。
  await expect(adminUserRow).toHaveAttribute("data-selected", "true");
  await expect(page).toHaveURL(/[?&]user=admin-user/u);
  await expect(page.getByRole("region", { name: "システム管理者" })).toBeVisible();
  const adminUserDetailActions = page.getByTestId("security-users-detail-actions");
  const adminMoreButton = adminUserDetailActions.getByRole("button", { name: "その他の操作" });
  await adminMoreButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(adminMoreButton).toBeFocused();
  await expect(adminUserDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(adminUserDetailActions.getByRole("button", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(adminMoreButton).toBeVisible();
  await salesUserRow.locator("td").nth(1).click();
  await expect(salesUserRow).toHaveAttribute("data-selected", "true");
  await expect(page.locator("dl").getByText("ロック中", { exact: true })).toBeVisible();
  await expect(page.getByText("ロック期限", { exact: true })).toHaveCount(0);
  const salesUserDetailActions = page.getByTestId("security-users-detail-actions");
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(
    salesUserDetailActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(salesUserDetailActions.getByRole("button", { name: "ロック解除" })).toBeVisible();
  await salesUserDetailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "有効化" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "削除" })).toBeVisible();
  await page.keyboard.press("Escape");
  await salesUserDetailActions.getByRole("button", { name: "編集" }).click();
  const disabledUserEditActions = page.getByRole("group", { name: "ユーザー編集操作" });
  await expect(page.getByTestId("security-users-submit")).toHaveCount(0);
  await expect(
    disabledUserEditActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(disabledUserEditActions.getByRole("button", { name: "有効化" })).toBeVisible();
  await expect(page.getByLabel("表示名")).toBeDisabled();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "一時パスワードをコピー" })).toBeDisabled();
  await expect(page.getByRole("radio")).toHaveCount(1);
  await expect(page.getByLabel("検索閲覧")).toBeVisible();
  await expect(page.getByLabel("検索閲覧")).toBeChecked();
  await expect(page.getByLabel("検索閲覧")).toBeDisabled();
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-role-system-admin-note")).toHaveText("システム管理者のロールは構成管理者（system_admin）だけが持つため、ここでは割り当てられません。");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-search").fill("sales");
  await expect(page.getByTestId("security-users-grid").getByText("営業ユーザー")).toBeVisible();
  await expect(page.getByTestId("security-users-grid").getByText("システム管理者")).toHaveCount(0);
  await page.getByTestId("security-users-search").fill("");

  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  const formStyle = await topLevelPanelStyle(page, "create", "security-users");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(usersLayout).toBeVisible();

  await adminUserRow.locator("td").first().click();
  // 選択が切り替わるまで待つ。待たずに「編集」を押すと、前に選んだ利用者の編集を開く（#3233）。
  await expect(page).toHaveURL(/[?&]user=admin-user/u);
  await expect(adminUserRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  expect(await topLevelPanelStyle(page, "edit", "security-users")).toEqual(formStyle);
  // 既存の割り当てを持つユーザーの編集でも、システム管理者のロールは選択肢に出さない（#1792）。
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-other-roles")).toContainText("システム管理者");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(usersLayout).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  // xl 未満は 1 ペイン。選んでいる利用者の詳細だけを出す。
  await expect(usersLayout).toHaveAttribute("data-list-detail-view", "detail");
  await expectNoPageHorizontalScroll(page);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(page.getByTestId("security-users-detail-actions").getByRole("button", { name: "パスワードをリセット" })).toHaveCount(0);
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー管理は削除済みの割り当てロールを ID（UUID）ではなく「（削除済みのロール）」と表示する（#1682）", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const deletedRoleId = "6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5";
  await page.route("**/api/security/roles?include_archived=false", (route) => fulfill(route, [systemRole]));
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "deleted-role-user",
        login_user_id: "deleted.role.user",
        display_name: "削除済みロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [deletedRoleId],
        // backend の AssignedRoleData.unresolved() の形（コード・表示名は空、deleted）。
        assigned_roles: [
          { role_id: deletedRoleId, role_code: "", display_name: "", is_built_in: false, archived: true, deleted: true },
        ],
        is_bootstrap_admin: false,
      },
    ])
  );

  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("（削除済みのロール）", { exact: true })).toBeVisible();
  await expect(page.locator("main")).not.toContainText(deletedRoleId);
  expect(await page.locator("main").innerHTML()).not.toContain(deletedRoleId);
});

test("ユーザー管理はアーカイブ済み割り当てロールを無効として表示し、新規選択肢には出さない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const archivedAssignedRole = {
    role_id: "role-archived-user",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: true,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "archived-role-user",
        login_user_id: "archive.user",
        display_name: "アーカイブロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [archivedAssignedRole.role_id],
        assigned_roles: [archivedAssignedRole],
        is_bootstrap_admin: false,
      },
    ])
  );

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users?user=archived-role-user");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("データ管理者（アーカイブ済み・無効）", { exact: true })).toBeVisible();
  await expect(
    page.getByText("アーカイブ済みロールの権限は、このユーザーの実アクセス権には反映されません。", {
      exact: true,
    })
  ).toBeVisible();

  const inactiveRoleBadge = page.getByText("データ管理者（アーカイブ済み・無効）", { exact: true }).last();
  // ロールバッジは共有 StatusBadge(token 化)へ移行。variant で判定する。
  await expect(inactiveRoleBadge).toHaveAttribute("data-status-variant", "neutral");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("radio", { name: /検索閲覧/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: /データ管理者/ })).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー管理は復元済みロールを通常表示し、割り当て選択肢に戻す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const restoredRole = {
    ...systemRole,
    role_id: "role-restored-user",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const restoredAssignedRole = {
    role_id: restoredRole.role_id,
    role_code: restoredRole.role_code,
    display_name: restoredRole.display_name,
    is_built_in: false,
    archived: false,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, restoredRole])
  );
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "restored-role-user",
        login_user_id: "restore.user",
        display_name: "復元ロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [restoredRole.role_id],
        assigned_roles: [restoredAssignedRole],
        is_bootstrap_admin: false,
      },
    ])
  );

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users?user=restored-role-user");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("データ管理者", { exact: true })).toBeVisible();
  await expect(page.getByText("データ管理者（アーカイブ済み・無効）", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("アーカイブ済みロールの権限は、このユーザーの実アクセス権には反映されません。", {
      exact: true,
    })
  ).toHaveCount(0);
  await expect(page.getByText("データ管理者", { exact: true }).last()).toHaveAttribute(
    "data-status-variant",
    "info"
  );
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("radio", { name: /データ管理者/ })).toBeVisible();
});

test("ロール管理はテーブル一覧と詳細で表示し、権限の選択は権限管理で行う", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  const permissionRows = [
    {
      code: "menu.settings_appearance",
      group: "システム設定",
      label: "外観と証明書",
      description: "外観と証明書を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.security_users",
      group: "セキュリティ管理",
      label: "ユーザー管理",
      description: "ユーザー管理を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.security_roles",
      group: "セキュリティ管理",
      label: "ロール・権限管理",
      description: "ロール・権限管理を表示し、関連操作を利用できます。",
      implies: [],
    },
  ];
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "SECURITY_VIEWER",
    display_name: "アプリ閲覧",
    description: "表示のみ",
    is_built_in: false,
    permissions: ["menu.security_users"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.setViewportSize({ width: 2048, height: 1000 });
  await page.goto("/settings/security/roles");

  // 一覧 + 詳細（ListDetailLayout。#2282）: xl 以上は 2 ペイン。
  const rolesLayout = page.getByTestId("list-detail-security-roles");
  await expect(rolesLayout).toHaveAttribute("data-list-detail-layout", "split");
  const grid = page.getByTestId("security-roles-grid");
  await expect(grid).toBeVisible();
  await expect(grid.getByRole("columnheader", { name: "ロール" })).toBeVisible();
  // 一覧の列は狭いので、説明は列にせず名前の下の補足 1 行に出す（#2282）。
  // ロール管理（共通画面）は権限を扱わない。権限は権限管理で設定する（#206）。
  await expect(grid.getByRole("columnheader", { name: "説明" })).toHaveCount(0);
  await expect(grid.getByRole("columnheader", { name: "機能権限" })).toHaveCount(0);
  // 先頭の行を自動で選ばない（#2282）。
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-roles-detail")).toContainText("ロールを選択してください");
  await grid.locator("tbody tr").filter({ hasText: "アプリ閲覧" }).locator("td").first().click();
  await expect(page).toHaveURL(/[?&]role=role-viewer/u);
  await expect(page.getByTestId("security-roles-permission-summary")).toContainText(
    "付与している機能権限: 1 件"
  );
  await expect(page.getByTestId("security-roles-open-permissions")).toHaveAttribute(
    "href",
    "/settings/security/permissions?role=role-viewer"
  );
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "機能権限" })).toHaveCount(0);
  await expect(page.getByText("構造化データ権限", { exact: true })).toHaveCount(0);
  await expect(grid.locator("tbody tr")).toHaveCount(2);
  const viewerRoleRow = grid.locator("tbody tr").filter({ hasText: "アプリ閲覧" });
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  // 一覧はロール名の順（アプリ閲覧 < システム管理者）、詳細見出しはロール名（#584・#1682）。
  await expect(page.getByRole("region", { name: "アプリ閲覧" })).toBeVisible();
  await expect(grid).not.toContainText("SECURITY_VIEWER");
  await expect(grid.getByRole("button", { name: "編集" })).toHaveCount(0);
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" })).toBeVisible();
  await viewerRoleRow.locator("td").nth(1).click();
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  const customRoleEditActions = page.getByRole("group", { name: "ロール編集操作" });
  // 保存は PageHeader の右端、キャンセルは左上の「一覧へ戻る」に置き換えた（#618）。
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  await expect(customRoleEditActions.getByRole("button", { name: "キャンセル" })).toHaveCount(0);
  await expect(customRoleEditActions.getByRole("button", { name: "アーカイブ" })).toHaveCount(0);
  await customRoleEditActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "アーカイブ" })).toHaveAttribute("data-entity-action-tone", "danger");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-roles-search").fill("閲覧");
  await expect(grid.getByText("アプリ閲覧")).toBeVisible();
  await expect(grid.getByText("システム管理者")).toHaveCount(0);
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-roles-search").fill("");

  await page.getByTestId("security-roles-actions").getByRole("button", { name: "新規作成" }).click();
  const formStyle = await topLevelPanelStyle(page, "create", "security-roles");
  await expect(page.getByLabel("ロールコード")).toBeVisible();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(rolesLayout).toBeVisible();

  // 権限管理: ロールごとの機能権限を一括選択できる。
  await page.getByTestId("security-roles-open-permissions").click();
  await expect(page).toHaveURL(/\/settings\/security\/permissions\?role=role-viewer$/u);
  const permissionGrid = page.getByTestId("security-permissions-grid");
  await expect(permissionGrid.getByRole("columnheader", { name: "機能権限" })).toBeVisible();
  await expect(
    permissionGrid.locator("tbody tr").filter({ hasText: "アプリ閲覧" })
  ).toHaveAttribute("data-selected", "true");
  await page
    .getByTestId("security-permissions-detail-actions")
    .getByRole("button", { name: "権限を編集" })
    .click();
  await expect(page.getByText("ダッシュボード表示", { exact: true })).toHaveCount(0);
  await expect(page.getByText("security.users.view", { exact: true })).toHaveCount(0);
  await expect(page.getByText("security.users.manage", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /外観と証明書/ })).toBeVisible();
  const permissionBulkActions = page.getByTestId("security-roles-permission-selection-actions");
  await expect(permissionBulkActions.getByRole("button", { name: "すべて選択" })).toBeEnabled();
  await expect(permissionBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeEnabled();
  await permissionBulkActions.getByRole("button", { name: "すべて選択" }).click();
  await expect(page.getByRole("checkbox", { name: /外観と証明書/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /ユーザー管理/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /^ロール管理/ })).toBeChecked();
  // 機能の一覧は左のナビのグループ・名前にそろえる（Issue 567）。backend のグループ「セキュリティ管理」・
  // 名前「ロール・権限管理」ではなく、ナビの「ユーザーとロール」「ロール管理」で出す。
  const securityGroupBulkActions = page.getByTestId("security-roles-ユーザーとロール-permission-selection-actions");
  const securityGroupHeading = securityGroupBulkActions
    .locator("..")
    .getByRole("heading", { name: "ユーザーとロール" });
  await expect
    .poll(async () => {
      const [actionsBox, headingBox] = await Promise.all([
        securityGroupBulkActions.boundingBox(),
        securityGroupHeading.boundingBox(),
      ]);
      if (!actionsBox || !headingBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - headingBox.x);
    })
    .toBeLessThanOrEqual(1);
  await expect(securityGroupBulkActions.getByRole("button", { name: "ユーザーとロール の選択をすべて解除" })).toBeEnabled();
  await securityGroupBulkActions.getByRole("button", { name: "ユーザーとロール の選択をすべて解除" }).click();
  await expect(page.getByRole("checkbox", { name: /ユーザー管理/ })).not.toBeChecked();
  await expect(page.getByRole("checkbox", { name: /^ロール管理/ })).not.toBeChecked();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  // 組み込みロールの権限は変更できないため、編集操作を出さない。
  await permissionGrid.locator("tbody tr").filter({ hasText: "システム管理者" }).locator("td").first().click();
  await expect(page.getByTestId("security-permissions-detail-actions")).toHaveCount(0);
  await expect(page.getByText("SYSTEM_ADMIN は現在および将来の全機能権限を自動的に持つ組み込みロールです。")).toBeVisible();

  await page.goto("/settings/security/roles");
  const systemRoleRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  await systemRoleRow.locator("td").first().click();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  expect(await topLevelPanelStyle(page, "edit", "security-roles")).toEqual(formStyle);
  const roleEditActions = page.getByRole("group", { name: "ロール編集操作" });
  await expect(page.getByTestId("security-roles-submit")).toHaveCount(0);
  await expect(page.getByTestId("security-roles-back")).toBeVisible();
  await expect(roleEditActions.getByRole("button", { name: "アーカイブ" })).toHaveCount(0);
  await expect(roleEditActions.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(rolesLayout).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  // xl 未満は 1 ペイン。選んでいるロールの詳細だけを出す。
  await expect(rolesLayout).toHaveAttribute("data-list-detail-view", "detail");
  await expectNoPageHorizontalScroll(page);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
});

test("ロールコード競合はコード欄へ結び付き、403 は安全なページ通知と request ID で示す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  let submitCount = 0;
  await page.route("**/api/security/roles", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, [systemRole]);
      return;
    }
    submitCount += 1;
    const response =
      submitCount === 1
        ? problemEnvelope({
          status: 409,
          code: "SECURITY_ROLE_CODE_CONFLICT",
          title: "ロールを作成できません",
          detail: "このロールコードは既に使用されています。別のコードを入力してください。",
          requestId: "role-conflict-request",
          fieldErrors: [
            {
              pointer: "/role_code",
              code: "already_exists",
              message: "別のロールコードを入力してください。",
            },
          ],
          })
        : submitCount === 2
          ? problemEnvelope({
              status: 500,
              code: "INTERNAL_SERVER_ERROR",
              title: "サーバー内部でエラーが発生しました",
              detail: "サーバー内部でエラーが発生しました。時間をおいて再試行してください。",
              requestId: "role-internal-request",
            })
          : problemEnvelope({
              status: 403,
              // 経路の権限拒否（#224）。権限なしの画面へ移る。
              code: "SECURITY_ROUTE_FORBIDDEN",
              title: "この操作を実行する権限がありません",
              detail: "ロールを作成する権限がありません。システム管理者に確認してください。",
              requestId: "role-forbidden-request",
            });
    await route.fulfill({
      status: response.problem.status,
      contentType: "application/json",
      headers: { "X-Request-ID": response.problem.request_id },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/roles");
  await page.getByTestId("security-roles-actions").getByRole("button", { name: "新規作成" }).click();
  const roleCode = page.getByLabel("ロールコード");
  // 作成は PageHeader の右端、保存の失敗はヘッダーの直下の SaveErrorBanner（#585 / #618）。
  const submitRole = page.getByTestId("security-roles-submit");
  const saveError = page.getByTestId("security-roles-save-error");
  await roleCode.fill("system_admin");
  await page.getByLabel("ロール名").fill("予約コードロール");
  await submitRole.click();

  const reservedMessage =
    "SYSTEM_ADMIN は組み込みロール専用のコードです。別のロールコードを入力してください。";
  await expect(page.getByText(reservedMessage, { exact: true })).toHaveCount(1);
  await expect(roleCode).toHaveAttribute("aria-invalid", "true");
  // エラーは共有の TextField が欄の下に出し、aria-describedby で結び付ける（id は useId を含む。#631）。
  await expect(roleCode).toHaveAccessibleDescription(/SYSTEM_ADMIN は組み込みロール専用のコードです。/);
  await expect(roleCode).toBeFocused();
  expect(submitCount).toBe(0);

  await roleCode.fill("DUPLICATE_ROLE");
  await expect(page.getByText(reservedMessage, { exact: true })).toHaveCount(0);
  await page.getByLabel("ロール名").fill("重複ロール");
  await submitRole.click();

  const conflictMessage = "このロールコードは既に使用されています。別のコードを入力してください。";
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(1);
  await expect(roleCode).toHaveAttribute("aria-invalid", "true");
  await expect(roleCode).toHaveAccessibleDescription(/このロールコードは既に使用されています。/);
  await expect(roleCode).toBeFocused();
  await expect(saveError.getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await roleCode.fill("AVAILABLE_ROLE");
  await expect(roleCode).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(0);
  await submitRole.click();
  await expect(saveError).toContainText("サーバー内部でエラーが発生しました。時間をおいて再試行してください。");
  await expect(saveError).toContainText("リクエストID: role-internal-request");
  await expect(saveError).not.toContainText("ORA-");
  await page.getByLabel("ロール名").fill("再試行ロール");
  await submitRole.click();
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("必要なロールが付与されているか、システム管理者に確認してください。");
  await expect(page.getByRole("status")).toContainText("リクエストID: role-forbidden-request");
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  expect(submitCount).toBe(3);
});

test("SYSTEM_ADMIN は権限管理で業務プロファイル利用権限を設定できる", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-query-default", role_code: "QUERY_DEFAULT", display_name: "SQL 利用者" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [],
      },
      ...Array.from({ length: 18 }, (_, index) => ({
        id: `verification-${String(index + 1).padStart(2, "0")}`,
        name: `検証プロファイル ${String(index + 1).padStart(2, "0")}`,
        category: "検証",
        description: "スクロール領域の表示確認用プロファイル",
        archived: false,
        allowed_roles: [],
      })),
    ]))
  );
  const permissionRows = [
    {
      code: "menu.query",
      group: "メニュー権限",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
  ];
  const queryRole = {
    ...systemRole,
    role_id: "role-query-default",
    role_code: "QUERY_DEFAULT",
    display_name: "SQL 利用者",
    description: "標準 profile のみ",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, queryRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));
  await page.route("**/api/security/roles/role-query-default/permissions", async (route) => {
    expect(route.request().method()).toBe("PUT");
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfill(route, {
      ...queryRole,
      version: 2,
      allowed_profile_ids: savedPayload.allowed_profile_ids,
    });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/permissions");
  await page
    .getByTestId("security-permissions-grid")
    .locator("tbody tr")
    .filter({ hasText: "SQL 利用者" })
    .locator("td")
    .first()
    .click();
  await expect(page.getByText("1 件", { exact: true }).first()).toBeVisible();
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  // 候補の一覧は共通の ListPicker（#600）: 見出しの名前の区画・検索欄・選択の行・listbox のスクロール領域。
  const profileSearch = page.getByRole("searchbox", { name: "業務プロファイルを検索" });
  const profilePicker = page.getByTestId("security-roles-profile-access-list");
  const profileAccessList = page.getByTestId("security-roles-profile-access-list-scroll-region");
  const readProfileAccessScrollState = () =>
    profileAccessList.evaluate((node) => {
      const computed = window.getComputedStyle(node);
      return {
        clientHeight: node.clientHeight,
        maxHeight: Number.parseFloat(computed.maxHeight),
        overflowX: computed.overflowX,
        overflowY: computed.overflowY,
        rootFontSize: Number.parseFloat(
          window.getComputedStyle(document.documentElement).fontSize
        ),
        scrollHeight: node.scrollHeight,
        noHorizontalOverflow: node.scrollWidth <= node.clientWidth + 1,
      };
    });
  await expect(profileSearch).toBeVisible();
  await expect(profilePicker).toHaveAccessibleName("使用可能な業務プロファイル");
  const profileListbox = profilePicker.getByRole("listbox", { name: "使用可能な業務プロファイル" });
  await expect(profileListbox).toHaveAttribute("tabindex", "0");
  const desktopScrollState = await readProfileAccessScrollState();
  expect(desktopScrollState.maxHeight).toBeCloseTo(
    28 * desktopScrollState.rootFontSize,
    0
  );
  expect(desktopScrollState.clientHeight).toBeLessThanOrEqual(
    Math.ceil(desktopScrollState.maxHeight)
  );
  expect(desktopScrollState.scrollHeight).toBeGreaterThan(desktopScrollState.clientHeight);
  expect(desktopScrollState.noHorizontalOverflow).toBe(true);
  expect(desktopScrollState.overflowY).toBe("auto");
  // Tab で listbox に入り、End で最後の候補へ移ると、スクロール領域の中だけが動く。
  await profileListbox.focus();
  await expect(profileListbox).toBeFocused();
  await profileListbox.press("End");
  await expect
    .poll(() => profileAccessList.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(0);
  await profileSearch.focus();
  await expect(profileSearch).toBeFocused();
  await profileSearch.fill("財務");
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeVisible();
  await expect(page.getByRole("option", { name: /標準プロファイル/ })).toHaveCount(0);
  const filteredScrollState = await readProfileAccessScrollState();
  expect(filteredScrollState.clientHeight).toBeLessThan(filteredScrollState.maxHeight);
  expect(filteredScrollState.scrollHeight).toBeLessThanOrEqual(
    filteredScrollState.clientHeight + 1
  );

  const profileBulkActions = page.getByTestId("security-roles-profile-access-list-bulk-actions");
  await expect
    .poll(async () => {
      const [actionsBox, listBox] = await Promise.all([
        profileBulkActions.boundingBox(),
        profileAccessList.boundingBox(),
      ]);
      if (!actionsBox || !listBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - listBox.x);
    })
    .toBeLessThanOrEqual(1);
  await profileBulkActions.getByRole("button", { name: "すべて選択" }).click();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeChecked();
  await profileBulkActions.getByRole("button", { name: "選択をすべて解除" }).click();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).not.toBeChecked();
  await page.getByRole("option", { name: /財務プロファイル/ }).check();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeChecked();
  await profileSearch.fill("");
  // 検索語は入力が止まってから適用する（debounce。#535）。一覧が戻るのを待ってから測る。
  await expect(page.getByRole("option", { name: /標準プロファイル/ })).toHaveCount(1);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  const mobileScrollState = await readProfileAccessScrollState();
  expect(mobileScrollState.maxHeight).toBeCloseTo(
    17.5 * mobileScrollState.rootFontSize,
    0
  );
  expect(mobileScrollState.clientHeight).toBeLessThanOrEqual(
    Math.ceil(mobileScrollState.maxHeight)
  );
  expect(mobileScrollState.scrollHeight).toBeGreaterThan(mobileScrollState.clientHeight);
  expect(mobileScrollState.noHorizontalOverflow).toBe(true);
  expect(mobileScrollState.overflowY).toBe("auto");
  await expect(profileSearch).toBeVisible();
  await expect
    .poll(async () => {
      const [actionsBox, listBox] = await Promise.all([
        profileBulkActions.boundingBox(),
        profileAccessList.boundingBox(),
      ]);
      if (!actionsBox || !listBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - listBox.x);
    })
    .toBeLessThanOrEqual(1);
  await page.getByTestId("security-permissions-submit").click();

  await expect
    .poll(() => (savedPayload?.allowed_profile_ids as string[] | undefined)?.sort())
    .toEqual(["default", "finance"]);
  const permissionPayload = savedPayload as Record<string, unknown> | null;
  expect(permissionPayload?.permissions).toEqual(["menu.query"]);
  expect(permissionPayload).not.toHaveProperty("display_name");
});

test("業務プロファイル管理権限のロールは全業務プロファイルとして扱う", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-profile-manager", role_code: "PROFILE_MANAGER", display_name: "業務プロファイル管理ロール" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [{ role_id: "role-profile-manager", role_code: "PROFILE_MANAGER", display_name: "業務プロファイル管理ロール" }],
      },
    ]))
  );
  const permissionRows = [
    {
      code: "menu.profiles",
      group: "メニュー権限",
      label: "業務プロファイル",
      description: "業務プロファイルを表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.manage"],
    },
    {
      code: "nl2sql.profiles.manage",
      group: "管理権限",
      label: "業務プロファイル管理",
      description: "業務プロファイルを管理できます。",
      implies: ["nl2sql.profiles.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
  ];
  const profileManagerRole = {
    ...systemRole,
    role_id: "role-profile-manager",
    role_code: "PROFILE_MANAGER",
    display_name: "業務プロファイル管理ロール",
    description: "個別 profile ID は保存しない",
    is_built_in: false,
    permissions: ["menu.profiles"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, profileManagerRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));
  await page.route("**/api/security/roles/role-profile-manager/permissions", async (route) => {
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfill(route, {
      ...profileManagerRole,
      version: 2,
      allowed_profile_ids: savedPayload.allowed_profile_ids,
    });
  });

  await page.goto("/settings/security/permissions");
  const grid = page.getByTestId("security-permissions-grid");
  await grid
    .locator("tbody tr")
    .filter({ hasText: "業務プロファイル管理ロール" })
    .locator("td")
    .first()
    .click();
  await expect(page.getByText("すべての業務プロファイル", { exact: true }).first()).toBeVisible();
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();

  await expect(
    page.getByText(
      "業務プロファイル管理権限により、すべての業務プロファイルを利用できます。個別選択は不要です。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(page.getByTestId("security-roles-profile-access-list")).toHaveCount(0);
  await page.getByTestId("security-permissions-submit").click();
  await expect.poll(() => savedPayload?.allowed_profile_ids).toEqual([]);
  await expectNoPageHorizontalScroll(page);
});

test("権限管理は業務プロファイル候補の取得失敗でもロール一覧を表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, "業務プロファイル候補の取得に失敗しました。", 500)
  );
  await page.route("**/api/security/permissions", (route) =>
    fulfill(route, [
      {
        code: "menu.query",
        group: "メニュー権限",
        label: "SQL 生成",
        description: "SQL 生成を表示し、関連操作を利用できます。",
        implies: [],
      },
    ])
  );
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [
      systemRole,
      {
        ...systemRole,
        role_id: "role-query-default",
        role_code: "QUERY_DEFAULT",
        display_name: "SQL 利用者",
        description: "標準 profile のみ",
        is_built_in: false,
        permissions: ["menu.query"],
        data_entitlements: [],
        allowed_profile_ids: ["default"],
      },
    ])
  );

  await page.goto("/settings/security/permissions");

  await expect(
    page.getByText("業務プロファイル利用権限の候補を読み込めませんでした。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(page.getByTestId("security-permissions-grid").getByText("SQL 利用者")).toBeVisible();
  await expect(page.getByText("対象データはありません。")).toHaveCount(0);
});

test("権限管理は詳細で権限名を伏せ、編集では SQL 生成由来の参照権限を継承表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const permissionRows = [
    {
      code: "menu.query",
      group: "メニュー権限",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.read", "nl2sql.schema.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "SQL 生成で業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
    {
      code: "nl2sql.schema.read",
      group: "参照権限",
      label: "スキーマ参照",
      description: "SQL 生成でスキーマ情報を参照できます。",
      implies: [],
    },
  ];
  const queryRole = {
    ...systemRole,
    role_id: "role-query-only",
    role_code: "QUERY_ONLY",
    display_name: "SQL 利用者",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, queryRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.goto("/settings/security/permissions");
  await page.getByTestId("security-permissions-grid").locator("tbody tr").filter({ hasText: "SQL 利用者" }).locator("td").first().click();

  await expect(page.getByText("業務プロファイル参照 (SQL 生成により付与)")).toHaveCount(0);
  await expect(page.getByText("スキーマ参照 (SQL 生成により付与)")).toHaveCount(0);
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  await expect(page.getByText("SQL 生成により付与", { exact: true }).first()).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ロール編集のその他の操作のメニューは画面の下端でも画面の中に開く", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  // ロール管理のフォームは基本情報だけなので、短い viewport で操作バーを下端に置く（#206）。
  await page.setViewportSize({ width: 1365, height: 480 });
  const permissionRows = [
    {
      code: "menu.query",
      group: "RAG",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: [],
    },
    ...Array.from({ length: 28 }, (_, index) => ({
      code: `menu.bottom_menu_check_${index + 1}`,
      group: index % 2 === 0 ? "システム設定" : "管理権限",
      label: `下端検証 ${String(index + 1).padStart(2, "0")}`,
      description: "下端メニューの表示位置を検証するための権限です。",
      implies: [],
    })),
  ];
  const activeRole = {
    ...systemRole,
    role_id: "role-bottom-menu",
    role_code: "BOTTOM_MENU",
    display_name: "下端メニュー検証ロール",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, activeRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.goto("/settings/security/roles");
  await page.getByTestId("security-roles-grid").locator("tbody tr").filter({ hasText: "下端メニュー検証ロール" }).locator("td").first().click();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();

  const editActions = page.getByRole("group", { name: "ロール編集操作" });
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  // 対象への操作はフォームのパネルの見出しの右（#618）。画面の下端に寄せても、メニューは画面の中に開く。
  const trigger = editActions.getByRole("button", { name: /^その他の操作/ });
  await trigger.evaluate((element) => element.scrollIntoView({ block: "end" }));
  const triggerBox = await trigger.boundingBox();
  expect(triggerBox).not.toBeNull();
  await trigger.click();

  const menu = page.getByRole("menu");
  await expectFloatingMenuInsideViewport(page, menu);
  const menuBox = await menu.boundingBox();
  expect(menuBox).not.toBeNull();
  if ((await menu.getAttribute("data-floating-menu-placement")) === "top") {
    expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(triggerBox!.y + 1);
  }
});

test("ロール管理の compact header は操作が 2 つなら畳まず、短い viewport でも横にはみ出さない", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.setViewportSize({ width: 375, height: 360 });
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  await page.goto("/settings/security/roles");
  const actions = page.getByTestId("security-roles-actions");
  // 共有 PageHeader は lg 未満で「その他の操作」+ 主操作に畳むが、メニューに入るのが 1 つだけなら
  // 畳まず両方を出す（1 項目のメニューを開かせない。#582）。
  await expect(actions.getByRole("button")).toHaveText(["最新情報を取得", "新規作成"]);
  await expect(actions.getByTestId("page-actions-more")).toHaveCount(0);
  const [refreshBox, createBox] = await Promise.all([
    actions.getByRole("button", { name: "最新情報を取得" }).boundingBox(),
    actions.getByRole("button", { name: "新規作成" }).boundingBox(),
  ]);
  // 2 つは 1 行に並ぶ（折り返してヘッダーを高くしない）。
  expect(Math.abs(refreshBox!.y - createBox!.y)).toBeLessThanOrEqual(1);
  await expectNoPageHorizontalScroll(page);
});

test("アーカイブ済みロールは権限が無効であることを明示し、権限管理でも編集できない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-archived-data", role_code: "DATA_ADMIN", display_name: "データ管理者" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [],
      },
    ]))
  );
  const permissionRows = [
    {
      code: "menu.query",
      group: "AI 活用",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.direct_sql",
      group: "AI 活用",
      label: "SELECT SQL を実行",
      description: "SELECT SQL 実行を表示し、関連操作を利用できます。",
      implies: [],
    },
  ];
  const activeRole = {
    ...systemRole,
    role_id: "role-active-data",
    role_code: "DATA_USER",
    display_name: "データユーザー",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const archivedRole = {
    ...systemRole,
    role_id: "role-archived-data",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: true,
    permissions: ["menu.query", "menu.direct_sql"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let roles: unknown[] = [systemRole, activeRole, archivedRole];
  let updateRoleRequestCount = 0;
  await page.route("**/api/security/roles?include_archived=true", (route) => fulfill(route, roles));
  await page.route("**/api/security/roles/role-archived-data", async (route) => {
    updateRoleRequestCount += 1;
    await fulfill(route, archivedRole);
  });
  await page.route("**/api/security/roles/role-active-data/archive", async (route) => {
    const updated = { ...activeRole, archived: true, version: activeRole.version + 1 };
    roles = [systemRole, updated, archivedRole];
    await fulfill(route, updated);
  });
  await page.route("**/api/security/roles/role-archived-data/restore", async (route) => {
    const restored = { ...archivedRole, archived: false, version: archivedRole.version + 1 };
    roles = [systemRole, activeRole, restored];
    await fulfill(route, restored);
  });
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  // 権限管理: アーカイブ済みロールは権限を編集できず、無効であることを示す。
  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/permissions");
  const permissionGrid = page.getByTestId("security-permissions-grid");
  await permissionGrid.locator("tbody tr").filter({ hasText: "データ管理者" }).locator("td").first().click();
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(page.getByTestId("security-permissions-detail-actions")).toHaveCount(0);
  await expect(page.getByRole("checkbox")).toHaveCount(0);

  await page.goto("/settings/security/roles");

  const grid = page.getByTestId("security-roles-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const archivedRow = grid.locator("tbody tr").filter({ hasText: "データ管理者" });
  await archivedRow.locator("td").first().click();
  await expect(archivedRow.getByText("アーカイブ済み", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toBeVisible();

  const archivedDetail = page.locator("section", { has: page.getByRole("heading", { name: "DATA_ADMIN", exact: true }) });
  await expect(archivedDetail.getByText("SQL 生成", { exact: true })).toHaveCount(0);

  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toBeVisible();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  const archivedEditPanel = page.locator("#security-roles-panel-edit");
  const archivedEditActions = page.getByRole("group", { name: "ロール編集操作" });
  const roleName = page.getByLabel("ロール名");
  const roleDescription = page.getByLabel("説明");
  await expect(roleName).toBeDisabled();
  await expect(roleDescription).toBeDisabled();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).focus();
  await page.keyboard.press("Tab");
  await expect(archivedEditActions.getByRole("button", { name: "復元" })).toBeFocused();
  await archivedEditPanel.locator("form").evaluate((form) =>
    (form as HTMLFormElement).requestSubmit()
  );
  expect(updateRoleRequestCount).toBe(0);
  await expect(page.getByTestId("security-roles-submit")).toHaveCount(0);
  await expect(archivedEditActions.getByRole("button", { name: "復元" })).toBeVisible();
  await archivedEditActions.getByRole("button", { name: "復元" }).click();
  const restoreDialog = page.getByRole("alertdialog");
  await expect(restoreDialog).toBeVisible();
  await expect(
    restoreDialog.getByText(
      "このロールを復元すると、このロールに紐づくユーザーへ、このロール由来の権限が次回リクエストから反映されます。",
      { exact: true }
    )
  ).toBeVisible();
  await restoreDialog.getByRole("button", { name: "実行" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  const restoredRow = grid.locator("tbody tr").filter({ hasText: "データ管理者" });
  await expect(restoredRow.getByText("カスタム", { exact: true })).toBeVisible();
  await expect(restoredRow.getByText("アーカイブ済み", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toHaveCount(0);
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(roleName).toBeEnabled();
  await expect(roleDescription).toBeEnabled();
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expectNoPageHorizontalScroll(page);

  const activeRow = grid.locator("tbody tr").filter({ hasText: "データユーザー" });
  await activeRow.locator("td").first().click();
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "アーカイブ" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("group", { name: "ロール編集操作" }).getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByRole("group", { name: "ロール編集操作" }).getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  const confirmDialog = page.getByRole("alertdialog");
  await expect(confirmDialog).toBeVisible();
  await expect(
    confirmDialog.getByText(
      "このロールをアーカイブすると、このロール由来の権限は利用者に反映されなくなります。ユーザー自体は無効化されません。",
      { exact: true }
    )
  ).toBeVisible();
});

test("アーカイブ済みカスタムロールの削除は409を保持し再試行できる", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const activeRole = {
    ...systemRole,
    role_id: "role-active-delete",
    role_code: "ACTIVE_DELETE",
    display_name: "有効カスタムロール",
    is_built_in: false,
  };
  const archivedRole = {
    ...activeRole,
    role_id: "role-archived-delete",
    role_code: "ARCHIVED_DELETE",
    display_name: "削除対象ロール",
    archived: true,
    version: 9,
  };
  let roles = [systemRole, activeRole, archivedRole];
  let deleteCount = 0;
  const observedIfMatch: string[] = [];

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, roles)
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  await page.route("**/api/security/roles/role-archived-delete", async (route) => {
    deleteCount += 1;
    observedIfMatch.push(route.request().headers()["if-match"] ?? "");
    if (deleteCount === 1) {
      const response = problemEnvelope({
        status: 409,
        code: "SECURITY_ROLE_DELETE_ASSIGNED",
        title: "現在の状態では削除できません",
        detail:
          "このロールはユーザーに割り当てられています。割り当てを解除してから削除してください。",
        requestId: "role-delete-conflict",
      });
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify(response),
      });
      return;
    }
    roles = roles.filter((role) => role.role_id !== archivedRole.role_id);
    await fulfill(route, {
      deleted: true,
      role_id: archivedRole.role_id,
      role_code: archivedRole.role_code,
    });
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/roles");

  const grid = page.getByTestId("security-roles-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const activeRow = grid.locator("tbody tr").filter({ hasText: "有効カスタムロール" });
  await activeRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-roles-detail-actions");
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  const archivedRow = grid.locator("tbody tr").filter({ hasText: "削除対象ロール" });
  await archivedRow.locator("td").first().click();
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  const deleteDialog = page.getByRole("alertdialog", { name: "削除" });
  await expect(deleteDialog).toContainText(
    "「削除対象ロール」を完全に削除します。"
  );
  await expect(deleteDialog).toContainText("ロールに設定した権限も削除され");
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ロール", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("main")).toContainText(
    "このロールはユーザーに割り当てられています。割り当てを解除してから削除してください。"
  );

  await page
    .getByTestId("security-roles-detail-actions")
    .getByRole("button", { name: "編集" })
    .click();
  const editActions = page.getByRole("group", { name: "ロール編集操作" });
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ロール", { exact: true })).toHaveCount(0);
  await expect(page.getByText("削除対象ロール を完全に削除しました。", { exact: true }).last()).toBeVisible();
  expect(observedIfMatch).toEqual(['"9"', '"9"']);
  expect(deleteCount).toBe(2);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ロール・権限管理はオントロジー提案取得が遅延しても読み込み完了する", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) => fulfill(route, systemAdminMe));
  const profile = {
    id: "default",
    name: "標準プロファイル",
    category: "",
    description: "",
    archived: false,
    allowed_tables: [],
    allowed_views: [],
    glossary: {},
    few_shot_examples: [],
    version: 1,
    etag: "profile-etag",
    updated_at: "2026-07-21T00:00:00Z",
  };
  let markdownRequests = 0;
  let releaseProposals = () => {};
  const proposalsGate = new Promise<void>((resolve) => {
    releaseProposals = resolve;
  });

  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfill(route, {
      items: [
        {
          id: profile.id,
          name: profile.name,
          category: profile.category,
          description: profile.description,
          archived: profile.archived,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: profile.version,
          etag: profile.etag,
          updated_at: profile.updated_at,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default", (route) => fulfill(route, profile));
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfill(route, { profile_ontology_view: null, ontology_graph: null, warnings_ja: [] })
  );
  await page.route("**/api/nl2sql/ontology/revisions", (route) =>
    fulfill(route, { revisions: [], active_revision_id: "" })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-markdown", async (route) => {
    markdownRequests += 1;
    await proposalsGate;
    try {
      await fulfill(route, {
        draft_markdown: "",
        published_markdown: "",
        draft_revision: null,
        published_revision: null,
        draft_etag: "",
        published_at: null,
      });
    } catch {
      // 画面遷移で abort 済みの request は fulfill できない場合がある。
    }
  });
  await page.route("**/api/nl2sql/profiles/default/ontology-build-jobs**", (route) =>
    fulfill(route, { jobs: [] })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-source-documents**", (route) =>
    fulfill(route, { source_documents: [] })
  );
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  try {
    await page.goto("/ontology-build?profile=default");
    await page.getByRole("button", { name: "情報を取得", exact: true }).click();
    await expect(page.getByTestId("profile-ontology-build")).toBeVisible({
      timeout: 30_000,
    });
    await expect.poll(() => markdownRequests).toBeGreaterThan(0);

    await page.goto("/settings/security/roles");

    const grid = page.getByTestId("security-roles-grid");
    await expect(grid).toBeVisible();
    await expect(grid.locator("tbody tr")).toHaveCount(1);
    await expect(grid.locator(".animate-pulse")).toHaveCount(0);
    await expect(grid.getByText("システム管理者")).toBeVisible();
    await expectNoPageHorizontalScroll(page);
  } finally {
    releaseProposals();
  }
});

// #608: 対象 table/view は 1 つを選ぶので、検索できる選択欄（SearchableSelectField）。候補は数千件になりうるので、
// サーバー側で検索し、続きは 50 件ずつ読む。desktop / 375px（projects）とライト / ダークで確かめる。
// #1708: 1 ページ目の候補が一覧の高さに満たず続きがあるとき（以前の backend は共通基盤・他製品のシステムテーブルを
// 取った後に落とし、1 ページ目が表 3 件だけになった）、スクロールを待たずに続きを読み、後ろに並ぶ view まで出す。
async function mockReviewedUsers(page: Page, locked = false) {
  await mockDatabaseGateReady(page);
  const role = { ...systemRole, role_id: "review-role", role_code: "REVIEW", display_name: "レビュー", is_built_in: false };
  const user = { user_uuid: "review-user", login_user_id: "review.user", display_name: "レビュー利用者", status: "ACTIVE", force_password_change: false, locked_until: locked ? "2099-01-01T00:00:00Z" : null, version: 1, role_ids: [role.role_id], is_bootstrap_admin: false };
  await page.route("**/api/security/roles?include_archived=false", route => fulfill(route, [role]));
  await page.route("**/api/security/users", route => fulfill(route, [user]));
  return user;
}

test("ユーザー管理レビュー: リセット待機中は編集対象と入力を固定する", async ({ page }) => {
  const user = await mockReviewedUsers(page);
  let pending: Route | undefined;
  await page.route("**/api/security/users/review-user/reset-password", route => { pending = route; });
  await page.goto("/settings/security/users?user=review-user");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集", exact: true }).click();
  await page.getByRole("button", { name: "パスワードをリセット", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "実行", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await expect(page.getByLabel("表示名", { exact: false })).toBeDisabled();
  await fulfill(pending!, { user, temporary_password: "SyntheticOnly!123" });
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue("SyntheticOnly!123");
  await expect(page.getByLabel("ログインユーザーID")).toHaveValue("review.user");
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeEnabled();
});

test("ユーザー管理レビュー: ロック解除の重複送信を抑止し失敗後に再試行する", async ({ page }) => {
  const user = await mockReviewedUsers(page, true);
  let pending: Route | undefined;
  let calls = 0;
  await page.route("**/api/security/users/review-user/unlock", route => { pending = route; calls += 1; });
  await page.goto("/settings/security/users?user=review-user");
  const actions = page.getByTestId("security-users-detail-actions");
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "ロック解除" }).click();
  await expect.poll(() => calls).toBe(1);
  await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "新規作成", exact: true })).toBeDisabled();
  await fulfill(pending!, "解除に失敗しました", 503);
  await expect(page.getByText("解除に失敗しました", { exact: true })).toBeVisible();
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "ロック解除" }).click();
  await expect.poll(() => calls).toBe(2);
  await fulfill(pending!, { ...user, locked_until: null });
  await expect(page.getByText("解除に失敗しました", { exact: true })).toHaveCount(0);
  await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeEnabled();
});

test("ユーザー管理レビュー: 未保存入力は戻ると内部リンクで破棄を確認する", async ({ page }) => {
  await mockReviewedUsers(page);
  await page.goto("/settings/security/users");
  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await page.getByLabel("表示名", { exact: false }).fill("未保存の利用者");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByLabel("表示名", { exact: false })).toHaveValue("未保存の利用者");
  await page.evaluate(() => {
    const link = document.createElement("a");
    link.href = "/settings/security/roles";
    link.textContent = "レビュー用内部リンク";
    // AppShell の外枠は fixed inset-0 で視口を覆う（#855）。body の末尾のリンクは外枠の下に隠れて押せないため、
    // 利用者が押せる本文（main#pr-main）の中に置く。
    document.getElementById("pr-main")!.append(link);
  });
  await page.getByRole("link", { name: "レビュー用内部リンク" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/security\/users/);
  const protectedReload = await page.evaluate(() => !window.dispatchEvent(new Event("beforeunload", { cancelable: true })));
  expect(protectedReload).toBe(true);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("security-users-grid")).toBeVisible();
});

async function mockReviewedRoles(page: Page, archived = false) {
  await mockDatabaseGateReady(page);
  const role = { ...systemRole, role_id: "review-role", role_code: "REVIEW", display_name: "レビュー役割", is_built_in: false, archived, permissions: ["menu.query", "menu.history"], allowed_profile_ids: [] };
  await page.route("**/api/security/roles?include_archived=true", route => fulfill(route, [role]));
  await page.route("**/api/security/permissions", route => fulfill(route, [
    { code: "menu.query", label: "SQL 生成", group: "AI 活用", description: "", implies: [] },
    { code: "menu.history", label: "実行履歴", group: "AI 活用", description: "", implies: [] },
  ]));
  return role;
}

for (const action of ["archive", "restore"] as const) {
  test(`ロール管理レビュー: ${action} 待機中の対象固定と失敗後の再試行`, async ({ page }) => {
    const role = await mockReviewedRoles(page, action === "restore");
    const label = action === "archive" ? "アーカイブ" : "復元";
    let pending: Route | undefined;
    let calls = 0;
    await page.route(`**/api/security/roles/review-role/${action}`, route => { pending = route; calls += 1; });
    await page.goto("/settings/security/roles?role=review-role");
    const actions = page.getByTestId("security-roles-detail-actions");
    const invoke = async () => {
      const direct = actions.getByRole("button", { name: label, exact: true });
      if (await direct.count()) await direct.click();
      else {
        await actions.getByRole("button", { name: "その他の操作" }).click();
        await page.getByRole("menuitem", { name: label, exact: true }).click();
      }
      await page.getByRole("alertdialog").getByRole("button", { name: "実行", exact: true }).click();
    };
    await expect(actions).toBeVisible();
    await invoke();
    await expect.poll(() => calls).toBe(1);
    await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "新規作成", exact: true })).toBeDisabled();
    await fulfill(pending!, "ロール操作に失敗しました", 503);
    await expect(page.getByText("ロール操作に失敗しました", { exact: true })).toBeVisible();
    await invoke();
    await expect.poll(() => calls).toBe(2);
    await fulfill(pending!, { ...role, archived: action === "archive", version: role.version + 1 });
    await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeEnabled();
    await expect(page.getByText("ロール操作に失敗しました", { exact: true })).toHaveCount(0);
  });
}

test("ロール管理レビュー: 未保存変更を保護し保存中は入力を固定する", async ({ page }) => {
  const role = await mockReviewedRoles(page);
  let pending: Route | undefined;
  await page.route("**/api/security/roles/review-role", route => { pending = route; });
  await page.goto("/settings/security/roles?role=review-role");
  const edit = () => page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集", exact: true }).click();
  await edit();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await edit();
  const name = page.getByLabel("ロール名", { exact: false });
  await name.fill("未保存ロール");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(name).toHaveValue("未保存ロール");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  // ロール管理の保存は基本情報だけを送る（#206）。
  expect(pending!.request().postDataJSON()).toEqual({
    version: role.version,
    display_name: "未保存ロール",
    description: role.description,
  });
  await expect(name).toBeDisabled();
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await fulfill(pending!, { ...role, display_name: "未保存ロール", version: role.version + 1 });
  await expect(name).toBeEnabled();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});

test("権限管理レビュー: 選択順を除いて未保存変更を保護し保存中は入力を固定する", async ({ page }) => {
  const role = await mockReviewedRoles(page);
  let pending: Route | undefined;
  await page.route("**/api/security/roles/review-role/permissions", route => { pending = route; });
  await page.goto("/settings/security/permissions?role=review-role");
  const edit = () => page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集", exact: true }).click();
  await edit();
  const query = page.getByRole("checkbox", { name: /SQL 生成/ });
  await query.uncheck();
  await query.check();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await edit();
  await query.uncheck();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(query).not.toBeChecked();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  expect(pending!.request().method()).toBe("PUT");
  expect(pending!.request().postDataJSON()).toEqual({
    version: role.version,
    permissions: ["menu.history"],
    allowed_profile_ids: [],
  });
  await expect(query).toBeDisabled();
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await fulfill(pending!, { ...role, permissions: ["menu.history"], version: role.version + 1 });
  await expect(query).toBeEnabled();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});


// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

test("ユーザー / ロール一覧の選択行はライト / ダークで文字が 4.5:1 以上になり、左バーでも選択を示す (#571)", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const customRole = { ...systemRole, role_id: "role-contrast", role_code: "CONTRAST_VIEWER", display_name: "コントラスト確認", description: "表示のみ", is_built_in: false };
  const users = ["contrast.first", "contrast.second"].map((login, index) => ({
    user_uuid: `contrast-user-${index}`,
    login_user_id: login,
    display_name: `コントラスト確認ユーザー ${index + 1}`,
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [customRole.role_id],
    is_bootstrap_admin: false,
  }));
  await page.route("**/api/security/roles?include_archived=*", (route) => fulfill(route, [systemRole, customRole]));
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  // 選択行と一覧を並べて見る 2 ペイン（xl 以上）で測る（xl 未満は選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const theme of ["light", "dark"] as const) {
    // theme.ts は ui-store のテーマ選好から html.dark を決めるため、永続化された選好として描画前に与える
    // （addInitScript は登録順に実行されるので、後の周回のテーマが勝つ）。
    await page.addInitScript((value) => {
      const key = "production-ready-nl2sql.ui";
      const stored = JSON.parse(window.localStorage.getItem(key) ?? "null") ?? { state: {}, version: 0 };
      stored.state = { ...stored.state, theme: value };
      window.localStorage.setItem(key, JSON.stringify(stored));
    }, theme);

    for (const prefix of ["security-users", "security-roles"] as const) {
      // 選択は URL が持ち、先頭の行を自動で選ばない（#2282）。
      await page.goto(
        prefix === "security-users"
          ? "/settings/security/users?user=contrast-user-0"
          : "/settings/security/roles?role=role-contrast"
      );
      if (theme === "dark") await expect(page.locator("html")).toHaveClass(/\bdark\b/);
      else await expect(page.locator("html")).not.toHaveClass(/\bdark\b/);
      const grid = page.getByTestId(`${prefix}-grid`);
      const selected = grid.locator('tbody tr[data-selected="true"]');
      await expect(selected).toHaveCount(1);
      const unselected = grid.locator('tbody tr[data-selected="false"]').first();
      await unselected.hover();

      const measured = await selected.evaluate((row) => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d", { willReadFrequently: true })!;
        const rgb = (color: string) => {
          context.clearRect(0, 0, 1, 1);
          context.fillStyle = color;
          context.fillRect(0, 0, 1, 1);
          return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
        };
        const luminance = (color: number[]) => {
          const [r, g, b] = color.map((v) => (v / 255 <= 0.03928 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4));
          return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        };
        const contrast = (a: number[], b: number[]) => {
          const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
          return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
        };
        const rowBg = rgb(getComputedStyle(row).backgroundColor);
        const button = row.querySelector("button")!;
        const firstCellShadow = getComputedStyle(row.firstElementChild!).boxShadow;
        const bar = firstCellShadow.match(/(rgba?\([^)]*\)) [\d.]+px 0px 0px 0px inset/);
        const subtitle = button.querySelector(":scope > span:nth-of-type(2)");
        const code = button.querySelector("span.font-mono");
        return {
          // ユーザーは表示名（ログイン ID・UUID を出さない。#1696）、ロールはロール名（コード・ID は出さない。#1682）。
          // 2 行目の補足は割り当てロール・説明（狭い一覧の列。#2282）。
          firstLineIsCode: button.firstElementChild?.querySelector(".font-mono") != null,
          hasSubtitle: subtitle != null,
          title: contrast(rgb(getComputedStyle(button.firstElementChild!).color), rowBg),
          name: subtitle ? contrast(rgb(getComputedStyle(subtitle).color), rowBg) : 0,
          code: code ? contrast(rgb(getComputedStyle(code).color), rowBg) : 0,
          body: contrast(rgb(getComputedStyle(row).color), rowBg),
          bar: bar ? contrast(rgb(bar[1]), rowBg) : 0,
          rowBgLuminance: luminance(rowBg),
        };
      });
      // ダークの選択行は暗い地のまま（明るい地に文字を載せない）。
      if (theme === "dark") expect(measured.rowBgLuminance).toBeLessThan(0.05);
      expect(measured.title, `${prefix} ${theme} 題名`).toBeGreaterThanOrEqual(4.5);
      // ユーザーの行は表示名（#1696）、ロールの行はロール名（#1682）。ID・コードの mono は出さない。
      // 補足の行（割り当てロール・説明。#2282）も 4.5:1 以上。
      expect(measured.firstLineIsCode, `${prefix} ${theme} ID・コードを出さない`).toBe(false);
      expect(measured.hasSubtitle, `${prefix} ${theme} 補足あり`).toBe(true);
      expect(measured.name, `${prefix} ${theme} 補足`).toBeGreaterThanOrEqual(4.5);
      expect(measured.body, `${prefix} ${theme} 本文`).toBeGreaterThanOrEqual(4.5);
      // 選択を背景色の差だけで示さない（WCAG 1.4.1）。左バーは非テキストの 3:1 以上。
      expect(measured.bar, `${prefix} ${theme} 左バー`).toBeGreaterThanOrEqual(3);
      await expect(unselected).not.toHaveAttribute("data-surface-tint");
    }
  }
});

