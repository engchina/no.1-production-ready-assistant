import { expect, type Page, test } from "./fixtures/test";

import { expectedControlHeight, expectNoPageOverflow, SYSTEM_TABLES_STATUS_OK, LOCAL_AUTH_ME } from "./_helpers";

interface DatabaseSettingsData {
  user: string;
  dsn: string;
  wallet_dir: string;
  wallet_uploaded: boolean;
  available_services: string[];
  has_password: boolean;
  has_wallet_password: boolean;
  readiness: string;
  embedding_dimension: number;
  vector_column: string;
  adb_ocid: string;
  region: string;
  driver_mode: "thin" | "thick";
  connection_security: "wallet_mtls" | "walletless_tls";
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
  dsn: "adb.ap-osaka-1.oraclecloud.com/ragdb_high",
  wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
  wallet_uploaded: false,
  available_services: [],
  has_password: false,
  has_wallet_password: false,
  readiness: "missing_credentials",
  embedding_dimension: 1536,
  vector_column: "VECTOR(1536, FLOAT32)",
  adb_ocid: "",
  region: "ap-osaka-1",
  driver_mode: "thick",
  connection_security: "wallet_mtls",
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

test("データベース設定から Wallet ZIP をアップロードできる", async ({ page }) => {
  let current = { ...databaseSettings };
  await mockDatabaseSettings(page, () => current, async () => {
    current = {
      ...current,
      wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
      wallet_uploaded: true,
      available_services: ["ragdb_high", "ragdb_low"],
      readiness: "ok",
    };
  });

  await page.goto("/settings/database");

  const main = page.getByRole("main");

  await expect(page.getByText("保存済みパスワードを削除する")).toHaveCount(0);
  await expect(page.getByText("保存済み Wallet パスワードを削除する")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "再読み込み" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "データベース設定" }).first()).toBeVisible();
  await expect(page.getByLabel("データベースユーザー")).toHaveValue("rag_app");
  await expect(page.getByLabel("データベースパスワード")).toBeVisible();
  // 共有画面（NL2SQL と同じ。#108）は右側の構成状態・.env プレビュー・運用メモを持たない。
  await expect(main.getByText("Wallet状態:")).toBeVisible();
  // 「未設定」は secret の入力欄のバッジ（SecretField。#296）にも出るので、Wallet 状態の行に絞る。
  const walletStatus = main.locator("p").filter({ hasText: "Wallet状態:" });
  await expect(walletStatus).toContainText("未設定");
  // 色の文字だけにせず、アイコン付きの StatusBadge で出す（#722）。
  await expect(walletStatus.locator('[data-status-variant="warning"]')).toHaveText("未設定");
  await expect(main.getByText("/u01/aipoc/instantclient_23_26/network/admin")).toBeVisible();
  await expect(page.getByRole("heading", { name: ".env プレビュー" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "運用メモ" })).toHaveCount(0);
  await expect(page.getByText("認証方式")).toHaveCount(0);
  await expect(main.getByText("Embedding 次元")).toHaveCount(0);
  await expect(main.getByText("ベクトル列")).toHaveCount(0);
  // RAG は Walletless TLS を使わないため、接続セキュリティの選択は出さない。
  await expect(page.getByLabel("接続セキュリティ")).toHaveCount(0);
  await page.getByTestId("oracle-wallet-upload-input").setInputFiles({
    name: "Wallet_RAGDB.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("wallet-zip"),
  });

  await expect(
    page.getByText("Wallet ZIP をアップロードしました: Wallet_RAGDB.zip").first()
  ).toBeVisible();
  await expect(main.getByText("設定済み", { exact: true })).toBeVisible();
  const walletService = page.getByRole("combobox", { name: "サービス名 / DSN" });
  await walletService.click();
  await page.getByRole("option", { name: "ragdb_high" }).click();
  await expect(walletService).toContainText("ragdb_high");
});

