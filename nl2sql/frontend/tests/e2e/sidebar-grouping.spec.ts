import { expect, test, type Page } from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import type { CurrentUser } from "../../src/features/security/types";

const SYSTEM_ADMIN_USER: CurrentUser = {
  user_uuid: "admin",
  login_user_id: "SYSTEM",
  display_name: "システム管理者",
  status: "ACTIVE",
  force_password_change: false,
  role_codes: ["SYSTEM_ADMIN"],
  role_display_names: ["システム管理者"],
  is_system_admin: true,
  permissions: [],
  allowed_profile_ids: [],
  debug_mode: false,
  password_change_allowed: true,
};

async function mockApi(page: Page, currentUser: CurrentUser = SYSTEM_ADMIN_USER) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const data =
      path === "/api/auth/me"
        ? currentUser
        : path === "/api/ready/database"
        ? { status: "ok", check: "ok", detail: null }
        : path === "/api/nl2sql/persistence"
          ? {
              mode: "oracle",
              ready: true,
              durable: true,
              writable: true,
              snapshot_loaded: true,
              reason_code: null,
              checked_at: "2026-07-19T00:00:00Z",
            }
        : path === "/api/schema/catalog/head"
          ? {
              catalog_version: 0,
              schema_fingerprint: "",
              refreshed_at: "2026-07-10T00:00:00Z",
              object_count: 0,
              column_count: 0,
              change_token: 0,
              etag: "",
            }
          : path === "/api/schema/objects"
            ? { items: [], next_cursor: null, total: 0, catalog_version: 0 }
            : path === "/api/schema/catalog"
              ? { refreshed_at: "2026-07-10T00:00:00Z", tables: [] }
        : path === "/api/nl2sql/profiles/search"
          ? { items: [], next_cursor: null, total: 0, change_token: 0 }
        : path === "/api/nl2sql/profiles"
          ? []
        : path === "/api/settings/upload-storage"
          ? {
              backend: "local",
              local_storage_dir: "/u01/data/production-ready-nl2sql",
              object_storage_region: "ap-osaka-1",
              object_storage_namespace: "exampletenancy",
              object_storage_bucket: "nl2sql-originals",
              readiness: "ok",
              max_upload_bytes: 104857600,
              config_source: "runtime",
            }
          : path === "/api/security/users" || path === "/api/security/roles"
            ? []
          : path === "/api/nl2sql/history"
            ? { items: [] }
            : null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data, error_messages: [], warning_messages: [] }),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("サイドバーを producer / consumer 思想のユーザー向け 5 セクションで表示する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await expect(page).toHaveURL(/\/query$/);
  await expect(sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" })).toHaveAttribute(
    "aria-expanded",
    "true"
  );
  for (const section of ["データ準備", "改善・運用", "セキュリティ設定", "ユーザーとロール", "運用設定", "システム設定"]) {
    await expect(sidebar.getByRole("button", { name: `${section} を展開` })).toHaveAttribute(
      "aria-expanded",
      "false"
    );
  }
  const activeQueryLink = sidebar.getByRole("link", { name: "SQL 生成" });
  await expect(activeQueryLink).toHaveAttribute("aria-current", "page");
  const [activeBackground, inactiveBackground] = await Promise.all([
    activeQueryLink.evaluate((element) => getComputedStyle(element).backgroundColor),
    sidebar
      .getByRole("link", { name: "SELECT SQL を実行" })
      .evaluate((element) => getComputedStyle(element).backgroundColor),
  ]);
  expect(activeBackground).not.toBe(inactiveBackground);
  await expect(activeQueryLink.locator("span.absolute")).toHaveCount(1);
  await expect(sidebar.getByText("テーブルの管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("フィードバック管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("ユーザー管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("OCI 認証", { exact: true })).toBeHidden();

  const menuIconSignatures = await sidebar.locator("nav a svg").evaluateAll((icons) =>
    icons.map((icon) => icon.innerHTML.replace(/\s+/g, " ").trim())
  );
  // AI 活用の先頭にチャットを足した（#871）。ユーザーとロールの末尾の API キー（#1805）、データ準備の
  // DB 管理チャット（#2264）、運用設定の回答の注意書き（#2405・#2449）とチャットの添付（#2517）と軌跡の記録（#3901）を足して 36 項目。
  expect(menuIconSignatures).toHaveLength(36);
  expect(new Set(menuIconSignatures).size).toBe(menuIconSignatures.length);

  for (const section of ["データ準備", "AI 活用", "改善・運用", "セキュリティ設定", "ユーザーとロール", "運用設定", "システム設定"]) {
    await expect(sidebar.getByText(section, { exact: true })).toBeVisible();
  }

  const aiUseBox = await sidebar.getByText("AI 活用", { exact: true }).boundingBox();
  const dataPrepareBox = await sidebar.getByText("データ準備", { exact: true }).boundingBox();
  const securityBox = await sidebar.getByText("セキュリティ設定", { exact: true }).boundingBox();
  const operationsBox = await sidebar.getByText("運用設定", { exact: true }).boundingBox();
  const userRolesBox = await sidebar.getByText("ユーザーとロール", { exact: true }).boundingBox();
  const settingsBox = await sidebar.getByText("システム設定", { exact: true }).boundingBox();
  if (!aiUseBox || !dataPrepareBox || !securityBox || !operationsBox || !userRolesBox || !settingsBox) {
    throw new Error("セクション見出しの位置を取得できませんでした。");
  }
  expect(aiUseBox.y).toBeLessThan(dataPrepareBox.y);
  // 下部は 3 製品で同じ「セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定」（#81 / #206 / #658）。
  expect(securityBox.y).toBeLessThan(userRolesBox.y);
  expect(userRolesBox.y).toBeLessThan(operationsBox.y);
  expect(operationsBox.y).toBeLessThan(settingsBox.y);

  for (const section of ["データ準備", "改善・運用", "セキュリティ設定", "ユーザーとロール", "運用設定", "システム設定"]) {
    await sidebar.getByRole("button", { name: `${section} を展開` }).click();
  }

  for (const label of [
    "管理 SQL を実行",
    "テーブルの管理",
    "ビューの管理",
    "データの管理",
    "コメント管理",
    "アノテーション管理",
    "ドメイン管理",
    "サンプルデータ管理",
    "業務プロファイル",
    "用語・同義語",
    "共通ルール",
  ]) {
    await expect(sidebar.getByText(label, { exact: true })).toBeVisible();
  }

  const aiUseLabels = ["チャット", "SQL 生成", "SELECT SQL を実行", "SQL から質問を生成", "実行履歴"];
  const aiUseItemBoxes = await Promise.all(
    aiUseLabels.map(async (label) => {
      const item = sidebar.getByText(label, { exact: true });
      await expect(item).toBeVisible();
      return item.boundingBox();
    })
  );
  if (aiUseItemBoxes.some((box) => box === null)) {
    throw new Error("AI 活用のメニュー位置を取得できませんでした。");
  }
  // AI 活用はチャットを先頭に置く（#871）。
  for (let index = 1; index < aiUseItemBoxes.length; index += 1) {
    expect(aiUseItemBoxes[index - 1]!.y).toBeLessThan(aiUseItemBoxes[index]!.y);
  }
  await expect(sidebar.getByText("SQL 確認・修復", { exact: true })).toHaveCount(0);

  const adminSqlBox = await sidebar.getByText("管理 SQL を実行", { exact: true }).boundingBox();
  const tableManagementBox = await sidebar.getByText("テーブルの管理", { exact: true }).boundingBox();
  if (!adminSqlBox || !tableManagementBox) {
    throw new Error("管理 SQL とテーブル管理メニューの位置を取得できませんでした。");
  }
  expect(adminSqlBox.y).toBeLessThan(tableManagementBox.y);

  for (const label of ["フィードバック管理", "質問分類モデル管理", "SQL生成評価"]) {
    await expect(sidebar.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(sidebar.getByRole("link", { name: "安全境界" })).toHaveCount(0);
  await expect(sidebar.getByText("エンジン運用", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("モデル学習", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("フィードバック学習", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("質問学習", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("接続診断", { exact: true })).toHaveCount(0);

  for (const label of ["OCI 認証", "アップロード保存先", "モデル", "データベース"]) {
    await expect(sidebar.getByText(label, { exact: true })).toBeVisible();
  }
  // 運用設定は「システムテーブル → 軌跡の記録 → チャットの添付 → 回答の注意書き → Select AI Credential」（#658・#2449・#2517・#2546）。
  const systemTablesBox = await sidebar.getByText("システムテーブル", { exact: true }).boundingBox();
  const traceRecordingBox = await sidebar.getByText("軌跡の記録", { exact: true }).boundingBox();
  const answerNoticeBox = await sidebar.getByText("回答の注意書き", { exact: true }).boundingBox();
  const attachmentsBox = await sidebar.getByText("チャットの添付", { exact: true }).boundingBox();
  const credentialBox = await sidebar.getByText("Select AI Credential", { exact: true }).boundingBox();
  if (!systemTablesBox || !traceRecordingBox || !answerNoticeBox || !attachmentsBox || !credentialBox) {
    throw new Error("運用設定のメニュー位置を取得できませんでした。");
  }
  expect(systemTablesBox.y).toBeLessThan(traceRecordingBox.y);
  expect(traceRecordingBox.y).toBeLessThan(attachmentsBox.y);
  expect(attachmentsBox.y).toBeLessThan(answerNoticeBox.y);
  expect(answerNoticeBox.y).toBeLessThan(credentialBox.y);

  // 「API キー」（全員のキーの管理。RAG / NL2SQL だけ。#1805）はユーザーとロールの末尾。アカウント欄の
  // 「API キー」（自分のキー。ボタン）と区別するため、ナビのリンクで探す。
  const securityLabels = ["権限管理", "ユーザー管理", "ロール管理", "API キー"];
  const securityItemBoxes = await Promise.all(
    securityLabels.map(async (label) => {
      const item = sidebar.getByRole("link", { name: label, exact: true });
      await expect(item).toBeVisible();
      return item.boundingBox();
    })
  );
  if (securityItemBoxes.some((box) => box === null)) {
    throw new Error("セキュリティとユーザー・ロールのメニュー位置を取得できませんでした。");
  }
  for (let index = 1; index < securityItemBoxes.length; index += 1) {
    expect(securityItemBoxes[index - 1]!.y).toBeLessThan(securityItemBoxes[index]!.y);
  }
  await expect(sidebar.getByRole("link", { name: "監査ログ" })).toHaveCount(0);
});

test("削除済みの安全境界 URL は専用 API を呼ばず、既存の全体 fallback へ移動する", { tag: "@responsive" }, async ({ page }) => {
  let diagnosticsRequests = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/nl2sql/diagnostics") diagnosticsRequests += 1;
  });

  await page.goto("/settings/nl2sql-database");

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByRole("heading", { name: "NL2SQL 安全境界・Readiness" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "安全境界" })).toHaveCount(0);
  expect(diagnosticsRequests).toBe(0);
});

test("削除済みの監査ログ URL は専用 API を呼ばず、既存の全体 fallback へ移動する", { tag: "@responsive" }, async ({ page }) => {
  let auditRequests = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/security/audit")) auditRequests += 1;
  });

  await page.goto("/settings/security/audit");

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByRole("link", { name: "監査ログ" })).toHaveCount(0);
  expect(auditRequests).toBe(0);
});

test("管理 SQL 画面からサイドバーを操作しても表示が空白にならない", { tag: "@responsive" }, async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/admin-sql");
  await expect(page.getByRole("heading", { level: 1, name: "管理 SQL を実行" })).toBeVisible();
  await page.locator("#admin-sql-input").fill("SELECT 1 FROM DUAL");

  // 375px ではナビがドロワー（#367）。リンクを押すと閉じるため、押す前に毎回開く。
  const sidebar = await openSidebarNav(page);
  const collapseDataPrep = sidebar.getByRole("button", { name: "データ準備 を折りたたむ" });
  if (await collapseDataPrep.isVisible()) {
    await collapseDataPrep.click();
    await expect(page.getByRole("heading", { level: 1, name: "管理 SQL を実行" })).toBeVisible();
    await sidebar.getByRole("button", { name: "データ準備 を展開" }).click();
  }

  await sidebar.getByRole("link", { name: "SELECT SQL を実行" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  await expect(page.getByRole("heading", { level: 1, name: "SELECT SQL を実行" })).toBeVisible();
  await openSidebarNav(page);
  await sidebar.getByRole("link", { name: "管理 SQL を実行" }).click();
  await expect(page).toHaveURL(/\/admin-sql$/);
  await expect(page.getByRole("heading", { level: 1, name: "管理 SQL を実行" })).toBeVisible();
  await expect(page.locator("#admin-sql-input")).toHaveValue("SELECT 1 FROM DUAL");

  await openSidebarNav(page);
  await sidebar.getByRole("link", { name: "SQL 生成" }).click();

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByRole("heading", { level: 1, name: "SQL 生成" })).toBeVisible();
  await expect(page.getByTestId("nl2sql-workspace-shell")).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("管理 SQL 権限だけの既定入口は管理 SQL 画面へ振り分ける", { tag: "@responsive" }, async ({ page }) => {
  await page.unroute("**/api/**");
  await mockApi(page, {
    ...SYSTEM_ADMIN_USER,
    user_uuid: "admin-sql-user",
    login_user_id: "admin.sql.user",
    display_name: "管理 SQL ユーザー",
    role_codes: ["ADMIN_SQL_OPERATOR"],
    is_system_admin: false,
    permissions: ["menu.admin_sql"],
  });

  await page.goto("/");

  await expect(page).toHaveURL(/\/admin-sql$/);
  await expect(page.getByRole("heading", { level: 1, name: "管理 SQL を実行" })).toBeVisible();
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "管理 SQL を実行" })).toHaveAttribute(
    "aria-current",
    "page"
  );
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toHaveCount(0);
});

test("共通ルールは用語・同義語の直下に独立メニューとして並び、専用ページへ遷移する", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.route("**/api/nl2sql/legacy-learning-material", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: { glossary: {}, rules: ["共通ルール", "SELECT/WITH のみ"] },
        error_messages: [],
        warning_messages: [],
      }),
    });
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await sidebar.getByRole("button", { name: "データ準備 を展開" }).click();
  const glossaryBox = await sidebar.getByText("用語・同義語", { exact: true }).boundingBox();
  const globalRulesBox = await sidebar.getByText("共通ルール", { exact: true }).boundingBox();
  if (!glossaryBox || !globalRulesBox) {
    throw new Error("メニュー項目の位置を取得できませんでした。");
  }
  // 用語・同義語 の「下」に並ぶ（= 中のタブではなく独立メニュー）。
  expect(globalRulesBox.y).toBeGreaterThan(glossaryBox.y);

  await sidebar.getByRole("link", { name: "共通ルール" }).click();
  await expect(page).toHaveURL(/\/global-rules$/);
  await expect(page.getByRole("heading", { name: "共通 SQL 生成ルール", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "共通 SQL 生成ルール", level: 2 })).toBeVisible();
});

