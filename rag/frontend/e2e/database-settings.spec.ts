import { expect, type Page, test } from "./fixtures/test";

import { expectNoPageOverflow, SYSTEM_TABLES_STATUS_OK, LOCAL_AUTH_ME } from "./_helpers";

interface DatabaseSettingsData {
  user: string;
  dsn: string;
  has_password: boolean;
  readiness: string;
  embedding_dimension: number;
  vector_column: string;
  adb_ocid: string;
  region: string;
  driver_mode: "thin" | "thick";
  client_lib_dir: string;
  config_source: "runtime";
}

interface DatabaseConnectionTestResult {
  status: "success" | "failed";
  readiness: string;
  message: string;
  elapsed_ms: number;
  troubleshooting: string[];
  details: Record<string, string | number | boolean | null>;
  checked_at: string;
  error_type: string | null;
}

const databaseSettings: DatabaseSettingsData = {
  user: "rag_app",
  dsn: "127.0.0.1:1525/ragdb_low",
  has_password: false,
  readiness: "missing_credentials",
  embedding_dimension: 1024,
  vector_column: "VECTOR(1024, FLOAT32)",
  adb_ocid: "",
  region: "ap-osaka-1",
  driver_mode: "thick",
  client_lib_dir: "/u01/aipoc/instantclient_23_26",
  config_source: "runtime",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }

    // 未モックの API は実 backend へ流さない（hermetic の dev サーバが 404 で終端する）。
    await route.fallback();
  });
});

