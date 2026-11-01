import { expect, test, type Page, type Route, type TestInfo } from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { measuredVisibleRowsHeight } from "./_helpers/data-table";
import { systemAdminMe } from "./_helpers/database-gate";
import { expectToastPlacement } from "./_helpers/toast";

function envelope(data: unknown, errors: string[] = [], errorCode?: string) {
  return {
    data,
    error_messages: errors,
    warning_messages: [],
    ...(errorCode ? { error_code: errorCode } : {}),
  };
}

async function fulfill(
  route: Route,
  data: unknown,
  status = 200,
  errors: string[] = [],
  errorCode?: string
) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(envelope(data, errors, errorCode)),
  });
}

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
  config_source: "runtime",
};

type SchemaStatus = "missing" | "partial" | "outdated" | "ready";
type SystemObjectType = "TABLE" | "INDEX" | "SEQUENCE" | "PACKAGE" | "PACKAGE BODY";

const SCHEMA_HEAD = 17;
const EXPECTED_OBJECT_COUNT = 53;
const EXPECTED_TABLE_COUNT = 27;
const APPLIED_VERSIONS = [0, 1, 2, 3, 5, 6, 7, 8, 9, 15, 17] as const;

const SYSTEM_TABLE_NAMES = [
  "NL2SQL_SCHEMA_OPERATIONS",
  "NL2SQL_SCHEMA_MIGRATIONS",
  "NL2SQL_ONTOLOGY_REVISIONS",
  "NL2SQL_ONTOLOGY_NODES",
  "NL2SQL_ONTOLOGY_EDGES",
  "NL2SQL_ONTOLOGY_PROFILE_VIEWS",
  "NL2SQL_ONTOLOGY_QUERY_SESSIONS",
  "NL2SQL_ONTOLOGY_ARTIFACTS",
  "NL2SQL_ONTOLOGY_PROPOSALS",
  "NL2SQL_ONTOLOGY_IDEMPOTENCY",
  "NL2SQL_ONTOLOGY_SOURCE_DOCS",
  "NL2SQL_ONTOLOGY_JOBS",
  "NL2SQL_ONTOLOGY_RECOMMENDATIONS",
  "NL2SQL_CHANGE_TOKENS",
  "NL2SQL_PROFILES",
  "NL2SQL_SCHEMA_CATALOG_HEAD",
  "NL2SQL_SCHEMA_OBJECTS",
  "NL2SQL_SCHEMA_COLUMNS",
  "NL2SQL_SCHEMA_CONSTRAINTS",
  "NL2SQL_SCHEMA_DEPENDENCIES",
  "NL2SQL_SCHEMA_SAMPLES",
  "NL2SQL_SCHEMA_REFRESH_JOBS",
  "NL2SQL_STATE_DOCUMENTS",
  "NL2SQL_MIGRATION_OUTBOX",
  "NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS",
  "NL2SQL_EVALUATION_JOBS",
  "NL2SQL_EVALUATION_RESULTS",
] as const;

const SYSTEM_INDEX_NAMES = [
  "IX_NL2SQL_ONT_NODE_PHYSICAL",
  "IX_NL2SQL_ONT_NODE_EMBED",
  "IX_NL2SQL_ONT_EDGE_SOURCE",
  "IX_NL2SQL_ONT_EDGE_TARGET",
  "IX_NL2SQL_ONT_SESSION_PROFILE",
  "IX_NL2SQL_ONT_ART_SESSION",
  "IX_NL2SQL_ONT_PROP_SESSION",
  "IX_NL2SQL_ONT_IDEMPOTENCY_RESOURCE",
  "IX_NL2SQL_ONT_SOURCE_PROFILE",
  "IX_NL2SQL_ONT_JOB_STATE",
  "IX_NL2SQL_ONT_REC_QUESTION",
  "UX_NL2SQL_ONT_ONE_PUBLISHED",
  "IX_NL2SQL_PROFILES_LIST",
  "IX_NL2SQL_SCHEMA_OBJECT_LIST",
  "IX_NL2SQL_SCHEMA_COLUMN_SEARCH",
  "IX_NL2SQL_SCHEMA_REFRESH_STATE",
  "IX_NL2SQL_STATE_DOCUMENT_LIST",
  "UX_NL2SQL_MIGRATION_OUTBOX_VERSION",
  "IX_NL2SQL_ONT_VIEW_REVISION",
  "IX_NL2SQL_ONT_PROPOSAL_PROFILE",
  "IX_NL2SQL_SCHEMA_REFRESH_LEASE",
  "IX_NL2SQL_EVAL_JOB_STATE",
  "IX_NL2SQL_EVAL_JOB_LEASE",
] as const;

