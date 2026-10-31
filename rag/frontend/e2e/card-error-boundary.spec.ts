import { expect, test } from "./fixtures/test";

import { SYSTEM_TABLES_STATUS_OK, mockLocalAuth } from "./_helpers";

/**
 * カード単位 error boundary の隔離検証(#67)。
 *
 * boundary が無いと 1 枚の描画例外でページ全体が unmount される(実例: #63)。ここでは
 * system table カード（運用設定の専用の画面。#658）だけを意図的に throw させ、
 * **画面の見出しが生き残ること**を検証する。
 */

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
  config_source: "runtime" as const,
};

const adbInfo = {
  data: {
    status: "success",
    message: "データベース情報を取得しました。",
    id: "ocid1.autonomousdatabase.oc1..rag",
    display_name: "RAG ADB",
    lifecycle_state: "STOPPED",
    db_name: "ragdb",
    cpu_core_count: 2,
    data_storage_size_in_tbs: 1,
    region: "ap-osaka-1",
  },
  error_messages: [],
  warning_messages: [],
};

/**
 * 型ガード(#63)は通過するが、詳細表示で throw する payload。
 *
 * `applied_versions` / `pending_versions` はカード内の `<details>` で `.join()` されるため、
 * 配列でないと描画中に TypeError になる。型ガードはカード上部の描画に必要な項目だけを見る
 * 浅い検査なので、この種の深い階層の破綻は boundary 側で受け止める設計。
 */
const systemTablesThrowingPayload = {
  data: {
    ...SYSTEM_TABLES_STATUS_OK.data,
    applied_versions: null,
    pending_versions: null,
  },
  error_messages: [],
  warning_messages: [],
};

test("データベース設定の画面にはシステムテーブルのカードを出さない（運用設定の専用の画面。#658）", async ({
  page,
}) => {
  await mockLocalAuth(page);
  await page.route("**/api/settings/database**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/settings/database/adb")) {
      await route.fulfill({ json: adbInfo });
      return;
    }
    await route.fulfill({
      json: { data: databaseSettings, error_messages: [], warning_messages: [] },
    });
  });

  await page.goto("/settings/database");

  await expect(page.getByRole("heading", { name: "Autonomous Database 管理" })).toBeVisible();
  await expect(page.locator("#system-tables")).toHaveCount(0);
});

test("system table カードが throw しても、見出しは残りカードだけがエラー表示になる", async ({
  page,
}) => {
  // 描画例外による console.error は想定内。テスト失敗の材料にしない。
  page.on("pageerror", () => {});

  await mockLocalAuth(page);
  await page.route("**/api/settings/database/system-tables", (route) =>
    route.fulfill({ json: systemTablesThrowingPayload })
  );

  await page.goto("/settings/system-tables");

  // 例外を起こしたカードは、そのカードだけがエラー表示へ差し替わる。
  await expect(page.getByText("「システムテーブル」を表示できません")).toBeVisible();
  // 画面の見出しは残り、カードの中から再試行できる。
  await expect(page.getByRole("heading", { name: "システムテーブル管理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
});
