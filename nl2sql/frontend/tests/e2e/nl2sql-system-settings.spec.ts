import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { expectedControlHeight } from "./_helpers/control-height";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { dropFiles } from "./_helpers/file-dropzone";
import { expectLargeActionButton } from "./_helpers/action-button";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

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
    adb_ocid: "ocid1.autonomousdatabase.oc1.ap-osaka-1.example",
    region: "ap-osaka-1",
    config_source: "runtime",
    ...overrides,
  };
}

function adbInfoFixture(overrides: Record<string, unknown> = {}) {
  return {
    status: "success",
    message: "ADB OCID が設定されています。",
    error_code: null,
    id: "ocid1.autonomousdatabase.oc1.ap-osaka-1.example",
    display_name: "nl2sqldb",
    lifecycle_state: "AVAILABLE",
    db_name: "NL2SQLDB",
    cpu_core_count: 2,
    data_storage_size_in_tbs: 1,
    region: "ap-osaka-1",
    ...overrides,
  };
}

const WALLET_PENDING_MESSAGE = "OCI から Wallet を取得し、サーバーへ安全に設定しています…";

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
        embedding_model: "cohere.embed-v4.0",
        embedding_dim: 1536,
        rerank_model: "cohere.rerank-v4.0-fast",
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
    backend: "local",
    local_storage_dir: "/u01/data/production-ready-nl2sql",
    object_storage_region: "ap-osaka-1",
    object_storage_namespace: "exampletenancy",
    object_storage_bucket: "nl2sql-originals",
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

function ociSettingsFixture() {
  return {
    config_file: "~/.oci/config",
    profile: "DEFAULT",
    user: "ocid1.user.oc1..example",
    fingerprint: "12:34:56:78:90:ab:cd:ef",
    tenancy: "ocid1.tenancy.oc1..example",
    region: "ap-osaka-1",
    key_file: "~/.oci/oci_api_key.pem",
    key_file_exists: true,
    config_file_exists: true,
    config_source: "runtime",
  };
}

type OciStageStatus = "success" | "failed" | "skipped";
const OCI_STAGE_KEYS = ["config_format", "key_file", "region", "authentication"] as const;

function ociStages(statuses: readonly OciStageStatus[], failure?: { message: string; action: string }) {
  return OCI_STAGE_KEYS.map((key, index) => {
    const status = statuses[index] ?? "skipped";
    return {
      key,
      status,
      message:
        status === "failed" && failure
          ? failure.message
          : status === "skipped"
            ? "前の段階が失敗したため実施していません。"
            : `${key} を確認しました。`,
      action: status === "failed" && failure ? failure.action : null,
    };
  });
}

function ociConfigTestFixture(overrides: Record<string, unknown> = {}) {
  return {
    status: "success",
    profile: "DEFAULT",
    config_file: "~/.oci/config",
    key_file: "~/.oci/oci_api_key.pem",
    config_file_exists: true,
    key_file_exists: true,
    missing_fields: [],
    permission_issues: [],
    oci_directory_mode: "0700",
    config_file_mode: "0600",
    key_file_mode: "0600",
    message: "OCI へ認証付きで接続できました（Object Storage GetNamespace）。",
    elapsed_ms: 7,
    checked_at: "2026-06-21T10:00:00.000Z",
    error_type: null,
    stages: ociStages(["success", "success", "success", "success"]),
    region: "ap-osaka-1",
    auth_check_operation: "Object Storage GetNamespace",
    http_status: null,
    service_code: null,
    request_id: null,
    ...overrides,
  };
}