const SYSTEM_REQUIRED_OBJECTS: ReadonlyArray<{
  name: string;
  object_type: SystemObjectType;
}> = [
  ...SYSTEM_TABLE_NAMES.map((name) => ({ name, object_type: "TABLE" as const })),
  ...SYSTEM_INDEX_NAMES.map((name) => ({ name, object_type: "INDEX" as const })),
  { name: "NL2SQL_MIGRATION_SNAPSHOT_SEQ", object_type: "SEQUENCE" },
  { name: "NL2SQL_ONT_ACTION_TX", object_type: "PACKAGE" },
  { name: "NL2SQL_ONT_ACTION_TX", object_type: "PACKAGE BODY" },
];

const PARTIAL_MISSING_NAMES = new Set([
  "NL2SQL_EVALUATION_JOBS",
  "NL2SQL_EVALUATION_RESULTS",
  "IX_NL2SQL_EVAL_JOB_STATE",
  "IX_NL2SQL_EVAL_JOB_LEASE",
]);

function systemObjectRows(status: SchemaStatus, count = EXPECTED_OBJECT_COUNT) {
  return SYSTEM_REQUIRED_OBJECTS.slice(0, count).map((object, index) => {
    const exists =
      status === "ready" ||
      status === "outdated" ||
      (status === "partial" && !PARTIAL_MISSING_NAMES.has(object.name));
    const isTable = object.object_type === "TABLE";
    return {
      ...object,
      owner: "NL2SQL_APP",
      qualified_name: `NL2SQL_APP.${object.name}`,
      exists,
      estimated_rows: exists && isTable ? index + 1 : null,
      created_at: exists ? "2026-07-19T00:00:00Z" : null,
      last_analyzed_at: exists && isTable ? "2026-07-19T01:00:00Z" : null,
    };
  });
}

function systemTables(
  status: SchemaStatus,
  options: {
    operationStatus?: string;
    objectCount?: number;
    lastErrorCode?: string;
  } = {}
) {
  const ready = status === "ready";
  const operationStatus = options.operationStatus ?? "idle";
  const missingObjects =
    status === "partial"
      ? SYSTEM_REQUIRED_OBJECTS.filter((object) => PARTIAL_MISSING_NAMES.has(object.name))
      : status === "missing"
        ? [...SYSTEM_REQUIRED_OBJECTS]
        : [];
  const missingTableCount = missingObjects.filter(
    (object) => object.object_type === "TABLE"
  ).length;
  const tableRows = systemObjectRows(status).filter(
    (object) => object.object_type === "TABLE"
  );
  return {
    status,
    schema_head: SCHEMA_HEAD,
    applied_versions: ready ? [...APPLIED_VERSIONS] : [0, 1, 2, 3, 5, 6],
    pending_versions: ready ? [] : [7, 8, 9, 15, 17],
    expected_object_count: EXPECTED_OBJECT_COUNT,
    existing_object_count: EXPECTED_OBJECT_COUNT - missingObjects.length,
    expected_table_count: EXPECTED_TABLE_COUNT,
    existing_table_count: EXPECTED_TABLE_COUNT - missingTableCount,
    missing_objects: missingObjects,
    tables: tableRows.map((object) => ({
      name: object.name,
      exists: object.exists,
      estimated_rows: object.estimated_rows,
      created_at: object.created_at,
      last_analyzed_at: object.last_analyzed_at,
    })),
    objects: systemObjectRows(status, options.objectCount),
    operation_state: {
      status: operationStatus,
      operation_kind: operationStatus === "running" ? "initialize" : null,
      lease_expires_at: operationStatus === "running" ? "2026-07-19T00:02:00Z" : null,
      last_error_code:
        operationStatus === "failed"
          ? (options.lastErrorCode ?? "ORA-00600")
          : null,
      schema_epoch: 7,
      updated_at: "2026-07-19T00:00:00Z",
    },
  };
}

