import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { dropFiles } from "./_helpers/file-dropzone";
import { expectLargeActionButton } from "./_helpers/action-button";

function settingsEnvelope(data: unknown) {
  return {
    data,
    error_messages: [],
    warning_messages: [],
  };
}

function databaseSettingsFixture(overrides: Record<string, unknown> = {}) {
  return {
    user: "NL2SQL_APP",
    dsn: "nl2sqldb_high",
    driver_mode: "thin",
    connection_security: "wallet_mtls",
    client_lib_dir: "",
    wallet_dir: "/u01/aipoc/wallet",
    wallet_uploaded: true,
    available_services: ["nl2sqldb_high", "nl2sqldb_low"],
    has_password: true,
    has_wallet_password: false,
    readiness: "ok",
    embedding_dimension: 1536,
    vector_column: "VECTOR(1536, FLOAT32)",
    config_source: "runtime",
    ...overrides,
  };
}


/** モデル設定の payload のプライマリ接続（#533。接続は一覧で持つ）。 */
function primaryConnection(enterprise: unknown): Record<string, unknown> {
  return ((enterprise as Record<string, unknown>).connections as Array<Record<string, unknown>>)[0];
}

function primaryConnectionFixture(overrides: Record<string, unknown> = {}) {
  return {
    connection_id: "primary",
    endpoint: "https://enterprise-ai.example.com",
    project_ocid: "ocid1.generativeaiproject.oc1.ap-osaka-1.example",
    api_key: "",
    has_api_key: true,
    clear_api_key: false,
    ...overrides,
  };
}

function modelSettingsFixture(overrides: Record<string, unknown> = {}) {
  return {
    settings: {
      enterprise_ai: {
        connections: [primaryConnectionFixture()],
        models: [
          {
            model_id: "enterprise-nl2sql-llm",
            display_name: "業務 NL2SQL 標準",
            vision_enabled: false,
            connection_id: "primary",
          },
          {
            model_id: "enterprise-nl2sql-vlm",
            display_name: "OCR / Vision",
            vision_enabled: true,
            connection_id: "primary",
          },
        ],
        default_text_model_id: "enterprise-nl2sql-llm",
        default_vision_model_id: "enterprise-nl2sql-vlm",
        api_path: "/responses",
        vlm_input_mode: "auto",
        text_payload_template: "",
        vision_payload_template: "",
        text_response_path: "",
        vision_response_path: "",
        timeout_seconds: 120,
        max_retries: 3,
      },
      generative_ai: {
        embedding_model: "text-embedding-v4",
        embedding_dim: 1536,
        embedding_base_url: "",
        rerank_model: "qwen3-rerank",
        rerank_base_url: "",
      },
    },
    model_settings_file: "runtime-settings",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
    ...overrides,
  };
}

function uploadStorageFixture(overrides: Record<string, unknown> = {}) {
  return {
    local_storage_dir: "/u01/data/production-ready-nl2sql",
    readiness: "ok",
    max_upload_bytes: 104857600,
    config_source: "runtime",
    ...overrides,
  };
}

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(settingsEnvelope(data)),
  });
}

function createRequestGate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

async function mockNl2sqlSettingsApi(page: Page) {
  const uploadStorage = uploadStorageFixture();

  const modelSettings = modelSettingsFixture();

  const databaseSettings = databaseSettingsFixture();

  await page.route("**/api/settings/upload-storage", (route) =>
    fulfillJson(route, uploadStorage)
  );
  await page.route("**/api/settings/model", (route) => fulfillJson(route, modelSettings));
  await page.route("**/api/settings/model/test", (route) => {
    const request = route.request().postDataJSON() as {
      target_type: "enterprise_text" | "enterprise_vision" | "embedding" | "rerank";
      model_id: string;
    };
    const details =
      request.target_type === "embedding"
        ? { vector_dim: 1536, input_count: 1 }
        : request.target_type === "rerank"
          ? { ranked_count: 1, top_score: 0.78962326 }
          : request.target_type === "enterprise_vision"
            ? { surface: "vision", response_chars: 1 }
            : { surface: "text", response_chars: 5 };
    return fulfillJson(route, {
      status: "success",
      target_type: request.target_type,
      model_id: request.model_id,
      message: `${request.model_id} の設定を確認しました。`,
      troubleshooting: [],
      raw_error: null,
      error_type: null,
      elapsed_ms: 12,
      checked_at: "2026-06-21T10:00:00.000Z",
      details,
    });
  });

  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, databaseSettings)
  );
  await page.route("**/api/settings/database/password/reveal", (route) =>
    fulfillJson(route, { password: "database-secret-fixture" })
  );
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfillJson(route, {
      status: "ready",
      schema_head: 15,
      applied_versions: [0, 1, 2, 3, 5, 6, 7, 8, 9, 15],
      pending_versions: [],
      expected_object_count: 51,
      existing_object_count: 51,
      missing_objects: [],
      tables: [
        {
          name: "NL2SQL_PROFILES",
          exists: true,
          estimated_rows: 3,
          created_at: "2026-07-19T00:00:00Z",
          last_analyzed_at: "2026-07-19T00:00:00Z",
        },
      ],
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
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [
        { owner: "APP", is_current: true, table_count: 8, view_count: 2 },
        { owner: "SH", is_current: false, table_count: 12, view_count: 1 },
      ],
      excluded_oracle_maintained_count: 29,
    })
  );
  await page.route("**/api/settings/database/test", (route) =>
    fulfillJson(route, {
      status: "success",
      readiness: "ok",
      message: "Oracle AI Database への接続に成功しました。",
      elapsed_ms: 1,
      troubleshooting: [],
      details: { network_call: true },
      checked_at: "2026-06-21T10:00:00.000Z",
      error_type: null,
    })
  );
  await page.route("**/api/settings/database/wallet", (route) =>
    fulfillJson(route, databaseSettings)
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

async function expectModelPreviewPanelsAbsent(page: Page) {
  await expect(page.getByText(".env プレビュー", { exact: true })).toHaveCount(0);
  await expect(page.getByText("JSON プレビュー", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "JSON をコピー" })).toHaveCount(0);
}

async function expectDatabaseSupplementalPanelsAbsent(page: Page) {
  await expect(page.getByText(".env プレビュー", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "接続状態" })).toHaveCount(0);
}

async function getSavedSecretBadgeStyle(page: Page, fieldId: string) {
  const badge = page
    .locator(`label[for="${fieldId}"]`)
    .locator("xpath=..")
    .getByText("保存済み", { exact: true });
  await expect(badge).toBeVisible();

  return badge.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderColor,
      borderRadius: style.borderRadius,
      borderStyle: style.borderStyle,
      className: element.getAttribute("class") ?? "",
      color: style.color,
      fontSize: style.fontSize,
      fontWeight: style.fontWeight,
      text: element.textContent,
      whiteSpace: style.whiteSpace,
    };
  });
}

async function getActionButtonStyle(button: Locator) {
  await expect(button).toBeVisible();

  return button.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderColor,
      className: element.getAttribute("class") ?? "",
      color: style.color,
    };
  });
}

async function expectModelSaveButtonsUsePrimaryStyle(page: Page) {
  const buttons = [
    page.getByRole("button", { name: "OCI Enterprise AI: 保存" }),
    page.getByRole("button", { name: "登録モデル: 保存" }),
    page.getByRole("button", { name: "埋め込み / 再ランク: 保存" }),
  ];
  await page.mouse.move(0, 0);
  await Promise.all(buttons.map((button) => button.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => undefined)));
  })));
  const styles = await Promise.all(buttons.map((button) => getActionButtonStyle(button)));
  const baseline = styles[0];

  for (const style of styles) {
    // 共有 Button primary variant（旧 bg-primary-fill → bg-accent-emphasis、
    // text-primary-fill-foreground → text-fg-on-accent）。secondary の枠線クラスは持たない。
    const classes = style.className.split(/\s+/);
    expect(classes).toContain("bg-accent-emphasis");
    expect(classes).toContain("text-fg-on-accent");
    expect(classes).not.toContain("border-border-control");
    expect(style.backgroundColor).toBe(baseline.backgroundColor);
    expect(style.borderColor).toBe(baseline.borderColor);
    expect(style.color).toBe(baseline.color);
  }
}

async function expectNoOperationsMemoOrReadiness(page: Page) {
  await expect(page.getByRole("heading", { name: "運用メモ" })).toHaveCount(0);
  await expect(page.getByText(/readiness/i)).toHaveCount(0);
}

async function expectNl2sqlShellFillsViewport(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(() => {
        const main = document.querySelector("main");
        const sidebar = document.querySelector(".sidebar-shell");
        if (!main || !sidebar) return false;

        const viewportBottom = window.innerHeight;
        const mainBottomGap = viewportBottom - main.getBoundingClientRect().bottom;
        const sidebarBottomGap = viewportBottom - sidebar.getBoundingClientRect().bottom;
        return (
          Math.abs(mainBottomGap) <= 1 &&
          Math.abs(sidebarBottomGap) <= 1
        );
      })
    )
    .toBeTruthy();
}