async function mockNl2sqlSettingsApi(page: Page) {
  const ociSettings = ociSettingsFixture();

  const uploadStorage = uploadStorageFixture();

  const modelSettings = modelSettingsFixture();

  const databaseSettings = databaseSettingsFixture();
  let selectAiCredential: {
    credential_name: "NL2SQL_OCI_CRED";
    schema_name: string;
    exists: boolean;
    region: "ap-osaka-1" | "us-chicago-1";
    oci_auth_ready: boolean;
    missing_fields: string[];
    operation: "created" | "recreated" | null;
  } = {
    credential_name: "NL2SQL_OCI_CRED" as const,
    schema_name: "ADMIN",
    exists: false,
    region: "us-chicago-1",
    oci_auth_ready: true,
    missing_fields: [] as string[],
    operation: null as "created" | "recreated" | null,
  };

  const adbInfo = adbInfoFixture();

  await page.route("**/api/settings/oci", (route) => fulfillJson(route, ociSettings));
  await page.route("**/api/settings/oci/config/test", (route) =>
    fulfillJson(route, ociConfigTestFixture())
  );
  await page.route("**/api/settings/oci/object-storage", (route) =>
    fulfillJson(route, uploadStorage)
  );
  await page.route("**/api/settings/oci/object-storage/namespace", (route) =>
    fulfillJson(route, { namespace: "exampletenancy" })
  );
  await page.route("**/api/settings/oci/config/read", (route) =>
    fulfillJson(route, {
      profile: "DEFAULT",
      user: ociSettings.user,
      fingerprint: ociSettings.fingerprint,
      tenancy: ociSettings.tenancy,
      region: ociSettings.region,
      key_file: ociSettings.key_file,
      applied_fields: ["user", "fingerprint", "tenancy", "region", "key_file"],
    })
  );
  await page.route("**/api/settings/oci/key-file", (route) =>
    fulfillJson(route, { key_file: "~/.oci/oci_api_key.pem", saved: true })
  );

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
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    if (route.request().method() === "POST") {
      const request = route.request().postDataJSON() as {
        region: "ap-osaka-1" | "us-chicago-1";
        recreate: boolean;
      };
      selectAiCredential = {
        ...selectAiCredential,
        exists: true,
        region: request.region,
        operation: request.recreate ? "recreated" : "created",
      };
    }
    await fulfillJson(route, selectAiCredential);
  });
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
  await page.route("**/api/settings/database/wallet/download", (route) =>
    fulfillJson(route, { status: "already_configured", settings: databaseSettings })
  );
  await page.route("**/api/settings/database/adb", (route) => fulfillJson(route, adbInfo));
  await page.route("**/api/settings/database/adb/settings", (route) =>
    fulfillJson(route, adbInfo)
  );
  await page.route("**/api/settings/database/adb/start", (route) =>
    fulfillJson(route, adbInfo)
  );
  await page.route("**/api/settings/database/adb/stop", (route) =>
    fulfillJson(route, adbInfo)
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

async function expectNoElementOverlap(items: Array<{ label: string; locator: Locator }>) {
  const boxes = await Promise.all(
    items.map(async (item) => ({ ...item, box: await item.locator.boundingBox() }))
  );

  for (const item of boxes) {
    if (!item.box) {
      throw new Error(`${item.label} の位置を取得できません。`);
    }
  }

  for (let index = 0; index < boxes.length; index += 1) {
    for (let compareIndex = index + 1; compareIndex < boxes.length; compareIndex += 1) {
      const current = boxes[index];
      const next = boxes[compareIndex];
      const currentBox = current.box;
      const nextBox = next.box;
      if (!currentBox || !nextBox) continue;

      const horizontalOverlap =
        Math.min(currentBox.x + currentBox.width, nextBox.x + nextBox.width) -
        Math.max(currentBox.x, nextBox.x);
      const verticalOverlap =
        Math.min(currentBox.y + currentBox.height, nextBox.y + nextBox.height) -
        Math.max(currentBox.y, nextBox.y);

      expect(
        horizontalOverlap > 1 && verticalOverlap > 1,
        `${current.label} と ${next.label} が重なっています。`
      ).toBeFalsy();
    }
  }
}

async function expectAdbActionButtonsStableDuringOperation(
  page: Page,
  operation: "start" | "stop"
) {
  const adbCard = page.locator("#adb-management");
  const saveButton = adbCard.getByRole("button", { name: "保存", exact: true });
  // 共有 Button の loading はラベルを差し替えない（DS ルール: 「起動中…」「停止中…」にしない）。
  // 操作中のボタンは同じラベルのまま先頭アイコンがスピナーになり aria-busy + disabled になる。
  const startButton = adbCard.getByRole("button", { name: "起動", exact: true });
  const stopButton = adbCard.getByRole("button", { name: "停止", exact: true });
  const busyButton = operation === "start" ? startButton : stopButton;

  await expect(busyButton).toHaveAttribute("aria-busy", "true");
  await expect(busyButton).toBeDisabled();
  await expect(busyButton.locator("svg.animate-spin")).toBeVisible();
  await expect(saveButton).toBeDisabled();
  await expect(adbCard.getByRole("button", { name: "保存中…", exact: true })).toHaveCount(0);
  await expectNoAdbWalletPendingStatus(page);
  await expectNoElementOverlap([
    { label: "保存ボタン", locator: saveButton },
    { label: "起動ボタン", locator: startButton },
    { label: "停止ボタン", locator: stopButton },
  ]);
  await expectNoHorizontalOverflow(page);
}

async function expectNoAdbWalletPendingStatus(page: Page) {
  await expect(
    page.locator("#adb-management").getByRole("status").filter({
      hasText: WALLET_PENDING_MESSAGE,
    })
  ).toHaveCount(0);
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
    page.getByRole("button", { name: "OCI Generative AI: 保存" }),
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

async function expectOciConfigFieldsAboveOcidFields(page: Page) {
  const configFile = page.getByLabel("OCI 設定ファイルのパス");
  const configProfile = page.getByLabel("OCI プロファイル");
  const userOcid = page.getByLabel("ユーザー OCID");
  const tenancyOcid = page.getByLabel("テナンシ OCID");

  await expect(configFile).toBeVisible();
  await expect(configProfile).toBeVisible();
  await expect(userOcid).toBeVisible();
  await expect(tenancyOcid).toBeVisible();

  const boxes = await Promise.all([
    configFile.boundingBox(),
    configProfile.boundingBox(),
    userOcid.boundingBox(),
    tenancyOcid.boundingBox(),
  ]);

  if (boxes.some((box) => box === null)) {
    throw new Error("OCI 認証フォームの入力欄位置を取得できません。");
  }

  const [configFileBox, configProfileBox, userOcidBox, tenancyOcidBox] = boxes as [
    NonNullable<(typeof boxes)[number]>,
    NonNullable<(typeof boxes)[number]>,
    NonNullable<(typeof boxes)[number]>,
    NonNullable<(typeof boxes)[number]>,
  ];
  const configFieldsBottomRow = Math.max(configFileBox.y, configProfileBox.y);
  const ocidFieldsTopRow = Math.min(userOcidBox.y, tenancyOcidBox.y);

  expect(configFieldsBottomRow).toBeLessThan(ocidFieldsTopRow);
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

async function expectAdbManagementAboveDatabaseSettings(page: Page) {
  const adbHeading = page
    .locator("#adb-management")
    .getByRole("heading", { name: "Autonomous Database 管理" });
  const databaseHeading = page
    .locator("form")
    .getByRole("heading", { name: "データベース設定" });

  await expect(adbHeading).toBeVisible();
  await expect(databaseHeading).toBeVisible();

  const [adbBox, databaseBox] = await Promise.all([
    adbHeading.boundingBox(),
    databaseHeading.boundingBox(),
  ]);

  if (!adbBox || !databaseBox) {
    throw new Error("ADB 管理カード / データベース設定カードの位置を取得できません。");
  }

  expect(adbBox.y).toBeLessThan(databaseBox.y);
}

test.beforeEach(async ({ page }) => {
  await mockNl2sqlSettingsApi(page);
  await mockDatabaseGateReady(page);
});

test("OCI 認証設定はブラウザ草稿のダミー値を runtime 空値で上書きする", async ({ page }) => {
  await page.addInitScript(() => {
    const staleDraft = JSON.stringify({
      userOcid: "ocid1.user.oc1..aaaaaaaa",
      fingerprint: "12:34:56:78:90:ab:cd:ef",
      tenancyOcid: "ocid1.tenancy.oc1..aaaaaaaa",
      region: "us-chicago-1",
      objectStorageRegion: "ap-osaka-1",
      objectStorageNamespace: "fake-namespace",
    });
    window.localStorage.setItem("production-ready-rag.oci-settings.v1", staleDraft);
    window.localStorage.setItem("production-ready-nl2sql.oci-settings.v1", staleDraft);
  });
  await page.route("**/api/settings/oci", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      config_file: "~/.oci/config",
      profile: "DEFAULT",
      user: "",
      fingerprint: "",
      tenancy: "",
      region: "",
      key_file: "~/.oci/oci_api_key.pem",
      key_file_exists: false,
      config_file_exists: false,
      config_source: "runtime",
    });
  });
  await page.route("**/api/settings/upload-storage", async (route) => {
    await fulfillJson(route, {
      backend: "local",
      local_storage_dir: "/u01/data/production-ready-nl2sql",
      object_storage_region: "",
      object_storage_namespace: "",
      object_storage_bucket: "nl2sql-originals",
      readiness: "ok",
      max_upload_bytes: 104857600,
      config_source: "runtime",
    });
  });

  await page.goto("/settings/oci");

  await expect(page.getByLabel("ユーザー OCID")).toHaveValue("");
  await expect(page.getByLabel("フィンガープリント")).toHaveValue("");
  await expect(page.getByLabel("テナンシ OCID")).toHaveValue("");
  await expect(
    page.getByRole("textbox", { name: /Object Storage ネームスペース/ })
  ).toHaveValue("");
  await expect(page.getByRole("heading", { name: "設定チェック" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
});

test("NL2SQL のシステム設定画面を表示できる", { tag: "@responsive" }, async ({ page }) => {
  await page.goto("/settings/oci");
  await expect(page.getByRole("heading", { name: "OCI 認証設定" }).first()).toBeVisible();
  await expect(page.getByLabel("ユーザー OCID")).toBeVisible();
  await expect(page.getByRole("button", { name: "OCI 設定を保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "接続テスト" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "設定チェック" })).toHaveCount(0);
  await expect(page.getByLabel(".env プレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(0);
  await expectNoOperationsMemoOrReadiness(page);
  await expectOciConfigFieldsAboveOcidFields(page);
  await expectNl2sqlShellFillsViewport(page);
  await expectNoExcessBottomWhitespace(page);
  await expectNoHorizontalOverflow(page);

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
  await page.getByRole("radio", { name: /OCI Object Storage/ }).check();
  await expect(page.getByLabel("Object Storage リージョン")).toContainText("ap-osaka-1");
  await expect(page.getByLabel("Object Storage ネームスペース")).toHaveValue("exampletenancy");
  await expect(page.getByLabel("Object Storage バケット")).toHaveValue("nl2sql-originals");
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
    page.getByRole("heading", { name: "OCI Generative AI", exact: true })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "OCI Enterprise AI: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "登録モデル: 保存" })).toBeVisible();
  await expect(page.getByRole("button", { name: "OCI Generative AI: 保存" })).toBeVisible();
  await expectModelSaveButtonsUsePrimaryStyle(page);
  await expect(page.locator("#enterprise-api-path")).toHaveCount(0);
  await expect(page.locator("#enterprise-vlm-input-mode")).toHaveCount(0);
  await expect(page.locator("#enterprise-timeout")).toHaveCount(0);
  await expect(page.locator("#enterprise-retries")).toHaveCount(0);
  await expectNoOperationsMemoOrReadiness(page);
  await expectModelPreviewPanelsAbsent(page);
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
  await expectAdbManagementAboveDatabaseSettings(page);
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
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("操作履歴")).toBeVisible();
  await expect(page.getByText("ADB OCID が設定されています。")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("OCI 認証設定は保存前に必須値と形式を検証する", async ({ page }) => {
  let authPatchRequests = 0;
  let storagePatchRequests = 0;

  await page.unroute("**/api/settings/oci");
  await page.route("**/api/settings/oci", async (route) => {
    if (route.request().method() === "PATCH") {
      authPatchRequests += 1;
    }
    await fulfillJson(route, {
      config_file: "~/.oci/config",
      profile: "DEFAULT",
      user: "",
      fingerprint: "",
      tenancy: "",
      region: "",
      key_file: "~/.oci/oci_api_key.pem",
      key_file_exists: false,
      config_file_exists: false,
      config_source: "runtime",
    });
  });
  await page.unroute("**/api/settings/upload-storage");
  await page.route("**/api/settings/upload-storage", (route) =>
    fulfillJson(
      route,
      uploadStorageFixture({
        object_storage_region: "",
        object_storage_namespace: "",
      })
    )
  );
  await page.unroute("**/api/settings/oci/object-storage");
  await page.route("**/api/settings/oci/object-storage", async (route) => {
    if (route.request().method() === "PATCH") {
      storagePatchRequests += 1;
    }
    await fulfillJson(
      route,
      uploadStorageFixture({
        object_storage_region: "",
        object_storage_namespace: "",
      })
    );
  });

  await page.goto("/settings/oci");
  await page.getByRole("button", { name: "OCI 認証設定: OCI 設定を保存" }).click();

  await expect(page.getByLabel("ユーザー OCID")).toHaveAttribute("aria-invalid", "true");
  // 未入力の文言は欄のラベルを主語にする（「〇〇を入力してください。」。#541）。
  await expect(page.getByText("ユーザー OCID を入力してください。")).toBeVisible();
  expect(authPatchRequests).toBe(0);

  await page.getByLabel("ユーザー OCID").fill("not-a-user-ocid");
  await page.getByLabel("フィンガープリント").fill("not-a-fingerprint");
  await page.getByLabel("テナンシ OCID").fill("not-a-tenancy-ocid");
  await page.getByRole("button", { name: "OCI 認証設定: OCI 設定を保存" }).click();

  await expect(page.getByText(/ユーザー OCID は ocid1\.user\./)).toBeVisible();
  await expect(page.getByText(/fingerprint は 16 進数/)).toBeVisible();
  await expect(page.getByText(/テナンシ OCID は ocid1\.tenancy\./)).toBeVisible();
  expect(authPatchRequests).toBe(0);

  await page.getByRole("button", { name: "Object Storage: 保存" }).click();

  await expect(page.getByLabel("Object Storage リージョン")).toHaveAttribute(
    "aria-invalid",
    "true"
  );
  await expect(
    page.getByRole("textbox", { name: /Object Storage ネームスペース/ })
  ).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("Object Storage リージョンを選択してください。")).toBeVisible();
  await expect(page.getByText("Object Storage ネームスペースを入力してください。")).toBeVisible();
  expect(storagePatchRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("アップロード保存先は OCI の region と namespace 不足を保存前に止める", async ({
  page,
}) => {
  let uploadStoragePatchRequests = 0;

  await page.unroute("**/api/settings/upload-storage");
  await page.route("**/api/settings/upload-storage", async (route) => {
    if (route.request().method() === "PATCH") {
      uploadStoragePatchRequests += 1;
    }
    await fulfillJson(
      route,
      uploadStorageFixture({
        object_storage_region: "",
        object_storage_namespace: "",
      })
    );
  });

  await page.goto("/settings/upload-storage");
  await page.getByRole("radio", { name: /OCI Object Storage/ }).check();

  await expect(page.getByLabel("Object Storage リージョン")).toBeVisible();
  await expect(page.getByLabel("Object Storage ネームスペース")).toHaveValue("");
  await expect(
    page.getByText(
      "OCI Object Storage を使うには、リージョンとネームスペースの設定が必要です。"
    )
  ).toBeVisible();
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(
    page.getByText("OCI 認証設定で Object Storage リージョンを選択してください。")
  ).toBeVisible();
  await expect(
    page.getByText("OCI 認証設定で Object Storage ネームスペースを設定してください。")
  ).toBeVisible();
  expect(uploadStoragePatchRequests).toBe(0);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByLabel("Object Storage リージョン")).toBeVisible();
  await expect(page.getByLabel("Object Storage ネームスペース")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("システム設定のテスト成功結果を共通パネルで表示する", async ({ page }, testInfo) => {
  await page.goto("/settings/oci");
  const ociTestButton = page.getByRole("button", { name: /接続テスト/ });
  await ociTestButton.focus();
  await expect(ociTestButton).toBeFocused();
  await ociTestButton.press("Enter");

  const ociResult = page.getByTestId("settings-oci-test-result");
  await expect(ociResult.getByRole("status")).toBeVisible();
  await expect(ociResult).toHaveAttribute("data-tone", "success");
  await expect(ociResult).toContainText(
    "OCI へ認証付きで接続できました（Object Storage GetNamespace）。"
  );
  await expect(ociResult).toContainText("所要時間: 7 ms");
  const ociStagesList = ociResult.getByRole("list", { name: "確認段階" });
  await expect(ociStagesList.getByRole("listitem")).toHaveCount(4);
  await expect(ociStagesList.locator('[data-stage-status="success"]')).toHaveCount(4);
  for (const label of ["設定の形式", "鍵の読み取り", "リージョン到達", "認証（API 応答）"]) {
    await expect(ociStagesList).toContainText(label);
  }
  await expect(ociResult).toContainText("Object Storage GetNamespace");
  await expect(ociResult).toContainText("config_file_mode");
  await expectNoHorizontalOverflow(page);
  await page.locator("#oci-user-ocid").fill("ocid1.user.oc1..changed");
  await expect(ociResult).toHaveCount(0);

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
    .getByRole("button", { name: "cohere.embed-v4.0 をテスト", exact: true })
    .click();
  const embeddingResult = page
    .getByText("cohere.embed-v4.0 の設定を確認しました。", { exact: true })
    .locator("xpath=ancestor::*[@data-settings-test-result][1]");
  await expect(embeddingResult).toContainText("vector_dim");
  await expect(embeddingResult).toContainText("1536");

  await page
    .getByRole("button", { name: "cohere.rerank-v4.0-fast をテスト", exact: true })
    .click();
  const rerankResult = page
    .getByText("cohere.rerank-v4.0-fast の設定を確認しました。", { exact: true })
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
  await page.unroute("**/api/settings/oci/config/test");
  await page.route("**/api/settings/oci/config/test", (route) =>
    fulfillJson(
      route,
      ociConfigTestFixture({
        status: "failed",
        key_file_exists: false,
        missing_fields: ["fingerprint"],
        permission_issues: ["OCI config ファイルは 0600 にしてください。"],
        config_file_mode: "0644",
        key_file_mode: null,
        message: "OCI config の必須項目が不足しています。",
        elapsed_ms: 4,
        error_type: "InvalidOciConfig",
        stages: ociStages(["failed"], {
          message: "OCI config の必須項目が不足しています。",
          action: "不足している項目を入力して認証設定を保存してください。",
        }),
        auth_check_operation: null,
      })
    )
  );
  await page.goto("/settings/oci");
  await page.getByRole("button", { name: /接続テスト/ }).click();
  const ociResult = page.getByTestId("settings-oci-test-result");
  await expect(ociResult.getByRole("alert")).toBeVisible();
  await expect(ociResult).toHaveAttribute("data-tone", "danger");
  await expect(ociResult).toContainText("確認ポイント");
  await expect(ociResult).toContainText("不足項目: fingerprint");
  await expectNoHorizontalOverflow(page);

  await page.unroute("**/api/settings/model/test");
  await page.route("**/api/settings/model/test", (route) =>
    fulfillJson(route, {
      status: "failed",
      target_type: "embedding",
      model_id: "cohere.embed-v4.0",
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
    .getByRole("button", { name: "cohere.embed-v4.0 をテスト", exact: true })
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

const OCI_CONNECTIVITY_FAILURES = [
  {
    name: "形式不正",
    statuses: ["failed", "skipped", "skipped", "skipped"],
    message: "OCI config の形式が正しくありません（fingerprint）。",
    action: "fingerprint は OCI コンソールの API キーに表示される 16 バイトのコロン区切りを設定してください。",
    extra: { error_type: null, auth_check_operation: null },
    detail: null,
  },
  {
    name: "認証失敗",
    statuses: ["success", "success", "success", "failed"],
    message: "OCI が認証を拒否しました（401 NotAuthenticated）。",
    action: "fingerprint が OCI コンソールの API キーと一致しているかを確認してください。",
    extra: {
      error_type: "ServiceError",
      http_status: 401,
      service_code: "NotAuthenticated",
      request_id: "E2EREQUEST401",
    },
    detail: "E2EREQUEST401",
  },
  {
    name: "権限不足",
    statuses: ["success", "success", "success", "failed"],
    message: "OCI がアクセスを許可しませんでした（404 NotAuthorizedOrNotFound）。",
    action: "IAM ポリシーでこのユーザーのグループに必要な権限が付与されているかを確認してください。",
    extra: {
      error_type: "ServiceError",
      http_status: 404,
      service_code: "NotAuthorizedOrNotFound",
      request_id: "E2EREQUEST404",
    },
    detail: "NotAuthorizedOrNotFound",
  },
  {
    name: "タイムアウト",
    statuses: ["success", "success", "failed", "skipped"],
    message: "ap-osaka-1 の OCI endpoint への接続が 5 秒以内に完了しませんでした。",
    action: "リージョン名（ap-osaka-1）と、バックエンドから OCI への HTTPS 通信を確認してください。",
    extra: { error_type: "ConnectTimeout" },
    detail: null,
  },
] as const;

for (const failure of OCI_CONNECTIVITY_FAILURES) {
  test(`OCI 接続テストの${failure.name}を段階・次の対処つきで表示する`, async ({ page }) => {
    const gate = createRequestGate();
    let testRequests = 0;
    await page.unroute("**/api/settings/oci/config/test");
    await page.route("**/api/settings/oci/config/test", async (route) => {
      testRequests += 1;
      expect(route.request().method()).toBe("POST");
      expect(route.request().postData() ?? "").toBe("");
      await gate.promise;
      await fulfillJson(
        route,
        ociConfigTestFixture({
          status: "failed",
          message: failure.message,
          stages: ociStages(failure.statuses, { message: failure.message, action: failure.action }),
          ...failure.extra,
        })
      );
    });

    await page.goto("/settings/oci");
    const testButton = page.getByRole("button", { name: /接続テスト/ });
    await testButton.click();
    // loading 中もラベルは「接続テスト」のまま（先頭アイコンだけがスピナーになる）。
    await expect(testButton).toHaveAccessibleName(/接続テスト$/);
    await expect(testButton).not.toContainText("テスト中");
    gate.release();

    const ociResult = page.getByTestId("settings-oci-test-result");
    await expect(ociResult.getByRole("alert")).toBeVisible();
    await expect(ociResult).toHaveAttribute("data-tone", "danger");
    await expect(ociResult.getByRole("alert")).toContainText(failure.message);
    const stagesList = ociResult.getByRole("list", { name: "確認段階" });
    for (const [index, key] of OCI_STAGE_KEYS.entries()) {
      await expect(stagesList.locator(`[data-stage="${key}"]`)).toHaveAttribute(
        "data-stage-status",
        failure.statuses[index]
      );
    }
    const failedStage = stagesList.locator('[data-stage-status="failed"]');
    await expect(failedStage).toContainText("失敗");
    if ((failure.statuses as readonly OciStageStatus[]).includes("skipped")) {
      await expect(stagesList.locator('[data-stage-status="skipped"]').first()).toContainText(
        "未実施"
      );
    }
    await expect(ociResult).toContainText("確認ポイント");
    await expect(ociResult).toContainText(failure.action);
    if (failure.detail) await expect(ociResult).toContainText(failure.detail);
    expect(testRequests).toBe(1);
    await expectNoHorizontalOverflow(page);
  });
}

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
  await expectAdbManagementAboveDatabaseSettings(page);

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
  await expectAdbManagementAboveDatabaseSettings(page);
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
  await expectAdbManagementAboveDatabaseSettings(page);
  await expectWalletAboveServiceDsn(page);
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectDatabaseSupplementalPanelsAbsent(page);
  await expectNoHorizontalOverflow(page);
});

test("OCI 秘密鍵と Wallet ZIP をドラッグ＆ドロップで即時アップロードできる", { tag: "@responsive" }, async ({
  page,
}) => {
  let keyUploadCount = 0;
  let walletUploadCount = 0;
  let keyUploadBody = "";
  let walletUploadBody = "";
  const keyUploadGate = createRequestGate();

  await page.unroute("**/api/settings/oci/key-file");
  await page.route("**/api/settings/oci/key-file", async (route) => {
    keyUploadCount += 1;
    keyUploadBody = route.request().postDataBuffer()?.toString("utf8") ?? "";
    await keyUploadGate.promise;
    await fulfillJson(route, { key_file: "~/.oci/oci_api_key.pem", saved: true });
  });
  await page.unroute("**/api/settings/database/wallet");
  await page.route("**/api/settings/database/wallet", async (route) => {
    walletUploadCount += 1;
    walletUploadBody = route.request().postDataBuffer()?.toString("utf8") ?? "";
    await fulfillJson(route, databaseSettingsFixture());
  });

  await page.goto("/settings/oci");
  const keyDropzone = page.getByTestId("oci-key-file-upload-dropzone");
  // ファイルの選択は入力欄と同じ md（タッチ端末は 44px。#613）。
  await expect(keyDropzone).toHaveCSS("height", `${await expectedControlHeight(page)}px`);
  await dropFiles(page, keyDropzone, [
    {
      name: "private.pem",
      type: "application/x-pem-file",
      content: "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",
    },
  ]);
  try {
    await expect(keyDropzone).toHaveAttribute("aria-busy", "true");
    await expect(page.getByTestId("oci-key-file-upload-input")).toBeDisabled();
  } finally {
    keyUploadGate.release();
  }
  await expect(page.getByText("秘密鍵ファイルをアップロードしました。")).toBeVisible();
  await expect.poll(() => keyUploadCount).toBe(1);
  expect(keyUploadBody).toContain('filename="private.pem"');

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
      backend: "local",
      local_storage_dir: "/u01/data/production-ready-nl2sql",
      object_storage_region: "ap-osaka-1",
      object_storage_namespace: "exampletenancy",
      object_storage_bucket: "nl2sql-originals",
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
  await expect(page.getByRole("button", { name: "OCI Generative AI: 保存" })).toBeDisabled();
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
  ).toBe("cohere.embed-v4.0");
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
    "cohere.embed-v4.0"
  );
  await expect(page.locator("#genai-embedding-model")).toHaveValue("cohere.unsaved-embed");

  const generativeSave = page.getByRole("button", { name: "OCI Generative AI: 保存" });
  await generativeSave.focus();
  await expect(generativeSave).toBeFocused();
  await generativeSave.press("Enter");
  await expect(page.getByText("OCI Generative AI 設定を保存しました。")).toBeVisible();
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
  await expect(page.getByRole("button", { name: "OCI Generative AI: 保存" })).toBeVisible();
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

test("Wallet 不足時はページ表示ごとに OCI 自動取得を一度だけ実行する", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const incompleteSettings = databaseSettingsFixture({
    wallet_uploaded: false,
    available_services: [],
    readiness: "wallet_not_found",
  });
  const downloadedSettings = databaseSettingsFixture();
  let downloadCount = 0;
  let notifyStarted: (() => void) | undefined;
  let releaseDownload: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    notifyStarted = resolve;
  });
  const downloadGate = new Promise<void>((resolve) => {
    releaseDownload = resolve;
  });

  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, incompleteSettings)
  );
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    downloadCount += 1;
    notifyStarted?.();
    await downloadGate;
    await fulfillJson(route, { status: "downloaded", settings: downloadedSettings });
  });

  await page.goto("/settings/database");
  await started;
  const pendingStatus = page
    .getByRole("status")
    .filter({ hasText: "OCI から Wallet を取得し、サーバーへ安全に設定しています…" });
  await expect(pendingStatus).toBeVisible();
  await expect(page.getByTestId("oracle-wallet-upload-input")).toBeDisabled();
  await expect(page.getByTestId("oracle-wallet-upload-dropzone")).toContainText(
    "ドラッグ＆ドロップまたは選択"
  );

  releaseDownload?.();

  const successToast = page.getByText(
    "Oracle Wallet を OCI から取得し、サーバーへ設定しました。"
  );
  await expect(successToast).toBeVisible();
  await expect(
    successToast.locator("xpath=ancestor::*[@aria-live='polite'][1]")
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.activeElement?.closest("[aria-live]") != null)
  ).toBe(false);
  await expect(page.getByText("設定済み", { exact: true })).toBeVisible();
  await expect.poll(() => downloadCount).toBe(1);
  await expectNoHorizontalOverflow(page);
});

test("Wallet 自動取得の失敗を保持し、キーボードで再取得できる", async ({ page }) => {
  const incompleteSettings = databaseSettingsFixture({
    wallet_uploaded: false,
    available_services: [],
    readiness: "wallet_not_found",
  });
  let downloadCount = 0;
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, incompleteSettings)
  );
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    downloadCount += 1;
    if (downloadCount === 1) {
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "OCI から Wallet を取得できませんでした。IAM 権限を確認して再試行するか、Wallet ZIP を手動アップロードしてください。",
        }),
      });
      return;
    }
    await fulfillJson(route, {
      status: "downloaded",
      settings: databaseSettingsFixture(),
    });
  });

  await page.goto("/settings/database");

  const retry = page.getByRole("button", { name: "OCI から Wallet を再取得" });
  await expect(retry).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: /IAM 権限を確認して再試行/ })
  ).toBeVisible();
  await expect(page.getByTestId("oracle-wallet-upload-dropzone")).toContainText(
    "ドラッグ＆ドロップまたは選択"
  );
  await retry.focus();
  await expect(retry).toBeFocused();
  await expect
    .poll(() => retry.evaluate((element) => getComputedStyle(element).outlineStyle))
    .not.toBe("none");
  await retry.press("Enter");

  await expect(
    page.getByText("Oracle Wallet を OCI から取得し、サーバーへ設定しました。")
  ).toBeVisible();
  await expect.poll(() => downloadCount).toBe(2);
  await expect(retry).toHaveCount(0);
});