async function mockDatabasePage(page: Page, user = systemAdminMe) {
  await page.route("**/api/auth/me", (route) => fulfill(route, user));
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: null })
  );
  await page.route("**/api/settings/database", (route) => fulfill(route, databaseSettings));
  await page.route("**/api/schema/owners", (route) =>
    fulfill(route, {
      current_owner: "NL2SQL_APP",
      owners: [
        { owner: "NL2SQL_APP", is_current: true, table_count: 49, view_count: 0 },
      ],
      excluded_oracle_maintained_count: 20,
    })
  );
}

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
      )
    )
    .toBeTruthy();
}

function expectedInformationRows(testInfo: TestInfo) {
  return testInfo.project.name === "mobile-375" ? 5 : 8;
}

test.beforeEach(async ({ page }) => {
  await mockDatabasePage(page);
});

test("運用設定のメニューからシステムテーブル管理を開ける", async ({ page }) => {
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready"))
  );

  await page.goto("/settings/database");
  await expect(page.locator("#system-tables")).toHaveCount(0);

  // システムテーブルは「運用設定」（既定で折りたたみ）にある（#81）。
  // 375px ではナビがドロワー（#367）。開いてから、展開ボタンがあるときだけ押す。
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "データベース" })).toBeVisible();
  const expandOperations = sidebar.getByRole("button", { name: "運用設定 を展開" });
  if (await expandOperations.isVisible()) await expandOperations.click();
  await page.getByRole("link", { name: "システムテーブル" }).click();
  await expect(page).toHaveURL(/\/settings\/system-tables$/);
  await expect(page.getByRole("heading", { name: "システムテーブル管理" })).toBeVisible();
  await expect(page.locator("#system-tables")).toBeVisible();
  await expect(page.locator("#system-tables").getByText("初期化済み", { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("状態取得中は aria status を表示し、完了後に操作を有効化する", async ({ page }) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/settings/database/system-tables", async (route) => {
    await pending;
    await fulfill(route, systemTables("ready"));
  });

  await page.goto("/settings/system-tables");
  await expect(
    page.getByRole("region", { name: "システムテーブルの状態を読み込んでいます" })
  ).toBeVisible();
  release();
  await expect(page.locator("#system-tables").getByRole("button", { name: "作成・更新" })).toBeEnabled();
});

test("四つの schema 状態を再取得し、詳細表を局所スクロールで表示する", async ({ page }) => {
  let status: SchemaStatus = "missing";
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables(status))
  );

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await expect(card).toBeVisible();

  for (const [nextStatus, label] of [
    ["missing", "未初期化"],
    ["partial", "一部不足"],
    ["outdated", "更新必要"],
    ["ready", "初期化済み"],
  ] as const) {
    status = nextStatus;
    await card.getByRole("button", { name: "最新情報を取得" }).click();
    await expect(page.getByRole("status").filter({ hasText: "最新の状態に更新しました。" }).last()).toBeVisible();
    await expect(
      card.locator("span").filter({ hasText: new RegExp(`^${label}$`) }).first()
    ).toBeVisible();
  }

  await card.getByText(/管理オブジェクトの詳細を表示/).click();
  await expect(card.getByRole("table")).toBeVisible();
  // 所有者付きの修飾名で表示する（#556）。
  await expect(card.getByText("NL2SQL_APP.NL2SQL_PROFILES", { exact: true })).toBeVisible();
  await expect(card.getByText("27 / 27", { exact: true })).toBeVisible();
  await expect(card.getByText("53 / 53", { exact: true })).toBeVisible();
  await expect(
    card.getByText("テーブル・索引・シーケンス・パッケージの合計", { exact: true })
  ).toBeVisible();
  const objectRows = card.getByTestId("system-tables-scroll-region").locator("tbody tr");
  await expect(objectRows).toHaveCount(EXPECTED_OBJECT_COUNT);
  await expect(card.getByRole("cell", { name: "テーブル", exact: true })).toHaveCount(27);
  await expect(card.getByRole("cell", { name: "索引", exact: true })).toHaveCount(23);
  await expect(card.getByRole("cell", { name: "シーケンス", exact: true })).toHaveCount(1);
  await expect(card.getByRole("cell", { name: "パッケージ（Package）", exact: true })).toHaveCount(1);
  await expect(card.getByRole("cell", { name: "パッケージ本体（Package Body）", exact: true })).toHaveCount(1);
  await expectNoPageOverflow(page);
});