test("セクション折りたたみで所属項目だけを隠し、他セクションは維持する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await sidebar.getByRole("button", { name: "データ準備 を展開" }).click();

  await expect(sidebar.getByText("テーブルの管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("SQL 生成", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("フィードバック管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("質問分類モデル管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("OCI 認証", { exact: true })).toBeHidden();

  await sidebar.getByRole("button", { name: "セキュリティ設定 を展開" }).click();
  await expect(sidebar.getByText("権限管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("ユーザー管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("テーブルの管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("OCI 認証", { exact: true })).toBeHidden();
});

test("手動で展開したセクションは再読み込み後も保存状態を維持する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await sidebar.getByRole("button", { name: "データ準備 を展開" }).click();
  await expect(sidebar.getByText("テーブルの管理", { exact: true })).toBeVisible();

  await page.reload();

  await expect(sidebar.getByRole("button", { name: "データ準備 を折りたたむ" })).toHaveAttribute(
    "aria-expanded",
    "true"
  );
  await expect(sidebar.getByText("テーブルの管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("フィードバック管理", { exact: true })).toBeHidden();
});

test("アクティブ経路のセクションは保存済み折りたたみ状態から自動展開する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await expect(sidebar.getByText("ユーザー管理", { exact: true })).toBeHidden();

  await page.goto("/settings/security/users");
  await expect(sidebar.getByRole("link", { name: "ユーザー管理" })).toHaveAttribute("aria-current", "page");
  await expect(sidebar.getByText("ロール管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "ユーザーとロール を折りたたむ" })).toHaveAttribute(
    "aria-expanded",
    "true"
  );
});

test("セクション見出しはキーボードで開閉できる", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");
  // root は最初の許可画面へ振り分ける。data router の navigate は非同期なので、振り分けが終わってから
  // 操作する（振り分け後の画面で、現在地のセクションに合わせて開閉状態が作り直されるため）。
  await expect(page).not.toHaveURL(/\/$/);

  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  const toggle = sidebar.getByRole("button", { name: "セキュリティ設定 を展開" });
  // 共有 Sidebar の disclosure は折りたたみ時 ChevronDown を -90deg(右向き)、展開時 0deg(下向き)にする
  // (docs/design-system/components-reference.md の Sidebar 参照実装)。旧 NL2SQL は globals.css で
  // 折りたたみ時だけ 90deg(左向き)に上書きしていたが、業務 repo で見た目を持たない規約により撤去済み。
  await expect.poll(() => toggle.locator("svg").evaluate((icon) => getComputedStyle(icon).rotate)).toBe("-90deg");
  await toggle.press("Enter");

  await expect(sidebar.getByText("権限管理", { exact: true })).toBeVisible();
  await expect(sidebar.getByText("監査ログ", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByRole("button", { name: "セキュリティ設定 を折りたたむ" })).toHaveAttribute(
    "aria-expanded",
    "true"
  );
  const expandedToggle = sidebar.getByRole("button", { name: "セキュリティ設定 を折りたたむ" });
  await expect.poll(() => expandedToggle.locator("svg").evaluate((icon) => getComputedStyle(icon).rotate)).toBe("0deg");

  await expandedToggle.press(" ");
  await expect(sidebar.getByText("権限管理", { exact: true })).toBeHidden();
  await expect(sidebar.getByText("監査ログ", { exact: true })).toHaveCount(0);
  await expect.poll(() => toggle.locator("svg").evaluate((icon) => getComputedStyle(icon).rotate)).toBe("-90deg");
});

test("SQL 生成権限がない既定入口は root から最初の許可画面へ振り分ける", { tag: "@responsive" }, async ({ page }) => {
  await page.unroute("**/api/**");
  await mockApi(page, {
    ...SYSTEM_ADMIN_USER,
    user_uuid: "appearance-user",
    login_user_id: "appearance.user",
    display_name: "外観閲覧ユーザー",
    role_codes: ["APPEARANCE_VIEWER"],
    is_system_admin: false,
    permissions: ["menu.settings_appearance"],
  });

  await page.goto("/");

  await expect(page).toHaveURL(/\/settings\/appearance$/);
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "外観と証明書" })).toHaveAttribute(
    "aria-current",
    "page"
  );
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toHaveCount(0);
});

test("settings_upload_storage 権限だけの既定入口は保存先設定へ振り分ける", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.unroute("**/api/**");
  await mockApi(page, {
    ...SYSTEM_ADMIN_USER,
    user_uuid: "upload-storage-user",
    login_user_id: "upload.storage.user",
    display_name: "保存先設定ユーザー",
    role_codes: ["UPLOAD_STORAGE_VIEWER"],
    is_system_admin: false,
    permissions: ["menu.settings_upload_storage"],
  });

  await page.goto("/");

  await expect(page).toHaveURL(/\/settings\/upload-storage$/);
  await expect(page.getByRole("heading", { name: "アップロード保存先" }).first()).toBeVisible();
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "アップロード保存先" })).toHaveAttribute(
    "aria-current",
    "page"
  );
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toHaveCount(0);
});