test("有効な Wallet がある場合は OCI 自動取得を呼ばない", async ({ page }) => {
  let downloadCount = 0;
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    downloadCount += 1;
    await fulfillJson(route, {
      status: "already_configured",
      settings: databaseSettingsFixture(),
    });
  });

  await page.goto("/settings/database");

  await expect(page.getByText("設定済み", { exact: true })).toBeVisible();
  await expect.poll(() => downloadCount).toBe(0);
  await expect(
    page.getByText("OCI から Wallet を取得し、サーバーへ安全に設定しています…")
  ).toHaveCount(0);
});

test("ADB 起動中は保存ボタンを無効化して保存表示のままにする", { tag: "@responsive" }, async ({ page }) => {
  const settingsGate = createRequestGate();
  const startGate = createRequestGate();
  let settingsCount = 0;
  let startCount = 0;

  await page.unroute("**/api/settings/database/adb");
  await page.route("**/api/settings/database/adb", (route) =>
    fulfillJson(route, adbInfoFixture({ lifecycle_state: "STOPPED" }))
  );
  await page.unroute("**/api/settings/database/adb/settings");
  await page.route("**/api/settings/database/adb/settings", async (route) => {
    settingsCount += 1;
    await settingsGate.promise;
    await fulfillJson(route, adbInfoFixture({ lifecycle_state: "STOPPED" }));
  });
  await page.unroute("**/api/settings/database/adb/start");
  await page.route("**/api/settings/database/adb/start", async (route) => {
    startCount += 1;
    await startGate.promise;
    await fulfillJson(
      route,
      adbInfoFixture({
        lifecycle_state: "STARTING",
        message: "データベース 'NL2SQLDB' の起動を開始しました。",
      })
    );
  });

  await page.goto("/settings/database");
  const adbCard = page.locator("#adb-management");
  await adbCard.getByRole("button", { name: "起動", exact: true }).click();

  await expect.poll(() => settingsCount).toBe(1);
  await expectAdbActionButtonsStableDuringOperation(page, "start");

  settingsGate.release();
  await expect.poll(() => startCount).toBe(1);
  await expectAdbActionButtonsStableDuringOperation(page, "start");

  await page.setViewportSize({ width: 375, height: 812 });
  await expectAdbActionButtonsStableDuringOperation(page, "start");

  startGate.release();
  await expect(page.getByText("データベース 'NL2SQLDB' の起動を開始しました。")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("ADB 停止中は保存ボタンを無効化して保存表示のままにする", { tag: "@responsive" }, async ({ page }) => {
  const settingsGate = createRequestGate();
  const stopGate = createRequestGate();
  let settingsCount = 0;
  let stopCount = 0;

  await page.unroute("**/api/settings/database/adb/settings");
  await page.route("**/api/settings/database/adb/settings", async (route) => {
    settingsCount += 1;
    await settingsGate.promise;
    await fulfillJson(route, adbInfoFixture({ lifecycle_state: "AVAILABLE" }));
  });
  await page.unroute("**/api/settings/database/adb/stop");
  await page.route("**/api/settings/database/adb/stop", async (route) => {
    stopCount += 1;
    await stopGate.promise;
    await fulfillJson(
      route,
      adbInfoFixture({
        lifecycle_state: "STOPPING",
        message: "データベース 'NL2SQLDB' の停止を開始しました。",
      })
    );
  });

  await page.goto("/settings/database");
  const adbCard = page.locator("#adb-management");
  await adbCard.getByRole("button", { name: "停止", exact: true }).click();

  await expect.poll(() => settingsCount).toBe(1);
  await expectAdbActionButtonsStableDuringOperation(page, "stop");

  settingsGate.release();
  await expect.poll(() => stopCount).toBe(1);
  await expectAdbActionButtonsStableDuringOperation(page, "stop");

  await page.setViewportSize({ width: 375, height: 812 });
  await expectAdbActionButtonsStableDuringOperation(page, "stop");

  stopGate.release();
  await expect(page.getByText("データベース 'NL2SQLDB' の停止を開始しました。")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("ADB 情報の status=error は lifecycle がなくても表示する", async ({ page }) => {
  const safeMessage =
    "ADB 情報を取得できませんでした。OCI 認証、リージョン、ADB OCID を確認して再試行してください。";

  await page.unroute("**/api/settings/database/adb");
  await page.route("**/api/settings/database/adb", (route) =>
    fulfillJson(
      route,
      adbInfoFixture({
        status: "error",
        message: safeMessage,
        error_code: "ADB_INFO_UNAVAILABLE",
        display_name: null,
        lifecycle_state: null,
        db_name: null,
        cpu_core_count: null,
        data_storage_size_in_tbs: null,
      })
    )
  );

  await page.goto("/settings/database");

  const adbCard = page.locator("#adb-management");
  const alert = adbCard.getByRole("alert").filter({ hasText: safeMessage });
  await expect(alert).toBeVisible();
  await expect(adbCard.getByText("raw-sdk-detail")).toHaveCount(0);
  await expect(adbCard.getByText("OCI ADB")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(alert).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("ADB 情報の not_configured は lifecycle がなくても表示する", async ({ page }) => {
  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, databaseSettingsFixture({ adb_ocid: "" }))
  );
  await page.unroute("**/api/settings/database/adb");
  await page.route("**/api/settings/database/adb", (route) =>
    fulfillJson(
      route,
      adbInfoFixture({
        status: "not_configured",
        message: "ADB OCID が設定されていません。",
        error_code: "ADB_NOT_CONFIGURED",
        id: null,
        display_name: null,
        lifecycle_state: null,
        db_name: null,
        cpu_core_count: null,
        data_storage_size_in_tbs: null,
      })
    )
  );

  await page.goto("/settings/database");

  const adbCard = page.locator("#adb-management");
  await expect(adbCard.getByRole("status")).toContainText("ADB OCID が設定されていません。");
  await expectNoHorizontalOverflow(page);
});

test("ADB のリージョンは ap-tokyo-1 / ap-osaka-1 だけを候補に出す（#660）", async ({ page }) => {
  await page.goto("/settings/database");

  const adbCard = page.locator("#adb-management");
  const region = adbCard.getByRole("combobox", { name: "リージョン", exact: true });
  await expect(region).toContainText("ap-osaka-1");
  await region.click();
  const listbox = page.getByRole("listbox", { name: "リージョン", exact: true });
  await expect(listbox.getByRole("option")).toHaveText(["ap-tokyo-1", "ap-osaka-1"]);
  await page.keyboard.press("Escape");
  await expect(listbox).toBeHidden();
  await expect(adbCard.getByText("はサポートしていません")).toHaveCount(0);
});

test("ADB のリージョンの保存値が us-chicago-1 でも画面を出し、選び直しを案内する（#660）", async ({ page }) => {
  await page.unroute("**/api/settings/database");
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(route, databaseSettingsFixture({ region: "us-chicago-1" }))
  );

  await page.goto("/settings/database");

  const adbCard = page.locator("#adb-management");
  const region = adbCard.getByRole("combobox", { name: "リージョン", exact: true });
  const unsupported = adbCard.getByText(
    "保存済みのリージョン us-chicago-1 はサポートしていません。ap-tokyo-1 または ap-osaka-1 を選んで保存してください。"
  );
  await expect(region).toContainText("us-chicago-1");
  await expect(unsupported).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await region.click();
  const listbox = page.getByRole("listbox", { name: "リージョン", exact: true });
  await expect(listbox.getByRole("option")).toHaveText(["ap-tokyo-1", "ap-osaka-1"]);
  await listbox.getByRole("option", { name: "ap-tokyo-1", exact: true }).click();
  await expect(region).toContainText("ap-tokyo-1");
  await expect(unsupported).toHaveCount(0);
});

test("ADB 情報の query error をカード内に表示する", async ({ page }) => {
  const apiError =
    "ADB 情報を取得できませんでした。OCI 認証、リージョン、ADB OCID を確認して再試行してください。";

  await page.unroute("**/api/settings/database/adb");
  await page.route("**/api/settings/database/adb", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: [apiError],
        warning_messages: [],
        error_code: "ADB_INFO_UNAVAILABLE",
        request_id: "req-adb-info",
      }),
    })
  );

  await page.goto("/settings/database");

  const adbCard = page.locator("#adb-management");
  const alert = adbCard.getByRole("alert").filter({ hasText: apiError });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("req-adb-info");
  await expectNoHorizontalOverflow(page);
});

test("情報を再取得は ADB 情報更新後に Wallet 取得も実行し進行メッセージは出さない", async ({
  page,
}) => {
  const walletGate = createRequestGate();
  let adbRefreshCount = 0;
  let walletDownloadCount = 0;

  await page.unroute("**/api/settings/database/adb/settings");
  await page.route("**/api/settings/database/adb/settings", async (route) => {
    adbRefreshCount += 1;
    await fulfillJson(route, adbInfoFixture());
  });
  await page.unroute("**/api/settings/database/wallet/download");
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    walletDownloadCount += 1;
    await walletGate.promise;
    await fulfillJson(route, {
      status: "downloaded",
      settings: databaseSettingsFixture(),
    });
  });

  await page.goto("/settings/database");
  await expect.poll(() => walletDownloadCount).toBe(0);

  const adbCard = page.locator("#adb-management");
  const refreshButton = adbCard.getByRole("button", { name: "情報を再取得" });
  await refreshButton.click();
  await expect.poll(() => adbRefreshCount).toBe(1);
  await expect.poll(() => walletDownloadCount).toBe(1);
  await expectNoAdbWalletPendingStatus(page);
  // スピナーは押した「情報を再取得」だけが出す。同じ保存と Wallet の取得を使う「保存」は無効にするだけ（#819）。
  await expect(refreshButton).toHaveAttribute("aria-busy", "true");
  const adbSaveButton = adbCard.getByRole("button", { name: "保存", exact: true });
  await expect(adbSaveButton).toBeDisabled();
  await expect(adbSaveButton).not.toHaveAttribute("aria-busy", /.*/);
  await expect(adbCard.locator("svg.animate-spin:visible")).toHaveCount(1);

  walletGate.release();

  await expectNoAdbWalletPendingStatus(page);
  await expect(page.getByText("ADB OCID が設定されています。")).toBeVisible();
  await expect(page.getByText("設定済み", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Oracle Wallet を OCI から取得し、サーバーへ設定しました。")
  ).toBeVisible();
  await expect(
    page.getByText("Oracle Wallet を OCI から取得し、サーバーへ設定しました。")
  ).toHaveCount(1);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoAdbWalletPendingStatus(page);
  await expectNoHorizontalOverflow(page);
});

test("保存は ADB 情報更新後に Wallet 取得も実行し進行メッセージは出さない", async ({
  page,
}) => {
  const walletGate = createRequestGate();
  let adbRefreshCount = 0;
  let walletDownloadCount = 0;

  await page.unroute("**/api/settings/database/adb/settings");
  await page.route("**/api/settings/database/adb/settings", async (route) => {
    adbRefreshCount += 1;
    await fulfillJson(route, adbInfoFixture());
  });
  await page.unroute("**/api/settings/database/wallet/download");
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    walletDownloadCount += 1;
    await walletGate.promise;
    await fulfillJson(route, {
      status: "downloaded",
      settings: databaseSettingsFixture(),
    });
  });

  await page.goto("/settings/database");
  await expect.poll(() => walletDownloadCount).toBe(0);

  const adbCard = page.locator("#adb-management");
  await adbCard.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => adbRefreshCount).toBe(1);
  await expect.poll(() => walletDownloadCount).toBe(1);
  await expectNoAdbWalletPendingStatus(page);
  await expectNoHorizontalOverflow(page);

  walletGate.release();

  await expectNoAdbWalletPendingStatus(page);
  await expect(page.getByText("ADB OCID が設定されています。")).toBeVisible();
  await expect(
    page.getByText("Oracle Wallet を OCI から取得し、サーバーへ設定しました。")
  ).toBeVisible();
  await expect(
    page.getByText("Oracle Wallet を OCI から取得し、サーバーへ設定しました。")
  ).toHaveCount(1);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoAdbWalletPendingStatus(page);
  await expectNoHorizontalOverflow(page);
});

