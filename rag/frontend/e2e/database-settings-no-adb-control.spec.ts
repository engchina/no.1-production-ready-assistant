import { expect, test } from "./fixtures/test";

import { SYSTEM_TABLES_STATUS_OK, expectNoPageOverflow, mockLocalAuth } from "./_helpers";

// DB の起動・停止・状態の取得は画面で扱わない（#10。DB は ADB Free コンテナで、起動・停止は docker 側の責務）。
// データベース設定には接続設定・Wallet・接続テストだけを出し、ADB の情報取得・起動・停止の API を呼ばない。

const databaseSettings = {
  user: "rag_app",
  dsn: "ragdb_high",
  wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
  wallet_uploaded: true,
  available_services: ["ragdb_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1536,
  vector_column: "VECTOR(1536, FLOAT32)",
  adb_ocid: "",
  region: "",
  driver_mode: "thick" as const,
  connection_security: "wallet_mtls" as const,
  client_lib_dir: "/u01/aipoc/instantclient_23_26",
  config_source: "runtime" as const,
};

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

test("データベース設定に ADB の運用パネルを出さず、ADB の制御 API を呼ばない", { tag: "@responsive" }, async ({ page }) => {
  const envelope = (data: unknown) => ({
    json: { data, error_messages: [], warning_messages: [] },
  });
  const adbRequests: string[] = [];
  await page.route("**/api/settings/database**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/settings/database/adb")) {
      adbRequests.push(url.pathname);
      await route.fulfill({ status: 404, json: { detail: "Not Found" } });
      return;
    }
    if (url.pathname === "/api/settings/database/system-tables") {
      await route.fulfill({ json: SYSTEM_TABLES_STATUS_OK });
      return;
    }
    await route.fulfill(envelope(databaseSettings));
  });

  await page.goto("/settings/database");

  await expect(page.getByRole("heading", { level: 1, name: "データベース設定" })).toBeVisible();
  await expect(page.getByRole("button", { name: "DB設定を保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "DB接続テスト" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Autonomous Database 管理" })).toHaveCount(0);
  await expect(page.locator("#adb-management")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "起動", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止", exact: true })).toHaveCount(0);
  expect(adbRequests).toEqual([]);
  await expectNoPageOverflow(page);
});