test("不足した索引を全管理オブジェクト一覧で特定できる", async ({ page }) => {
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("partial"))
  );

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card.getByText(/管理オブジェクトの詳細を表示/).click();
  const missingIndex = card.getByRole("row", { name: /IX_NL2SQL_EVAL_JOB_STATE/ });
  await expect(missingIndex).toContainText("索引");
  await expect(missingIndex).toContainText("不足");
  await expect(missingIndex.getByText("対象外", { exact: true })).toHaveCount(2);
  await expectNoPageOverflow(page);
});

test("旧API応答では既存のテーブル一覧へ安全にフォールバックする", async ({ page }) => {
  const legacyResponse = systemTables("ready");
  delete (legacyResponse as { objects?: unknown }).objects;
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, legacyResponse)
  );

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card.getByText(/管理オブジェクトの詳細を表示/).click();
  await expect(
    card.getByTestId("system-tables-scroll-region").locator("tbody tr")
  ).toHaveCount(EXPECTED_TABLE_COUNT);
  await expect(card.getByRole("cell", { name: "テーブル", exact: true })).toHaveCount(
    EXPECTED_TABLE_COUNT
  );
  await expectNoPageOverflow(page);
});

test("Oracle RDF Network 管理カードと API 呼び出しを表示しない", async ({ page }) => {
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready"))
  );
  let rdfRequests = 0;
  await page.route("**/api/settings/database/rdf-network**", (route) => {
    rdfRequests += 1;
    return fulfill(route, null, 410, ["RDF Network API は削除済みです。"]);
  });

  await page.goto("/settings/system-tables");
  await expect(page.locator("#system-tables")).toBeVisible();
  await expect(page.locator("#rdf-network")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "RDF Network" })).toHaveCount(0);
  expect(rdfRequests).toBe(0);
  await expectNoPageOverflow(page);
});

test("詳細表はデスクトップ8行・モバイル5行の高さに収め、次行から内部スクロールする", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const expectedRows = expectedInformationRows(testInfo);
  let objectCount = expectedRows;
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready", { objectCount }))
  );

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card.getByText(/管理オブジェクトの詳細を表示/).click();

  const scrollRegion = page.getByTestId("system-tables-scroll-region");
  await expect(scrollRegion).toHaveAttribute("role", "region");
  await expect(scrollRegion).toHaveAttribute(
    "aria-label",
    "管理オブジェクト一覧（存在 53 / 必須 53）。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(scrollRegion.locator("tbody tr")).toHaveCount(expectedRows);

  const exactRows = await scrollRegion.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    const rootFontSize = Number.parseFloat(
      window.getComputedStyle(document.documentElement).fontSize
    );
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      maxHeight: Number.parseFloat(computed.maxHeight),
      overflowX: computed.overflowX,
      overflowY: computed.overflowY,
      rootFontSize,
    };
  });
  // 共有 DataTable の visibleRows は表頭と先頭 N 行の実測で高さを決める（#530）。
  const expectedMaxHeight = await measuredVisibleRowsHeight(scrollRegion, "tbody tr", expectedRows);
  expect(Math.abs(exactRows.maxHeight - expectedMaxHeight)).toBeLessThanOrEqual(1);
  expect(exactRows.scrollHeight).toBeLessThanOrEqual(exactRows.clientHeight + 2);
  expect(exactRows.overflowX).toBe("auto");
  expect(exactRows.overflowY).toBe("auto");

  objectCount = expectedRows + 1;
  await card.getByRole("button", { name: "最新情報を取得" }).click();
  await expect(scrollRegion.locator("tbody tr")).toHaveCount(expectedRows + 1);

  const overflowing = await scrollRegion.evaluate((node) => {
    const regionRect = node.getBoundingClientRect();
    const header = node.querySelector("thead");
    const rows = Array.from(node.querySelectorAll("tbody tr"));
    if (!header) throw new Error("system table header is missing");
    const headerRect = header.getBoundingClientRect();
    const visibleTop = headerRect.bottom;
    const visibleRows = rows.filter((row) => {
      const rect = row.getBoundingClientRect();
      return rect.top >= visibleTop - 1 && rect.bottom <= regionRect.bottom + 1;
    });
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      scrollWidth: node.scrollWidth,
      clientWidth: node.clientWidth,
      visibleRowCount: visibleRows.length,
      headerPosition: window.getComputedStyle(header).position,
    };
  });
  expect(overflowing.scrollHeight).toBeGreaterThan(overflowing.clientHeight);
  expect(overflowing.visibleRowCount).toBe(expectedRows);
  expect(overflowing.headerPosition).toBe("sticky");
  if (testInfo.project.name === "mobile-375") {
    expect(overflowing.scrollWidth).toBeGreaterThan(overflowing.clientWidth);
  }

  await scrollRegion.focus();
  await expect(scrollRegion).toBeFocused();
  await page.keyboard.press("PageDown");
  await expect.poll(() => scrollRegion.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);

  await scrollRegion.evaluate((node) => node.scrollTo({ top: node.scrollHeight }));
  const bottomState = await scrollRegion.evaluate((node) => {
    const regionRect = node.getBoundingClientRect();
    const header = node.querySelector("thead");
    const lastRow = node.querySelector("tbody tr:last-child");
    if (!header || !lastRow) throw new Error("system table rows are missing");
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
  await expectNoPageOverflow(page);
});

