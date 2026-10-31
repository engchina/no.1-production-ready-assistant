import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Page, type Route } from "./_helpers/test";

function envelope(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

async function fulfill(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(envelope(data)),
  });
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      user_uuid: "admin",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      role_codes: ["SYSTEM_ADMIN"],
      role_display_names: ["システム管理者"],
      is_system_admin: true,
      permissions: [],
      data_entitlements: [],
      debug_mode: false,
      password_change_allowed: true,
    })
  );
  await page.route("**/api/schema/owners", (route) =>
    fulfill(route, {
      current_owner: "NL2SQL_APP",
      owners: [],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, {
      status: "ready",
      schema_head: 15,
      applied_versions: [0, 1, 2, 3, 5, 6, 7, 8, 9, 15],
      pending_versions: [],
      expected_object_count: 51,
      existing_object_count: 51,
      missing_objects: [],
      tables: [],
      operation_state: {
        status: "idle",
        operation_kind: null,
        lease_expires_at: null,
        last_error_code: null,
        schema_epoch: 1,
        updated_at: "2026-07-19T00:00:00Z",
      },
    })
  );
});

const databaseSettings = {
  user: "NL2SQL_APP",
  dsn: "nl2sqldb_high",
  driver_mode: "thin",
  connection_security: "wallet_mtls",
  client_lib_dir: "",
  wallet_dir: "/wallet",
  wallet_uploaded: true,
  available_services: ["nl2sqldb_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1536,
  vector_column: "VECTOR(1536, FLOAT32)",
  adb_ocid: "ocid1.autonomousdatabase.oc1.ap-osaka-1.example",
  region: "ap-osaka-1",
  config_source: "runtime",
};

function adbInfo(lifecycleState: string) {
  return {
    status: lifecycleState === "STARTING" ? "accepted" : "success",
    message: `ADB ${lifecycleState}`,
    id: databaseSettings.adb_ocid,
    display_name: "nl2sqldb",
    lifecycle_state: lifecycleState,
    db_name: "NL2SQLDB",
    cpu_core_count: 2,
    data_storage_size_in_tbs: 1,
    region: "ap-osaka-1",
  };
}

async function mockDatabaseSettings(page: Page, lifecycle: () => string) {
  await page.route("**/api/settings/database", (route) => fulfill(route, databaseSettings));
  await page.route("**/api/settings/database/adb/settings", (route) =>
    fulfill(route, adbInfo(lifecycle()))
  );
  await page.route("**/api/settings/database/adb", (route) =>
    fulfill(route, adbInfo(lifecycle()))
  );
}

async function mockProfilePage(page: Page, persistenceMode: "memory" | "oracle" = "oracle") {
  const profiles = [
    {
      id: "persisted",
      name: "保存済みプロファイル",
      category: "営業",
      description: "Oracle snapshot から復元",
      allowed_tables: [],
      allowed_views: [],
      glossary: {},
      sql_rules: [],
      default_row_limit: 100,
      safety_policy: "select_only",
      few_shot_examples: [],
      select_ai_config: {
        profile_name: "NL2SQL_PERSISTED_PROFILE",
        region: "ap-osaka-1",
        model: "",
        embedding_model: "cohere.embed-v4.0",
        max_tokens: 32000,
        enforce_object_list: true,
        comments: true,
        annotations: false,
        constraints: false,
        role: "",
        additional_instructions: "",
      },
      archived: false,
    },
  ];
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: persistenceMode,
      ready: true,
      durable: persistenceMode === "oracle",
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/persistence/recover", (route) =>
    fulfill(route, {
      mode: persistenceMode,
      ready: true,
      durable: persistenceMode === "oracle",
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/schema/catalog", (route) =>
    fulfill(route, { refreshed_at: "2026-07-19T00:00:00Z", tables: [] })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-empty",
      refreshed_at: "2026-07-19T00:00:00Z",
      object_count: 0,
      column_count: 0,
      change_token: 1,
      etag: "schema-empty",
    })
  );
  await page.route("**/api/schema/objects?*", (route) =>
    fulfill(route, { items: [], next_cursor: null, total: 0, catalog_version: 1 })
  );
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfill(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfill(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/select-ai/db-profiles**", (route) =>
    fulfill(route, { runtime: "deterministic", profiles: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/profiles", (route) => fulfill(route, profiles));
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfill(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: 0,
        allowed_view_count: 0,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: "etag-persisted",
        updated_at: "2026-07-19T00:00:00Z",
      })),
      next_cursor: null,
      total: profiles.length,
      change_token: 1,
    })
  );
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
      )
    )
    .toBeTruthy();
}