test("情報を再取得の Wallet 取得失敗は ADB 操作フィードバックとして表示する", async ({
  page,
}) => {
  let adbRefreshCount = 0;
  let walletDownloadCount = 0;
  const walletError =
    "Wallet 保存領域を使用できません。管理者に保存領域の書き込み権限を確認するよう依頼してから再試行してください。";

  await page.unroute("**/api/settings/database/adb/settings");
  await page.route("**/api/settings/database/adb/settings", async (route) => {
    adbRefreshCount += 1;
    await fulfillJson(route, adbInfoFixture());
  });
  await page.unroute("**/api/settings/database/wallet/download");
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    walletDownloadCount += 1;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: { "X-Request-ID": "wallet-storage-unavailable-request" },
      body: JSON.stringify({
        data: null,
        error_messages: [walletError],
        warning_messages: [],
        error_code: "WALLET_STORAGE_UNAVAILABLE",
        problem: {
          type: "urn:nl2sql:problem:wallet-storage-unavailable",
          title: "サーバー内部でエラーが発生しました",
          status: 500,
          detail: walletError,
          code: "WALLET_STORAGE_UNAVAILABLE",
          request_id: "wallet-storage-unavailable-request",
          retryable: true,
          field_errors: [],
        },
      }),
    });
  });

  await page.goto("/settings/database");
  await page.getByRole("button", { name: "情報を再取得" }).click();

  await expect.poll(() => adbRefreshCount).toBe(1);
  await expect.poll(() => walletDownloadCount).toBe(1);
  const adbAlert = page
    .locator("#adb-management")
    .getByRole("alert")
    .filter({ hasText: /保存領域の書き込み権限を確認/ });
  await expect(adbAlert).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: /保存領域の書き込み権限を確認/ })
  ).toHaveCount(1);
  await expect(adbAlert).toContainText("リクエストID: wallet-storage-unavailable-request");
  await expect(page.getByRole("button", { name: "OCI から Wallet を再取得" })).toHaveCount(
    0
  );
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(adbAlert).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("ADB OCID がない場合は自動取得せず手動アップロードを案内する", async ({ page }) => {
  let downloadCount = 0;
  await page.route("**/api/settings/database", (route) =>
    fulfillJson(
      route,
      databaseSettingsFixture({
        wallet_uploaded: false,
        available_services: [],
        readiness: "wallet_not_found",
        adb_ocid: "",
      })
    )
  );
  await page.route("**/api/settings/database/wallet/download", async (route) => {
    downloadCount += 1;
    await fulfillJson(route, {
      status: "downloaded",
      settings: databaseSettingsFixture(),
    });
  });

  await page.goto("/settings/database");

  await expect(page.getByText(/ADB OCID が未設定のため自動取得は行いません/)).toBeVisible();
  await expect(page.getByTestId("oracle-wallet-upload-dropzone")).toContainText(
    "ドラッグ＆ドロップまたは選択"
  );
  await expect.poll(() => downloadCount).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("Select AI Credential を明示確認で作成し、成功状態だけを表示する", async ({
  page,
}) => {
  let posted: Record<string, unknown> | null = null;
  let credential = {
    credential_name: "NL2SQL_OCI_CRED",
    schema_name: "ADMIN",
    exists: false,
    region: "ap-osaka-1",
    oci_auth_ready: true,
    missing_fields: [],
    operation: null as string | null,
  };
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    if (route.request().method() === "POST") {
      posted = route.request().postDataJSON() as Record<string, unknown>;
      credential = { ...credential, exists: true, region: "us-chicago-1", operation: "created" };
    }
    await fulfillJson(route, credential);
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  await expect(card.getByRole("heading", { name: "Select AI Credential" })).toBeVisible();
  await expect(card.getByText("NL2SQL_OCI_CRED", { exact: true })).toBeVisible();
  await expect(card.getByText("ADMIN", { exact: true })).toBeVisible();
  await card.getByRole("combobox", { name: "Select AI 既定リージョン" }).click();
  await page.getByRole("option", { name: "us-chicago-1" }).click();
  const confirmation = card.getByTestId("execution-confirmation-field").getByRole("textbox");
  const create = card.getByRole("button", { name: "Credential を作成" });
  await expect(create).toBeDisabled();
  await confirmation.fill("ADMIN_EXECUTE");
  await expect(create).toBeEnabled();
  await create.click();

  await expect.poll(() => posted).not.toBeNull();
  expect(posted).toEqual({
    region: "us-chicago-1",
    confirmation: "ADMIN_EXECUTE",
    recreate: false,
  });
  expect(posted).not.toHaveProperty("private_key");
  const success = card.getByTestId("select-ai-credential-success");
  await expect(success).toContainText("Select AI Credential を作成");
  await expect(success.getByRole("link")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("Select AI Credential のリージョンは短い選択の幅で、placeholder が欄に収まる（#1891）", { tag: "@responsive" }, async ({ page }) => {
  // 2 つの幅を 1 つの test で回る。
  test.setTimeout(90_000);
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", (route) =>
    fulfillJson(route, {
      credential_name: "NL2SQL_OCI_CRED",
      schema_name: "ADMIN",
      exists: false,
      region: "ap-osaka-1",
      oci_auth_ready: true,
      missing_fields: [],
      operation: null,
    })
  );
  const projectWidth = page.viewportSize()?.width ?? 1280;
  const height = page.viewportSize()?.height ?? 900;
  // 375px の project は 375px だけ、desktop は 1280px と 1920px。
  const widths = projectWidth < 640 ? [projectWidth] : [1280, 1920];
  for (const width of widths) {
    await page.setViewportSize({ width, height });
    const where = `${width}px`;
    await page.goto("/settings/select-ai-credential");
    const region = page.getByTestId("select-ai-credential-card").getByRole("combobox", { name: "Select AI 既定リージョン" });
    await expect(region).toBeVisible();
    expect(await measureHorizontalOverflow(page), where).toBeLessThanOrEqual(1);
    expect(await findClippedPlaceholders(page), where).toEqual([]);
    if (width < 640) continue;
    expect(await findFieldsOffWidthScale(page), where).toEqual([]);
    // リージョンは短い選択（sm 12rem = 168px）。
    expect(Math.abs((await region.boundingBox())!.width - 168), where).toBeLessThanOrEqual(1);
  }
});

test("既存 Select AI Credential の再作成は確認語だけで直接実行する", async ({
  page,
}) => {
  let recreateRequest: Record<string, unknown> | null = null;
  const existing = {
    credential_name: "NL2SQL_OCI_CRED",
    schema_name: "ADMIN",
    exists: true,
    region: "ap-osaka-1",
    oci_auth_ready: true,
    missing_fields: [],
    operation: null,
  };
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    if (route.request().method() === "POST") {
      recreateRequest = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillJson(route, { ...existing, operation: "recreated" });
      return;
    }
    await fulfillJson(route, existing);
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  await expect(card.getByText("作成済み", { exact: true })).toBeVisible();
  await card.getByTestId("execution-confirmation-field").getByRole("textbox").fill(
    "ADMIN_EXECUTE"
  );
  await card.getByRole("button", { name: "Credential を再作成" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect.poll(() => recreateRequest).not.toBeNull();
  expect(recreateRequest).toEqual({
    region: "ap-osaka-1",
    confirmation: "ADMIN_EXECUTE",
    recreate: true,
  });
  await expect(card.getByTestId("select-ai-credential-success")).toBeVisible();
});

test("OCI 認証材料不足を 375px で案内し、作成操作を無効化する", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  let statusRequests = 0;
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", (route) => {
    statusRequests += 1;
    return fulfillJson(route, {
      credential_name: "NL2SQL_OCI_CRED",
      schema_name: "ADMIN",
      exists: false,
      region: "ap-osaka-1",
      oci_auth_ready: false,
      missing_fields: ["fingerprint", "key_file_permissions"],
      operation: null,
    });
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  const readinessStatus = card
    .getByRole("status")
    .filter({ hasText: "OCI 認証材料を準備できません" });
  await expect(readinessStatus).toContainText("Fingerprint");
  await expect(readinessStatus).toContainText("秘密鍵のファイル権限 (0600)");
  await expect(readinessStatus).toContainText(
    "OCI 認証設定を確認して、修正後に状態を再取得してください。"
  );
  await expect(card.getByRole("link", { name: "OCI 認証設定を開く" })).toHaveCount(0);
  const initialStatusRequests = statusRequests;
  expect(initialStatusRequests).toBeGreaterThan(0);
  await card.getByRole("button", { name: "最新情報を取得" }).click();
  await expect.poll(() => statusRequests).toBe(initialStatusRequests + 1);
  await expect(card.getByRole("button", { name: "Credential を作成" })).toBeDisabled();
  await expect(card.getByTestId("execution-confirmation-field").getByRole("textbox")).toBeDisabled();
  await expectNoHorizontalOverflow(page);
});

test("Select AI Credential 状態の取得中は経過時間付きの読み込み表示と Skeleton を出す", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    await pending;
    await fulfillJson(route, {
      credential_name: "NL2SQL_OCI_CRED",
      schema_name: "ADMIN",
      exists: false,
      region: "us-chicago-1",
      oci_auth_ready: true,
      missing_fields: [],
      operation: null,
    });
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  const loading = card.getByRole("region", { name: "Select AI Credential の状態を読み込んでいます" });
  await expect(loading).toBeVisible();
  await expect(loading.getByRole("timer")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  release();
  await expect(loading).toHaveCount(0);
  await expect(card.getByText("NL2SQL_OCI_CRED", { exact: true })).toBeVisible();
});

test("Select AI Credential 状態の初回取得失敗は標準 ErrorState で再取得できる", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const apiError =
    "Select AI Credential の状態を Oracle から取得できませんでした。データベース接続を確認して再試行してください。";
  let statusRequests = 0;
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", (route) => {
    statusRequests += 1;
    return route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: [apiError],
        warning_messages: [],
        error_code: "SELECT_AI_CREDENTIAL_STATUS_FAILED",
        request_id: "req-select-ai-status-initial",
      }),
    });
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  const alert = card.getByRole("alert");
  await expect(alert).toHaveCount(1);
  await expect(alert).toContainText(apiError);
  await expect(alert).toContainText("req-select-ai-status-initial");
  const retry = card.getByRole("button", { name: "再試行" });
  await expect(retry).toBeVisible();
  await retry.click();
  await expect.poll(() => statusRequests).toBeGreaterThanOrEqual(2);
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("Select AI Credential 状態の再取得失敗は標準 Banner で知らせ、旧データを保持する", async ({
  page,
}) => {
  const apiError =
    "Select AI Credential の状態を Oracle から取得できませんでした。データベース接続を確認して再試行してください。";
  let statusRequests = 0;
  let failNextRequest = false;
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", (route) => {
    statusRequests += 1;
    if (failNextRequest) {
      failNextRequest = false;
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          data: null,
          error_messages: [apiError],
          warning_messages: [],
          error_code: "SELECT_AI_CREDENTIAL_STATUS_FAILED",
          request_id: "req-select-ai-status-refresh",
        }),
      });
    }
    return fulfillJson(route, {
      credential_name: "NL2SQL_OCI_CRED",
      schema_name: "ADMIN",
      exists: false,
      region: "ap-osaka-1",
      oci_auth_ready: false,
      missing_fields: ["fingerprint"],
      operation: null,
    });
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  await expect(card.getByText("NL2SQL_OCI_CRED", { exact: true })).toBeVisible();
  await expect(card.getByText("ADMIN", { exact: true })).toBeVisible();
  const initialStatusRequests = statusRequests;
  failNextRequest = true;
  await card.getByRole("button", { name: "最新情報を取得" }).click();
  await expect.poll(() => statusRequests).toBeGreaterThan(initialStatusRequests);
  const alert = card.getByRole("alert");
  await expect(alert).toHaveCount(1);
  await expect(alert).toContainText(apiError);
  await expect(alert).toContainText("req-select-ai-status-refresh");
  await expect(card.getByText("NL2SQL_OCI_CRED", { exact: true })).toBeVisible();
  await expect(card.getByText("ADMIN", { exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: "再試行" })).toHaveCount(1);
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("Select AI Credential API 失敗は固定 alert だけに表示し Toast を重複させない", async ({
  page,
}) => {
  const apiError = "Select AI Credential を Oracle に作成できませんでした。";
  await page.unroute("**/api/settings/database/select-ai-credential");
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          data: null,
          error_messages: [apiError],
          warning_messages: [],
          error_code: "SELECT_AI_CREDENTIAL_CREATE_FAILED",
        }),
      });
      return;
    }
    await fulfillJson(route, {
      credential_name: "NL2SQL_OCI_CRED",
      schema_name: "ADMIN",
      exists: false,
      region: "ap-osaka-1",
      oci_auth_ready: true,
      missing_fields: [],
      operation: null,
    });
  });

  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  await card.getByTestId("execution-confirmation-field").getByRole("textbox").fill(
    "ADMIN_EXECUTE"
  );
  await card.getByRole("button", { name: "Credential を作成" }).click();

  await expect(card.getByRole("alert").filter({ hasText: apiError })).toBeVisible();
  await expect(card.getByRole("link", { name: "OCI 認証設定を開く" })).toHaveCount(0);
  await expect(page.getByText(apiError, { exact: true })).toHaveCount(1);
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0);
});