test("初期化中は重複操作を無効化し、成功後に Toast と ready 状態を表示する", async ({ page }) => {
  let status: SchemaStatus = "missing";
  let requestCount = 0;
  let requestBody: unknown = null;
  // 実行中の表示を確かめ終えるまで応答を止める（固定の待ち時間だと、負荷が高いと確認の前に完了して flaky になる）。
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables(status))
  );
  await page.route("**/api/settings/database/system-tables/initialize", async (route) => {
    requestCount += 1;
    requestBody = route.request().postDataJSON();
    await pending;
    status = "ready";
    await fulfill(route, {
      ...systemTables("ready"),
      operation: "initialized",
      applied_versions: [...APPLIED_VERSIONS],
      dropped_object_count: 0,
      created_object_count: EXPECTED_OBJECT_COUNT,
    });
  });

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  const initialize = card.getByRole("button", { name: "作成・更新" });
  await initialize.click();
  await expect(initialize).toBeDisabled();
  await expect(card.getByRole("button", { name: "すべて再作成" })).toBeDisabled();
  await expect(card.getByRole("button", { name: "最新情報を取得" })).toBeDisabled();
  release();
  await expect(page.getByText("システムテーブルを初期作成しました。")).toBeVisible();
  await expect(card.getByText("初期化済み", { exact: true })).toBeVisible();
  await expect(card.getByText("53 / 53", { exact: true })).toBeVisible();
  await card.getByText(/管理オブジェクトの詳細を表示/).click();
  // 適用済みの migration は件数だけを出す（#658）。
  await expect(card.getByTestId("system-tables-versions")).toHaveText(
    `適用済みの migration ${APPLIED_VERSIONS.length} 件 / 未適用・不一致 0 件`
  );
  await expect(
    page
      .getByRole("region", { name: "通知" })
      .getByRole("status")
      .filter({ hasText: "システムテーブルを初期作成しました。" })
  ).toHaveCount(1);
  expect(requestCount).toBe(1);
  expect(requestBody).toEqual({ recreate: false });
});

test("システムテーブル管理権限だけの利用者は初期化を実行できる", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "system-tables-operator",
      login_user_id: "system.tables.operator",
      display_name: "システムテーブル管理者",
      role_codes: ["SYSTEM_TABLES_OPERATOR"],
      is_system_admin: false,
      permissions: ["menu.settings_system_tables"],
    })
  );
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("partial"))
  );
  let requestBody: unknown = null;
  await page.route("**/api/settings/database/system-tables/initialize", (route) => {
    requestBody = route.request().postDataJSON();
    return fulfill(route, {
      ...systemTables("ready"),
      operation: "migrated",
      applied_versions: [...APPLIED_VERSIONS],
      dropped_object_count: 0,
      created_object_count: PARTIAL_MISSING_NAMES.size,
    });
  });

  await page.goto("/settings/system-tables");

  const card = page.locator("#system-tables");
  await expect(page).toHaveURL(/\/settings\/system-tables$/);
  await expect(card.getByText(/システムテーブル管理権限が必要/)).toHaveCount(0);
  const initialize = card.getByRole("button", { name: "作成・更新" });
  await expect(initialize).toBeEnabled();
  await expect(card.getByRole("button", { name: "すべて再作成" })).toBeVisible();

  await initialize.click();

  await expect(page.getByText("システムテーブルを更新しました。")).toBeVisible();
  expect(requestBody).toEqual({ recreate: false });
  await expectNoPageOverflow(page);
});