const DATABASE_UNAVAILABLE_MESSAGE =
  "データベースが停止しているか、ネットワーク経由で到達できません。データベース設定で起動状態と接続情報を確認してから、再試行してください。";
const DATABASE_SETTINGS_HINT =
  "OCI 認証・アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。";

async function expectDatabaseGate(
  page: Page,
  {
    title = "データベースに接続できません",
    message = DATABASE_UNAVAILABLE_MESSAGE,
    actionName = "データベース設定を開く",
    actionHref = "/settings/database#adb-management",
    settingsHint = DATABASE_SETTINGS_HINT,
  }: {
    title?: string;
    message?: string;
    actionName?: string;
    actionHref?: string;
    settingsHint?: string;
  } = {}
) {
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(page.getByText(message, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
  const actionLink = page.getByRole("link", { name: actionName });
  await expect(actionLink).toHaveAttribute("href", actionHref);
  await expect(page.getByText(settingsHint, { exact: true })).toBeVisible();
  return actionLink;
}

test("DB 到達不可では Profile を空表示せず ADB 管理へ誘導する", async ({ page }) => {
  let profileRequests = 0;
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "unreachable", check: "ok", detail: "ORA-12514" })
  );
  await page.route("**/api/nl2sql/profiles", async (route) => {
    profileRequests += 1;
    await fulfill(route, []);
  });
  await mockDatabaseSettings(page, () => "STOPPED");

  await page.goto("/profiles");

  const settingsLink = await expectDatabaseGate(page);
  await expect(page.getByText("業務プロファイルがありません")).toHaveCount(0);
  expect(profileRequests).toBe(0);
  await settingsLink.focus();
  await expect(settingsLink).toBeFocused();
  await settingsLink.click();

  await expect(page).toHaveURL(/\/settings\/database#adb-management$/);
  await expect(page.locator("#adb-management")).toBeVisible();
  await expect(page.getByRole("button", { name: "起動" })).toBeEnabled();
  await expectNoHorizontalOverflow(page);
});

test("DB 到達不可ではシステムテーブル管理も共通の起動案内を表示する", async ({ page }) => {
  let systemTablesRequests = 0;
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "unreachable", check: "ok", detail: "ORA-12514" })
  );
  await page.route("**/api/settings/database/system-tables", (route) => {
    systemTablesRequests += 1;
    return fulfill(route, null, 503);
  });

  await page.goto("/settings/system-tables");

  await expectDatabaseGate(page);
  await expect(page.getByRole("heading", { name: "システムテーブル管理" })).toHaveCount(0);
  expect(systemTablesRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("未設定でも共通の起動案内から設定ページを Gate 外で開ける", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "not_configured", check: "missing", detail: null })
  );
  await mockDatabaseSettings(page, () => "STOPPED");

  await page.goto("/profiles");
  const settingsLink = await expectDatabaseGate(page, {
    title: "データベースの接続情報が未設定です",
    message:
      "NL2SQL の各機能（SQL 生成・データ準備・改善・運用）を利用するには、まずデータベースの接続情報を設定してください。設定が完了すると、この画面は自動的に利用できるようになります。",
    actionHref: "/settings/database",
  });
  await expect(page.getByText(/RAG 機能/)).toHaveCount(0);
  await expectNoHorizontalOverflow(page);

  // 未設定は接続情報の入力（ページの先頭）へ案内する（#820）。
  await settingsLink.click();
  await expect(page).toHaveURL(/\/settings\/database$/);
  await expect(page.locator("#adb-management")).toBeVisible();
});