async function expectNoExcessBottomWhitespace(page: Page) {
  const metrics = await page.evaluate(() => {
    const main = document.querySelector("main");
    const scroller = main instanceof HTMLElement ? main : null;
    if (!scroller) {
      throw new Error("main scroller が見つかりません。");
    }

    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "instant" });
    const mainBottom = scroller.getBoundingClientRect().bottom;
    const cards = Array.from(scroller.querySelectorAll("div")).filter((element) => {
      const className = element.getAttribute("class") ?? "";
      // 共有 Card: border-border + bg-surface（旧 bg-card）。bg-surface-sunken 等は含めない
      const classes = className.split(/\s+/);
      return classes.includes("border-border") && classes.includes("bg-surface");
    });
    if (cards.length === 0) {
      throw new Error("設定カードが見つかりません。");
    }

    const lastVisibleCardBottom = Math.max(
      ...cards
        .map((card) => card.getBoundingClientRect())
        .filter((rect) => rect.width > 0 && rect.height > 0)
        .map((rect) => rect.bottom)
    );

    return {
      bottomWhitespace: Math.round(mainBottom - lastVisibleCardBottom),
      scrollHeight: scroller.scrollHeight,
      clientHeight: scroller.clientHeight,
    };
  });

  expect(metrics.bottomWhitespace).toBeGreaterThanOrEqual(0);
  expect(metrics.bottomWhitespace).toBeLessThanOrEqual(96);
}

async function expectWalletAboveServiceDsn(page: Page) {
  const wallet = page.getByTestId("oracle-wallet-upload");
  const service = page.getByRole("combobox", { name: /サービス名 \/ DSN/ });

  await expect(wallet).toBeVisible();
  await expect(service).toBeVisible();

  const [walletBox, serviceBox] = await Promise.all([
    wallet.boundingBox(),
    service.boundingBox(),
  ]);

  if (!walletBox || !serviceBox) {
    throw new Error("データベース設定フォームの Wallet / DSN 位置を取得できません。");
  }

  expect(walletBox.y).toBeLessThan(serviceBox.y);
}

async function expectNoAdbManagementPanel(page: Page) {
  // DB の起動・停止・状態の取得は画面で扱わない（#10。DB は ADB Free コンテナで、起動・停止は docker 側の責務）。
  await expect(page.locator("form").getByRole("heading", { name: "データベース設定" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Autonomous Database 管理" })).toHaveCount(0);
  await expect(page.locator("#adb-management")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "起動", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止", exact: true })).toHaveCount(0);
}

test.beforeEach(async ({ page }) => {
  await mockNl2sqlSettingsApi(page);
  await mockDatabaseGateReady(page);
});

test("NL2SQL のシステム設定画面を表示できる", { tag: "@responsive" }, async ({ page }) => {
  // OCI 認証設定の画面は削除した（#11）。旧 URL は既定の画面へ戻す。
  await page.goto("/settings/oci");
  await expect(page).not.toHaveURL(/\/settings\/oci$/);
  await expect(page.getByRole("heading", { name: "OCI 認証設定" })).toHaveCount(0);

  await page.goto("/settings/upload-storage");
  await expect(page.getByRole("heading", { name: "アップロード保存先" }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "保存先", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "保存先状態" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expect(page.getByLabel("ローカル保存ディレクトリ")).toHaveValue(
    "/u01/data/production-ready-nl2sql"
  );
  await expectNoOperationsMemoOrReadiness(page);
  // 保存先はローカルのディレクトリだけ（OCI Object Storage は #11 で削除）。
  await expect(page.getByRole("radio")).toHaveCount(0);
  await expect(page.getByText("OCI Object Storage")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("heading", { name: "保存先状態" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.setViewportSize({ width: 1280, height: 720 });

  await page.goto("/settings/model");
  await expect(page.getByRole("heading", { name: "モデル設定" }).first()).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "OCI Enterprise AI", exact: true })
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "登録モデル", exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "埋め込み / 再ランク", exact: true })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "OCI Enterprise AI: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "登録モデル: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "埋め込み / 再ランク: 保存" })).toBeVisible();
  await expectModelSaveButtonsUsePrimaryStyle(page);
  await expect(page.locator("#enterprise-api-path")).toHaveCount(0);
  await expect(page.locator("#enterprise-vlm-input-mode")).toHaveCount(0);
  await expect(page.locator("#enterprise-timeout")).toHaveCount(0);
  await expect(page.locator("#enterprise-retries")).toHaveCount(0);
  await expectNoOperationsMemoOrReadiness(page);
  await expectModelPreviewPanelsAbsent(page);
  await expectNl2sqlShellFillsViewport(page);
  await expectNoExcessBottomWhitespace(page);
  const modelSavedSecretBadgeStyle = await getSavedSecretBadgeStyle(
    page,
    "enterprise-api-key"
  );
  // 旧 border-success/30 / bg-success-bg / text-success → 共有 success トークン
  expect(modelSavedSecretBadgeStyle.className).toContain("border-success-border");
  expect(modelSavedSecretBadgeStyle.className).toContain("bg-success-subtle");
  expect(modelSavedSecretBadgeStyle.className).toContain("text-success-fg");
  await expectNoHorizontalOverflow(page);

  await page.goto("/settings/database");
  await expect(page.getByRole("heading", { name: "データベース設定" }).first()).toBeVisible();
  await expect(page.getByLabel("データベースユーザー")).toBeVisible();
  await expectNoAdbManagementPanel(page);
  await expectWalletAboveServiceDsn(page);
  await expectDatabaseSupplementalPanelsAbsent(page);
  const databaseSavedSecretBadgeStyle = await getSavedSecretBadgeStyle(page, "oracle-password");
  expect(databaseSavedSecretBadgeStyle).toEqual(modelSavedSecretBadgeStyle);
  await expectNoOperationsMemoOrReadiness(page);
  await page.getByRole("button", { name: "DB接続テスト" }).click();
  await expect(page.getByText("Oracle AI Database への接続に成功しました。")).toBeVisible();
  await expect(page.getByTestId("settings-database-test-result")).toHaveAttribute(
    "data-tone",
    "success"
  );
  await expectNoOperationsMemoOrReadiness(page);
  await expectNoHorizontalOverflow(page);
});

test("システム設定のテスト成功結果を共通パネルで表示する", async ({ page }, testInfo) => {
  await page.goto("/settings/model");
  await page
    .getByRole("button", { name: "enterprise-nl2sql-vlm をテスト", exact: true })
    .click();
  const enterpriseResult = page
    .getByText("enterprise-nl2sql-vlm の設定を確認しました。", { exact: true })
    .locator("xpath=ancestor::*[@data-settings-test-result][1]");
  await expect(enterpriseResult).toHaveAttribute("data-tone", "success");
  await expect(enterpriseResult).toContainText("surface");
  await expect(enterpriseResult).toContainText("vision");

  await page
    .getByRole("button", { name: "text-embedding-v4 をテスト", exact: true })
    .click();
  const embeddingResult = page
    .getByText("text-embedding-v4 の設定を確認しました。", { exact: true })
    .locator("xpath=ancestor::*[@data-settings-test-result][1]");
  await expect(embeddingResult).toContainText("vector_dim");
  await expect(embeddingResult).toContainText("1536");

  await page
    .getByRole("button", { name: "qwen3-rerank をテスト", exact: true })
    .click();
  const rerankResult = page
    .getByText("qwen3-rerank の設定を確認しました。", { exact: true })
    .locator("xpath=ancestor::*[@data-settings-test-result][1]");
  await expect(rerankResult).toContainText("top_score");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("settings-model-test-result-success.png"),
    fullPage: true,
  });
  await page.getByRole("textbox", { name: "モデル ID 2" }).fill("enterprise-nl2sql-vlm-v2");
  await expect(enterpriseResult).toHaveCount(0);

  await page.unroute("**/api/settings/database/test");
  await page.route("**/api/settings/database/test", (route) =>
    fulfillJson(route, {
      status: "success",
      readiness: "ok",
      message: "Oracle AI Database への接続に成功しました。",
      elapsed_ms: 9,
      troubleshooting: [],
      details: { network_call: true },
      checked_at: "2026-06-21T10:00:00.000Z",
      error_type: null,
    })
  );
  await page.goto("/settings/database");
  await page.getByRole("button", { name: "DB接続テスト" }).click();
  const databaseResult = page.getByTestId("settings-database-test-result");
  await expect(databaseResult.getByRole("status")).toBeVisible();
  await expect(databaseResult).toHaveAttribute("data-tone", "success");
  await expect(databaseResult).toContainText("Oracle AI Database への接続に成功しました。");
  await expect(databaseResult).toContainText("所要時間: 9 ms");
  await expect(databaseResult).toContainText("確認時刻:");
  await expect(databaseResult).toContainText("network_call");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("settings-test-result-success.png"),
    fullPage: true,
  });
  await page.locator("#oracle-user").fill("NL2SQL_APP_CHANGED");
  await expect(databaseResult).toHaveCount(0);
});

