import { expect, type Page, test } from "./fixtures/test";

import { SYSTEM_TABLES_STATUS_OK, expectNoPageOverflow, mockLocalAuth } from "./_helpers";

type AdbStatus =
  | "success"
  | "not_configured"
  | "error"
  | "accepted"
  | "already_available"
  | "already_stopped"
  | "cannot_start"
  | "cannot_stop";

interface AdbInfoData {
  status: AdbStatus;
  message: string;
  id: string | null;
  display_name: string | null;
  lifecycle_state: string | null;
  db_name: string | null;
  cpu_core_count: number | null;
  data_storage_size_in_tbs: number | null;
  region: string | null;
}

const databaseSettings = {
  user: "rag_app",
  dsn: "adb.ap-osaka-1.oraclecloud.com/ragdb_high",
  wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
  wallet_uploaded: true,
  available_services: ["ragdb_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1536,
  vector_column: "VECTOR(1536, FLOAT32)",
  adb_ocid: "ocid1.autonomousdatabase.oc1..rag",
  region: "ap-osaka-1",
  driver_mode: "thick" as const,
  connection_security: "wallet_mtls" as const,
  client_lib_dir: "/u01/aipoc/instantclient_23_26",
  config_source: "runtime" as const,
};

function adbInfo(overrides: Partial<AdbInfoData> = {}): AdbInfoData {
  return {
    status: "success",
    message: "データベース情報を取得しました。",
    id: "ocid1.autonomousdatabase.oc1..rag",
    display_name: "RAG ADB",
    lifecycle_state: "STOPPED",
    db_name: "ragdb",
    cpu_core_count: 2,
    data_storage_size_in_tbs: 1,
    region: "ap-osaka-1",
    ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

interface AdbMockHandlers {
  info: () => AdbInfoData;
  onStart?: () => AdbInfoData;
  onStop?: () => AdbInfoData;
  onSaveSettings?: (payload: Record<string, unknown>) => AdbInfoData;
}

async function mockDatabaseAndAdb(page: Page, handlers: AdbMockHandlers) {
  const envelope = (data: unknown) => ({
    json: { data, error_messages: [], warning_messages: [] },
  });

  await page.route("**/api/settings/database**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/settings/database/adb/start") {
      await route.fulfill(envelope(handlers.onStart?.() ?? handlers.info()));
      return;
    }
    if (url.pathname === "/api/settings/database/adb/stop") {
      await route.fulfill(envelope(handlers.onStop?.() ?? handlers.info()));
      return;
    }
    if (url.pathname === "/api/settings/database/adb/settings") {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill(envelope(handlers.onSaveSettings?.(payload) ?? handlers.info()));
      return;
    }
    if (url.pathname === "/api/settings/database/wallet/download") {
      // ADB の保存 / 再取得のあとに Wallet の自動取得を試みる（共有画面。#108）。
      await route.fulfill(envelope({ status: "already_configured", settings: databaseSettings }));
      return;
    }
    if (url.pathname === "/api/settings/database/system-tables") {
      await route.fulfill({ json: SYSTEM_TABLES_STATUS_OK });
      return;
    }
    if (url.pathname === "/api/settings/database/adb") {
      await route.fulfill(envelope(handlers.info()));
      return;
    }
    if (url.pathname === "/api/settings/database" && method === "GET") {
      await route.fulfill(envelope(databaseSettings));
      return;
    }

    await route.fulfill(envelope(databaseSettings));
  });
}

test("ADB 管理パネルが情報を表示し起動操作できる", async ({ page }) => {
  let started = false;
  await mockDatabaseAndAdb(page, {
    info: () => adbInfo({ lifecycle_state: started ? "STARTING" : "STOPPED" }),
    onStart: () => {
      started = true;
      return adbInfo({
        status: "accepted",
        message: "データベース 'RAG ADB' の起動を開始しました。",
        lifecycle_state: "STARTING",
      });
    },
  });

  await page.goto("/settings/database");

  await expect(
    page.getByRole("heading", { name: "Autonomous Database 管理" })
  ).toBeVisible();
  await expect(page.getByLabel("ADB OCID")).toHaveValue("ocid1.autonomousdatabase.oc1..rag");
  await expect(page.getByText("OCI ADB: 停止済み")).toBeVisible();

  await page.getByRole("button", { name: "起動", exact: true }).click();

  await expect(page.getByText("OCI ADB: 起動中")).toBeVisible();
  await expect(page.getByText("操作履歴")).toBeVisible();
  // 操作履歴の状態は API の enum ではなく、日本語の StatusBadge で出す（#722）。
  await expect(
    page.locator('[data-status-variant="success"]').filter({ hasText: "受付済み" })
  ).toBeVisible();
  await expect(page.getByText("accepted", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("データベース 'RAG ADB' の起動を開始しました。")
  ).toBeVisible();
});

test("ADB の情報を取得している間は経過時間付きの読み込み表示を出す", async ({ page }) => {
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockDatabaseAndAdb(page, {
    info: () => adbInfo({ lifecycle_state: "AVAILABLE" }),
  });
  // 後から登録した route が先に当たる。ADB の情報の取得だけを止める。
  await page.route("**/api/settings/database/adb", async (route) => {
    await released;
    await route.fallback();
  });

  await page.goto("/settings/database");

  const loading = page.getByTestId("settings-adb-loading");
  await expect(loading).toContainText("Autonomous Database の情報を読み込んでいます");
  await expect(loading).toContainText("経過時間");
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeDisabled();
  await expectNoPageOverflow(page);

  release();
  await expect(loading).toHaveCount(0);
  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible();
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeEnabled();
});

test("ADB 管理パネルで起動前に設定を保存できる", async ({ page }) => {
  let saveCalls = 0;
  let savedPayload: Record<string, unknown> | null = null;
  await mockDatabaseAndAdb(page, {
    info: () => adbInfo(),
    onSaveSettings: (payload) => {
      saveCalls += 1;
      savedPayload = payload;
      return adbInfo();
    },
  });

  await page.goto("/settings/database");

  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect.poll(() => saveCalls).toBe(1);
  expect(savedPayload).toEqual({
    adb_ocid: "ocid1.autonomousdatabase.oc1..rag",
    region: "ap-osaka-1",
  });
  await expect(page.getByText("操作履歴")).toBeVisible();
  await expect(page.getByText("データベース情報を取得しました。")).toBeVisible();
});

test("起動後に lifecycle がポーリングで自動更新される(STARTING→AVAILABLE)", async ({
  page,
}) => {
  let started = false;
  let pollsSinceStart = 0;
  await mockDatabaseAndAdb(page, {
    info: () => {
      if (!started) return adbInfo({ lifecycle_state: "STOPPED" });
      pollsSinceStart += 1;
      // 起動後の最初のポーリングは起動中、その後 AVAILABLE へ遷移。
      return adbInfo({ lifecycle_state: pollsSinceStart >= 2 ? "AVAILABLE" : "STARTING" });
    },
    onStart: () => {
      started = true;
      return adbInfo({
        status: "accepted",
        message: "データベース 'RAG ADB' の起動を開始しました。",
        lifecycle_state: "STARTING",
      });
    },
  });

  await page.goto("/settings/database");

  await expect(page.getByText("OCI ADB: 停止済み")).toBeVisible();
  await page.getByRole("button", { name: "起動", exact: true }).click();
  await expect(page.getByText("OCI ADB: 起動中")).toBeVisible();

  // クリックや「情報を再取得」なしで、ポーリングにより起動済みへ更新される。
  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible({ timeout: 15000 });
});

test("起動済み ADB を停止操作できる", async ({ page }) => {
  let stopping = false;
  await mockDatabaseAndAdb(page, {
    info: () => adbInfo({ lifecycle_state: stopping ? "STOPPING" : "AVAILABLE" }),
    onStop: () => {
      stopping = true;
      return adbInfo({
        status: "accepted",
        message: "データベース 'RAG ADB' の停止を開始しました。",
        lifecycle_state: "STOPPING",
      });
    },
  });

  await page.goto("/settings/database");

  await expect(page.getByText("OCI ADB: 起動済み")).toBeVisible();

  await page.getByRole("button", { name: "停止", exact: true }).click();

  await expect(page.getByText("OCI ADB: 停止中")).toBeVisible();
  await expect(
    page.getByText("データベース 'RAG ADB' の停止を開始しました。")
  ).toBeVisible();
});

test("OCID 未入力では起動 / 停止できない", async ({ page }) => {
  await mockDatabaseAndAdb(page, {
    info: () =>
      adbInfo({
        status: "not_configured",
        message: "ADB OCID が設定されていません。",
        id: null,
        display_name: null,
        lifecycle_state: null,
        db_name: null,
        cpu_core_count: null,
        data_storage_size_in_tbs: null,
      }),
  });

  // OCID 空の DB 設定を返す
  await page.route("**/api/settings/database", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        json: {
          data: { ...databaseSettings, adb_ocid: "" },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    // 保存などは先に登録した mock に渡す（実 backend へは流さない）。
    await route.fallback();
  });

  await page.goto("/settings/database");

  await expect(
    page.getByRole("heading", { name: "Autonomous Database 管理" })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "起動", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeDisabled();
});