test("外観と証明書にテーマの選択は無く、配色は OS の設定にかかわらずライトに固定する (#17)", async ({ page }) => {
  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();
  await expect(page.getByText("HTTPS の接続に使う証明書を取得します。配色はライトの 1 種類で、切り替えはありません。")).toBeVisible();
  await expect(page.getByTestId("appearance-theme-toggle")).toHaveCount(0);
  for (const name of ["ライト", "ダーク", "自動（OS 設定）"]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }
  const html = page.locator("html");
  // 変数の文字列ではなく、実際に解決された色（body の背景色 / 変数を塗ったプローブ要素の background-color）を検証する。
  // 期待値は packages/ui/src/styles/tokens/palette.css の neutral / teal スケール由来（#18）。
  const hexToRgb = (hex: string) => {
    const value = Number.parseInt(hex.slice(1), 16);
    return `rgb(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255})`;
  };
  // body { background: var(--color-canvas) }
  const bgVar = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const tokenVar = (name: string) =>
    page.evaluate((tokenName) => {
      const probe = document.createElement("div");
      probe.style.backgroundColor = `var(${tokenName})`;
      document.body.appendChild(probe);
      const color = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return color;
    }, name);
  // OS が dark でも、html は class・data-theme を持たず color-scheme: light のまま。
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme });
    await page.reload();
    await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();
    await expect(html).toHaveCSS("color-scheme", "light");
    await expect(html).not.toHaveAttribute("data-theme");
    expect(await html.evaluate((node) => node.className)).not.toMatch(/\bdark\b/);
    await expect.poll(bgVar).toBe(hexToRgb("#f7f8fa"));
    // canvas = neutral-50 / accent-emphasis = teal-500 / sidebar = teal-850
    expect(await tokenVar("--color-accent-emphasis")).toBe(hexToRgb("#0d7469"));
    expect(await tokenVar("--color-sidebar")).toBe(hexToRgb("#0f2c2a"));
  }
});