test("システム設定の失敗と API エラーを共通 danger パネルで表示する", async ({ page }) => {
  await page.unroute("**/api/settings/model/test");
  await page.route("**/api/settings/model/test", (route) =>
    fulfillJson(route, {
      status: "failed",
      target_type: "embedding",
      model_id: "text-embedding-v4",
      message: "Embedding モデルのテストに失敗しました。",
      troubleshooting: ["OCI Generative AI の設定を確認してください。"],
      raw_error: "gateway timeout",
      error_type: "TimeoutError",
      elapsed_ms: 30000,
      checked_at: "2026-06-21T10:00:00.000Z",
      details: {},
    })
  );
  await page.goto("/settings/model");
  await page
    .getByRole("button", { name: "text-embedding-v4 をテスト", exact: true })
    .click();
  const modelResult = page
    .getByText("Embedding モデルのテストに失敗しました。", { exact: true })
    .locator("xpath=ancestor::*[@data-settings-test-result][1]");
  await expect(modelResult.getByRole("alert")).toBeVisible();
  await expect(modelResult).toContainText("確認ポイント");
  await expect(modelResult).toContainText("TimeoutError");
  await expect(modelResult).toContainText("gateway timeout");
  await expectNoHorizontalOverflow(page);

  await page.unroute("**/api/settings/database/test");
  await page.route("**/api/settings/database/test", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "DB 接続サービスに到達できません。" }),
    })
  );
  await page.goto("/settings/database");
  await page.getByRole("button", { name: "DB接続テスト" }).click();
  const databaseResult = page.getByTestId("settings-database-test-result");
  await expect(databaseResult.getByRole("alert")).toBeVisible();
  await expect(databaseResult).toHaveAttribute("data-tone", "danger");
  await expect(databaseResult).toContainText("DB 接続サービスに到達できません。");
  await expect(databaseResult).toContainText("再試行してください。");
  await expectNoHorizontalOverflow(page);
});

test("データベース設定は Wallet ZIP の下にサービス名を置き、保存済みパスワードを明示表示する", { tag: "@responsive" }, async ({
  page,
}) => {
  let revealCount = 0;
  await page.unroute("**/api/settings/database/password/reveal");
  await page.route("**/api/settings/database/password/reveal", async (route) => {
    revealCount += 1;
    await fulfillJson(route, { password: "database-secret-fixture" });
  });

  await page.goto("/settings/database");
  await expect.poll(() => revealCount).toBe(0);

  await expectWalletAboveServiceDsn(page);
  await expectNoAdbManagementPanel(page);

  const password = page.getByLabel("データベースパスワード");
  await expect(password).toHaveValue("");
  await expect(password).toHaveAttribute("type", "password");
  await expectDatabaseSupplementalPanelsAbsent(page);

  await page.getByRole("button", { name: "DB パスワードを表示" }).click();
  await expect.poll(() => revealCount).toBe(1);
  await expect(password).toHaveValue("database-secret-fixture");
  await expect(password).toHaveAttribute("type", "text");
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expect
    .poll(() =>
      page.evaluate(() => document.body.innerText.includes("database-secret-fixture"))
    )
    .toBeFalsy();

  await page.getByRole("button", { name: "DB パスワードを隠す" }).click();
  await expect(password).toHaveAttribute("type", "password");
  await expect(password).toHaveValue("database-secret-fixture");

  const revealAgain = page.getByRole("button", { name: "DB パスワードを表示" });
  await revealAgain.focus();
  await expect(revealAgain).toBeFocused();
  await revealAgain.press("Enter");
  await expect.poll(() => revealCount).toBe(1);
  await expect(password).toHaveAttribute("type", "text");
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoAdbManagementPanel(page);
  await expectWalletAboveServiceDsn(page);
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoHorizontalOverflow(page);
});

test("データベース設定は Wallet パスワードを保存し、保存済み値を削除できる", async ({
  page,
}) => {
  const savedRequests: Array<Record<string, unknown>> = [];
  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", async (route) => {
    if (route.request().method() === "PATCH") {
      savedRequests.push(route.request().postDataJSON() as Record<string, unknown>);
      await fulfillJson(route, databaseSettingsFixture({ has_wallet_password: true }));
      return;
    }
    await fulfillJson(route, databaseSettingsFixture({ has_wallet_password: true }));
  });

  await page.goto("/settings/database");

  const walletPassword = page.getByLabel("Wallet パスワード", { exact: true });
  await expect(walletPassword).toBeVisible();
  await expect(walletPassword).toHaveValue("");
  await expect(walletPassword).toHaveAttribute("type", "password");
  const walletSavedSecretBadgeStyle = await getSavedSecretBadgeStyle(
    page,
    "oracle-wallet-password"
  );
  expect(walletSavedSecretBadgeStyle.text).toBe("保存済み");
  await expect(
    page.getByText("保存済み Wallet パスワードがあります。", { exact: false })
  ).toBeVisible();

  await walletPassword.fill("wallet-secret-fixture");
  await page.getByRole("button", { name: "Wallet パスワードを表示", exact: true }).click();
  await expect(walletPassword).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "Wallet パスワードを隠す", exact: true }).click();
  await expect(walletPassword).toHaveAttribute("type", "password");

  await page.getByRole("button", { name: "DB設定を保存" }).click();
  await expect.poll(() => savedRequests.length).toBe(1);
  expect(savedRequests[0]).toMatchObject({
    connection_security: "wallet_mtls",
    wallet_password: "wallet-secret-fixture",
  });
  expect(savedRequests[0]).not.toHaveProperty("clear_wallet_password");

  const clearWalletPassword = page.getByLabel("保存済み Wallet パスワードを削除する");
  await clearWalletPassword.check();
  await expect(walletPassword).toBeDisabled();
  await page.getByRole("button", { name: "DB設定を保存" }).click();
  await expect.poll(() => savedRequests.length).toBe(2);
  expect(savedRequests[1]).toMatchObject({
    connection_security: "wallet_mtls",
    clear_wallet_password: true,
  });
  expect(savedRequests[1]).not.toHaveProperty("wallet_password");
  await expectNoHorizontalOverflow(page);
});

test("DB パスワード表示ボタンの取得中 icon は上下に浮動しない共有 Spinner を使う", async ({
  page,
}) => {
  const revealGate = createRequestGate();
  let revealCount = 0;
  await page.unroute("**/api/settings/database/password/reveal");
  await page.route("**/api/settings/database/password/reveal", async (route) => {
    revealCount += 1;
    await revealGate.promise;
    await fulfillJson(route, { password: "database-secret-fixture" });
  });

  await page.goto("/settings/database");
  await expect.poll(() => revealCount).toBe(0);

  const revealButton = page.locator("#oracle-password + button");
  await expect(revealButton).toHaveAttribute("aria-label", "DB パスワードを表示");
  await revealButton.click();
  await expect.poll(() => revealCount).toBe(1);
  await expect(revealButton).toHaveAttribute("aria-label", "DB パスワードを取得中");
  await expect(revealButton).toBeDisabled();

  // 共有 Spinner（svg.animate-spin: 全周トラック circle + 180 度対称の 2 本のアーク。#1180）。
  // 見た目の重心が動かないことは button-spinner-alignment.spec.ts で測る。ここではトラックの有無を確かめる。
  const loadingIcon = revealButton.locator("svg.animate-spin");
  await expect(loadingIcon).toBeVisible();
  await expect(revealButton.locator("svg:visible")).toHaveCount(1);
  await expect(loadingIcon.locator("circle")).toHaveCount(1);
  const metrics = await loadingIcon.evaluate((node) => {
    const icon = node.getBoundingClientRect();
    const button = (node.closest("button") as HTMLElement).getBoundingClientRect();
    const computed = getComputedStyle(node);
    return {
      width: computed.width,
      height: computed.height,
      transformBox: computed.transformBox,
      animationName: computed.animationName,
      iconCenterY: icon.y + icon.height / 2,
      buttonCenterY: button.y + button.height / 2,
    };
  });
  expect(metrics.width).toBe("16px");
  expect(metrics.height).toBe("16px");
  expect(metrics.transformBox).toBe("view-box");
  expect(metrics.animationName).not.toBe("none");
  expect(Math.abs(metrics.iconCenterY - metrics.buttonCenterY)).toBeLessThan(0.02);

  revealGate.release();
  await expect(page.locator("#oracle-password")).toHaveAttribute("type", "text");
  await expectNoHorizontalOverflow(page);
});