test("メニュー権限がない root は無権限画面へ移動し、再転送を繰り返さない", { tag: "@responsive" }, async ({ page }) => {
  await page.unroute("**/api/**");
  await mockApi(page, {
    ...SYSTEM_ADMIN_USER,
    user_uuid: "no-permissions-user",
    login_user_id: "no.permissions",
    display_name: "権限なしユーザー",
    role_codes: ["NO_ACCESS"],
    is_system_admin: false,
    permissions: [],
  });

  await page.goto("/");

  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  await page.goto("/query");
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

test("375px 幅ではナビがドロワーになり、開くと主要リンクへ到達できる（#367）", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/");
  // `/` は `/query` へ移る。移る前にドロワーを開くと、移ったときに閉じて展開のボタンが見えなくなる
  // （2 worker の nightly で 30 秒を超えた。#1519）。
  await expect(page).toHaveURL(/\/query$/);

  // 閉じている間はサイドナビを出さず、上端のバーの「メニュー」から開く。
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
  const sidebar = await openSidebarNav(page);

  // ドロワーの中は展開した幅で描くため、desktop と同じくセクションの開閉ボタンがある。
  await expect(sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" })).toBeVisible();
  // 既定で畳むセクション（nav-config の initiallyCollapsed）を開く。
  for (const section of ["データ準備", "改善・運用", "セキュリティ設定", "ユーザーとロール", "運用設定", "システム設定"]) {
    await sidebar.getByRole("button", { name: `${section} を展開`, exact: true }).click();
    await expect(sidebar.getByRole("button", { name: `${section} を折りたたむ`, exact: true })).toBeVisible();
  }
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "SQL から質問を生成" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "サンプルデータ管理" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "検証用サンプルデータ" })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "SQL生成評価" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "安全境界" })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "ユーザー管理" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "ロール管理" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "権限管理" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "監査ログ" })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "データベース設定" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth
    )
  ).toBe(true);
});