test("Wallet ZIP をアップロードしたら DB ゲートの状態を確かめ直す", async ({ page }) => {
  let current = { ...databaseSettings, readiness: "wallet_not_found" };
  let walletInstalled = false;
  let statusCalls = 0;
  await mockDatabaseSettings(page, () => current, async () => {
    walletInstalled = true;
    current = {
      ...current,
      wallet_uploaded: true,
      available_services: ["ragdb_high"],
      readiness: "ok",
    };
  });
  await page.route("**/api/ready/database", async (route) => {
    statusCalls += 1;
    await route.fulfill({
      json: {
        data: walletInstalled
          ? { status: "ok", check: "ok", detail: null }
          : { status: "not_configured", check: "wallet_not_found", detail: null },
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
  const callsBeforeUpload = statusCalls;

  // 案内からデータベース設定へ移り（画面の再読み込みなし）、Wallet を置く。
  await gate.getByRole("link", { name: "データベース設定を開く" }).click();
  await expect(page).toHaveURL(/\/settings\/database$/);
  await page.getByTestId("oracle-wallet-upload-input").setInputFiles({
    name: "Wallet_RAGDB.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("wallet-zip"),
  });
  await expect(
    page.getByText("Wallet ZIP をアップロードしました: Wallet_RAGDB.zip").first()
  ).toBeVisible();

  // ゲートの cache（15 秒）の間に戻っても、古い「未設定」の案内を出さず、状態を確かめ直す。
  await page.goBack();
  await expect(page).toHaveURL(/\/knowledge-bases$/);
  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(gate).toHaveCount(0);
  expect(statusCalls).toBeGreaterThan(callsBeforeUpload);
});

test("保存済み DB 認証 secret をチェックボックスで削除できる", async ({ page }) => {
  let savedPayload: Record<string, unknown> | null = null;
  await mockDatabaseSettings(
    page,
    () => ({
      ...databaseSettings,
      has_password: true,
      has_wallet_password: true,
      readiness: "ok",
    }),
    undefined,
    async (payload) => {
      savedPayload = payload;
    }
  );

  await page.goto("/settings/database");

  const password = page.getByLabel("データベースパスワード", { exact: true });
  await expect(page.getByText("保存済みパスワードを削除する")).toBeVisible();
  // 共有画面は Wallet パスワードの入力と削除も持つ（NL2SQL と同じ。#108）。
  await expect(page.getByText("保存済み Wallet パスワードを削除する")).toBeVisible();

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
      wallet_uploaded: true,
      available_services: ["ragdb_high"],
      readiness: "ok",
    }),
    undefined,
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
      () => ({ ...databaseSettings, wallet_uploaded: true, available_services: ["ragdb_high"] }),
      undefined,
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
  { name: "desktop", width: 1280, height: 720, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`Wallet ZIP アップロード導線が収まる (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    await mockDatabaseSettings(page, () => databaseSettings);

    await page.goto("/settings/database");

    // Wallet ZIP のアップロードは共有のドロップ領域（NL2SQL と同じ。#108）。
    const button = page.getByTestId("oracle-wallet-upload-dropzone");
    await expect(button).toBeVisible();
    const metrics = await button.evaluate((element) => ({
      clientHeight: element.clientHeight,
      clientWidth: element.clientWidth,
      offsetHeight: (element as HTMLElement).offsetHeight,
      scrollHeight: element.scrollHeight,
      scrollWidth: element.scrollWidth,
    }));
    // ファイルの選択は入力欄と同じ md（タッチ端末は 44px。#613）。
    expect(metrics.offsetHeight).toBeGreaterThanOrEqual(await expectedControlHeight(page));
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
    expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.clientHeight + 1);
  });
}

async function mockDatabaseSettings(
  page: Page,
  getCurrent: () => DatabaseSettingsData,
  onWalletUpload?: () => Promise<void>,
  onSave?: (payload: Record<string, unknown>) => Promise<void>,
  onTest?: () => Promise<DatabaseConnectionTestResult>
) {
  await page.route("**/api/settings/database**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/settings/database/system-tables") {
      await route.fulfill({ json: SYSTEM_TABLES_STATUS_OK });
      return;
    }

    if (url.pathname.startsWith("/api/settings/database/adb")) {
      await route.fulfill({
        json: {
          data: {
            status: "not_configured",
            message: "ADB OCID が設定されていません。",
            id: null,
            display_name: null,
            lifecycle_state: null,
            db_name: null,
            cpu_core_count: null,
            data_storage_size_in_tbs: null,
            region: "ap-osaka-1",
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/settings/database/wallet") {
      await onWalletUpload?.();
      await route.fulfill({
        json: {
          data: getCurrent(),
          error_messages: [],
          warning_messages: [],
        },
      });
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