test("データベース設定は接続セキュリティで Wallet mTLS と Walletless TLS を切り替える", async ({
  page,
}) => {
  await page.goto("/settings/database");

  const securityMode = page.getByRole("combobox", { name: "接続セキュリティ" });
  await expect(securityMode).toBeVisible();
  await expect(securityMode).toContainText("Wallet mTLS");
  await expect(page.getByTestId("oracle-wallet-upload")).toBeVisible();
  await expect(page.getByLabel("Wallet パスワード", { exact: true })).toBeVisible();

  await securityMode.click();
  await page.getByRole("option", { name: /Walletless TLS/ }).click();

  await expect(page.getByText("Walletless TLS では Wallet", { exact: false })).toBeVisible();
  await expect(page.getByTestId("oracle-wallet-upload")).toHaveCount(0);
  await expect(page.getByLabel("Wallet パスワード", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("接続 DSN")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("アップロード保存先は右側情報カラムを表示しない", async ({
  page,
}) => {
  await page.goto("/settings/upload-storage");

  await expect(page.getByRole("heading", { name: "アップロード保存先" }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "保存先", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "保存先状態" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expect(page.getByLabel("ローカル保存ディレクトリ")).toHaveValue(
    "/u01/data/production-ready-nl2sql"
  );
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("heading", { name: "保存先状態" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("データベース設定は右側情報カラムを表示しない", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(
      route,
      databaseSettingsFixture({
        driver_mode: "thin",
        client_lib_dir: "",
        wallet_dir: "/u01/aipoc/wallet",
      })
    )
  );

  await page.goto("/settings/database");

  await expect(page.getByRole("heading", { name: "データベース設定" }).first()).toBeVisible();
  await expectNoAdbManagementPanel(page);
  await expectWalletAboveServiceDsn(page);
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoHorizontalOverflow(page);
});

test("Wallet ZIP をドラッグ＆ドロップで即時アップロードできる", { tag: "@responsive" }, async ({
  page,
}) => {
  let walletUploadCount = 0;
  let walletUploadBody = "";

  await page.unroute("**/api/settings/database/wallet");
  await page.route("**/api/settings/database/wallet", async (route) => {
    walletUploadCount += 1;
    walletUploadBody = route.request().postDataBuffer()?.toString("utf8") ?? "";
    await fulfillJson(route, databaseSettingsFixture());
  });

  await page.goto("/settings/database");
  const walletInput = page.getByTestId("oracle-wallet-upload-input");
  await walletInput.focus();
  await expect(walletInput).toBeFocused();
  await dropFiles(page, page.getByTestId("oracle-wallet-upload-dropzone"), [
    {
      name: "wallet.zip",
      type: "application/zip",
      content: "PK\u0003\u0004test",
    },
  ]);
  await expect(page.getByText("Wallet ZIP をアップロードしました: wallet.zip")).toBeVisible();
  await expect.poll(() => walletUploadCount).toBe(1);
  expect(walletUploadBody).toContain('filename="wallet.zip"');
  await expectNoHorizontalOverflow(page);
});

test("データベース設定の可視読込は共通スケルトン内に経過時間を表示する", async ({
  page,
}) => {
  const gate = createRequestGate();
  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", async (route) => {
    await gate.promise;
    await fulfillJson(route, databaseSettingsFixture());
  });

  await page.goto("/settings/database");
  const loading = page.getByTestId("settings-database-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText("データベース設定を読み込んでいます");
  await expect(loading.getByRole("timer")).toHaveAccessibleName("経過時間 00:00");
  await expect(loading.getByRole("timer")).toHaveAttribute("aria-live", "off");

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoHorizontalOverflow(page);
  gate.release();
  await expect(page.getByLabel("データベースユーザー")).toBeVisible();
  await expect(loading).toHaveCount(0);
});

test("非正常な readiness 値も設定画面には表示しない", async ({ page }) => {
  await page.unroute("**/api/settings/upload-storage");
  await page.route("**/api/settings/upload-storage", (route) =>
    fulfillJson(route, {
      local_storage_dir: "/u01/data/production-ready-nl2sql",
      readiness: "missing_credentials",
      max_upload_bytes: 104857600,
      config_source: "runtime",
    })
  );

  await page.goto("/settings/upload-storage");
  await expect(page.getByRole("heading", { name: "保存先", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "保存先状態" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(
    page.getByText("backend/.env + 現在のプロセス設定", { exact: true })
  ).toHaveCount(0);
  await expectNoOperationsMemoOrReadiness(page);

  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, databaseSettingsFixture({ readiness: "invalid" }))
  );
  await page.unroute("**/api/settings/database/test");
  await page.route("**/api/settings/database/test", (route) =>
    fulfillJson(route, {
      status: "failed",
      readiness: "error",
      message: "接続設定を確認してください。",
      elapsed_ms: 3,
      troubleshooting: ["DSN と Wallet を確認してください。"],
      details: { network_call: false },
      checked_at: "2026-06-21T10:00:00.000Z",
      error_type: "InvalidConfiguration",
    })
  );

  await page.goto("/settings/database");
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoOperationsMemoOrReadiness(page);
  await page.getByRole("button", { name: "DB接続テスト" }).click();
  await expect(page.getByText("接続設定を確認してください。")).toBeVisible();
  await expect(page.getByText("所要時間: 3 ms")).toBeVisible();
  await expect(page.getByTestId("settings-database-test-result")).toHaveAttribute(
    "data-tone",
    "danger"
  );
  await expect(page.getByTestId("settings-database-test-result")).toContainText("network_call");
  await expectNoOperationsMemoOrReadiness(page);
  await expectNoHorizontalOverflow(page);
});

test("Endpoint URL の公式ドキュメントをキーボードで開き、未保存入力を保持する", async ({
  page,
  context,
}, testInfo) => {
  const docsUrl = "https://docs.oracle.com/en-us/iaas/Content/generative-ai/openai-compatible-api.htm";
  // 外部サイトへ接続せず、新しいタブの遷移先を検証する。
  await context.route(docsUrl, (route) => route.fulfill({
    contentType: "text/html",
    body: "<title>OCI OpenAI-Compatible Endpoints</title>",
  }));
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/settings/model") && request.method() !== "GET") {
      mutations.push(request.method());
    }
  });

  await page.goto("/settings/model");
  const endpoint = page.getByRole("textbox", { name: "Endpoint URL" });
  const docsLink = page.getByRole("link", { name: "公式ドキュメント（新しいタブで開く）" });
  await expect(docsLink).toBeVisible();
  await expect(docsLink).toHaveAttribute("href", docsUrl);
  await expect(docsLink).toHaveAttribute("target", "_blank");
  await expect(docsLink).toHaveAttribute("rel", "noopener noreferrer");
  await expect(endpoint).toHaveAccessibleDescription(
    /OpenAI-compatible base URL を \/openai\/v1 まで.*\/responses.*path は補いません.*公式ドキュメント/,
  );
  await endpoint.fill("https://unsaved.example.com/openai/v1");
  await endpoint.press("Tab");
  await expect(docsLink).toBeFocused();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("endpoint-docs-link.png") });

  const popupPromise = page.waitForEvent("popup");
  await docsLink.press("Enter");
  const popup = await popupPromise;
  await expect(popup).toHaveURL(docsUrl);
  await popup.close();
  await expect(page).toHaveURL(/\/settings\/model$/);
  await expect(endpoint).toHaveValue("https://unsaved.example.com/openai/v1");
  expect(mutations).toEqual([]);
});

test("モデル設定の読込失敗から再試行し、未設定 Endpoint URL の説明リンクを表示する", async ({ page }) => {
  await page.unroute("**/api/settings/model");
  const gate = createRequestGate();
  let fail = true;
  await page.route("**/api/settings/model", async (route) => {
    if (fail) {
      await gate.promise;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "モデル設定を取得できませんでした。" }),
      });
      return;
    }
    const fixture = modelSettingsFixture();
    fixture.settings.enterprise_ai.connections = [primaryConnectionFixture({ endpoint: "" })];
    await fulfillJson(route, fixture);
  });
  await page.goto("/settings/model");
  await expect(page.getByTestId("settings-model-loading")).toBeVisible();
  gate.release();
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByRole("textbox", { name: "Endpoint URL" })).toHaveValue("");
  await expect(page.getByRole("link", { name: "公式ドキュメント（新しいタブで開く）" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("モデル設定を3カードごとに独立保存し、非表示設定と未保存入力を保持する", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.unroute("**/api/settings/model");
  const base = modelSettingsFixture();
  let persisted = modelSettingsFixture({
    settings: {
      ...base.settings,
      enterprise_ai: {
        ...base.settings.enterprise_ai,
        api_path: "/custom-responses",
        vlm_input_mode: "inline_image",
        timeout_seconds: 177,
        max_retries: 4,
        // 出力の上限は登録モデルごと（#1719）。画面の 1 つ目のモデルの値を保存で保つ。
        models: (base.settings.enterprise_ai.models as Array<Record<string, unknown>>).map(
          (model, index) => (index === 0 ? { ...model, max_output_tokens: 65536 } : model)
        ),
      },
    },
  });
  const requests: Array<Record<string, unknown>> = [];
  const firstSaveGate = createRequestGate();
  let holdFirstSave = true;
  let rejectNextSave = false;

  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() !== "PATCH") {
      await fulfillJson(route, persisted);
      return;
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    requests.push(body);
    if (holdFirstSave) {
      holdFirstSave = false;
      await firstSaveGate.promise;
    }
    if (rejectNextSave) {
      rejectNextSave = false;
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "登録モデルを保存できませんでした。" }),
      });
      return;
    }
    persisted = modelSettingsFixture({ settings: body });
    await fulfillJson(route, persisted);
  });

  await page.goto("/settings/model");
  await expect(page.locator("#enterprise-api-path")).toHaveCount(0);
  await expect(page.locator("#enterprise-vlm-input-mode")).toHaveCount(0);
  await expect(page.locator("#enterprise-timeout")).toHaveCount(0);
  await expect(page.locator("#enterprise-retries")).toHaveCount(0);

  await page.locator("#enterprise-endpoint").fill("https://changed.example.com");
  await page.getByRole("textbox", { name: "モデル ID 1" }).fill("enterprise-unsaved-model");
  await page.locator("#genai-embedding-model").fill("cohere.unsaved-embed");

  await expectLargeActionButton(page.getByRole("button", { name: "OCI Enterprise AI: 保存" }));
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  // loading 中もラベルは変えない（DS ルール）。aria-busy + disabled + スピナーで保存中を表す。
  const enterpriseSaving = page.getByRole("button", { name: "OCI Enterprise AI: 保存" });
  await expect(enterpriseSaving).toBeDisabled();
  await expect(enterpriseSaving).toHaveAttribute("aria-busy", "true");
  await expectLargeActionButton(enterpriseSaving);
  await expect(page.getByRole("button", { name: "登録モデル: 保存" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "埋め込み / 再ランク: 保存" })).toBeDisabled();
  firstSaveGate.release();
  await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。")).toBeVisible();

  const connectionRequest = requests[0];
  const connectionEnterprise = connectionRequest.enterprise_ai as Record<string, unknown>;
  expect(primaryConnection(connectionEnterprise).endpoint).toBe("https://changed.example.com");
  expect(connectionEnterprise.api_path).toBe("/custom-responses");
  expect(connectionEnterprise.vlm_input_mode).toBe("inline_image");
  expect(connectionEnterprise.timeout_seconds).toBe(177);
  expect(connectionEnterprise.max_retries).toBe(4);
  expect(
    ((connectionEnterprise.models as Array<Record<string, unknown>>)[0]).max_output_tokens
  ).toBe(65536);
  expect(connectionEnterprise).not.toHaveProperty("llm_max_output_tokens");
  expect(
    ((connectionEnterprise.models as Array<Record<string, unknown>>)[0]).model_id
  ).toBe("enterprise-nl2sql-llm");
  expect(
    (connectionRequest.generative_ai as Record<string, unknown>).embedding_model
  ).toBe("text-embedding-v4");
  await expect(page.getByRole("textbox", { name: "モデル ID 1" })).toHaveValue(
    "enterprise-unsaved-model"
  );
  await expect(page.locator("#genai-embedding-model")).toHaveValue("cohere.unsaved-embed");

  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。")).toBeVisible();
  const modelsRequest = requests[1];
  const modelsEnterprise = modelsRequest.enterprise_ai as Record<string, unknown>;
  expect(primaryConnection(modelsEnterprise).endpoint).toBe("https://changed.example.com");
  expect(modelsEnterprise.api_path).toBe("/custom-responses");
  expect(modelsEnterprise.vlm_input_mode).toBe("inline_image");
  expect(modelsEnterprise.timeout_seconds).toBe(177);
  expect(modelsEnterprise.max_retries).toBe(4);
  expect(
    ((modelsEnterprise.models as Array<Record<string, unknown>>)[0]).max_output_tokens
  ).toBe(65536);
  expect(((modelsEnterprise.models as Array<Record<string, unknown>>)[0]).model_id).toBe(
    "enterprise-unsaved-model"
  );
  expect((modelsRequest.generative_ai as Record<string, unknown>).embedding_model).toBe(
    "text-embedding-v4"
  );
  await expect(page.locator("#genai-embedding-model")).toHaveValue("cohere.unsaved-embed");

  const generativeSave = page.getByRole("button", { name: "埋め込み / 再ランク: 保存" });
  await generativeSave.focus();
  await expect(generativeSave).toBeFocused();
  await generativeSave.press("Enter");
  await expect(page.getByText("埋め込み / 再ランクの設定を保存しました。")).toBeVisible();
  const generativeRequest = requests[2];
  expect(
    (generativeRequest.generative_ai as Record<string, unknown>).embedding_model
  ).toBe("cohere.unsaved-embed");
  expect(
    ((generativeRequest.enterprise_ai as Record<string, unknown>).models as Array<
      Record<string, unknown>
    >)[0].model_id
  ).toBe("enterprise-unsaved-model");
  expect(
    ((generativeRequest.enterprise_ai as Record<string, unknown>).models as Array<
      Record<string, unknown>
    >)[0].max_output_tokens
  ).toBe(65536);

  rejectNextSave = true;
  await page.getByRole("textbox", { name: "モデル ID 1" }).fill("enterprise-failed-model");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  const modelsForm = page
    .getByRole("button", { name: "登録モデル: 保存" })
    .locator("xpath=ancestor::form[1]");
  await expect(modelsForm.getByRole("alert")).toContainText(
    "登録モデルを保存できませんでした。"
  );
  await expect(page.getByRole("textbox", { name: "モデル ID 1" })).toHaveValue(
    "enterprise-failed-model"
  );

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoHorizontalOverflow(page);
  await expect(page.getByRole("button", { name: "OCI Enterprise AI: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "登録モデル: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "埋め込み / 再ランク: 保存" })).toBeVisible();
  await expectModelSaveButtonsUsePrimaryStyle(page);
});