test("migration 未適用では通常機能を止め、システムテーブル管理は開ける", async ({ page }) => {
  let persistenceRequests = 0;
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, {
      status: "setup_required",
      check: "migration_required",
      detail: "migration 3 is required",
    })
  );
  await page.route("**/api/nl2sql/persistence", (route) => {
    persistenceRequests += 1;
    return fulfill(route, {
      mode: "oracle",
      ready: false,
      durable: false,
      writable: false,
      snapshot_loaded: false,
      reason_code: "should_not_be_called",
      checked_at: "2026-07-19T00:00:00Z",
    });
  });

  await page.goto("/profiles");

  const systemTablesLink = await expectDatabaseGate(page, {
    title: "システムテーブルの作成・更新が必要です",
    message:
      "データベースには接続できています。NL2SQL のシステムテーブルが作成されていないか、未適用の更新（migration）があります。運用設定の「システムテーブル」で作成・更新してから、再試行してください。",
    actionName: "システムテーブルを開く",
    actionHref: "/settings/system-tables",
    settingsHint:
      "OCI 認証・アップロード保存先・モデル・データベース・システムテーブル・外観と証明書の各設定ページは引き続き利用できます。",
  });

  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, {
      status: "missing",
      schema_head: 15,
      applied_versions: [],
      pending_versions: [0, 1, 2, 3, 5, 6, 7, 8, 9, 15],
      expected_object_count: 51,
      existing_object_count: 0,
      missing_objects: [],
      tables: [],
      operation_state: {
        status: "idle",
        operation_kind: null,
        lease_expires_at: null,
        last_error_code: null,
        schema_epoch: 0,
        updated_at: null,
      },
    })
  );
  await systemTablesLink.click();
  await expect(page).toHaveURL(/\/settings\/system-tables$/);
  await expect(page.getByRole("heading", { name: "システムテーブル管理" })).toBeVisible();
  await expect(page.locator("#system-tables")).toBeVisible();
  await expect(
    page.locator("#system-tables").locator("span").filter({ hasText: /^未初期化$/ }).first()
  ).toBeVisible();
  expect(persistenceRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("STOPPED から起動し AVAILABLE 後もサービス利用状態と戻る導線を表示しない", async ({
  page,
}) => {
  let lifecycle = "STOPPED";
  let adbPollsAfterStart = 0;
  let recoveryRequests = 0;
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, {
      status: lifecycle === "AVAILABLE" ? "ok" : "unreachable",
      check: "ok",
      detail: lifecycle === "AVAILABLE" ? null : "ORA-12514",
    })
  );
  await page.route("**/api/settings/database", (route) => fulfill(route, databaseSettings));
  await page.route("**/api/settings/database/adb/settings", (route) =>
    fulfill(route, adbInfo(lifecycle))
  );
  await page.route("**/api/settings/database/adb/start", async (route) => {
    lifecycle = "STARTING";
    await fulfill(route, adbInfo(lifecycle));
  });
  await page.route("**/api/settings/database/adb", async (route) => {
    if (lifecycle === "STARTING") {
      adbPollsAfterStart += 1;
      if (adbPollsAfterStart >= 2) lifecycle = "AVAILABLE";
    }
    await fulfill(route, adbInfo(lifecycle));
  });
  await mockProfilePage(page);
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: recoveryRequests > 0,
      durable: true,
      writable: recoveryRequests > 0,
      snapshot_loaded: recoveryRequests > 0,
      reason_code: recoveryRequests > 0 ? null : "snapshot_load_failed",
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/persistence/recover", async (route) => {
    recoveryRequests += 1;
    await fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-07-19T00:00:01Z",
    });
  });

  await page.goto("/profiles");
  await page.getByRole("link", { name: /データベース設定を開く/ }).click();
  await expect(page.getByText("OCI ADB: 停止済み")).toBeVisible();
  await page.getByRole("button", { name: "起動" }).click();
  await expect(page.getByText("OCI ADB: 起動中")).toBeVisible();
  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "停止" })).toBeEnabled();
  await expect(page.getByText("サービス利用状態")).toHaveCount(0);
  await expect(page.getByText("Oracle SQL 接続")).toHaveCount(0);
  await expect(page.getByText("NL2SQL 保存領域")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存領域へ再接続" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "元の画面に戻る" })).toHaveCount(0);
  await expect(page).toHaveURL(/\/settings\/database#adb-management$/);
  expect(recoveryRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("ADB 起動済みでも設定画面にはサービス利用状態を表示しない", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "unreachable", check: "ok", detail: "ORA-12514" })
  );
  await mockDatabaseSettings(page, () => "AVAILABLE");

  await page.goto("/settings/database#adb-management");

  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible();
  await expect(page.getByText(/Wallet のサービス名、DSN、認証情報/)).toHaveCount(0);
  await expect(page.getByText("サービス利用状態")).toHaveCount(0);
  await expect(page.getByText("Oracle SQL 接続")).toHaveCount(0);
  await expect(page.getByText("NL2SQL 保存領域")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存領域へ再接続" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "起動" })).toBeDisabled();
  await expect(page.getByRole("link", { name: "元の画面に戻る" })).toHaveCount(0);
});