test("OCI 初期取得失敗は編集を許可せず、再試行後の遅延保存中も入力と重複操作を防ぐ", async ({ page }) => {
  let failLoad = true;
  let saves = 0;
  const gate = createRequestGate();
  await page.route("**/api/settings/oci", async (route) => {
    if (route.request().method() === "PATCH") {
      saves += 1;
      await gate.promise;
      await fulfillJson(route, { ...ociSettingsFixture(), ...route.request().postDataJSON() });
    } else if (failLoad) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "OCI 設定取得失敗" }) });
    } else await fulfillJson(route, ociSettingsFixture());
  });
  await page.goto("/settings/oci");
  await expect(page.getByText("OCI 設定取得失敗")).toBeVisible();
  await expect(page.locator("#oci-user-ocid")).toHaveCount(0);
  failLoad = false;
  await page.getByRole("button", { name: /再試行/ }).click();
  const user = page.locator("#oci-user-ocid");
  await user.fill("ocid1.user.oc1..edited");
  await page.getByRole("button", { name: "OCI 認証設定: OCI 設定を保存" }).click();
  await expect.poll(() => saves).toBe(1);
  await expect(user).toBeDisabled();
  await expect(page.locator("#oci-object-storage-region")).toBeDisabled();
  gate.release();
  await expect(user).toBeEnabled();
  await expect(user).toHaveValue("ocid1.user.oc1..edited");
  await expectNoHorizontalOverflow(page);
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
  await expect(page.locator("#upload-storage-oci")).toBeDisabled();
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