test("モデル API Key を .env に新規保存して削除でき、プレビュー欄を表示しない", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.unroute("**/api/settings/model");
  const missing = modelSettingsFixture({
    settings: {
      ...modelSettingsFixture().settings,
      enterprise_ai: {
        ...modelSettingsFixture().settings.enterprise_ai,
        connections: [primaryConnectionFixture({ has_api_key: false })],
      },
    },
    secret_source: "missing",
  });
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      requests.push(body);
      const cleared = primaryConnection(body.enterprise_ai).clear_api_key === true;
      await fulfillJson(
        route,
        modelSettingsFixture({
          settings: {
            ...missing.settings,
            enterprise_ai: {
              ...missing.settings.enterprise_ai,
              connections: [primaryConnectionFixture({ has_api_key: !cleared })],
            },
          },
          secret_source: cleared ? "missing" : "environment",
        })
      );
      return;
    }
    await fulfillJson(route, missing);
  });

  await page.goto("/settings/model");
  // ラベルには「OCI 運用時必須」のタグも入るので、欄は id で引く（#542）。
  await page.locator("#enterprise-api-key").fill("new-key-fixture");
  await expectModelPreviewPanelsAbsent(page);
  await expect(page.locator("body")).not.toContainText("new-key-fixture");
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  const notificationRegion = page.getByRole("region", { name: "通知" });
  await expect(notificationRegion).toContainText("OCI Enterprise AI 接続設定を保存しました。");
  await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。")).toHaveCount(1);
  expect(primaryConnection(requests[0].enterprise_ai).api_key).toBe("new-key-fixture");

  await page.getByLabel("保存済み API key を削除する").check();
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  expect(primaryConnection(requests[1].enterprise_ai).clear_api_key).toBe(true);
  await expect(page.getByText("未設定", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("モデル設定はプライマリ接続とセカンダリ接続をタブで切り替え、登録モデルの接続を選んで保存する (#542)", async ({
  page,
}) => {
  await page.unroute("**/api/settings/model");
  const base = modelSettingsFixture();
  // #533 の表示名（display_name）が残った応答も読める（#542 で廃止。画面は ID から名前を出す）。
  const persisted = modelSettingsFixture({
    settings: {
      ...base.settings,
      enterprise_ai: {
        ...base.settings.enterprise_ai,
        connections: [
          primaryConnectionFixture({ display_name: "大阪" }),
          primaryConnectionFixture({
            connection_id: "secondary",
            display_name: "シカゴ",
            endpoint: "https://secondary.example.com",
          }),
        ],
      },
    },
  });
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      requests.push(body);
      await fulfillJson(route, modelSettingsFixture({ settings: body }));
      return;
    }
    await fulfillJson(route, persisted);
  });

  await page.goto("/settings/model");
  const tablist = page.getByRole("tablist", { name: "OCI Enterprise AI の接続" });
  await expect(tablist.getByRole("tab", { name: "プライマリ接続" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(page.getByText("シカゴ")).toHaveCount(0);
  await tablist.getByRole("tab", { name: "セカンダリ接続" }).click();
  await expect(page.locator("#enterprise-secondary-endpoint")).toHaveValue(
    "https://secondary.example.com"
  );
  const modelConnection = page.getByRole("combobox", { name: "モデル 2 の接続" });
  await expect(modelConnection).toContainText("プライマリ接続");
  await modelConnection.click();
  const options = page.getByRole("listbox", { name: "モデル 2 の接続" }).getByRole("option");
  await expect(options).toHaveText(["プライマリ接続", "セカンダリ接続"]);
  await options.filter({ hasText: "セカンダリ接続" }).click();
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。")).toBeVisible();
  const enterprise = requests[0].enterprise_ai as Record<string, unknown>;
  const models = enterprise.models as Array<Record<string, unknown>>;
  expect(models.map((model) => model.connection_id)).toEqual(["primary", "secondary"]);
  // 保存する payload に表示名を含めない。
  for (const connection of enterprise.connections as Array<Record<string, unknown>>) {
    expect(connection).not.toHaveProperty("display_name");
  }
  await expectNoHorizontalOverflow(page);
});

test("登録モデルは入力の上限の隣に出力の上限を持ち、入力の上限を超える値は欄の直下で止める (#1719)", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.unroute("**/api/settings/model");
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      requests.push(body);
      await fulfillJson(route, modelSettingsFixture({ settings: body }));
      return;
    }
    await fulfillJson(route, modelSettingsFixture());
  });

  await page.goto("/settings/model");
  const input = page.getByRole("spinbutton", { name: "モデル 1 の入力の上限（tokens）" });
  const output = page.getByRole("spinbutton", { name: "モデル 1 の出力の上限（tokens）" });
  // 値の無いモデル（#1719 より前の保存）は既定の 32768。
  await expect(input).toHaveValue("128000");
  await expect(output).toHaveValue("32768");
  // 入力の上限と取り違えないよう、両方の説明に違いを書く。
  await expect(page.locator("#enterprise-model-max-output-tokens-help")).toContainText(
    "推論の token も含まれる場合があります"
  );
  await expect(page.locator("#enterprise-model-max-input-tokens-help")).toContainText(
    "入力と出力を合わせたコンテキスト長"
  );
  const isWide = (page.viewportSize()?.width ?? 0) >= 1536;
  if (isWide) {
    // 広い画面では表の列として、入力の上限の右に並ぶ。
    const inputBox = await input.boundingBox();
    const outputBox = await output.boundingBox();
    expect(outputBox!.x).toBeGreaterThan(inputBox!.x + inputBox!.width - 1);
    expect(Math.abs(outputBox!.y - inputBox!.y)).toBeLessThan(2);
  }

  await output.fill("131072");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(output).toBeFocused();
  await expect(output).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("出力の上限は入力の上限（128000）以下にしてください。")).toBeVisible();
  expect(requests).toHaveLength(0);
  if (process.env.MODEL_OUTPUT_SCREENSHOT_DIR) {
    await page.screenshot({
      path: `${process.env.MODEL_OUTPUT_SCREENSHOT_DIR}/model-output-limit-error-${test.info().project.name}.png`,
      fullPage: false,
    });
  }

  // 128k 以上も、入力の上限の中なら保存できる（xai.grok-4.3 の context は 1M）。
  await input.fill("1000000");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。")).toBeVisible();
  const models = (requests[0].enterprise_ai as Record<string, unknown>).models as Array<
    Record<string, unknown>
  >;
  expect(models.map((model) => [model.max_input_tokens, model.max_output_tokens])).toEqual([
    [1000000, 131072],
    [128000, 32768],
  ]);
  expect(requests[0].enterprise_ai).not.toHaveProperty("llm_max_output_tokens");
  await expectNoHorizontalOverflow(page);
  if (process.env.MODEL_OUTPUT_SCREENSHOT_DIR && !test.info().project.name.includes("mobile")) {
    // PR 用の画面（表の形になる 1920px の登録モデル）。
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.locator("#enterprise-model-catalog").scrollIntoViewIfNeeded();
    await page.locator("#enterprise-model-catalog").screenshot({
      path: `${process.env.MODEL_OUTPUT_SCREENSHOT_DIR}/model-catalog-1920.png`,
    });
  }
});