test("no-op Toast は文末で折り返し、通知領域・焦点・閉じる操作を統一する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready"))
  );
  await page.route("**/api/settings/database/system-tables/initialize", (route) =>
    fulfill(route, {
      ...systemTables("ready"),
      operation: "no_op",
      applied_versions: [...APPLIED_VERSIONS],
      dropped_object_count: 0,
      created_object_count: 0,
    })
  );

  await page.goto("/settings/system-tables");
  const initialize = page.locator("#system-tables").getByRole("button", { name: "作成・更新" });
  await initialize.focus();
  await initialize.click();

  const region = page.getByRole("region", { name: "通知" });
  await expect(region).toHaveAttribute("aria-live", "polite");
  const toastStatus = region.getByRole("status");
  await expect(toastStatus).toContainText("システムテーブルは最新です。変更はありません。");
  await expectToastPlacement(page);
  expect(
    await page.evaluate(() => document.activeElement?.closest('[role="region"]') != null)
  ).toBe(false);

  const sentenceSegments = toastStatus.locator("[data-message-sentence]");
  await expect(sentenceSegments).toHaveCount(2);
  await expect(sentenceSegments.nth(0)).toHaveText("システムテーブルは最新です。");
  await expect(sentenceSegments.nth(1)).toHaveText("変更はありません。");
  const segmentTops = await sentenceSegments.evaluateAll((nodes) =>
    nodes.map((node) => Math.round(node.getBoundingClientRect().top))
  );
  // #899: 通知は内容に合わせて 32rem まで広がるため、desktop では 2 文が 1 行に収まる（以前の 22rem 固定では 2 行）。
  // 375px では幅が足りず、文の途中ではなく文末（2 文目の前）で折り返す。
  if (testInfo.project.name === "mobile-375") {
    expect(segmentTops[1]).toBeGreaterThan(segmentTops[0]);
  } else {
    expect(segmentTops[1]).toBe(segmentTops[0]);
  }

  const close = toastStatus.getByRole("button", { name: "閉じる" });
  const closeBox = await close.boundingBox();
  expect(closeBox?.width ?? 0).toBeGreaterThanOrEqual(43.9);
  expect(closeBox?.height ?? 0).toBeGreaterThanOrEqual(43.9);
  await expectNoPageOverflow(page);

  await close.focus();
  await page.keyboard.press("Enter");
  await expect(toastStatus).toHaveCount(0);
});