test("DB 設定を保存したら DB ゲートの状態を確かめ直す", async ({ page }) => {
  let current = { ...databaseSettings };
  let saved = false;
  let statusCalls = 0;
  await mockDatabaseSettings(page, () => current, async () => {
    saved = true;
    current = { ...current, has_password: true, readiness: "ok" };
  });
  await page.route("**/api/ready/database", async (route) => {
    statusCalls += 1;
    await route.fulfill({
      json: {
        data: saved
          ? { status: "ok", check: "ok", detail: null }
          : { status: "not_configured", check: "missing_credentials", detail: null },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: {
        data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );

  await page.goto("/knowledge-bases");
  const gate = page.locator('section[aria-labelledby="database-unavailable-title"]');
  await expect(gate.getByRole("heading", { name: "データベースの接続情報が未設定です" })).toBeVisible();
  const callsBeforeSave = statusCalls;

  // 案内からデータベース設定へ移り（画面の再読み込みなし）、パスワードを保存する。
  await gate.getByRole("link", { name: "データベース設定を開く" }).click();
  await expect(page).toHaveURL(/\/settings\/database$/);
  await page.locator("#oracle-password").fill("database-secret-fixture");
  await page.getByRole("button", { name: "DB設定を保存", exact: true }).click();
  await expect(page.getByText("保存しました").first()).toBeVisible();

  // ゲートの cache（15 秒）の間に戻っても、古い「未設定」の案内を出さず、状態を確かめ直す。
  await page.goBack();
  await expect(page).toHaveURL(/\/knowledge-bases$/);
  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(gate).toHaveCount(0);
  expect(statusCalls).toBeGreaterThan(callsBeforeSave);
});

test("保存済み DB 認証 secret をチェックボックスで削除できる", async ({ page }) => {
  let savedPayload: Record<string, unknown> | null = null;
  await mockDatabaseSettings(
    page,
    () => ({
      ...databaseSettings,
      has_password: true,
      readiness: "ok",
    }),
    async (payload) => {
      savedPayload = payload;
    }
  );

  await page.goto("/settings/database");

  const password = page.getByLabel("データベースパスワード", { exact: true });
  await expect(page.getByText("保存済みパスワードを削除する")).toBeVisible();
  await page.getByLabel("保存済みパスワードを削除する").check();
  await expect(password).toBeDisabled();

  await page.getByRole("button", { name: "DB設定を保存", exact: true }).click();

  expect(savedPayload).toMatchObject({
    clear_password: true,
  });
  expect(savedPayload).not.toHaveProperty("password");
});

test("接続テストのタイムアウト診断を表示できる", async ({ page }) => {
  await mockDatabaseSettings(
    page,
    () => ({
      ...databaseSettings,
      readiness: "ok",
    }),
    undefined,
    async () => ({
      status: "failed",
      readiness: "ok",
      message: "Oracle AI Database の接続テストが 15 秒でタイムアウトしました。",
      elapsed_ms: 15001,
      troubleshooting: [
        "接続テストがタイムアウトしました。ADB が起動中か、TCPS 1522 に到達できるか確認してください。",
      ],
      details: { timeout_seconds: 15, tcp_connect_timeout_seconds: 10 },
      checked_at: "2026-06-14T00:00:00Z",
      error_type: "OracleConnectionTimeoutError",
    })
  );

  await page.goto("/settings/database");
  await page.getByRole("button", { name: "DB接続テスト" }).click();

  await expect(page.getByText("Oracle AI Database の接続テストが 15 秒でタイムアウトしました。")).toBeVisible();
  await expect(page.getByText(/所要時間: 15001 ms/)).toBeVisible();
  await expect(page.getByText(/TCPS 1522/)).toBeVisible();
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 720 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`DB 接続テストの間は経過時間を出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // 接続テストの応答を止めて、実行中の表示（#376、共通のシステム設定）を確かめてから返す。
    let release: () => void = () => {};
    const released = new Promise<void>((resolve) => {
      release = resolve;
    });
    await mockDatabaseSettings(
      page,
      () => ({ ...databaseSettings }),
      undefined,
      async () => {
        await released;
        return {
          status: "success",
          readiness: "ok",
          message: "Oracle AI Database に接続できました。",
          elapsed_ms: 1200,
          error_type: null,
          troubleshooting: [],
          details: {},
          checked_at: "2026-06-14T00:00:00Z",
        };
      }
    );

    await page.goto("/settings/database");
    await page.getByRole("button", { name: "DB接続テスト" }).click();

    const processing = page.getByTestId("settings-database-test-processing");
    await expect(processing).toContainText("データベースへの接続を確認しています");
    await expect(processing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expect(processing).toHaveAttribute("data-processing-activity-icon", "none");
    await expectNoPageOverflow(page);

    release();
    await expect(page.getByText("Oracle AI Database に接続できました。")).toBeVisible();
    await expect(processing).toHaveCount(0);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 720 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`DSN の欄は全幅で、形式の例を説明に出す (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockDatabaseSettings(page, () => databaseSettings);

    await page.goto("/settings/database");

    await expect(page.locator("#oracle-dsn")).toHaveValue(databaseSettings.dsn);
    await expect(page.getByText(/127\.0\.0\.1:1525\/myatp_low\.adb\.oraclecloud\.com/)).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

async function mockDatabaseSettings(
  page: Page,
  getCurrent: () => DatabaseSettingsData,
  onSave?: (payload: Record<string, unknown>) => Promise<void>,
  onTest?: () => Promise<DatabaseConnectionTestResult>
) {
  await page.route("**/api/settings/database**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/settings/database/system-tables") {
      await route.fulfill({ json: SYSTEM_TABLES_STATUS_OK });
      return;
    }

    if (url.pathname === "/api/settings/database/test") {
      await route.fulfill({
        json: {
          data:
            (await onTest?.()) ?? {
              status: "success",
              readiness: "ok",
              message: "Oracle AI Database への接続に成功しました。",
              elapsed_ms: 12,
              troubleshooting: [],
              details: { timeout_seconds: 15, tcp_connect_timeout_seconds: 10 },
              checked_at: "2026-06-14T00:00:00Z",
              error_type: null,
            },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/settings/database" && route.request().method() === "PATCH") {
      await onSave?.(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({
        json: {
          data: getCurrent(),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    await route.fulfill({
      json: {
        data: getCurrent(),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}