test("ターシャリ接続は Endpoint URL と API key だけで設定し、登録モデルで選べる (#786)", async ({
  page,
}) => {
  await page.unroute("**/api/settings/model");
  let current: Record<string, unknown> = modelSettingsFixture();
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      requests.push(body);
      current = modelSettingsFixture({ settings: body });
    }
    await fulfillJson(route, current);
  });

  await page.goto("/settings/model");
  const tablist = page.getByRole("tablist", { name: "OCI Enterprise AI の接続" });
  // タブの並びはプライマリ接続 → セカンダリ接続 → ターシャリ接続。
  await expect(tablist.getByRole("tab")).toHaveText([
    "プライマリ接続",
    "セカンダリ接続",
    "ターシャリ接続",
  ]);
  await tablist.getByRole("tab", { name: "ターシャリ接続" }).click();
  await expect(page.getByTestId("enterprise-connection-tertiary-empty")).toContainText(
    "ターシャリ接続は設定されていません。"
  );
  await page.getByRole("button", { name: "ターシャリ接続を設定" }).click();
  const endpoint = page.locator("#enterprise-tertiary-endpoint");
  const project = page.locator("#enterprise-tertiary-project-ocid");
  const apiKey = page.locator("#enterprise-tertiary-api-key");
  await expect(endpoint).toBeFocused();
  await expect(endpoint).toHaveAttribute("aria-required", "true");
  await expect(apiKey).toHaveAttribute("aria-required", "true");
  await expect(project).not.toHaveAttribute("aria-required", "true");
  await expect(endpoint).toHaveAttribute("placeholder", "https://api.openai.com/v1");
  // Endpoint URL は入力どおり使う（#1440）。書き方（path まで）と例を説明に出す。
  await expect(endpoint).toHaveAccessibleDescription(
    /base URL を path まで.*https:\/\/api\.openai\.com\/v1.*https:\/\/api\.deepseek\.com\/v1.*path は補いません/,
  );
  await expect(apiKey).toHaveAttribute("placeholder", /^sk-/);

  await endpoint.fill("https://api.openai.com/v1");
  await apiKey.fill("sk-openai-fixture");
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。").first()).toBeVisible();
  const connections = (requests[0].enterprise_ai as Record<string, unknown>)
    .connections as Array<Record<string, unknown>>;
  expect(connections.map((connection) => connection.connection_id)).toEqual([
    "primary",
    "tertiary",
  ]);
  expect(connections[1]).toMatchObject({
    endpoint: "https://api.openai.com/v1",
    project_ocid: "",
    api_key: "sk-openai-fixture",
  });

  const modelConnection = page.getByRole("combobox", { name: "モデル 2 の接続" });
  await modelConnection.click();
  const options = page.getByRole("listbox", { name: "モデル 2 の接続" }).getByRole("option");
  await expect(options).toHaveText(["プライマリ接続", "ターシャリ接続"]);
  await options.filter({ hasText: "ターシャリ接続" }).click();
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。")).toBeVisible();
  const models = (requests[1].enterprise_ai as Record<string, unknown>).models as Array<
    Record<string, unknown>
  >;
  expect(models.map((model) => model.connection_id)).toEqual(["primary", "tertiary"]);
  await expectNoHorizontalOverflow(page);
});

test("legacy JSON の原因と復旧方法を表示し、保存時に既存 Key を保持して移行する", async ({
  page,
}) => {
  await page.unroute("**/api/settings/model");
  const legacy = modelSettingsFixture({
    secret_source: "legacy_json",
    legacy_secret_detected: true,
  });
  const savedRequests: Array<Record<string, unknown>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      savedRequests.push(route.request().postDataJSON() as Record<string, unknown>);
      await fulfillJson(route, modelSettingsFixture());
      return;
    }
    await fulfillJson(route, legacy);
  });

  await page.goto("/settings/model");
  await expect(page.getByText("旧 JSON に API Key が残っています")).toBeVisible();
  await expect(page.getByText(/原因: 旧形式の model-settings.json/)).toBeVisible();
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();

  expect(savedRequests).toHaveLength(1);
  const enterprise = primaryConnection(savedRequests[0]?.enterprise_ai);
  expect(enterprise.api_key).toBe("");
  expect(enterprise.has_api_key).toBe(true);
  expect(enterprise.clear_api_key).toBe(false);
  await expect(page.getByText("旧 JSON に API Key が残っています")).toHaveCount(0);
  await expect(page.getByText("保存済み", { exact: true })).toBeVisible();
});