test("保存領域が閉じていても設定画面には分離表示と再接続を出さない", async ({
  page,
}) => {
  let recovered = false;
  let recoveryRequests = 0;
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: null })
  );
  await mockDatabaseSettings(page, () => "AVAILABLE");
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: recovered,
      durable: true,
      writable: recovered,
      snapshot_loaded: false,
      reason_code: recovered ? null : "incremental_schema_search_failed",
      checked_at: "2026-07-22T00:00:00Z",
      circuit_state: recovered ? "closed" : "open",
      retry_after_seconds: recovered ? 0 : 5,
    })
  );
  await page.route("**/api/nl2sql/persistence/recover", async (route) => {
    recoveryRequests += 1;
    if (recoveryRequests <= 2) {
      await fulfill(route, null, 503);
      return;
    }
    recovered = true;
    await fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: false,
      reason_code: null,
      checked_at: "2026-07-22T00:00:01Z",
      circuit_state: "closed",
      retry_after_seconds: 0,
    });
  });

  await page.goto("/profiles");
  await expect(
    page.getByRole("heading", { name: "保存済みの業務データを復元できません" })
  ).toBeVisible();
  await page.getByRole("link", { name: "データベース設定を開く" }).click();

  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible();
  await expect(page.getByText("サービス利用状態")).toHaveCount(0);
  await expect(page.getByText("Oracle SQL 接続")).toHaveCount(0);
  await expect(page.getByText("NL2SQL 保存領域")).toHaveCount(0);
  await expect(page.getByText(/incremental_schema_search_failed/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存領域へ再接続" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "元の画面に戻る" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(page.locator("#adb-management")).toBeVisible();
  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible();
  await expect(page.getByText("サービス利用状態")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("明示 memory モードでは利用を許可し再起動で失う警告を常設する", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: "memory" })
  );
  await mockProfilePage(page, "memory");

  await page.goto("/profiles");

  await expect(page.getByText("非永続モードで実行中です")).toBeVisible();
  await expect(page.getByText(/バックエンドの再起動で失われます/)).toBeVisible();
  await expect(page.getByText("保存済みプロファイル")).toBeVisible();
});

test("接続設定の不備は診断コードと補足を示し、ok の診断コードは出さない", async ({ page }) => {
  let check = "walletless_tls_dsn_required";
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, check === "ok"
      ? { status: "unreachable", check, detail: "Oracle connection probe failed (ORA-12514)." }
      : { status: "not_configured", check, detail: null })
  );

  await page.goto("/profiles");
  await expectDatabaseGate(page, {
    title: "データベースの接続情報が未設定です",
    message:
      "NL2SQL の各機能（SQL 生成・データ準備・改善・運用）を利用するには、まずデータベースの接続情報を設定してください。設定が完了すると、この画面は自動的に利用できるようになります。",
    actionHref: "/settings/database",
  });
  await expect(page.getByText("診断コード: walletless_tls_dsn_required", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "Walletless TLS では、Wallet のサービス名ではなく ADB の TCPS 接続文字列または Easy Connect の DSN を指定してください。",
      { exact: true }
    )
  ).toBeVisible();

  // DeepSec の設定の不備は NL2SQL の文言で補足する。
  check = "invalid_configuration";
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByText("診断コード: invalid_configuration", { exact: true })).toBeVisible();
  await expect(page.getByText(/Oracle Deep Data Security は python-oracledb の Thin mode でだけ使えます/)).toBeVisible();

  // 設定は揃っていて接続できないだけのとき（check=ok）は、診断コードも ORA コードも出さない。
  check = "ok";
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByRole("heading", { name: "データベースに接続できません" })).toBeVisible();
  await expect(page.getByText(/診断コード/)).toHaveCount(0);
  await expect(page.getByText(/ORA-12514/)).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("バックエンドの状態確認失敗では再試行だけを出す（設定を直しても解消しないため）", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["バックエンドに接続できません。"],
        warning_messages: [],
      }),
    })
  );

  await page.goto("/profiles");

  const card = page.locator('section[aria-labelledby="database-unavailable-title"]');
  await expect(card.getByRole("heading", { name: "データベースの状態を確認できません" })).toBeVisible();
  await expect(
    card.getByText("バックエンドの起動状態を確認して再試行してください。", { exact: true })
  ).toBeVisible();
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(card.getByRole("link")).toHaveCount(0);
});