test("Credential 確認は region 変更と実行失敗で解除し、処理中は region を固定する", async ({ page }) => {
  const gate = createRequestGate();
  let creates = 0;
  await page.route("**/api/settings/database/select-ai-credential", async (route) => {
    if (route.request().method() === "POST") {
      creates += 1;
      await gate.promise;
      await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Credential 操作失敗" }) });
    } else await fulfillJson(route, { credential_name: "NL2SQL_OCI_CRED", schema_name: "ADMIN", exists: false,
      region: "us-chicago-1", oci_auth_ready: true, missing_fields: [], operation: null });
  });
  await page.goto("/settings/select-ai-credential");
  const card = page.getByTestId("select-ai-credential-card");
  const field = card.getByRole("textbox", { name: "実行確認語" });
  const region = card.getByRole("combobox", { name: "Select AI 既定リージョン" });
  const create = card.getByRole("button", { name: "Credential を作成" });
  await field.fill("ADMIN_EXECUTE");
  await region.click();
  await page.getByRole("option", { name: "ap-osaka-1" }).click();
  await expect(field).toHaveValue("");
  await expect(create).toBeDisabled();
  await field.fill("ADMIN_EXECUTE");
  await create.click();
  await expect.poll(() => creates).toBe(1);
  await expect(region).toBeDisabled();
  gate.release();
  await expect(card.getByText("Credential 操作失敗", { exact: true })).toBeVisible();
  await expect(field).toHaveValue("");
  await expect(create).toBeDisabled();
});