test("全再作成は実行確認語の完全一致まで実行できない", { tag: "@responsive" }, async ({ page }) => {
  let recreateRequests = 0;
  let requestBody: unknown = null;
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready"))
  );
  await page.route("**/api/settings/database/system-tables/initialize", async (route) => {
    recreateRequests += 1;
    requestBody = route.request().postDataJSON();
    await fulfill(route, {
      ...systemTables("ready"),
      operation: "recreated",
      applied_versions: [...APPLIED_VERSIONS],
      dropped_object_count: EXPECTED_OBJECT_COUNT - 1,
      created_object_count: EXPECTED_OBJECT_COUNT - 1,
    });
  });

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  const trigger = card.getByRole("button", { name: "すべて再作成" });
  const confirmationField = card.getByTestId("execution-confirmation-field");
  const description = card.getByText("NL2SQL の中核データを削除して migration を最初から適用します。削除したデータは復元できません。", { exact: true });
  const [descriptionBox, confirmationBox] = await Promise.all([description.boundingBox(), confirmationField.boundingBox()]);
  expect(confirmationBox!.y).toBeGreaterThanOrEqual(descriptionBox!.y + descriptionBox!.height);
  if (page.viewportSize()!.width >= 1280) {
    const lines = await description.evaluate(node => node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight));
    expect(lines).toBeLessThan(1.1);
  }
  const field = card.getByRole("textbox", { name: "実行確認語" });

  // 未入力・不一致(ADMIN_EXECUTE を含む)では実行できない。
  await expect(trigger).toBeDisabled();
  await field.fill("ADMIN_EXECUTE");
  await expect(confirmationField.getByText("不一致")).toBeVisible();
  await expect(trigger).toBeDisabled();
  expect(recreateRequests).toBe(0);

  await field.fill("RECREATE_NL2SQL_SYSTEM_TABLES");
  await expect(confirmationField.getByText("確認済み")).toBeVisible();
  await expect(trigger).toBeEnabled();
  await trigger.click();
  await expect(page.getByText("システムテーブルをすべて再作成しました。")).toBeVisible();
  expect(recreateRequests).toBe(1);
  expect(requestBody).toEqual({
    recreate: true,
    confirmation: "RECREATE_NL2SQL_SYSTEM_TABLES",
  });
  // 成功後は確認語がクリアされ再度実行不可に戻る。
  await expect(trigger).toBeDisabled();
});

test("システムテーブル管理権限がない利用者は管理画面を開けない", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      role_codes: ["DB_VIEWER"],
      is_system_admin: false,
      permissions: ["menu.settings_database"],
    })
  );
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, systemTables("ready"))
  );

  await page.goto("/settings/system-tables");
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
  await expect(page.locator("#system-tables")).toHaveCount(0);
});

test("接続・操作失敗を操作領域で通知し、復旧方法を提示する", async ({ page }) => {
  let loadFails = true;
  let operationFailed = false;
  const longFailureDetail =
    `Oracle の対象オブジェクトのロックが 30 秒以内に解放されませんでした (ORA-00054)。` +
    `${"状態競合の原因を確認するための長い識別情報".repeat(24)}` +
    "実行中の schema refresh、Ontology、SQL生成評価 job を完了または停止してから、状態を再取得して再試行してください。";
  await page.route("**/api/settings/database/system-tables**", (route) =>
    loadFails
      ? fulfill(route, null, 503, ["Oracle に接続できませんでした (ORA-12514)。"])
      : fulfill(
          route,
          systemTables("partial", {
            operationStatus: operationFailed ? "failed" : "idle",
            lastErrorCode: "ORA-00054",
          })
        )
  );
  await page.route("**/api/settings/database/system-tables/initialize", (route) => {
    operationFailed = true;
    return fulfill(route, null, 409, [longFailureDetail], "ORA-00054");
  });

  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await expect(card.getByText("データベースに接続できません", { exact: true })).toBeVisible();
  await expect(
    card.getByText(
      "データベースにネットワーク経由で到達できないか、接続設定が正しくありません。データベース設定で接続ユーザー・パスワード・サービス名（DSN）と Wallet を確認してから、再試行してください。",
      { exact: true }
    )
  ).toHaveCount(0);
  await expect(
    card.getByText(
      "アップロード保存先・モデル・データベースの各設定ページは引き続き利用できます。",
      { exact: true }
    )
  ).toHaveCount(0);
  await expect(card.getByText("システムテーブルの状態を取得できません")).toHaveCount(0);
  await expect(card.getByText(/ORA-12514/)).toHaveCount(0);

  const settingsLink = card.getByRole("link", { name: "データベース設定を開く" });
  const retry = card.getByRole("button", { name: "再試行" });
  await expect(settingsLink).toHaveCount(0);
  await retry.focus();
  await expect(retry).toBeFocused();

  loadFails = false;
  await page.keyboard.press("Enter");
  await expect(
    card.locator("span").filter({ hasText: /^一部不足$/ }).first()
  ).toBeVisible();
  const initialize = card.getByRole("button", { name: "作成・更新" });
  await initialize.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("system-tables-operation-error")).toBeFocused();
  const operationAlert = card.getByRole("alert");
  await expect(operationAlert).toHaveCount(1);
  await expect(operationAlert).toContainText("ORA-00054");
  await expect(operationAlert).toContainText("30 秒以内に解放されませんでした");
  await expect(operationAlert).toContainText("schema refresh、Ontology、SQL生成評価 job");
  await expect(operationAlert).toContainText("状態を再取得して再試行");
  await expect(operationAlert).not.toContainText("前回の操作が完了していません");
  await expect(operationAlert.locator("[data-message-sentence]")).not.toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("再作成確認は再取得・失敗時に解除し、取得失敗中は操作しない", async ({ page }) => {
  let failStatus = false;
  let operations = 0;
  await page.route("**/api/settings/database/system-tables", (route) => failStatus
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "状態取得失敗" }) })
    : fulfill(route, systemTables("ready")));
  await page.route("**/api/settings/database/system-tables/initialize", async (route) => {
    operations += 1;
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "再作成失敗" }) });
  });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  const field = card.getByRole("textbox", { name: "実行確認語" });
  const recreate = card.getByRole("button", { name: "すべて再作成" });
  await field.fill("RECREATE_NL2SQL_SYSTEM_TABLES");
  await recreate.click();
  await expect.poll(() => operations).toBe(1);
  await expect(field).toHaveValue("");
  await expect(recreate).toBeDisabled();
  await expect(field).toBeEnabled();
  await field.fill("RECREATE_NL2SQL_SYSTEM_TABLES");
  failStatus = true;
  await card.getByRole("button", { name: "最新情報を取得" }).click();
  await expect(field).toHaveValue("");
  await expect(recreate).toBeDisabled();
  await expect(card.getByRole("button", { name: "作成・更新" })).toBeDisabled();
});