test("外観設定でダーク/ライト/自動テーマを切り替えられる", async ({ page }) => {
  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();
  const html = page.locator("html");
  // dev サーバでは getPropertyValue が "light-dark(a, b)" 文字列を返すため、変数の文字列ではなく
  // 実際に解決された色（body の背景色 / 変数を塗ったプローブ要素の background-color）を検証する。
  // 期待値は packages/ui/src/styles/tokens/palette.css の neutral / blue スケール由来。
  const hexToRgb = (hex: string) => {
    const value = Number.parseInt(hex.slice(1), 16);
    return `rgb(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255})`;
  };
  // body { background: var(--color-canvas) }（旧 --background 相当）
  const bgVar = () =>
    page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const tokenVar = (name: string) =>
    page.evaluate((tokenName) => {
      const probe = document.createElement("div");
      probe.style.backgroundColor = `var(${tokenName})`;
      document.body.appendChild(probe);
      const color = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return color;
    }, name);
  const sidebarBgVar = () => tokenVar("--color-sidebar");

  // 既定はライト。
  await expect(html).not.toHaveClass(/dark/);
  await expect.poll(bgVar).toBe(hexToRgb("#f7f8fa"));

  const toggle = page.getByTestId("appearance-theme-toggle");
  await toggle.getByRole("button", { name: "ダーク" }).click();
  await expect(html).toHaveClass(/dark/);
  // 冷調 blue-gray の semantic token。純黒・大面積の純白は使わない。
  // canvas = neutral-950 / sidebar = neutral-1000 / fg = neutral-100 / fg-muted = neutral-350 /
  // border-control = neutral-650 / accent-emphasis = blue-600（いずれもダーク側の値）
  await expect.poll(bgVar).toBe(hexToRgb("#101318"));
  expect(await sidebarBgVar()).toBe(hexToRgb("#0b0e13"));
  expect(await tokenVar("--color-fg")).toBe(hexToRgb("#f2f4f7"));
  expect(await tokenVar("--color-fg-muted")).toBe(hexToRgb("#b2bac5"));
  expect(await tokenVar("--color-border-control")).toBe(hexToRgb("#5d6878"));
  expect(await tokenVar("--color-accent-emphasis")).toBe(hexToRgb("#286abd"));
  await expect(toggle.getByRole("button", { name: "ダーク" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toHaveCSS(
    "color",
    "rgb(242, 244, 247)"
  );

  // 再読込しても永続化される。
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);

  // 自動テーマは実行中の OS 設定変更にも追従する。
  await page.emulateMedia({ colorScheme: "dark" });
  await page.getByTestId("appearance-theme-toggle").getByRole("button", { name: "自動（OS 設定）" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect.poll(bgVar).toBe(hexToRgb("#101318"));
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await expect.poll(bgVar).toBe(hexToRgb("#f7f8fa"));

  await page.getByTestId("appearance-theme-toggle").getByRole("button", { name: "ライト" }).click();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await expect.poll(bgVar).toBe(hexToRgb("#f7f8fa"));
});

test("テーマ切替は transition で旧テーマの色から補間せず、一度で切り替える (#571)", async ({ page }) => {
  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();

  for (const label of ["ダーク", "ライト"]) {
    // クリックと同じタスク内で次のフレームまで待ち、実行中の CSS transition を集める。
    // 押したトグル自身の aria-pressed の変化は操作へのフィードバックなので対象外にする。
    const running = await page.evaluate(async (name) => {
      const group = document.querySelector('[data-testid="appearance-theme-toggle"]')!;
      const button = Array.from(group.querySelectorAll("button")).find((node) => node.textContent?.trim() === name)!;
      button.click();
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return document
        .getAnimations()
        .filter((animation): animation is CSSTransition => animation instanceof CSSTransition)
        .filter((animation) => {
          const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null;
          return !(target instanceof Node && group.contains(target));
        })
        .map((animation) => {
          const target = (animation.effect as KeyframeEffect).target as Element;
          return `${target.tagName.toLowerCase()}.${String(target.className).slice(0, 40)} ${animation.transitionProperty}`;
        });
    }, label);
    expect(running, `${label} へ切り替えた直後の transition`).toEqual([]);
  }
  await expect(page.locator("html")).not.toHaveClass(/dark/);

  // 切替後は通常どおり transition が効く（止めたままにしない）。
  await expect
    .poll(() => page.locator('aside a[aria-current="page"]').evaluate((node) => getComputedStyle(node).transitionDuration))
    .not.toBe("0s");
});

test("アップロード保存先は保存中の編集と重複要求を止め、成功後の入力を保持する", async ({ page }) => {
  let persisted = uploadStorageFixture();
  const gate = createRequestGate();
  let saves = 0;
  let reads = 0;
  await page.route("**/api/settings/upload-storage", async (route) => {
    if (route.request().method() === "PATCH") {
      saves += 1;
      await gate.promise;
      persisted = { ...persisted, ...route.request().postDataJSON() };
    }
    if (route.request().method() === "GET") reads += 1;
    await fulfillJson(route, persisted);
  });
  await page.goto("/settings/upload-storage");
  const path = page.locator("#upload-storage-local-dir");
  await path.fill("/tmp/review-draft");
  persisted = { ...persisted, local_storage_dir: "/tmp/server-update" };
  const initialReads = reads;
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  await expect(path).toHaveValue("/tmp/review-draft");
  expect(await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  })).toBe(true);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => saves).toBe(1);
  await expect(path).toBeDisabled();
  gate.release();
  await expect(path).toBeEnabled();
  await expect(path).toHaveValue("/tmp/review-draft");
  await expectNoHorizontalOverflow(page);
});

test("モデルのテスト中は編集・削除・別テスト・保存を止め、編集後は旧結果を消す", async ({ page }) => {
  const gate = createRequestGate();
  let tests = 0;
  await page.route("**/api/settings/model/test", async (route) => {
    tests += 1;
    await gate.promise;
    const body = route.request().postDataJSON();
    await fulfillJson(route, { status: "success", target_type: body.target_type,
      model_id: body.model_id, message: "レビュー対象モデルの確認成功", elapsed_ms: 10,
      checked_at: "2026-09-11T00:00:00Z", troubleshooting: [], details: {}, raw_error: null, error_type: null });
  });
  await page.goto("/settings/model");
  const model = page.getByRole("textbox", { name: "モデル ID 1" });
  const testButton = page.getByRole("button", { name: /enterprise-nl2sql-llm.*テスト|テスト.*enterprise-nl2sql-llm/ });
  await testButton.click();
  await expect.poll(() => tests).toBe(1);
  await expect(model).toBeDisabled();
  await expect(page.getByRole("button", { name: "OCI Enterprise AI: 保存" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /モデルを削除 1/ })).toBeDisabled();
  gate.release();
  await expect(page.getByText("レビュー対象モデルの確認成功")).toBeVisible();
  await expect(model).toBeEnabled();
  await model.fill("different-model");
  await expect(page.getByText("レビュー対象モデルの確認成功")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("Wallet 更新は未保存 DB ユーザーとパスワードを保持し、接続テスト中は入力を固定する", async ({ page }) => {
  const gate = createRequestGate();
  let tests = 0;
  await page.route("**/api/settings/database/test", async (route) => {
    tests += 1;
    await gate.promise;
    await fulfillJson(route, { status: "success", readiness: "ok", message: "編集済み接続の確認成功",
      elapsed_ms: 5, checked_at: "2026-09-11T00:00:00Z", troubleshooting: [], details: {} });
  });
  await page.goto("/settings/database");
  const user = page.locator("#oracle-user");
  const password = page.getByLabel("データベースパスワード", { exact: true });
  await user.fill("REVIEW_DRAFT_USER");
  await password.fill("new-draft-secret-fixture");
  await page.getByTestId("oracle-wallet-upload").locator('input[type="file"]').setInputFiles({
    name: "review.zip", mimeType: "application/zip", buffer: Buffer.from("fixture-wallet"),
  });
  await expect(page.getByText(/review.zip.*アップロード|Wallet.*アップロードしました/)).toBeVisible();
  await expect(user).toHaveValue("REVIEW_DRAFT_USER");
  await expect(password).toHaveValue("new-draft-secret-fixture");
  await page.getByRole("button", { name: "DB接続テスト", exact: true }).click();
  await expect.poll(() => tests).toBe(1);
  await expect(user).toBeDisabled();
  await expect(password).toBeDisabled();
  gate.release();
  await expect(page.getByText("編集済み接続の確認成功")).toBeVisible();
  await expect(user).toBeEnabled();
  await user.fill("NEXT_DRAFT_USER");
  await expect(page.getByText("編集済み接続の確認成功")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("レビュー補完: モデル追加・既定のモデル 2 つ・Vision・削除確認を保存 payload まで確認する", { tag: "@responsive" }, async ({ page }) => {
  let persisted = modelSettingsFixture();
  const writes: Array<Record<string, any>> = [];
  await page.route("**/api/settings/model", async (route) => {
    if (route.request().method() === "PATCH") {
      const payload = route.request().postDataJSON();
      writes.push(payload);
      persisted = modelSettingsFixture({ settings: payload });
    }
    await fulfillJson(route, persisted);
  });
  await page.goto("/settings/model");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await page.getByRole("textbox", { name: "モデル ID 3", exact: true }).fill("review-model");
  await page.getByRole("textbox", { name: "表示名 3", exact: true }).fill("レビュー用");
  await page.getByRole("switch", { name: "画像入力に対応 3", exact: true }).click();
  // 既定の画像対応モデル（Vision 対応のモデルだけ）と既定のテキストモデルを別々に選ぶ（#499）。
  const visionDefault = page.getByRole("combobox", { name: "既定の画像対応モデル" });
  const textDefault = page.getByRole("combobox", { name: "既定のテキストモデル" });
  // 並びはテキスト → Vision（desktop の 2 列でも 375px の縦積みでも同じ順）で、2 つとも必須（#566）。
  const textBox = await textDefault.boundingBox();
  const visionBox = await visionDefault.boundingBox();
  expect(textBox && visionBox).toBeTruthy();
  expect(
    Math.abs(textBox!.y - visionBox!.y) < 1 ? textBox!.x < visionBox!.x : textBox!.y < visionBox!.y
  ).toBe(true);
  await expect(textDefault).toHaveAttribute("aria-required", "true");
  await expect(visionDefault).toHaveAttribute("aria-required", "true");
  await visionDefault.click();
  await page.getByRole("listbox", { name: "既定の画像対応モデル" }).getByRole("option", { name: /レビュー用/ }).click();
  await textDefault.click();
  await page.getByRole("listbox", { name: "既定のテキストモデル" }).getByRole("option", { name: /レビュー用/ }).click();
  await page.getByRole("button", { name: "登録モデル: 保存", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].enterprise_ai.default_vision_model_id).toBe("review-model");
  expect(writes[0].enterprise_ai.default_text_model_id).toBe("review-model");
  expect(writes[0].enterprise_ai.models[2]).toEqual({
    model_id: "review-model",
    display_name: "レビュー用",
    vision_enabled: true,
    connection_id: "primary",
    // 追加したモデルの推論の量は既定の low（#1425・#3004）。
    reasoning_effort: "low",
    // 追加したモデルの入力の上限は既定の 128000（#1437）。
    max_input_tokens: 128000,
    // 追加したモデルの出力の上限は既定の 32768（#1719）。
    max_output_tokens: 32768,
  });
  const remove = page.getByRole("button", { name: "モデルを削除 3", exact: true });
  await expect(remove).toBeEnabled();
  await remove.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "モデル ID 3", exact: true })).toHaveValue("review-model");
  await remove.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "モデル ID 3", exact: true })).toHaveCount(0);
  // 既定に選んでいたモデルを削除すると、保存前に両方の欄でエラーを出し、保存は送らない。
  // フォーカスは画面の並びで最初の欄（テキスト。#566）。
  await expect(visionDefault).toHaveAttribute("aria-invalid", "true");
  await expect(textDefault).toHaveAttribute("aria-invalid", "true");
  await page.getByRole("button", { name: "登録モデル: 保存", exact: true }).click();
  await expect(textDefault).toBeFocused();
  expect(writes).toHaveLength(1);
  await visionDefault.click();
  await page.getByRole("listbox", { name: "既定の画像対応モデル" }).getByRole("option", { name: /OCR \/ Vision/ }).click();
  await textDefault.click();
  // テキストに未選択へ戻す選択肢（「既定の画像対応モデルを使う」）はない（#566）。
  const textListbox = page.getByRole("listbox", { name: "既定のテキストモデル" });
  await expect(textListbox.getByRole("option", { name: "既定の画像対応モデルを使う" })).toHaveCount(0);
  await textListbox.getByRole("option", { name: /業務 NL2SQL 標準/ }).click();
  await expect(visionDefault).toHaveAttribute("aria-invalid", "false");
  await expect(textDefault).toHaveAttribute("aria-invalid", "false");
  await page.getByRole("button", { name: "登録モデル: 保存", exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].enterprise_ai.models).toHaveLength(2);
  expect(writes[1].enterprise_ai.default_vision_model_id).toBe("enterprise-nl2sql-vlm");
  expect(writes[1].enterprise_ai.default_text_model_id).toBe("enterprise-nl2sql-llm");
});

test("レビュー補完: 保存先の失敗再試行を確認する", async ({ page }) => {
  let failSave = true;
  let persisted = uploadStorageFixture();
  await page.route("**/api/settings/upload-storage", async (route) => {
    if (route.request().method() === "PATCH") {
      if (failSave) {
        await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "保存先の書込失敗" }) });
        return;
      }
      persisted = { ...persisted, ...route.request().postDataJSON() };
    }
    await fulfillJson(route, persisted);
  });
  await page.goto("/settings/upload-storage");
  const path = page.locator("#upload-storage-local-dir");
  await path.fill("/tmp/review-retry");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("保存先の書込失敗", { exact: true })).toBeVisible();
  await expect(path).toHaveValue("/tmp/review-retry");
  failSave = false;
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("保存先の書込失敗", { exact: true })).toHaveCount(0);
  await expect.poll(() => persisted.local_storage_dir).toBe("/tmp/review-retry");
  await expect(page.getByRole("button", { name: "OCI 認証設定を開く" })).toHaveCount(0);
});