test("保存済みデータの復元失敗も共通の起動案内へ統一する", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: null })
  );
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: false,
      durable: false,
      writable: false,
      snapshot_loaded: false,
      reason_code: "snapshot_load_failed",
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/persistence/recover", (route) => fulfill(route, null, 503));

  await page.goto("/profiles");

  await expectDatabaseGate(page, {
    title: "保存済みの業務データを復元できません",
    message:
      "データベース接続は正常ですが、NL2SQL の保存領域を利用できません。再試行しても解消しない場合は、システムテーブルとバックエンドログを確認してください。",
  });
});

test("Profile 保存が 503 のとき成功通知を出さず失敗を通知する", async ({ page }) => {
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: null })
  );
  await mockProfilePage(page);
  await page.route("**/api/nl2sql/profiles", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 503,
      headers: { "Retry-After": "5" },
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["業務データを永続化するデータベースを利用できません。"],
        warning_messages: [],
      }),
    });
  });

  await page.goto("/profiles");
  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  // 名称は識別子規則(英字開始・英数字/アンダースコア)、カテゴリは必須のクライアント検証あり。
  await page.getByLabel("名称").fill("SAVE_FAIL_PROFILE");
  await page.getByLabel("カテゴリ").fill("保存失敗検証");
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(
    page.getByText("業務データを永続化するデータベースを利用できません。")
  ).toBeVisible();
  await expect(page.getByText("プロファイルを保存しました。")).toHaveCount(0);
});

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

test("設定を開けない利用者には導線を出さず、システム管理者への連絡を案内する（#820）", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      user_uuid: "viewer",
      login_user_id: "viewer",
      display_name: "閲覧 次郎",
      status: "ACTIVE",
      force_password_change: false,
      role_codes: ["NL2SQL_USER"],
      is_system_admin: false,
      permissions: ["menu.profiles"],
      data_entitlements: [],
      debug_mode: false,
      password_change_allowed: true,
    })
  );
  let status: { status: string; check: string; detail: string | null; adb_lifecycle_state: string | null } = {
    status: "unreachable",
    check: "ok",
    detail: "ORA-12514",
    adb_lifecycle_state: "STOPPED",
  };
  await page.route("**/api/ready/database", (route) => fulfill(route, status));

  await page.goto("/profiles");

  const card = page.locator('section[aria-labelledby="database-unavailable-title"]');
  await expect(card.getByRole("heading", { name: "Autonomous Database が停止しています" })).toBeVisible();
  await expect(card.getByText(/^システム管理者に連絡して、Autonomous Database の起動を依頼してください。/)).toBeVisible();
  await expect(card.getByText("Autonomous Database: 停止済み", { exact: true })).toBeVisible();
  await expect(card.getByRole("link")).toHaveCount(0);
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoHorizontalOverflow(page);

  status = { status: "setup_required", check: "migration_required", detail: null, adb_lifecycle_state: null };
  await card.getByRole("button", { name: "再試行" }).click();
  await expect(card.getByRole("heading", { name: "システムテーブルの作成・更新が必要です" })).toBeVisible();
  await expect(card.getByText(/システム管理者に連絡して、システムテーブルの作成・更新を依頼してください。$/)).toBeVisible();
  await expect(card.getByRole("link")).toHaveCount(0);
});