// ---- 台帳が最新でも欠けた列（#2458） --------------------------------------------------

test(
  "台帳が最新でも既存の表に無い列を更新必要として並べ、更新で直らなければ列の名前で止める（#2458）",
  { tag: "@responsive" },
  async ({ page }, testInfo) => {
    const missingColumns = [
      { table_name: "NL2SQL_SCHEMA_REFRESH_JOBS", column_name: "WORKER_ID" },
      { table_name: "NL2SQL_ONTOLOGY_REVISIONS", column_name: "PROFILE_ID" },
    ];
    const postconditionMessage =
      "システムテーブル操作後も必須の列が不足しています" +
      "（NL2SQL_SCHEMA_REFRESH_JOBS.WORKER_ID, NL2SQL_ONTOLOGY_REVISIONS.PROFILE_ID）。" +
      "更新を再試行しても直りません。配備の手順の既存環境の更新手順を確認してください。";
    let initializeCalls = 0;
    await page.route("**/api/settings/database/system-tables", (route) =>
      fulfill(route, {
        ...systemTables("outdated"),
        applied_versions: [...APPLIED_VERSIONS],
        pending_versions: [],
        missing_columns: missingColumns,
      })
    );
    await page.route("**/api/settings/database/system-tables/initialize", (route) => {
      initializeCalls += 1;
      return fulfill(route, null, 500, [postconditionMessage], "SCHEMA_POSTCONDITION_FAILED");
    });

    await page.goto("/settings/system-tables");
    const card = page.locator("#system-tables");
    await expect(card.getByText("更新必要", { exact: true }).first()).toBeVisible();
    const missing = card.getByTestId("system-tables-missing-columns");
    await expect(missing).toContainText("既存のテーブルに必須の列が 2 件ありません");
    await expect(missing.getByRole("listitem")).toHaveText([
      "NL2SQL_SCHEMA_REFRESH_JOBS.WORKER_ID",
      "NL2SQL_ONTOLOGY_REVISIONS.PROFILE_ID",
    ]);
    await expectNoPageOverflow(page);
    await missing.screenshot({
      path: testInfo.outputPath(`system-tables-missing-columns-${testInfo.project.name}.png`),
    });

    await card.getByRole("button", { name: "作成・更新" }).click();
    const operationAlert = card.getByRole("alert");
    await expect(operationAlert).toHaveCount(1);
    await expect(operationAlert).toContainText("NL2SQL_SCHEMA_REFRESH_JOBS.WORKER_ID");
    await expect(operationAlert).toContainText("更新を再試行しても直りません");
    expect(initializeCalls).toBe(1);
    // 列は戻っていないので、一覧は残す。
    await expect(missing).toBeVisible();
    await expectNoPageOverflow(page);
  }
);