test("レビュー補完: OCI config 反映・namespace 取得・保存の action を確認する", async ({ page }) => {
  let saved: Record<string, unknown> | null = null;
  await page.route("**/api/settings/oci/object-storage", async (route) => {
    saved = route.request().postDataJSON();
    await fulfillJson(route, uploadStorageFixture(saved ?? {}));
  });
  await page.goto("/settings/oci");
  await page.locator("#oci-user-ocid").fill("ocid1.user.oc1..draft");
  await page.getByRole("button", { name: /config から反映/ }).click();
  await expect(page.locator("#oci-user-ocid")).toHaveValue("ocid1.user.oc1..example");
  await page.getByRole("button", { name: /Object Storage ネームスペース: 取得/ }).click();
  await expect(page.locator("#oci-object-storage-namespace")).toHaveValue("exampletenancy");
  await expect(page.locator("#oci-object-storage-namespace")).toHaveAttribute("readonly", "");
  await page.getByRole("button", { name: "Object Storage: 保存", exact: true }).click();
  await expect.poll(() => saved).toEqual({ object_storage_region: "ap-osaka-1", object_storage_namespace: "exampletenancy" });
  await expect(page.getByText("Object Storage 設定を保存しました。", { exact: true })).toBeVisible();
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

test("レビュー補完: 保存先の失敗再試行と OCI への離脱確認を確認する", async ({ page }) => {
  let failSave = true;
  let persisted = uploadStorageFixture({ object_storage_namespace: "" });
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
  await page.getByRole("radio", { name: /OCI Object Storage/ }).check();
  await page.getByRole("button", { name: "OCI 認証設定を開く", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page).toHaveURL(/settings\/upload-storage$/);
  await page.getByRole("button", { name: "OCI 認証設定を開く", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動", exact: true }).click();
  await expect(page).toHaveURL(/settings\/oci$/);
});

test("未保存の変更があるときはブラウザの戻る操作でも破棄を確認する（#138）", async ({ page }) => {
  await page.goto("/settings/upload-storage");
  await expect(page.locator("#upload-storage-local-dir")).toBeEditable();
  // 変更がなければ確認なしで移動できる（SPA 内の PUSH で履歴を 1 つ積む）。
  await (await openSidebarNav(page)).getByRole("link", { name: "OCI 認証" }).click();
  await expect(page).toHaveURL(/settings\/oci$/);
  await expect(page.getByRole("alertdialog")).toHaveCount(0);

  const userOcid = page.locator("#oci-user-ocid");
  await expect(userOcid).toBeEditable();
  await userOcid.fill("ocid1.user.oc1..back-guard");

  // 戻る → 確認。キャンセルすると URL と入力が残る。
  await page.goBack();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page).toHaveURL(/settings\/oci$/);
  await expect(userOcid).toHaveValue("ocid1.user.oc1..back-guard");

  // もう一度戻る → 破棄して移動。
  await page.goBack();
  await dialog.getByRole("button", { name: "破棄して移動", exact: true }).click();
  await expect(page).toHaveURL(/settings\/upload-storage$/);
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});

test("レビュー補完: システム設定6画面の表示とキーボード・横幅を記録する", async ({ page }, testInfo) => {
  // 外観と証明書はテーマの選択が無い（#17）ので、CA の証明書を配る状態にしてダウンロードのリンクへフォーカスする。
  await page.route("**/platform/ca.crt", (route) =>
    route.fulfill({ status: 200, headers: { "Content-Type": "application/x-x509-ca-cert" }, body: "" }),
  );
  for (const slug of ["oci", "upload-storage", "model", "database", "system-tables", "appearance"]) {
    await page.goto(`/settings/${slug}`);
    await expect(page.locator("main")).toBeVisible();
    const ready = slug === "oci" ? page.locator("#oci-user-ocid")
      : slug === "upload-storage" ? page.locator("#upload-storage-local-dir")
      : slug === "model" ? page.locator("#enterprise-endpoint")
      : slug === "database" ? page.locator("#oracle-user")
      : slug === "system-tables" ? page.locator("#system-tables").getByRole("button", { name: "最新情報を取得" })
      : page.getByRole("link", { name: "CA の証明書をダウンロード" });
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
test("外観と証明書の HTTPS の証明書", async ({ page }) => {
  await page.goto("/settings/appearance");

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