test("未保存の変更があるときはブラウザの戻る操作でも破棄を確認する（#138）", async ({ page }) => {
  await page.goto("/settings/model");
  await expect(page.locator("#enterprise-endpoint")).toBeVisible();
  // 変更がなければ確認なしで移動できる（SPA 内の PUSH で履歴を 1 つ積む）。
  await (await openSidebarNav(page)).getByRole("link", { name: "アップロード保存先" }).click();
  await expect(page).toHaveURL(/settings\/upload-storage$/);
  await expect(page.getByRole("alertdialog")).toHaveCount(0);

  const localDir = page.locator("#upload-storage-local-dir");
  await expect(localDir).toBeEditable();
  await localDir.fill("/tmp/back-guard");

  // 戻る → 確認。キャンセルすると URL と入力が残る。
  await page.goBack();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page).toHaveURL(/settings\/upload-storage$/);
  await expect(localDir).toHaveValue("/tmp/back-guard");

  // もう一度戻る → 破棄して移動。
  await page.goBack();
  await dialog.getByRole("button", { name: "破棄して移動", exact: true }).click();
  await expect(page).toHaveURL(/settings\/model$/);
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});

test("レビュー補完: システム設定5画面の表示とキーボード・横幅を記録する", async ({ page }, testInfo) => {
  for (const slug of ["upload-storage", "model", "database", "system-tables", "appearance"]) {
    await page.goto(`/settings/${slug}`);
    await expect(page.locator("main")).toBeVisible();
    const ready = slug === "upload-storage" ? page.locator("#upload-storage-local-dir")
      : slug === "model" ? page.locator("#enterprise-endpoint")
      : slug === "database" ? page.locator("#oracle-user")
      : slug === "system-tables" ? page.locator("#system-tables").getByRole("button", { name: "最新情報を取得" })
      : page.getByRole("button", { name: "ライト", exact: true });
    await expect(ready).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await ready.focus();
    await expect(ready).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`settings-${slug}.png`), fullPage: true });
  }
});

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

// HTTPS の証明書（#1316）: 1 台の Compute の Nginx が /platform/ca.crt で CA の証明書を配る。製品は /<製品>/ の下でも
// サイトの root の /platform/ca.crt を見る。ローカルの開発（Vite）は配っていないので説明だけを出す。
for (const theme of ["ライト", "ダーク"] as const) {
  test(`外観と証明書の HTTPS の証明書（${theme}）`, async ({ page }) => {
    await page.goto("/settings/appearance");
    await page.getByTestId("appearance-theme-toggle").getByRole("button", { name: theme }).click();
    await expect(page.locator("html")).toHaveClass(theme === "ダーク" ? /dark/ : /^(?!.*\bdark\b).*$/);

    const card = page.getByTestId("appearance-ca-certificate");
    await expect(card.getByText("HTTPS の証明書", { exact: true })).toBeVisible();
    await expect(card.getByText("この環境では HTTPS の自己署名の証明書を使っていません。")).toBeVisible();
    await expect(card.getByRole("link", { name: "CA の証明書をダウンロード" })).toHaveCount(0);

    await page.route("**/platform/ca.crt", (route) =>
      route.fulfill({ status: 200, headers: { "Content-Type": "application/x-x509-ca-cert" }, body: "" }),
    );
    await page.reload();
    await expect(card.getByRole("link", { name: "CA の証明書をダウンロード" })).toHaveAttribute("href", "/platform/ca.crt");
    await card.getByText("端末への取り込み方").click();
    // OS の見出しは使うブラウザを添え、ブラウザの項は Edge → Chrome → Firefox の順（#1662）。
    await expect(card.getByTestId("appearance-ca-certificate-steps").locator("dt")).toHaveText([
      "Windows（Edge・Chrome）",
      "macOS（Safari・Chrome・Edge）",
      "iPhone / iPad（すべてのブラウザ）",
      "Android（Chrome）",
      "Edge",
      "Chrome",
      "Firefox",
    ]);
    await expect(card.getByText("Firefox", { exact: true })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
