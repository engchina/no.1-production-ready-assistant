import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { expectedControlHeight } from "./_helpers/control-height";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectSingleSpinner } from "./_helpers/single-spinner";
import { expectCompactSortHeaders } from "./_helpers/sort-header";
import { chooseSelectFieldOption } from "./_helpers/select-field";
import { remToPx, SUBPIXEL_TOLERANCE } from "./_helpers/spacing";
import {
  closeListToolbarFilters,
  expectDbObjectToolbarLayout,
  listToolbarUsesSheet,
  openListToolbarFilters,
} from "./_helpers/db-object-toolbar";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => {
  await mockDatabaseGateReady(page);
  // 共通 job 表示が購読する API も隔離し、未 mock の 404 を console 検証に混ぜない。
});

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

function createRequestGate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

async function clickPageHeaderAction(page: Page, testId: string, name: string) {
  const actions = page.getByTestId(testId);
  await expect(actions).toBeVisible();
  const visibleButton = actions.getByRole("button", { name, exact: true });
  if (await visibleButton.isVisible()) {
    await visibleButton.click();
    return;
  }
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

async function expectNoHorizontalScroll(page: Page) {
  const size = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(size.scrollWidth).toBeLessThanOrEqual(size.width + 1);
}

async function expectMainScrollDoesNotExposeTrailingBlank(panel: Locator) {
  const metrics = await panel.evaluate((node) => {
    // 共有 AppShell の <main id="pr-main"> は唯一の main ランドマークなので aria-label を持たない。
    const main = document.querySelector<HTMLElement>("main");
    if (!main) return null;
    main.scrollTop = main.scrollHeight;
    const mainRect = main.getBoundingClientRect();
    const panelRect = node.getBoundingClientRect();
    const panelBottomInScrollContent = panelRect.bottom - mainRect.top + main.scrollTop;
    return {
      clientHeight: main.clientHeight,
      scrollHeight: main.scrollHeight,
      // 画面に収まる工程では余白＝残りの viewport なので、超過分だけを測る。
      trailingBlank: main.scrollHeight - Math.max(panelBottomInScrollContent, main.clientHeight),
    };
  });
  expect(metrics).not.toBeNull();
  expect(metrics!.trailingBlank).toBeLessThanOrEqual(48);
}

async function dispatchDevToolsWebVitalsStartTimeError(page: Page) {
  return page.evaluate(() => {
    const error = new Error("Cannot read properties of undefined (reading 'startTime')");
    error.stack = [
      "TypeError: Cannot read properties of undefined (reading 'startTime')",
      "    at et.reportAllChanges (<anonymous>:2:19429)",
      "    at n.timeout (<anonymous>:2:5652)",
    ].join("\n");
    const event = new ErrorEvent("error", {
      cancelable: true,
      message: "Uncaught TypeError: Cannot read properties of undefined (reading 'startTime')",
      filename: "VM327",
      lineno: 2,
      colno: 19429,
      error,
    });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
}

async function pickerRowNames(list: Locator) {
  return list.locator("tbody tr").evaluateAll((rows) =>
    rows.map((row) => row.querySelector("button span")?.textContent?.trim() ?? "")
  );
}

async function expectContentActionsRightAligned(actions: Locator) {
  const metrics = await actions.evaluate((node) => {
    const group = node.querySelector('[role="group"]');
    const firstButton = group?.querySelector("button");
    const panel = node.closest('[role="tabpanel"]');
    const code = panel?.querySelector("pre");
    if (!group || !firstButton || !panel || !code) return null;
    const buttonRect = firstButton.getBoundingClientRect();
    const groupRect = group.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const codeRect = code.getBoundingClientRect();
    return {
      firstButtonLeft: buttonRect.left,
      codeLeft: codeRect.left,
      groupLeft: groupRect.left,
      groupRight: groupRect.right,
      panelLeft: panelRect.left,
      panelRight: panelRect.right,
    };
  });
  expect(metrics).not.toBeNull();
  expect(metrics!.groupRight).toBeGreaterThanOrEqual(metrics!.panelRight - 20);
  expect(metrics!.groupLeft).toBeGreaterThan(metrics!.panelLeft);
  expect(metrics!.firstButtonLeft).toBeGreaterThan(metrics!.codeLeft);
}

const scenarios = [
  {
    title: "テーブル一覧",
    path: "/table-management",
    apiPath: "**/api/nl2sql/db-admin/tables",
    prefix: "TABLE",
    objectType: "table",
  },
  {
    title: "ビュー一覧",
    path: "/view-management",
    apiPath: "**/api/nl2sql/db-admin/views",
    prefix: "VIEW",
    objectType: "view",
  },
] as const;

const metadataScenarios = [
  {
    title: "コメント管理",
    path: "/comment-management",
    idPrefix: "comment-management",
  },
  {
    title: "アノテーション管理",
    path: "/annotation-management",
    idPrefix: "annotation-management",
  },
  {
    title: "ドメイン管理",
    path: "/domain-management",
    idPrefix: "domain-management",
  },
] as const;

type MockAdminObjectItem = {
  name: string;
  owner: string;
  qualified_name?: string;
  object_type: "table" | "view";
  row_count: number | null;
  comment: string;
};

const sampleProfile = {
  id: "default",
  name: "既定プロファイル",
  category: "既定",
  description: "管理画面標準化の表示確認",
  allowed_tables: ["TABLE_01"],
  allowed_views: ["VIEW_01"],
  glossary: { 売上: "SALES.AMOUNT" },
  sql_rules: ["SELECT のみ"],
  default_row_limit: 100,
  safety_policy: "select_only",
  few_shot_examples: [{ question: "売上を見せて", sql: "SELECT * FROM TABLE_01" }],
  archived: false,
};

const managementCatalog = {
  refreshed_at: "2026-06-21T10:00:00.000Z",
  tables: [
    {
      table_name: "TABLE_01",
      logical_name: "テーブル01",
      owner: "APP",
      table_type: "TABLE",
      comment: "標準化テーブル",
      row_count: 10,
      columns: [],
      constraints: [],
    },
    {
      table_name: "VIEW_01",
      logical_name: "ビュー01",
      owner: "APP",
      table_type: "VIEW",
      comment: "標準化ビュー",
      row_count: null,
      columns: [],
      constraints: [],
    },
  ],
};

const tableObjects = {
  runtime: "deterministic",
  items: [
    { name: "TABLE_01", owner: "APP", object_type: "table", row_count: 10, comment: "標準化テーブル" },
  ],
  warnings: [],
};

const viewObjects = {
  runtime: "deterministic",
  items: [
    { name: "VIEW_01", owner: "APP", object_type: "view", row_count: null, comment: "標準化ビュー" },
  ],
  warnings: [],
};

const prepareManagementScenarios = [
  {
    title: "データ管理",
    path: "/data-management",
    idPrefix: "data-management",
    tabs: [
      // データの表示は一覧 + 詳細（ListDetailLayout）で、一覧と詳細の列がそれぞれ枠を持つので外側の枠を付けない（#2315）。
      { id: "preview", tabName: "テーブル・ビューデータの表示", listDetail: true },
      { id: "csv", tabName: "Excel/CSV アップロード(既存テーブル)" },
    ],
  },
  {
    title: "検証用サンプルデータ",
    path: "/sample-data",
    idPrefix: "sample-data",
    tabs: [
      { id: "import", tabName: "取り込み実行" },
      { id: "delete", tabName: "削除実行" },
    ],
  },
] as const;

async function mockObjectManagementApi(
  page: Page,
  scenario: (typeof scenarios)[number],
  options: { itemCount?: number; columnCount?: number; extraItems?: MockAdminObjectItem[] } = {}
) {
  const itemCount = options.itemCount ?? 30;
  const columns = Array.from({ length: options.columnCount ?? 0 }, (_, index) => ({
    column_name: `COLUMN_${String(index + 1).padStart(2, "0")}`,
    logical_name: `列 ${index + 1}`,
    data_type: index % 3 === 0 ? "VARCHAR2(200)" : "NUMBER",
    nullable: index % 2 === 0,
    comment: `列情報の高さ確認 ${index + 1}`,
    sample_values: [`sample-${index + 1}`],
  }));
  const generatedItems: MockAdminObjectItem[] = Array.from({ length: itemCount }, (_, index) => ({
    name: `${scenario.prefix}_${String(index + 1).padStart(2, "0")}`,
    owner: "APP",
    qualified_name: `APP.${scenario.prefix}_${String(index + 1).padStart(2, "0")}`,
    object_type: scenario.objectType,
    row_count: null,
    comment: "",
  }));
  const items = [...generatedItems, ...(options.extraItems ?? [])];
  const typeItems = items.filter((item) => item.object_type === scenario.objectType);

  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, {
      refreshed_at: "2026-06-21T10:00:00.000Z",
      tables: [],
    })
  );
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [
        {
          owner: "APP",
          is_current: true,
          table_count: typeItems.filter((item) => item.object_type === "table").length,
          view_count: typeItems.filter((item) => item.object_type === "view").length,
        },
      ],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route(scenario.apiPath, (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: typeItems,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: items.length,
      table_count: scenario.objectType === "table" ? items.length : 0,
      view_count: scenario.objectType === "view" ? items.length : 0,
      next_cursor: null,
      refreshed_at: "2026-06-21T10:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "TABLE_01");
    return fulfillJson(route, {
      name,
      owner: "APP",
      qualified_name: `APP.${name}`,
      object_type: "table",
      row_count: null,
      comment: "",
      columns,
      ddl: `CREATE TABLE "${name}" ("ID" NUMBER)`,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/views/*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "VIEW_01");
    return fulfillJson(route, {
      name,
      owner: "APP",
      qualified_name: `APP.${name}`,
      object_type: "view",
      row_count: null,
      comment: "",
      columns,
      ddl: `CREATE OR REPLACE VIEW "${name}" AS SELECT 1 AS ID FROM DUAL`,
      warnings: [],
    });
  });
}

async function mockMetadataManagementApi(
  page: Page,
  options: {
    empty?: boolean;
    extraTableItems?: MockAdminObjectItem[];
    extraViewItems?: MockAdminObjectItem[];
  } = {}
) {
  const baseTableItems: MockAdminObjectItem[] = options.empty
    ? []
    : Array.from({ length: 20 }, (_, index) => ({
        name: `META_TABLE_${String(index + 1).padStart(2, "0")}`,
        owner: "APP",
        object_type: "table",
        row_count: index,
        comment: `コメント対象テーブル ${index + 1}`,
      }));
  const baseViewItems: MockAdminObjectItem[] = options.empty
    ? []
    : Array.from({ length: 10 }, (_, index) => ({
        name: `META_VIEW_${String(index + 1).padStart(2, "0")}`,
        owner: "APP",
        object_type: "view",
        row_count: null,
        comment: `コメント対象ビュー ${index + 1}`,
      }));
  const tableItems = [...baseTableItems, ...(options.extraTableItems ?? [])];
  const viewItems = [...baseViewItems, ...(options.extraViewItems ?? [])];
  const catalog = {
    refreshed_at: "2026-06-21T10:00:00.000Z",
    tables: [],
  };

  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, catalog));
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [
        { owner: "APP", is_current: true, table_count: tableItems.length, view_count: viewItems.length },
      ],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: tableItems,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: viewItems,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const items = [...tableItems, ...viewItems];
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: items.length,
      table_count: tableItems.length,
      view_count: viewItems.length,
      next_cursor: null,
      refreshed_at: catalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });
}

async function mockDataManagementApi(page: Page) {
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, managementCatalog));
  await page.route("**/api/nl2sql/db-admin/tables", (route) => fulfillJson(route, tableObjects));
  await page.route("**/api/nl2sql/db-admin/views", (route) => fulfillJson(route, viewObjects));
  await page.route("**/api/nl2sql/db-admin/preview-data", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      sql: 'SELECT * FROM "TABLE_01" FETCH FIRST 10 ROWS ONLY',
      results: {
        columns: ["ID", "NAME"],
        rows: [{ ID: 1, NAME: "標準化サンプル" }],
        total: 1,
      },
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const items = [...tableObjects.items, ...viewObjects.items];
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: items.length,
      table_count: tableObjects.items.length,
      view_count: viewObjects.items.length,
      next_cursor: null,
      refreshed_at: managementCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });
}

async function mockSortableDataManagementApi(page: Page) {
  const items = [
    { name: "Z_PAYMENTS", owner: "BILLING", object_type: "table", row_count: 20, comment: "支払一覧" },
    { name: "A_VIEW", owner: "APP", object_type: "view", row_count: null, comment: "ビュー対象" },
    { name: "M_EMPTY", owner: "APP", object_type: "table", row_count: 0, comment: "空テーブル" },
    { name: "B_AUDIT", owner: "APP", object_type: "table", row_count: 3, comment: "監査ログ" },
  ];
  const tableItems = items.filter((item) => item.object_type === "table");
  const viewItems = items.filter((item) => item.object_type === "view");

  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, managementCatalog));
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: tableItems, warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: viewItems, warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "all";
    const sourceItems = type === "table" ? tableItems : type === "view" ? viewItems : items;
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: sourceItems,
      total: sourceItems.length,
      table_count: type === "view" ? 0 : tableItems.length,
      view_count: type === "table" ? 0 : viewItems.length,
      next_cursor: null,
      refreshed_at: managementCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });
}

async function mockPagedDbAdminObjectsApi(page: Page) {
  const tableItems = Array.from({ length: 149 }, (_, index) => ({
    name: `PAGED_TABLE_${String(index + 1).padStart(3, "0")}`,
    owner: "APP",
    object_type: "table",
    row_count: index + 1,
    comment: `ページング対象テーブル ${index + 1}`,
  }));
  const viewItems = Array.from({ length: 2 }, (_, index) => ({
    name: `PAGED_VIEW_${String(index + 1).padStart(3, "0")}`,
    owner: "APP",
    object_type: "view",
    row_count: null,
    comment: `ページング対象ビュー ${index + 1}`,
  }));

  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, {
      refreshed_at: "2026-06-21T10:00:00.000Z",
      tables: [],
    })
  );
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [
        { owner: "APP", is_current: true, table_count: tableItems.length, view_count: viewItems.length },
      ],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "all";
    const cursor = url.searchParams.get("cursor") ?? "";
    // backend と同じく検索語（q）で名前・コメントを絞る。無視して先頭の 100 件を返すと、画面は読み込んだ候補を
    // 検索語で絞るため 0 件になり、検索の結果を確かめるテストが応答の前の一瞬にしか通らない（#655）。
    const query = (url.searchParams.get("q") ?? "").toLowerCase();
    const matches = (item: { name: string; comment: string }) =>
      !query || `${item.name} ${item.comment}`.toLowerCase().includes(query);
    const matchedTables = tableItems.filter(matches);
    const matchedViews = viewItems.filter(matches);
    const sourceItems =
      type === "table" ? matchedTables : type === "view" ? matchedViews : [...matchedTables, ...matchedViews];
    const items = cursor ? sourceItems.slice(100) : sourceItems.slice(0, 100);
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: sourceItems.length,
      table_count: type === "view" ? 0 : matchedTables.length,
      view_count: type === "table" ? 0 : matchedViews.length,
      next_cursor: !cursor && sourceItems.length > 100 ? "paged-db-admin-objects-2" : null,
      refreshed_at: "2026-06-21T10:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables/PAGED_TABLE_*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "PAGED_TABLE_001");
    return fulfillJson(route, {
      name,
      owner: "APP",
      object_type: "table",
      row_count: 1,
      comment: "ページング対象テーブル",
      columns: [],
      ddl: "",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/views/PAGED_VIEW_*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "PAGED_VIEW_001");
    return fulfillJson(route, {
      name,
      owner: "APP",
      object_type: "view",
      row_count: null,
      comment: "ページング対象ビュー",
      columns: [],
      ddl: "",
      warnings: [],
    });
  });
}

async function mockOwnerFilterDbAdminObjectsApi(page: Page) {
  const items = [
    {
      name: "ORDERS_TABLE_01",
      owner: "APP",
      object_type: "table",
      row_count: 3,
      comment: "受注テーブル",
    },
    {
      name: "LEDGER_TABLE_01",
      owner: "BILLING",
      object_type: "table",
      row_count: 7,
      comment: "元帳テーブル",
    },
    {
      name: "ORDERS_VIEW_01",
      owner: "APP",
      object_type: "view",
      row_count: null,
      comment: "受注ビュー",
    },
    {
      name: "LEDGER_VIEW_01",
      owner: "BILLING",
      object_type: "view",
      row_count: null,
      comment: "元帳ビュー",
    },
  ];
  const tableItems = items.filter((item) => item.object_type === "table");
  const viewItems = items.filter((item) => item.object_type === "view");

  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, {
      refreshed_at: "2026-06-21T10:00:00.000Z",
      tables: [],
    })
  );
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [
        { owner: "APP", is_current: true, table_count: 1, view_count: 1 },
        { owner: "BILLING", is_current: false, table_count: 1, view_count: 1 },
      ],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "all";
    const ownerPrefix = (url.searchParams.get("owner_prefix") ?? "").toUpperCase();
    const query = (url.searchParams.get("q") ?? "").toLowerCase();
    const sourceItems =
      type === "table" ? tableItems : type === "view" ? viewItems : items;
    const filteredItems = sourceItems.filter((item) => {
      if (ownerPrefix && !item.owner.startsWith(ownerPrefix)) return false;
      return !query || `${item.name} ${item.comment}`.toLowerCase().includes(query);
    });
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: filteredItems,
      total: filteredItems.length,
      table_count: type === "view" ? 0 : filteredItems.filter((item) => item.object_type === "table").length,
      view_count: type === "table" ? 0 : filteredItems.filter((item) => item.object_type === "view").length,
      next_cursor: null,
      refreshed_at: "2026-06-21T10:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables/*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "ORDERS_TABLE_01");
    const owner = new URL(route.request().url()).searchParams.get("owner") ?? "APP";
    return fulfillJson(route, {
      name,
      owner,
      object_type: "table",
      row_count: name.startsWith("BILLING") ? 7 : 3,
      comment: "",
      columns: [],
      ddl: "",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/views/*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "ORDERS_VIEW_01");
    const owner = new URL(route.request().url()).searchParams.get("owner") ?? "APP";
    return fulfillJson(route, {
      name,
      owner,
      object_type: "view",
      row_count: null,
      comment: "",
      columns: [],
      ddl: "",
      warnings: [],
    });
  });
}

async function mockSampleDataApi(page: Page) {
  await page.route("**/api/nl2sql/sample-data", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      profile_id: "default",
      confirmation: "ADMIN_EXECUTE",
      objects: ["TABLE_01", "VIEW_01"],
      imported_objects: ["TABLE_01"],
      sql: {
        tables: ['CREATE TABLE "TABLE_01" ("ID" NUMBER)'],
        views: ['CREATE VIEW "VIEW_01" AS SELECT 1 AS ID FROM DUAL'],
        data: ['INSERT INTO "TABLE_01" ("ID") VALUES (1)'],
        delete: ['DROP TABLE "TABLE_01"'],
      },
      warnings: [],
    })
  );
}

async function mockProfileManagementApi(page: Page) {
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, managementCatalog));
  await page.route("**/api/nl2sql/db-admin/views", (route) => fulfillJson(route, viewObjects));
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, [sampleProfile]));
}

async function mockPrepareManagementApi(page: Page, path: string) {
  if (path === "/data-management") {
    await mockDataManagementApi(page);
    return;
  }
  if (path === "/sample-data") {
    await mockSampleDataApi(page);
    return;
  }
  if (path === "/profiles") {
    await mockProfileManagementApi(page);
  }
}

async function expectSingleLine(locator: Locator) {
  const lineCount = await locator.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
    range.detach();
    // 識別子は <wbr> で text node が分かれ、同じ行でも rect が複数になるため行の top で数える。
    return new Set(rects.map((rect) => Math.round(rect.top))).size;
  });
  expect(lineCount).toBeLessThanOrEqual(1);
}

/**
 * 一覧の各行で、セルの内容（バッジ・文字）が自セルの右端と、同じ行に並ぶ次のセルの左端を超えないことを実測する。
 * sr-only（absolute）と非表示要素は除外する。
 */
async function expectRowCellsDoNotOverlap(rows: Locator) {
  expect(await rows.count()).toBeGreaterThan(0);
  const problems = await rows.evaluateAll((rowNodes) => {
    const found: string[] = [];
    const isMeasured = (element: Element) => {
      const style = getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden" && style.position !== "absolute";
    };
    for (const row of rowNodes) {
      const cells = Array.from(row.children).filter((cell) => isMeasured(cell) && cell.getBoundingClientRect().width > 0);
      cells.forEach((cell, index) => {
        const cellBox = cell.getBoundingClientRect();
        let contentRight = cellBox.left;
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
          acceptNode: (node) =>
            node instanceof Element && !isMeasured(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
        });
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const rects = node instanceof Element
            ? [node.getBoundingClientRect()]
            : (() => {
                const range = document.createRange();
                range.selectNodeContents(node);
                return Array.from(range.getClientRects());
              })();
          for (const rect of rects) {
            if (rect.width > 0 && rect.height > 0) contentRight = Math.max(contentRight, rect.right);
          }
        }
        const label = `${row.textContent?.trim().slice(0, 40)} / cell ${index}`;
        if (contentRight > cellBox.right + 1) found.push(`${label}: content ${contentRight} > cell ${cellBox.right}`);
        const next = cells[index + 1]?.getBoundingClientRect();
        const sameLine = next && next.top < cellBox.bottom - 1 && next.bottom > cellBox.top + 1;
        if (next && sameLine && contentRight > next.left + 1) {
          found.push(`${label}: content ${contentRight} > next cell ${next.left}`);
        }
      });
    }
    return found;
  });
  expect(problems).toEqual([]);
}

async function expectNoStatusIcon(badge: Locator) {
  await expect(badge).toBeVisible();
  await expect(badge.locator("svg")).toHaveCount(0);
}

/**
 * 1 ペイン（xl 未満。ListDetailLayout の詳細だけの表示。#2281）なら「一覧へ戻る」で一覧へ戻る。2 ペインでは何もしない。
 * 詳細を出したまま一覧の行・検索欄を操作する前に呼ぶ。
 */
async function returnToListIfSinglePane(page: Page, idPrefix: string) {
  const back = page.getByTestId(`${idPrefix}-detail-back`);
  if (await back.isVisible()) {
    await back.click();
    await expect(page.getByTestId(`list-detail-${idPrefix}-list`)).toBeVisible();
  }
}



async function topLevelPanelStyle(page: Page, id: string, idPrefix = "table-management") {
  return page.locator(`#${idPrefix}-panel-${id}`).evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      backgroundColor: computed.backgroundColor,
      borderTopColor: computed.borderTopColor,
      borderTopWidth: computed.borderTopWidth,
      borderRadius: computed.borderRadius,
      boxShadow: computed.boxShadow,
      display: computed.display,
      gap: computed.gap,
      paddingTop: computed.paddingTop,
    };
  });
}

async function compactVisualStyle(locator: Locator) {
  return locator.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      alignItems: computed.alignItems,
      backgroundColor: computed.backgroundColor,
      borderTopColor: computed.borderTopColor,
      borderTopWidth: computed.borderTopWidth,
      color: computed.color,
      display: computed.display,
      fontSize: computed.fontSize,
      fontWeight: computed.fontWeight,
      gap: computed.gap,
      minHeight: computed.minHeight,
      paddingTop: computed.paddingTop,
      paddingBottom: computed.paddingBottom,
    };
  });
}

test("コメント付きテーブル一覧でも読込・取得エラー・再試行後の空状態を表示する", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0], { itemCount: 0 });
  const gate = createRequestGate();
  let fail = true;
  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    await gate.promise;
    if (fail) {
      await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "一覧の取得に失敗しました。" }) });
    } else {
      await fulfillJson(route, { runtime: "deterministic", items: [], total: 0, next_cursor: null, warnings: [] });
    }
  });
  await page.goto("/table-management");
  await expect(page.getByTestId("table-management-list-skeleton")).toBeVisible();
  // 初回の読込のスピナーは一覧の読込表示の 1 つだけ（詳細の Skeleton は経過時間だけ、ヘッダーの
  // 「最新情報を取得」は狭い画面では「その他の操作」の中なので回さない。#416）。
  await expectSingleSpinner(page, page.getByTestId("table-management-list-skeleton"));
  gate.release();
  await expect(page.getByText("一覧の取得に失敗しました。", { exact: true })).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "再試行", exact: true }).click();
  await expect(page.getByText("テーブルがありません", { exact: true })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

for (const scenario of scenarios) {
  test(`${scenario.title}は Profile 一覧と同じ補足表示でコメントを名前の下に表示する`, async ({ page }, testInfo) => {
    const shortComment = "請求書の金額と支払期日を管理します。";
    const longComment = "長い日本語のオブジェクトコメントです。".repeat(12) + "LONG_IDENTIFIER_".repeat(12);
    await mockObjectManagementApi(page, scenario, {
      itemCount: 1,
      extraItems: [
        { name: "INVOICES", owner: "APP", object_type: scenario.objectType, row_count: 10, comment: shortComment },
        { name: "LONG_COMMENT", owner: "APP", object_type: scenario.objectType, row_count: 20, comment: longComment },
        { name: "WHITESPACE", owner: "APP", object_type: scenario.objectType, row_count: null, comment: "   " },
      ],
    });
    await page.goto(scenario.path);
    const idPrefix = `${scenario.objectType}-management`;
    const grid = page.getByTestId(`${idPrefix}-grid`);
    await expect(grid.locator("tbody tr")).toHaveCount(4);
    await expect(grid.getByRole("columnheader", { name: /コメント/ })).toHaveCount(0);
    for (const [name, comment] of [["INVOICES", shortComment], ["LONG_COMMENT", longComment], [`${scenario.prefix}_01`, "-"], ["WHITESPACE", "-"]]) {
      const button = grid.getByRole("button", { name: `APP.${name} を表示` });
      const text = button.locator(":scope > span").nth(1);
      await expect(text).toHaveText(comment);
      await expect(button).toHaveAccessibleDescription(comment);
      const layout = await button.evaluate((node) => {
        // 共有 RowTitleButton（#421）: 題名と補足の枠の中に、名前とコメント（aria-describedby の先）を置く。
        const name = node.querySelector(":scope > span")!;
        const comment = document.getElementById(node.getAttribute("aria-describedby")!)!;
        const nameBox = name.getBoundingClientRect();
        const commentBox = comment.getBoundingClientRect();
        const cellBox = node.closest("td")!.getBoundingClientRect();
        const style = getComputedStyle(comment);
        return { below: commentBox.top >= nameBox.bottom, contained: commentBox.left >= cellBox.left && commentBox.right <= cellBox.right,
          clamp: style.webkitLineClamp, height: commentBox.height, lineHeight: parseFloat(style.lineHeight) };
      });
      expect(layout.below).toBe(true);
      expect(layout.contained).toBe(true);
      expect(layout.clamp).toBe("2");
      expect(layout.height).toBeLessThanOrEqual(layout.lineHeight * 2 + 1);
    }
    await expectRowCellsDoNotOverlap(grid.locator("tbody tr"));
    // 件数は見出しのバッジではなく、ツールバーの ListCount（#1891）。
    await expect(page.locator(`section[aria-labelledby='${scenario.objectType}-grid-heading'] [data-status-variant]`)).toHaveCount(0);
    await expect(page.getByTestId(`${idPrefix}-toolbar`).locator("[data-list-count]")).toHaveText("全 4 件");
    await grid.getByText(shortComment, { exact: true }).click();
    await expect(page.getByTestId(`${idPrefix}-detail-header`)).toContainText("INVOICES");
    await returnToListIfSinglePane(page, idPrefix);
    const longButton = grid.getByRole("button", { name: "APP.LONG_COMMENT を表示" });
    await longButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId(`${idPrefix}-detail-header`)).toContainText("LONG_COMMENT");
    await returnToListIfSinglePane(page, idPrefix);
    if (testInfo.project.name !== "mobile-375") await expect(longButton).toHaveAttribute("aria-current", "true");
    const gridScroll = await page.getByTestId("db-admin-object-list").evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(gridScroll).toBeLessThanOrEqual(1);
    await grid.screenshot({ path: testInfo.outputPath(`${scenario.objectType}-name-comments.png`) });
    await expectNoHorizontalScroll(page);
    await page.getByRole("searchbox", { name: "検索" }).fill("支払期日");
    await expect(grid.locator("tbody tr")).toHaveCount(1);
    await expect(grid.getByText(shortComment, { exact: true })).toBeVisible();
  });
}

test("データ管理の対象一覧はコメントを名前の下に表示し、種類・行数が隣の列に重ならない", async ({ page }, testInfo) => {
  const shortComment = "伝票の処理履歴を保持します。";
  const longComment = "長い日本語のテーブルコメントです。".repeat(10);
  const items = [
    { name: "DENPYO_ACTIVITY_LOG", owner: "ADMIN", object_type: "table", row_count: 266, comment: shortComment },
    { name: "LONG_COMMENT_TABLE", owner: "ADMIN", object_type: "table", row_count: null, comment: longComment },
    { name: "EMPTY_COMMENT", owner: "ADMIN", object_type: "table", row_count: 1234567, comment: "  " },
    { name: "ORDER_SUMMARY_V", owner: "ADMIN", object_type: "view", row_count: null, comment: "受注サマリ" },
  ];
  const tableItems = items.filter((item) => item.object_type === "table");
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, managementCatalog));
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "all";
    const q = (url.searchParams.get("q") ?? url.searchParams.get("search") ?? "").toLowerCase();
    const source = (type === "table" ? tableItems : items).filter((item) =>
      !q || `${item.name} ${item.comment}`.toLowerCase().includes(q)
    );
    return fulfillJson(route, {
      runtime: "deterministic", owner: "ADMIN", items: source, total: source.length,
      table_count: source.filter((item) => item.object_type === "table").length,
      view_count: source.filter((item) => item.object_type === "view").length,
      next_cursor: null, refreshed_at: managementCatalog.refreshed_at, catalog_version: 1, warnings: [],
    });
  });
  await page.goto("/data-management");

  // 件数は見出しのバッジではなく、ツールバーの ListCount 1 か所（#1891）。行の種類のバッジは数えない（#2315）。
  await expect(page.getByTestId("data-management-preview-toolbar").locator("[data-status-variant]")).toHaveCount(0);
  await expect(page.locator("#data-preview-list-heading [data-status-variant]")).toHaveCount(0);
  await expect(page.getByTestId("data-management-preview-toolbar").locator("[data-list-count]")).toHaveText("全 4 件");
  const previewList = page.getByTestId("data-management-preview-grid");
  await expect(previewList.locator("tbody tr")).toHaveCount(4);
  for (const [name, comment] of [
    ["DENPYO_ACTIVITY_LOG", shortComment],
    ["LONG_COMMENT_TABLE", longComment],
    ["EMPTY_COMMENT", "-"],
    ["ORDER_SUMMARY_V", "受注サマリ"],
  ]) {
    const button = previewList.getByRole("button", { name: `ADMIN.${name} を選択` });
    await expect(button).toHaveAccessibleDescription(comment);
    const layout = await button.evaluate((node) => {
      // 共有 RowTitleButton（#421）: 題名と補足の枠の中に、名前とコメント（aria-describedby の先）を置く。
      const nameNode = node.querySelector(":scope > span")!;
      const commentNode = document.getElementById(node.getAttribute("aria-describedby")!)!;
      const nameBox = nameNode.getBoundingClientRect();
      const commentBox = commentNode.getBoundingClientRect();
      const style = getComputedStyle(commentNode);
      return { below: commentBox.top >= nameBox.bottom - 1, clamp: style.webkitLineClamp, height: commentBox.height, lineHeight: parseFloat(style.lineHeight), text: commentNode.textContent };
    });
    expect(layout.text).toBe(comment);
    expect(layout.below).toBe(true);
    expect(layout.clamp).toBe("2");
    expect(layout.height).toBeLessThanOrEqual(layout.lineHeight * 2 + 1);
  }
  for (const kind of ["テーブル", "ビュー"]) {
    await expectNoStatusIcon(previewList.locator("[data-status-variant]").filter({ hasText: kind, visible: true }).first());
  }
  await expectRowCellsDoNotOverlap(previewList.locator("tbody tr"));
  // 識別子は語の途中ではなく . / _ の位置で折り返す（1 行に収まる場合は折り返さない）。
  const nameBreaks = await previewList.getByRole("button", { name: "ADMIN.DENPYO_ACTIVITY_LOG を選択" })
    // 共有 RowTitleButton（#421）の題名の枠の中の識別子（DbObjectName）。
    .locator(":scope > span > span").first()
    .evaluate((node) => Array.from(node.childNodes).filter((child) => child.nodeType === Node.TEXT_NODE).map((child) => child.textContent));
  expect(nameBreaks).toEqual(["ADMIN.", "DENPYO_", "ACTIVITY_", "LOG"]);
  await previewList.screenshot({ path: testInfo.outputPath("data-preview-name-comments.png") });

  await previewList.getByText(shortComment, { exact: true }).click();
  // 選択は URL が持つ（375px の 1 ペインでは選ぶと一覧が隠れる。#2315）。
  await expect(page).toHaveURL(/[?&]object=ADMIN\.DENPYO_ACTIVITY_LOG/);
  await expect(page.getByTestId("data-preview-detail-header")).toContainText("ADMIN.DENPYO_ACTIVITY_LOG");
  await expectNoHorizontalScroll(page);

  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const csvList = page.getByTestId("data-csv-table-list");
  await expect(csvList.locator("tbody tr")).toHaveCount(3);
  await expect(csvList.getByRole("button", { name: "ADMIN.DENPYO_ACTIVITY_LOG を選択" })).toHaveAccessibleDescription(shortComment);
  await expect(csvList.getByRole("button", { name: "ADMIN.EMPTY_COMMENT を選択" })).toHaveAccessibleDescription("-");
  await expectRowCellsDoNotOverlap(csvList.locator("tbody tr"));
  await csvList.screenshot({ path: testInfo.outputPath("data-csv-name-comments.png") });
  await expectNoHorizontalScroll(page);
});

for (const scenario of scenarios) {
  test(
    `${scenario.title}は一覧の列の高さ（画面の高さ）に収めて中をスクロールし、先頭を自動で選ばない`, { tag: "@responsive" },
    async ({ page }, testInfo) => {
      await mockObjectManagementApi(page, scenario);
      await page.goto(scenario.path);

      const list = page.getByTestId("db-admin-object-list");
      const grid = page.getByTestId(`${scenario.objectType}-management-grid`);
      const detailHeader = page.getByTestId(`${scenario.objectType}-management-detail-header`);
      await expect(grid.locator("tbody tr")).toHaveCount(30);
      // 一覧 + 詳細（ListDetailLayout。#2281）は先頭の行を自動で選ばない。
      await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
      await expect(detailHeader).toHaveCount(0);
      await expectCompactSortHeaders(grid);

      // 一覧の列は画面の高さの中に収まり、行は列の中でスクロールする（8 行の固定の高さではない）。
      const fit = await list.evaluate((node) => {
        const column = node.closest<HTMLElement>('[data-testid^="list-detail-"][data-testid$="-list"]')!;
        return {
          scrolls: node.scrollHeight > node.clientHeight,
          columnHeight: column.getBoundingClientRect().height,
          viewport: window.innerHeight,
          position: getComputedStyle(column).position,
        };
      });
      expect(fit.scrolls).toBe(true);
      expect(fit.columnHeight).toBeLessThanOrEqual(fit.viewport);
      expect(fit.position).toBe("sticky");

      await grid.getByRole("button", { name: `APP.${scenario.prefix}_01 を表示` }).click();
      await expect(detailHeader).toContainText(`${scenario.prefix}_01`);
      await returnToListIfSinglePane(page, `${scenario.objectType}-management`);
      await page.getByRole("searchbox", { name: "検索" }).fill(`${scenario.prefix}_02`);
      await expect(grid.locator("tbody tr")).toHaveCount(1);
      // 絞り込みで選択を別の行へ置き換えない（2 ペインでは URL の選択が正本のまま残る）。
      await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
      if (testInfo.project.name !== "mobile-375") {
        await expect(page).toHaveURL(new RegExp(`${scenario.objectType}=APP\\.${scenario.prefix}_01`));
      }
    }
  );

  test(
    `${scenario.title}の列情報は詳細と一緒にページでスクロールし、入れ子の縦スクロールを持たない`, { tag: "@responsive" },
    async ({ page }) => {
      await mockObjectManagementApi(page, scenario, { columnCount: 30 });
      await page.goto(`${scenario.path}?${scenario.objectType}=APP.${scenario.prefix}_01`);

      const detailColumns = page.getByTestId("db-admin-detail-columns");
      await expect(detailColumns.locator("tbody tr")).toHaveCount(30);
      const scrollState = await detailColumns.evaluate((node) => ({
        scrollHeight: node.scrollHeight,
        clientHeight: node.clientHeight,
      }));
      expect(scrollState.scrollHeight).toBeLessThanOrEqual(scrollState.clientHeight + 1);
      await expectNoHorizontalScroll(page);
    }
  );

  test(`${scenario.title}は共通検索フィールドで絞り込める`, async ({ page }) => {
    await mockObjectManagementApi(page, scenario);
    await page.goto(scenario.path);

    const search = page.getByRole("searchbox", { name: "検索" });
    await expect(search).toHaveAttribute("placeholder", "名前・コメントを入力");
    // 検索欄は共有 TextField の先頭アイコン（入力欄と同じ枠の中。#384）。
    await expect(search.locator("xpath=..").locator('svg.lucide-search[data-text-field-slot="leading"]')).toBeVisible();

    const grid = page.getByTestId(`${scenario.objectType}-management-grid`);
    await search.fill("_01");
    await expect(grid.locator("tbody tr")).toHaveCount(1);
    await expect(grid.getByText(`APP.${scenario.prefix}_01`, { exact: true })).toBeVisible();
    await search.clear();
    await expect(grid.locator("tbody tr")).toHaveCount(30);
  });

  test(`${scenario.title}は NL2SQL_ システム object を一覧に表示しない`, async ({ page }) => {
    await mockObjectManagementApi(page, scenario, {
      itemCount: 1,
      extraItems: [
        {
          name: "NL2SQL_SCHEMA_OBJECTS",
          owner: "APP",
          qualified_name: "APP.NL2SQL_SCHEMA_OBJECTS",
          object_type: scenario.objectType,
          row_count: null,
          comment: "system object",
        },
        {
          name: `${scenario.prefix}_OWNER_OK`,
          owner: "NL2SQL_APP",
          qualified_name: `NL2SQL_APP.${scenario.prefix}_OWNER_OK`,
          object_type: scenario.objectType,
          row_count: null,
          comment: "business object under NL2SQL_APP",
        },
        {
          name: `TD_NL2SQL_${scenario.prefix}`,
          owner: "APP",
          qualified_name: `APP.TD_NL2SQL_${scenario.prefix}`,
          object_type: scenario.objectType,
          row_count: null,
          comment: "business object with middle token",
        },
      ],
    });
    await page.goto(scenario.path);

    const search = page.getByRole("searchbox", { name: "検索" });
    const grid = page.getByTestId(`${scenario.objectType}-management-grid`);
    await expect(grid.getByText(`APP.${scenario.prefix}_01`, { exact: true })).toBeVisible();
    await expect(
      grid.getByText(`NL2SQL_APP.${scenario.prefix}_OWNER_OK`, { exact: true })
    ).toBeVisible();
    await expect(grid.getByText(`APP.TD_NL2SQL_${scenario.prefix}`, { exact: true })).toBeVisible();
    await expect(grid.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);

    await search.fill("NL2SQL_SCHEMA");
    await expect(grid.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
    await expect(grid.locator("tbody tr")).toHaveCount(0);
  });
}

for (const scenario of metadataScenarios) {
  // 対象の選択は大量の候補から選ぶ共通の ListPicker（#608）。行は名前・コメント（1 行で省略）・種類のバッジ。
  test(`${scenario.title}は対象名の下にコメントを表示し、種類が重ならない`, async ({ page }, testInfo) => {
    const longComment = "長い日本語の対象コメントです。".repeat(10);
    await mockMetadataManagementApi(page, {
      empty: true,
      extraTableItems: [
        { name: "DENPYO_ACTIVITY_LOG", owner: "ADMIN", object_type: "table", row_count: 266, comment: "伝票の処理履歴" },
        { name: "LONG_COMMENT_TABLE", owner: "ADMIN", object_type: "table", row_count: null, comment: longComment },
        { name: "NO_COMMENT", owner: "ADMIN", object_type: "table", row_count: null, comment: "" },
      ],
      extraViewItems: [
        { name: "ORDER_SUMMARY_V", owner: "ADMIN", object_type: "view", row_count: null, comment: "受注サマリ" },
      ],
    });
    await page.goto(scenario.path);

    const picker = page.getByTestId(`${scenario.idPrefix}-target`);
    await expect(picker.getByRole("option")).toHaveCount(4);
    for (const [name, comment, kind] of [
      ["DENPYO_ACTIVITY_LOG", "伝票の処理履歴", "テーブル"],
      ["LONG_COMMENT_TABLE", longComment, "テーブル"],
      ["NO_COMMENT", "", "テーブル"],
      ["ORDER_SUMMARY_V", "受注サマリ", "ビュー"],
    ]) {
      const option = picker.getByRole("option", { name: `ADMIN.${name}`, exact: true });
      await expect(option).toHaveAccessibleDescription(comment ? `${comment} ${kind}` : kind);
      const layout = await option.evaluate((node) => {
        const box = node.getBoundingClientRect();
        const parts = Array.from(node.querySelectorAll(":scope > span")).map((part) => part.getBoundingClientRect());
        const description = node.querySelector("[id$='-description']");
        return {
          partsInside: parts.every((part) => part.left >= box.left - 1 && part.right <= box.right + 1),
          partsDoNotOverlap: parts.every((part, index) =>
            parts.slice(index + 1).every((other) => other.left >= part.right - 1 || other.right <= part.left + 1)
          ),
          descriptionTruncated: description
            ? getComputedStyle(description).textOverflow === "ellipsis" && getComputedStyle(description).whiteSpace === "nowrap"
            : true,
        };
      });
      expect(layout.partsInside).toBe(true);
      expect(layout.partsDoNotOverlap).toBe(true);
      expect(layout.descriptionTruncated).toBe(true);
    }
    for (const kind of ["テーブル", "ビュー"]) {
      await expectNoStatusIcon(picker.locator("[data-status-variant]").filter({ hasText: kind }).first());
    }
    await picker.getByText("伝票の処理履歴", { exact: true }).click();
    await expect(picker.getByRole("option", { name: "ADMIN.DENPYO_ACTIVITY_LOG", exact: true })).toHaveAttribute(
      "aria-checked",
      "true"
    );
    await page.getByRole("searchbox", { name: "検索" }).fill("受注サマリ");
    await expect(picker.getByRole("option")).toHaveCount(1);
    await page.getByRole("searchbox", { name: "検索" }).clear();
    await expect(picker.getByRole("option")).toHaveCount(4);
    await picker.screenshot({ path: testInfo.outputPath(`${scenario.idPrefix}-target-comments.png`) });
    await expectNoHorizontalScroll(page);
  });

  test(`${scenario.title}は対象の一覧を 5 / 8 行の固定高さに収め、中をスクロールする`, { tag: "@responsive" }, async ({ page }) => {
    await mockMetadataManagementApi(page);
    await page.goto(scenario.path);

    const scrollRegion = page.getByTestId(`${scenario.idPrefix}-target-scroll-region`);
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByRole("option")).toHaveCount(30);
    const fit = await scrollRegion.evaluate((node) => {
      const listBox = node.getBoundingClientRect();
      const rows = Array.from(node.querySelectorAll('[role="option"]')).map((row) => row.getBoundingClientRect());
      return {
        listHeight: listBox.height,
        visibleRows: rows.filter((row) => row.bottom <= listBox.bottom + 1 && row.top >= listBox.top - 1).length,
        scrolls: node.scrollHeight > node.clientHeight,
        overflowY: getComputedStyle(node).overflowY,
      };
    });
    // md 未満は 5 行（17.5rem）、md 以上は 8 行（28rem）の固定の高さ（一覧の表示密度。#265 / #600）。
    const expectedListHeight = page.viewportSize()!.width < 768 ? 247 : 394;
    expect(Math.abs(fit.listHeight - expectedListHeight)).toBeLessThanOrEqual(4);
    expect(fit.visibleRows).toBe(page.viewportSize()!.width < 768 ? 5 : 8);
    expect(fit.scrolls).toBe(true);
    expect(fit.overflowY).toBe("auto");
  });

  test(`${scenario.title}は検索・所有者・種類フィルタを共通配置する`, { tag: "@responsive" }, async ({ page }) => {
    await mockOwnerFilterDbAdminObjectsApi(page);
    const ownerPrefixRequests: string[] = [];
    const exactOwnerRequests: string[] = [];
    const queryScopes: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/nl2sql/db-admin/objects") {
        ownerPrefixRequests.push(url.searchParams.get("owner_prefix") ?? "");
        queryScopes.push(url.searchParams.get("query_scope") ?? "");
        if (url.searchParams.has("owner")) exactOwnerRequests.push(url.searchParams.get("owner") ?? "");
      }
    });

    await page.goto(scenario.path);
    const toolbar = page.getByTestId(`${scenario.idPrefix}-target-toolbar`);
    const search = toolbar.getByRole("searchbox", { name: "検索" });
    await expect(search).toHaveAttribute("placeholder", "名前・コメントを入力");
    // 375px は検索・所有者・種類の 3 つをシート（「絞り込み」）にまとめる（#1933）。
    const sheet = await listToolbarUsesSheet(toolbar);
    expect(sheet).toBe(page.viewportSize()!.width < 640);
    const filters = await openListToolbarFilters(toolbar);
    const ownerFilter = filters.getByRole("searchbox", { name: "所有者" });
    await expect(ownerFilter).toBeVisible();
    await expect(ownerFilter).toHaveAttribute("placeholder", "所有者の先頭（例：ADM）");
    await expect(filters.getByRole("combobox", { name: "所有者" })).toHaveCount(0);
    const typeFilter = filters.getByRole("combobox", { name: "種類", exact: true });
    await expect(typeFilter).toBeVisible();
    await expect(typeFilter).toContainText(/種類:\s*すべて/);
    // ListToolbar: 検索 lg → 所有者 md → 種類 sm（シートの中は全幅で縦に積む）。欄の上にラベルを置かない（#1891 / #1933）。
    await expectDbObjectToolbarLayout(search, ownerFilter, typeFilter);
    if (!sheet) {
      expect(await findToolbarMisalignment(page)).toEqual([]);
      await search.focus();
      await page.keyboard.press("Tab");
    } else {
      await ownerFilter.focus();
    }
    await expect(ownerFilter).toBeFocused();
    expect(await ownerFilter.evaluate((node) => getComputedStyle(node).boxShadow)).not.toBe("none");
    await page.keyboard.press("Tab");
    await expect(typeFilter).toBeFocused();
    // 種類は共有の SelectField（#631）。フォーカスの表示は共有の :focus-visible（#355）。
    expect(await typeFilter.evaluate((node) => node.matches(":focus-visible"))).toBe(true);
    await closeListToolbarFilters(page);
    await search.fill("BILLING");
    await expect(page.getByText("条件に一致する対象がありません")).toBeVisible();
    // 条件があるあいだだけ「条件をクリア」を出し、押すと検索語・所有者・種類を既定に戻す（#1933）。
    const clear = toolbar.getByRole("button", { name: "条件をクリア", exact: true });
    await clear.click();
    await expect(search).toHaveValue("");
    await expect(clear).toHaveCount(0);
    await openListToolbarFilters(toolbar);
    await ownerFilter.fill("bil");
    await expect(ownerFilter).toHaveValue("BIL");
    await expect.poll(() => ownerPrefixRequests.includes("BIL")).toBe(true);
    await expect.poll(() => queryScopes.includes("name_comment")).toBe(true);
    expect(exactOwnerRequests).toEqual([]);
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("BILLING.LEDGER_TABLE_01", { exact: true })).toBeVisible();
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("APP.ORDERS_TABLE_01", { exact: true })).toHaveCount(0);
    await ownerFilter.fill("ZZZ");
    await expect(page.getByText("条件に一致する対象がありません")).toBeVisible();
    await ownerFilter.fill("");
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("APP.ORDERS_TABLE_01", { exact: true })).toBeVisible();
    await chooseSelectFieldOption(typeFilter, "view");
    // 既定以外を選ぶと値を強調する（`FilterSelect` の外枠の data-filter-active）。
    await expect(typeFilter.locator("xpath=ancestor::*[@data-filter-active][1]")).toHaveAttribute("data-filter-active", "true");
    await closeListToolbarFilters(page);
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("APP.ORDERS_VIEW_01", { exact: true })).toBeVisible();
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("APP.ORDERS_TABLE_01", { exact: true })).toHaveCount(0);
    if (sheet) {
      await expect(toolbar.getByRole("button", { name: "絞り込み（1）", exact: true })).toBeVisible();
    }
    await toolbar.getByRole("button", { name: "条件をクリア", exact: true }).click();
    await expect(page.getByTestId(`${scenario.idPrefix}-target`).getByText("APP.ORDERS_TABLE_01", { exact: true })).toBeVisible();
    await expectNoHorizontalScroll(page);
  });

  test(`${scenario.title}は NL2SQL_ システム object を対象 picker に表示しない`, async ({ page }) => {
    await mockMetadataManagementApi(page, {
      empty: true,
      extraTableItems: [
        {
          name: "NL2SQL_SCHEMA_OBJECTS",
          owner: "APP",
          object_type: "table",
          row_count: null,
          comment: "system table",
        },
        {
          name: "ORDERS",
          owner: "NL2SQL_APP",
          object_type: "table",
          row_count: null,
          comment: "business table",
        },
        {
          name: "TD_NL2SQL_ORDERS",
          owner: "APP",
          object_type: "table",
          row_count: null,
          comment: "business table",
        },
      ],
      extraViewItems: [
        {
          name: "NL2SQL_SYSTEM_VIEW",
          owner: "APP",
          object_type: "view",
          row_count: null,
          comment: "system view",
        },
      ],
    });
    await page.goto(scenario.path);

    const grid = page.getByTestId(`${scenario.idPrefix}-target`);
    await expect(grid.getByText("NL2SQL_APP.ORDERS", { exact: true })).toBeVisible();
    await expect(grid.getByText("APP.TD_NL2SQL_ORDERS", { exact: true })).toBeVisible();
    await expect(grid.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
    await expect(grid.getByText("APP.NL2SQL_SYSTEM_VIEW", { exact: true })).toHaveCount(0);

    await page.getByRole("searchbox", { name: "検索" }).fill("NL2SQL_SCHEMA");
    await expect(grid.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
    await expect(grid.getByRole("option")).toHaveCount(0);
  });
}

test("データ管理のテーブル・ビュー件数 badge は API 総数を表示する", async ({ page }) => {
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/data-management");

  const controls = page.getByTestId("list-detail-data-management-preview-list");
  await expect(controls.getByRole("heading", { name: "対象テーブル/ビュー" })).toBeVisible();
  // 件数はツールバーの ListCount（API の total。#1891）。見出しのバッジは出さない。
  await expect(page.getByTestId("data-management-preview-toolbar").locator("[data-list-count]")).toHaveText("全 151 件");
  for (const badge of ["全151件", "テーブル149", "ビュー2"]) {
    await expect(controls.getByText(badge, { exact: true })).toHaveCount(0);
  }
  await expect(page.getByTestId("data-management-preview-footer")).toContainText("100 / 151 件を表示");
});

test("データプレビューは検索・所有者・種別フィルタを共通配置する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockDataManagementApi(page);
  await page.goto("/data-management");

  const toolbar = page.getByTestId("data-management-preview-toolbar");
  const search = toolbar.getByRole("searchbox", { name: "検索" });
  // 375px は検索・所有者・種別の 3 つをシート（「絞り込み」）にまとめる（#1933）。
  const sheet = await listToolbarUsesSheet(toolbar);
  const filters = await openListToolbarFilters(toolbar);
  const ownerFilter = filters.getByRole("searchbox", { name: "所有者" });
  const kindFilter = filters.getByRole("combobox", { name: "種別", exact: true });
  await expect(kindFilter).toContainText(/種別:\s*すべて/);
  await expectDbObjectToolbarLayout(search, ownerFilter, kindFilter);

  if (!sheet) {
    expect(await findToolbarMisalignment(page)).toEqual([]);
    await search.focus();
    await page.keyboard.press("Tab");
  } else {
    await ownerFilter.focus();
  }
  await expect(ownerFilter).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(kindFilter).toBeFocused();

  // 対象を選ぶまで詳細（取得件数上限・結果）は出さない（一覧 + 詳細。#2315）。
  await expect(page.getByLabel("取得件数上限")).toHaveCount(0);
  const previewList = page.getByTestId("data-management-preview-grid");
  await expect(previewList.getByText("操作", { exact: true })).toHaveCount(0);
  await expect(previewList.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await chooseSelectFieldOption(kindFilter, "view");
  await closeListToolbarFilters(page);
  await expect(previewList.getByText("APP.VIEW_01", { exact: true })).toBeVisible();
  await expect(previewList.getByText("APP.TABLE_01", { exact: true })).toHaveCount(0);
  await toolbar.getByRole("button", { name: "条件をクリア", exact: true }).click();
  await page.getByRole("button", { name: "APP.TABLE_01 を選択" }).click();
  const previewRowLimitInput = page.getByLabel("取得件数上限");
  await expect(previewRowLimitInput).toHaveValue("100");
  await expect(page.getByText("1〜100000 の整数。取得上限を明示してください。")).toBeVisible();
  await expect(page.getByText("表示件数 10 件固定")).toHaveCount(0);
  // 行を選ぶと今の取得件数上限でデータを表示する。「データを表示」は件数を変えた後の取り直し。
  await expect(page.getByTestId("query-results-summary")).toBeVisible();
  const previewShowButton = page.getByRole("button", { name: "データを表示", exact: true });
  await expect(previewShowButton).toBeEnabled();
  await expect(page.getByRole("button", { name: "表示件数・結果をリセット", exact: true })).toHaveCount(0);
  await previewShowButton.click();
  await expect(page.getByTestId("query-results-summary")).toBeVisible();
  const previewResultsActions = page.getByTestId("data-preview-results-actions");
  const previewExportButton = previewResultsActions.getByRole("button", { name: "XLSX ダウンロード" });
  const previewMoreButton = previewResultsActions.getByRole("button", { name: "その他の操作" });
  await expect(previewExportButton).toBeVisible();
  await expect(previewMoreButton).toBeVisible();
  const [exportBox, moreBox] = await Promise.all([
    previewExportButton.boundingBox(),
    previewMoreButton.boundingBox(),
  ]);
  expect(exportBox).not.toBeNull();
  expect(moreBox).not.toBeNull();
  if (Math.abs(moreBox!.y - exportBox!.y) <= 2) {
    expect(moreBox!.x).toBeGreaterThan(exportBox!.x);
  } else {
    expect(moreBox!.y).toBeGreaterThan(exportBox!.y);
  }
  await previewMoreButton.click();
  await expect(page.getByRole("menuitem", { name: "APP.TABLE_01 のデータを空にする" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expectNoHorizontalScroll(page);

  if (testInfo.project.name === "mobile-375") {
    await page.setViewportSize({ width: 812, height: 375 });
    // 812px も xl 未満の 1 ペイン。一覧へ戻ると、列の幅いっぱいのツールバーは絞り込みを 1 行に並べる。
    await page.getByTestId("data-management-detail-back").click();
    await expectDbObjectToolbarLayout(
      search,
      toolbar.getByRole("searchbox", { name: "所有者" }),
      toolbar.getByRole("combobox", { name: "種別", exact: true })
    );
    await expectNoHorizontalScroll(page);
  }
});

test("データ管理の対象ピッカーはヘッダーで並び替えできる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockSortableDataManagementApi(page);
  await page.goto("/data-management");

  // データの表示の一覧は一覧 + 詳細の狭い列（#2315）。列は名前（+ コメント）と行数（+ 種類）の 2 つだけで、
  // 375px でも同じ 2 列のまま横スクロールを出さない。並べ替えは名前・行数の列見出し。
  const previewList = page.getByTestId("data-management-preview-grid");
  await expect(previewList).toBeVisible();
  await expectNoHorizontalScroll(page);
  await expect(previewList.getByRole("columnheader").filter({ visible: true })).toHaveText([/対象名/, /行数/]);
  await expectCompactSortHeaders(previewList);

  await expect.poll(() => pickerRowNames(previewList)).toEqual([
    "APP.A_VIEW",
    "APP.B_AUDIT",
    "APP.M_EMPTY",
    "BILLING.Z_PAYMENTS",
  ]);
  // 先頭の行を自動で選ばない（#2281・#2315）。
  await expect(previewList.locator("tbody tr[aria-current]")).toHaveCount(0);

  await previewList.getByRole("button", { name: /対象名/ }).click();
  await expect.poll(() => pickerRowNames(previewList)).toEqual([
    "BILLING.Z_PAYMENTS",
    "APP.M_EMPTY",
    "APP.B_AUDIT",
    "APP.A_VIEW",
  ]);
  await expect(previewList.locator("tbody tr[aria-current]")).toHaveCount(0);

  await previewList.getByRole("button", { name: /行数/ }).click();
  await expect.poll(() => pickerRowNames(previewList)).toEqual([
    "APP.A_VIEW",
    "APP.M_EMPTY",
    "APP.B_AUDIT",
    "BILLING.Z_PAYMENTS",
  ]);
  if (testInfo.project.name === "mobile-375") return;

  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const csvList = page.getByTestId("data-csv-table-list");
  await expect(csvList).toBeVisible();
  await expect.poll(() => pickerRowNames(csvList)).toEqual([
    "APP.B_AUDIT",
    "APP.M_EMPTY",
    "BILLING.Z_PAYMENTS",
  ]);
  await expect(csvList.locator("tbody tr").first()).toHaveAttribute("aria-current", "true");

  await csvList.getByRole("button", { name: /対象名/ }).click();
  await expect.poll(() => pickerRowNames(csvList)).toEqual([
    "BILLING.Z_PAYMENTS",
    "APP.M_EMPTY",
    "APP.B_AUDIT",
  ]);
  await expect(csvList.locator("tbody tr").first()).toHaveAttribute("aria-current", "true");

  await csvList.getByRole("button", { name: /行数/ }).click();
  await expect.poll(() => pickerRowNames(csvList)).toEqual([
    "APP.M_EMPTY",
    "APP.B_AUDIT",
    "BILLING.Z_PAYMENTS",
  ]);

  await csvList.getByRole("button", { name: /所有者/ }).click();
  await expect.poll(() => pickerRowNames(csvList)).toEqual([
    "APP.M_EMPTY",
    "APP.B_AUDIT",
    "BILLING.Z_PAYMENTS",
  ]);
});

test("データ管理の対象 picker は NL2SQL_ システム object を表示しない", async ({ page }) => {
  const items: MockAdminObjectItem[] = [
    {
      name: "ORDERS",
      owner: "APP",
      object_type: "table",
      row_count: 10,
      comment: "業務テーブル",
    },
    {
      name: "NL2SQL_SCHEMA_OBJECTS",
      owner: "APP",
      object_type: "table",
      row_count: null,
      comment: "system table",
    },
    {
      name: "NL2SQL_SYSTEM_VIEW",
      owner: "APP",
      object_type: "view",
      row_count: null,
      comment: "system view",
    },
    {
      name: "ORDERS",
      owner: "NL2SQL_APP",
      object_type: "table",
      row_count: 2,
      comment: "business owner table",
    },
    {
      name: "TD_NL2SQL_ORDERS",
      owner: "APP",
      object_type: "table",
      row_count: 1,
      comment: "middle-token business table",
    },
  ];
  const tableItems = items.filter((item) => item.object_type === "table");
  const viewItems = items.filter((item) => item.object_type === "view");

  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, managementCatalog));
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: tableItems, warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: viewItems, warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: items.length,
      table_count: tableItems.length,
      view_count: viewItems.length,
      next_cursor: null,
      refreshed_at: managementCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    })
  );

  await page.goto("/data-management");

  const previewList = page.getByTestId("data-management-preview-grid");
  await expect(previewList.getByText("APP.ORDERS", { exact: true })).toBeVisible();
  await expect(previewList.getByText("NL2SQL_APP.ORDERS", { exact: true })).toBeVisible();
  await expect(previewList.getByText("APP.TD_NL2SQL_ORDERS", { exact: true })).toBeVisible();
  await expect(previewList.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
  await expect(previewList.getByText("APP.NL2SQL_SYSTEM_VIEW", { exact: true })).toHaveCount(0);

  await page.getByRole("searchbox", { name: "検索" }).fill("NL2SQL_SCHEMA");
  await expect(previewList.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
  await expect(previewList.getByText("NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);

  await page.getByRole("searchbox", { name: "検索" }).clear();
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const csvList = page.getByTestId("data-csv-table-list");
  await expect(csvList.getByText("NL2SQL_APP.ORDERS", { exact: true })).toBeVisible();
  await expect(csvList.getByText("APP.TD_NL2SQL_ORDERS", { exact: true })).toBeVisible();
  await expect(csvList.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
});

// #608: 大量の候補から複数を選ぶ画面は共通の ListPicker。151 件（100 件ずつの追加読み込み）で、
// 選択の行・「選択中だけ表示」・キーボード操作・追加読み込みを確かめる（desktop / 375px は projects）。
test(`コメント管理の対象は大量でも ListPicker で追加読み込みし、選択中だけ表示できる`, async ({
  page,
}, testInfo) => {
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/comment-management");

  const picker = page.getByTestId("comment-management-target");
  const footer = page.getByTestId("comment-management-target-footer");
  await expect(picker.getByRole("listbox")).toHaveAttribute("aria-multiselectable", "true");
  await expect(footer).toContainText("100 / 151 件を表示、選択 0 件");

  // キーボード: Tab で一覧に入り、↓ で移って Space で選ぶ。
  const listbox = picker.getByRole("listbox");
  await listbox.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Space");
  await expect(footer).toContainText("選択 1 件");
  await expect(picker.getByRole("option", { name: "APP.PAGED_TABLE_002", exact: true })).toHaveAttribute(
    "aria-checked",
    "true"
  );

  // 続きは「さらに読み込む」で足す（ページを移らないので、選んだ候補は残る）。
  await footer.getByRole("button", { name: "さらに読み込む" }).click();
  await expect(footer).toContainText("151 / 151 件を表示、選択 1 件");
  await expect(footer.getByRole("button", { name: "さらに読み込む" })).toHaveCount(0);

  // 検索で選んだ候補が見えなくなっても、「選択中だけ表示」で確かめられる。
  await page.getByTestId("comment-management-target-toolbar").getByRole("searchbox", { name: "検索" }).fill("PAGED_VIEW");
  await expect(picker.getByRole("option")).toHaveCount(2);
  await picker.getByTestId("comment-management-target-show-selected").click();
  await expect(picker.getByRole("option")).toHaveCount(1);
  await expect(picker.getByRole("option", { name: "APP.PAGED_TABLE_002", exact: true })).toBeVisible();

  const colors = await picker.evaluate((node) => {
    const style = getComputedStyle(node);
    return { background: style.backgroundColor, color: style.color };
  });
  expect(colors.background).not.toBe(colors.color);
  await picker.screenshot({ path: testInfo.outputPath(`comment-target-picker-light.png`) });
  await expectNoHorizontalScroll(page);
});

test("コメント管理の対象件数 badge は footer と同じ API 総数を表示する", async ({ page }) => {
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/comment-management");

  const panel = page.locator("#comment-management-panel-targets");
  await expect(panel.getByRole("heading", { name: "対象選択" })).toBeVisible();
  // 件数はツールバーの ListCount（API の total。#1891）。見出しには選択の数だけを出す。
  await expect(page.getByTestId("comment-management-target-toolbar").locator("[data-list-count]")).toHaveText("全 151 件");
  await expect(panel.getByText("151 件", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("100 / 151 件を表示");
});

test("テーブル管理・ビュー管理の一覧件数 badge は API 総数を表示する", async ({ page }) => {
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/table-management");

  // 一覧 + 詳細（ListDetailLayout。#2281）の一覧の列。
  const tableGrid = page.getByTestId("list-detail-table-management-list");
  await expect(page.getByTestId("table-management-toolbar").locator("[data-list-count]")).toHaveText("全 149 件");
  await expect(tableGrid.locator("[data-status-variant]")).toHaveCount(0);
  await expect(page.getByTestId("table-management-footer")).toContainText("100 / 149 件を表示");

  await page.goto("/view-management");
  const viewGrid = page.getByTestId("list-detail-view-management-list");
  await expect(page.getByTestId("view-management-toolbar").locator("[data-list-count]")).toHaveText("全 2 件");
  await expect(viewGrid.locator("[data-status-variant]")).toHaveCount(0);
  await expect(page.getByTestId("view-management-footer")).toContainText("2 / 2 件を表示");
});

test("テーブル管理は DevTools web-vitals の startTime 例外だけを console に出さない", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/table-management");
  await expect(page.getByTestId("table-management-grid")).toBeVisible();

  expect(await dispatchDevToolsWebVitalsStartTimeError(page)).toBe(true);
  await page.waitForTimeout(50);
  expect(consoleErrors).toEqual([]);
});

test("テーブル管理・ビュー管理は共通検索と所有者入力で一覧を絞り込む", { tag: "@responsive" }, async ({ page }) => {
  await mockOwnerFilterDbAdminObjectsApi(page);
  const ownerPrefixRequests: string[] = [];
  const exactOwnerRequests: string[] = [];
  const queryScopes: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/nl2sql/db-admin/objects") {
      ownerPrefixRequests.push(url.searchParams.get("owner_prefix") ?? "");
      queryScopes.push(url.searchParams.get("query_scope") ?? "");
      if (url.searchParams.has("owner")) exactOwnerRequests.push(url.searchParams.get("owner") ?? "");
    }
  });

  await page.goto("/table-management");
  // 一覧 + 詳細（ListDetailLayout。#2281）の一覧の列。
  const tableGrid = page.getByTestId("list-detail-table-management-list");
  const tableSearch = tableGrid.getByRole("searchbox", { name: "検索" });
  const tableOwnerFilter = tableGrid.getByRole("searchbox", { name: "所有者" });
  await expect(tableSearch).toHaveAttribute("placeholder", "名前・コメントを入力");
  await expect(tableOwnerFilter).toHaveAttribute("placeholder", "所有者の先頭（例：ADM）");
  await expectDbObjectToolbarLayout(tableSearch, tableOwnerFilter);
  // 狭い一覧の列（既定 20rem）でも検索欄と所有者の幅がそろい、欄の上にラベルを置かず中心がそろう（#1891 段 3・#1933・#2281）。
  expect(await findToolbarFieldWidthMismatch(page)).toEqual([]);
  expect(await findToolbarMisalignment(page)).toEqual([]);
  await expect(tableOwnerFilter).toBeVisible();
  await expect(tableGrid.getByRole("combobox", { name: "所有者" })).toHaveCount(0);
  await expect(tableGrid.getByLabel("行数フィルタ")).toHaveCount(0);
  await tableSearch.focus();
  await page.keyboard.press("Tab");
  await expect(tableOwnerFilter).toBeFocused();
  await expect(tableGrid.getByText("APP.ORDERS_TABLE_01", { exact: true })).toBeVisible();
  await expect(tableGrid.getByText("BILLING.LEDGER_TABLE_01", { exact: true })).toBeVisible();
  await tableSearch.fill("BILLING");
  await expect(tableGrid.getByText("条件に一致するテーブルがありません")).toBeVisible();
  // 条件があるあいだだけ「条件をクリア」を出す（#1933）。
  const tableClear = tableGrid.getByRole("button", { name: "条件をクリア", exact: true });
  await tableClear.click();
  await expect(tableSearch).toHaveValue("");
  await expect(tableClear).toHaveCount(0);
  await tableOwnerFilter.fill("bil");
  await expect(tableOwnerFilter).toHaveValue("BIL");
  await expect.poll(() => ownerPrefixRequests.includes("BIL")).toBe(true);
  await expect.poll(() => queryScopes.includes("name_comment")).toBe(true);
  expect(exactOwnerRequests).toEqual([]);
  await expect(tableGrid.getByText("BILLING.LEDGER_TABLE_01", { exact: true })).toBeVisible();
  await expect(tableGrid.getByText("APP.ORDERS_TABLE_01", { exact: true })).toHaveCount(0);
  await tableOwnerFilter.fill("ZZZ");
  await expect(tableGrid.getByText("条件に一致するテーブルがありません")).toBeVisible();
  await tableOwnerFilter.fill("");
  await expect(tableGrid.getByText("APP.ORDERS_TABLE_01", { exact: true })).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.goto("/view-management");
  const viewGrid = page.getByTestId("list-detail-view-management-list");
  const viewSearch = viewGrid.getByRole("searchbox", { name: "検索" });
  const viewOwnerFilter = viewGrid.getByRole("searchbox", { name: "所有者" });
  await expect(viewOwnerFilter).toBeVisible();
  await expectDbObjectToolbarLayout(viewSearch, viewOwnerFilter);
  await expect(viewGrid.getByRole("combobox", { name: "所有者" })).toHaveCount(0);
  await expect(viewGrid.getByLabel("行数フィルタ")).toHaveCount(0);
  const requestCountBeforeViewFilter = ownerPrefixRequests.length;
  await viewOwnerFilter.fill("bil");
  await expect.poll(() => ownerPrefixRequests.slice(requestCountBeforeViewFilter).includes("BIL")).toBe(true);
  await expect(viewGrid.getByText("BILLING.LEDGER_VIEW_01", { exact: true })).toBeVisible();
  await expect(viewGrid.getByText("APP.ORDERS_VIEW_01", { exact: true })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("テーブル管理は 150% zoom 相当でもタブ・列名・ヘッダー操作を折り返さず、一覧操作列を持たない", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1365, height: 900 });
  await mockObjectManagementApi(page, scenarios[0]);
  // 詳細のタブを確かめるため、対象を選んだ URL で開く（先頭を自動で選ばない。#2281）。
  await page.goto("/table-management?table=APP.TABLE_01");

  // 一覧が既定。作成/取込はタブではなくツールバーのアクションボタン。
  const actions = page.getByTestId("table-management-actions");
  // 共有 PageHeader は utility → secondary → primary の順に並べ、主操作を右端に置く
  // (docs/design-system/README.md §7 #11)。旧実装の data-page-action-group 属性は共有版に存在しない。
  await expect(actions.getByRole("button")).toHaveText([
    "最新情報を取得",
    "DB 構造を再取得",
    "Excel/CSV 取込(新規テーブル)",
    "テーブル作成",
  ]);
  const [refreshBox, schemaRefreshBox, importBox, createBox] = await Promise.all([
    actions.getByRole("button", { name: "最新情報を取得" }).boundingBox(),
    actions.getByRole("button", { name: "DB 構造を再取得" }).boundingBox(),
    actions.getByRole("button", { name: "Excel/CSV 取込(新規テーブル)" }).boundingBox(),
    actions.getByRole("button", { name: "テーブル作成" }).boundingBox(),
  ]);
  expect(createBox).not.toBeNull();
  expect(importBox).not.toBeNull();
  expect(refreshBox).not.toBeNull();
  expect(schemaRefreshBox).not.toBeNull();
  expect(schemaRefreshBox!.x).toBeGreaterThan(refreshBox!.x);
  expect(importBox!.x).toBeGreaterThan(schemaRefreshBox!.x);
  expect(createBox!.x).toBeGreaterThan(importBox!.x);
  await expectSingleLine(actions.getByRole("button", { name: "テーブル作成" }).locator("span"));
  await expectSingleLine(actions.getByRole("button", { name: "Excel/CSV 取込(新規テーブル)" }).locator("span"));
  // 詳細内タブ(列情報/DDL)は維持。
  await expectSingleLine(page.getByRole("tab", { name: "列情報" }).locator("span").last());
  await expectSingleLine(page.getByRole("tab", { name: "DDL" }).locator("span").last());

  const grid = page.getByTestId("table-management-grid");
  await expect(grid.locator("tbody tr")).toHaveCount(30);
  await expect(grid.getByRole("columnheader", { name: /コメント/ })).toHaveCount(0);
  // 狭い一覧の列では所有者の列を持たない（所有者は名前の OWNER. に出る。#2281）。
  await expect(grid.getByRole("columnheader", { name: /所有者/ })).toHaveCount(0);
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /操作: APP\.TABLE_01/ })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: "詳細" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: "削除" })).toHaveCount(0);
  const secondRow = grid.locator("tbody tr").nth(1);
  await secondRow.locator("td").nth(1).click();
  await expect(secondRow).toHaveAttribute("data-selected", "true");

  const scroll = await page.getByTestId("db-admin-object-list").evaluate((node) => ({
    internalWidthStable: node.scrollWidth >= node.clientWidth,
    pageHorizontal:
      document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
      document.body.scrollWidth > document.body.clientWidth + 1,
  }));
  expect(scroll.internalWidthStable).toBe(true);
  expect(scroll.pageHorizontal).toBe(false);
});

test("テーブル管理の一覧下端は操作列なしで行選択できる", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1365, height: 900 });
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");

  const list = page.getByTestId("db-admin-object-list");
  await list.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });

  const grid = page.getByTestId("table-management-grid");
  const bottomRow = grid.locator("tbody tr").filter({ hasText: "APP.TABLE_30" });
  await expect(bottomRow).toBeVisible();
  await expect(grid.getByRole("button", { name: /操作: APP\.TABLE_30/ })).toHaveCount(0);
  await bottomRow.click();
  await expect(bottomRow).toHaveAttribute("data-selected", "true");
  await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_30");
});

test("テーブル管理の少件数一覧も操作列なしで行選択できる", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1365, height: 900 });
  await mockObjectManagementApi(page, scenarios[0], { itemCount: 1 });
  await page.goto("/table-management");

  const shortList = page.getByTestId("db-admin-object-list");
  const shortListBox = await shortList.boundingBox();
  expect(shortListBox).not.toBeNull();
  const grid = page.getByTestId("table-management-grid");
  const row = grid.locator("tbody tr").filter({ hasText: "APP.TABLE_01" });
  await expect(row).toBeVisible();
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /操作: APP\.TABLE_01/ })).toHaveCount(0);
  await row.click();
  await expect(row).toHaveAttribute("data-selected", "true");
});

test("テーブル詳細は列タブでは DDL を取得せず、DDL タブ初回表示で後追い取得する", { tag: "@responsive" }, async ({
  page,
}) => {
  const detailUrls: string[] = [];
  const exactCountGate = createRequestGate();
  let catalogHit = false;
  await page.route("**/api/schema/catalog", (route) => {
    catalogHit = true;
    return fulfillJson(route, { refreshed_at: "2026-06-21T10:00:00.000Z", tables: [] });
  });
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: [
        { name: "TABLE_01", owner: "APP", object_type: "table", row_count: null, comment: "" },
      ],
      refreshed_at: "2026-07-14T03:53:00+00:00",
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: [
        { name: "TABLE_01", owner: "APP", object_type: "table", row_count: null, comment: "" },
      ],
      total: 1,
      table_count: 1,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-14T03:53:00+00:00",
      catalog_version: 1,
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", async (route) => {
    const url = route.request().url();
    detailUrls.push(url);
    // 重い GET_DDL を伴う DDL は include_ddl=1 のときだけ返す(バックエンドの挙動を模す)。
    const withDdl = url.includes("include_ddl=1");
    const exactCount = url.includes("exact_count=1");
    if (exactCount) await exactCountGate.promise;
    return fulfillJson(route, {
      name: "TABLE_01",
      owner: "APP",
      object_type: "table",
      // 既定は num_rows 統計、exact_count=1 のときだけ COUNT(*) 相当の正確値を返す。
      row_count: exactCount ? 999 : null,
      comment: "",
      // サンプル値は詳細応答が返す(catalog 全取得に依存しない)。
      columns: [
        {
          column_name: "STATUS",
          logical_name: "状態",
          data_type: "VARCHAR2(20)",
          nullable: false,
          comment: "",
          sample_values: ["NEW", "PAID"],
        },
      ],
      ddl: withDdl ? 'CREATE TABLE "TABLE_01" ("ID" NUMBER)' : "",
      warnings: [],
    });
  });

  await page.goto("/table-management?table=APP.TABLE_01");

  // 列タブ(既定)の初期表示は DDL 抜きで取得し、サンプル値は詳細応答由来で表示される。
  await expect(page.getByRole("heading", { name: "TABLE_01" })).toBeVisible();
  await expect(page.getByText("NEW, PAID")).toBeVisible();
  const detailHeader = page.getByTestId("table-management-detail-header");
  await expect(detailHeader.getByText("-", { exact: true })).toBeVisible(); // 未取得時も行数スロットを予約する
  expect(detailUrls.length).toBeGreaterThan(0);
  expect(detailUrls.every((url) => url.includes("include_ddl=0"))).toBe(true);
  expect(detailUrls.some((url) => url.includes("include_ddl=1"))).toBe(false);
  expect(detailUrls.some((url) => url.includes("exact_count=1"))).toBe(false);
  // catalog 全取得は行わない(サンプル値も取得日時も一覧/詳細で賄う)。
  expect(catalogHit).toBe(false);

  const headerBoxBefore = await detailHeader.boundingBox();
  expect(headerBoxBefore).not.toBeNull();
  const exactCountButton = page.getByRole("button", { name: "COUNT(*) で正確な行数を取得" });

  // 「正確な件数を取得」は loading 中も消えず、ヘッダー高さを変えず、完了後も再実行できる。
  await exactCountButton.click();
  await expect.poll(() => detailUrls.filter((url) => url.includes("exact_count=1")).length).toBe(1);
  await expect(exactCountButton).toBeVisible();
  await expect(exactCountButton).toBeDisabled();
  const headerBoxLoading = await detailHeader.boundingBox();
  expect(headerBoxLoading).not.toBeNull();
  expect(Math.abs(headerBoxLoading!.height - headerBoxBefore!.height)).toBeLessThanOrEqual(1);
  exactCountGate.release();
  await expect(page.getByText("999 行")).toBeVisible();
  await expect(exactCountButton).toBeVisible();
  await expect(exactCountButton).toBeEnabled();
  const headerBoxAfter = await detailHeader.boundingBox();
  expect(headerBoxAfter).not.toBeNull();
  expect(Math.abs(headerBoxAfter!.height - headerBoxBefore!.height)).toBeLessThanOrEqual(1);
  expect(detailUrls.some((url) => url.includes("exact_count=1"))).toBe(true);
  await exactCountButton.click();
  await expect.poll(() => detailUrls.filter((url) => url.includes("exact_count=1")).length).toBeGreaterThanOrEqual(2);

  // DDL タブを開くと include_ddl=1 で後追い取得し、DDL が表示される。
  await page.getByRole("tab", { name: "DDL" }).click();
  await expect(page.getByText('CREATE TABLE "TABLE_01" ("ID" NUMBER)')).toBeVisible();
  await expectContentActionsRightAligned(page.getByTestId("table-management-ddl-actions"));
  expect(detailUrls.some((url) => url.includes("include_ddl=1"))).toBe(true);
});

for (const scenario of scenarios) {
  test(`${scenario.title}の DDL 後追い取得中は詳細を保った局所スケルトンと経過時間を表示する`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const objectName = `${scenario.prefix}_DDL_LOADING`;
    const loadingLabel =
      scenario.objectType === "table"
        ? "テーブルの DDL を取得しています"
        : "ビューの DDL を取得しています";
    const ddl =
      scenario.objectType === "table"
        ? `CREATE TABLE "${objectName}" ("ID" NUMBER)`
        : `CREATE OR REPLACE VIEW "${objectName}" AS SELECT 1 AS ID FROM DUAL`;
    const ddlGate = createRequestGate();
    let ddlRequestCount = 0;
    let markDdlStarted: () => void = () => undefined;
    const ddlStarted = new Promise<void>((resolve) => {
      markDdlStarted = resolve;
    });
    const objectItems = [
      {
        name: objectName,
        owner: "APP",
        object_type: scenario.objectType,
        row_count: 1,
        comment: "",
      },
    ];

    await page.route(scenario.apiPath, (route) =>
      fulfillJson(route, {
        runtime: "deterministic",
        items: objectItems,
        refreshed_at: "2026-07-23T00:00:00+00:00",
        warnings: [],
      }),
    );
    await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
      fulfillJson(route, {
        runtime: "deterministic",
        owner: "APP",
        items: objectItems,
        total: objectItems.length,
        table_count: scenario.objectType === "table" ? objectItems.length : 0,
        view_count: scenario.objectType === "view" ? objectItems.length : 0,
        next_cursor: null,
        refreshed_at: "2026-07-23T00:00:00+00:00",
        catalog_version: 1,
        warnings: [],
      }),
    );
    await page.route(`${scenario.apiPath}/*`, async (route) => {
      const withDdl = route.request().url().includes("include_ddl=1");
      if (withDdl) {
        ddlRequestCount += 1;
        markDdlStarted();
        await ddlGate.promise;
      }
      await fulfillJson(route, {
        name: objectName,
        owner: "APP",
        object_type: scenario.objectType,
        row_count: 1,
        comment: "",
        columns: [
          {
            column_name: "ID",
            logical_name: "識別子",
            data_type: "NUMBER",
            nullable: false,
            comment: "",
            sample_values: ["1"],
          },
        ],
        ddl: withDdl ? ddl : "",
        warnings: [],
      });
    });

    await page.goto(`${scenario.path}?${scenario.objectType}=APP.${objectName}`);
    await expect(page.getByRole("heading", { name: objectName })).toBeVisible();
    await page.getByRole("tab", { name: "DDL" }).click();
    await ddlStarted;

    const idPrefix = scenario.path.slice(1);
    const skeleton = page.getByTestId(`${idPrefix}-ddl-skeleton`);
    await expect(skeleton).toBeVisible();
    await expect(skeleton).toHaveAttribute("aria-busy", "true");
    await expect(skeleton).toHaveAttribute("data-processing-placement", "tab");
    await expect(skeleton).toContainText(loadingLabel);
    await expect(skeleton.getByRole("timer")).toHaveAccessibleName(/経過時間 00:0\d/);
    await expect(page.getByRole("heading", { name: objectName })).toBeVisible();
    await expect(page.getByRole("tab", { name: "DDL" })).toBeVisible();
    await expect(page.getByRole("button", { name: "コピー" })).toHaveCount(0);
    expect(
      await skeleton.evaluate((element) => {
        const detailPanel = element.closest("section[aria-labelledby]");
        const tabList = detailPanel?.querySelector('[role="tablist"]');
        return Boolean(
          detailPanel &&
            tabList &&
            tabList.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING,
        );
      }),
    ).toBe(true);

    const skeletonHeights = await skeleton
      .getByTestId("db-management-skeleton-block")
      .evaluateAll((elements) =>
        elements.map((element) =>
          Math.round(Number.parseFloat(window.getComputedStyle(element).height)),
        ),
      );
    expect(skeletonHeights).toEqual([40, 287]);
    await expect(skeleton.getByTestId("db-management-skeleton-block").first()).toHaveCSS(
      "animation-name",
      "none",
    );
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
          document.body.scrollWidth > document.body.clientWidth + 1,
      ),
    ).toBe(false);

    ddlGate.release();
    await expect(skeleton).toHaveCount(0);
    await expect(page.getByText(ddl)).toBeVisible();

    await page.getByRole("tab", { name: "列情報" }).click();
    await page.getByRole("tab", { name: "DDL" }).click();
    await expect(page.getByText(ddl)).toBeVisible();
    expect(ddlRequestCount).toBe(1);
  });
}

test("明示的な一覧再取得はヘッダーではなくテーブル作業領域の先頭に表示する", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const refreshGate = createRequestGate();
  let objectRequestCount = 0;
  let holdNextRefresh = false;
  let markRefreshStarted: () => void = () => undefined;
  const refreshStarted = new Promise<void>((resolve) => {
    markRefreshStarted = resolve;
  });
  const tableItems = [
    {
      name: "TABLE_REFRESH",
      owner: "APP",
      object_type: "table",
      row_count: 12,
      comment: "",
    },
  ];

  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    objectRequestCount += 1;
    if (holdNextRefresh) {
      holdNextRefresh = false;
      markRefreshStarted();
      await refreshGate.promise;
    }
    await fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: tableItems,
      total: 1,
      table_count: 1,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-29T00:00:00.000Z",
      catalog_version: objectRequestCount,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables/*", (route) =>
    fulfillJson(route, {
      name: "TABLE_REFRESH",
      owner: "APP",
      object_type: "table",
      row_count: 12,
      comment: "",
      columns: [
        {
          column_name: "ID",
          logical_name: "識別子",
          data_type: "NUMBER",
          nullable: false,
          comment: "",
          sample_values: ["1"],
        },
      ],
      ddl: "",
      warnings: [],
    }),
  );

  await page.goto("/table-management?table=APP.TABLE_REFRESH");
  await expect(page.getByRole("heading", { name: "TABLE_REFRESH" })).toBeVisible();
  holdNextRefresh = true;
  await clickPageHeaderAction(page, "table-management-actions", "最新情報を取得");
  await refreshStarted;

  const processing = page.getByTestId("table-management-workspace-processing");
  await expect(processing).toBeVisible();
  await expect(processing).toHaveAttribute("data-processing-placement", "workspace");
  await expect(processing).toContainText("テーブル一覧を更新しています");
  await expect(page.getByTestId("table-management-actions-processing")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "TABLE_REFRESH" })).toBeVisible();

  const shell = page
    .getByTestId("management-panel-shell")
    .filter({ has: page.getByTestId("list-detail-table-management") });
  await expect(shell.getByTestId("table-management-workspace-processing")).toBeVisible();
  expect(
    await processing.evaluate((element) => {
      const split = document.querySelector(
        '[data-testid="list-detail-table-management"]',
      );
      return Boolean(
        split &&
          element.compareDocumentPosition(split) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }),
  ).toBe(true);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoHorizontalScroll(page);
  await expect(processing).toHaveAttribute("data-processing-activity-icon", "none");
  await expect(processing.locator("svg.animate-spin")).toHaveCount(0);

  refreshGate.release();
  await expect(processing).toHaveCount(0);
});

test("DDL の遅延応答中に別テーブルを選ぶと旧 DDL を破棄する", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile-375", "一覧と詳細を並べて選び直す流れは 2 ペイン（xl 以上）で確かめる（#2281）");
  const ddlGate = createRequestGate();
  let markDdlStarted: () => void = () => undefined;
  const ddlStarted = new Promise<void>((resolve) => {
    markDdlStarted = resolve;
  });
  const tableNames = ["TABLE_A", "TABLE_B"];
  const tableItems = tableNames.map((name) => ({
    name,
    owner: "APP",
    object_type: "table",
    row_count: 1,
    comment: "",
  }));

  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: tableItems,
      refreshed_at: "2026-07-23T00:00:00+00:00",
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: tableItems,
      total: tableItems.length,
      table_count: tableItems.length,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-23T00:00:00+00:00",
      catalog_version: 1,
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", async (route) => {
    const url = new URL(route.request().url());
    const tableName = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    const withDdl = url.searchParams.get("include_ddl") === "1";
    if (tableName === "TABLE_A" && withDdl) {
      markDdlStarted();
      await ddlGate.promise;
    }
    try {
      await fulfillJson(route, {
        name: tableName,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
        columns: [
          {
            column_name: `${tableName}_ID`,
            logical_name: `${tableName} の識別子`,
            data_type: "NUMBER",
            nullable: false,
            comment: "",
            sample_values: ["1"],
          },
        ],
        ddl: withDdl ? `CREATE TABLE "${tableName}" ("ID" NUMBER)` : "",
        warnings: [],
      });
    } catch {
      // TABLE_A の DDL は TABLE_B 選択時に client 側で abort されるため fulfill 不可でも正常。
    }
  });

  await page.goto("/table-management?table=APP.TABLE_A");
  await expect(page.getByRole("heading", { name: "TABLE_A" })).toBeVisible();
  await page.getByRole("tab", { name: "DDL" }).click();
  await ddlStarted;
  await expect(page.getByTestId("table-management-ddl-skeleton")).toBeVisible();

  await page.getByRole("button", { name: "TABLE_B を表示" }).click();
  await expect(page.getByRole("heading", { name: "TABLE_B" })).toBeVisible();
  await expect(page.getByTestId("db-admin-detail-columns")).toContainText("TABLE_B の識別子");

  ddlGate.release();
  await page.waitForTimeout(100);
  await expect(page.getByText('CREATE TABLE "TABLE_A" ("ID" NUMBER)')).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "TABLE_B" })).toBeVisible();
});

test("テーブル詳細は経過時間、遅延案内、切替リセット、取消、成功を同じ領域で扱う", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile-375", "一覧と詳細を並べて選び直す流れは 2 ペイン（xl 以上）で確かめる（#2281）");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.install({ time: new Date("2026-07-29T00:00:00.000Z") });
  const firstSlowGate = createRequestGate();
  const cancelledSlowGate = createRequestGate();
  const tableNames = ["TABLE_SLOW", "TABLE_FAST"];
  const tableItems = tableNames.map((name) => ({
    name,
    owner: "APP",
    object_type: "table",
    row_count: 1,
    comment: "",
  }));
  let slowAttempts = 0;

  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: tableItems,
      total: tableItems.length,
      table_count: tableItems.length,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-29T00:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", async (route) => {
    const url = new URL(route.request().url());
    const tableName = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    if (tableName === "TABLE_SLOW") {
      slowAttempts += 1;
      if (slowAttempts === 1) await firstSlowGate.promise;
      if (slowAttempts === 2) await cancelledSlowGate.promise;
    }
    try {
      await fulfillJson(route, {
        name: tableName,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
        columns: [
          {
            column_name: `${tableName}_ID`,
            logical_name: `${tableName} の識別子`,
            data_type: "NUMBER",
            nullable: false,
            comment: "",
            sample_values: ["1"],
          },
        ],
        ddl: "",
        warnings: [],
      });
    } catch {
      // 切替または取消で client が破棄した旧 request は fulfill できなくても正常。
    }
  });

  // 先頭を自動で選ばない（#2281）ので、速い詳細（TABLE_FAST）を選んだ URL で開き、遅い詳細は明示的に選択する。
  await page.goto("/table-management?table=APP.TABLE_FAST");
  await expect(page.getByRole("heading", { name: "TABLE_FAST" })).toBeVisible();
  await page.getByRole("button", { name: "TABLE_SLOW を表示" }).click();
  const skeleton = page.getByTestId("table-management-detail-skeleton");
  await expect(skeleton).toBeVisible();
  await expect(skeleton).toHaveAttribute("data-processing-placement", "panel");
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:00");
  // スピナーは reduced motion でも回転を止めない（#440）。
  await expect(skeleton.locator("svg.animate-spin")).toHaveCSS("animation-name", "spin");

  await page.clock.fastForward(11_000);
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:11");
  await expect(skeleton).toContainText("通常より時間がかかっています");

  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1,
    ),
  ).toBe(false);
  // xl 未満は 1 ペイン（詳細だけ）なので、一覧から選び直すために 2 ペインの幅へ戻す（#2281）。
  await page.setViewportSize({ width: 1280, height: 900 });

  // 処理中に別テーブルへ切り替えると旧 request を無効化し、新しい timer は 00:00 から始まる。
  await page.getByRole("button", { name: "TABLE_FAST を表示" }).click();
  await expect(page.getByRole("heading", { name: "TABLE_FAST" })).toBeVisible();
  firstSlowGate.release();
  await page.waitForTimeout(50);
  await expect(page.getByText("TABLE_SLOW の識別子")).toHaveCount(0);

  // 同じ対象を再度読み込み、キーボードで取消しても選択状態は保持する。
  await page.getByRole("button", { name: "TABLE_SLOW を表示" }).click();
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:00");
  const cancel = skeleton.getByRole("button", { name: "キャンセル" });
  await cancel.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "TABLE_SLOW を表示" })).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(skeleton).toHaveCount(0);
  cancelledSlowGate.release();

  // 通常 request の完了後は timer を残さず、結果へ自然に置き換える。
  await page.getByRole("button", { name: "TABLE_SLOW を表示" }).click();
  await expect(page.getByRole("heading", { name: "TABLE_SLOW" })).toBeVisible();
  await expect(page.getByRole("timer")).toHaveCount(0);
});

test("テーブル詳細の30秒 timeout は選択を保持して再試行できる", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile-375", "一覧と詳細を並べて選び直す流れは 2 ペイン（xl 以上）で確かめる（#2281）");
  // 30 秒の timeout は、再試行の前だけ 150ms に縮める（再試行の取得が遅い環境で timeout にならないように）。
  await page.addInitScript(() => {
    const flags = window as unknown as { __e2eShortDetailTimeout?: boolean };
    flags.__e2eShortDetailTimeout = true;
    const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
    Object.defineProperty(AbortSignal, "timeout", {
      configurable: true,
      value: (delay: number) =>
        nativeTimeout(delay === 30_000 && flags.__e2eShortDetailTimeout ? 150 : delay),
    });
  });
  const timeoutGate = createRequestGate();
  let attempts = 0;
  // 初回の表示の取得は timeout まで止める。開発の server（nightly）は StrictMode で effect を 2 回実行し、
  // 最初の取得を取り消して取り直すため、「1 回目の request」ではなく「再試行の前の request」をすべて止める。
  let holdDetail = true;
  const objectName = "TABLE_TIMEOUT";
  const items = [
    { name: objectName, owner: "APP", object_type: "table", row_count: 1, comment: "" },
  ];

  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: 1,
      table_count: 1,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-29T00:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", async (route) => {
    attempts += 1;
    if (holdDetail) await timeoutGate.promise;
    try {
      await fulfillJson(route, {
        name: objectName,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
        columns: [],
        ddl: "",
        warnings: [],
      });
    } catch {
      // timeout で破棄された最初の request は fulfill 不可でも正常。
    }
  });

  await page.goto(`/table-management?table=APP.${objectName}`);
  await expect(page.getByTestId("table-management-detail-error")).toContainText(
    "30秒以内に完了しませんでした",
  );
  await expect(page.getByRole("button", { name: `${objectName} を表示` })).toHaveAttribute(
    "aria-current",
    "true",
  );

  holdDetail = false;
  await page.evaluate(() => {
    (window as unknown as { __e2eShortDetailTimeout?: boolean }).__e2eShortDetailTimeout = false;
  });
  const attemptsBeforeRetry = attempts;
  timeoutGate.release();
  const retry = page.getByRole("button", { name: "再試行" });
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: objectName })).toBeVisible();
  // 再試行は 1 回だけ取り直す。
  expect(attempts).toBe(attemptsBeforeRetry + 1);
});

for (const scenario of scenarios) {
  test(`${scenario.title}の詳細エラーは局所表示され、キーボードで再試行できる`, async ({
    page,
  }) => {
    const objectName = `${scenario.prefix}_RETRY`;
    let detailAttempts = 0;
    // 再試行の前の取得はすべて失敗させる（開発の server の StrictMode は初回の取得を取り消して取り直す）。
    let failDetail = true;
    // 一覧は増分 API(/db-admin/objects)から取得する。
    await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
      fulfillJson(route, {
        runtime: "deterministic",
        owner: "APP",
        items: [
          {
            name: objectName,
            owner: "APP",
            object_type: scenario.objectType,
            row_count: 1,
            comment: "",
          },
        ],
        total: 1,
        table_count: scenario.objectType === "table" ? 1 : 0,
        view_count: scenario.objectType === "view" ? 1 : 0,
        next_cursor: null,
        refreshed_at: "2026-07-21T00:00:00+00:00",
        catalog_version: 1,
        warnings: [],
      }),
    );
    await page.route(scenario.apiPath, (route) =>
      fulfillJson(route, {
        runtime: "deterministic",
        items: [
          {
            name: objectName,
            owner: "APP",
            object_type: scenario.objectType,
            row_count: 1,
            comment: "",
          },
        ],
        refreshed_at: "2026-07-21T00:00:00+00:00",
        warnings: [],
      }),
    );
    await page.route(`${scenario.apiPath}/*`, async (route) => {
      detailAttempts += 1;
      if (failDetail) {
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ error_messages: ["詳細サービスが応答しませんでした。"] }),
        });
        return;
      }
      await fulfillJson(route, {
        name: objectName,
        owner: "APP",
        object_type: scenario.objectType,
        row_count: 1,
        comment: "",
        columns: [
          {
            column_name: "ID",
            logical_name: "識別子",
            data_type: "NUMBER",
            nullable: false,
            comment: "",
            sample_values: ["1"],
          },
        ],
        ddl: "",
        warnings: [],
      });
    });

    await page.goto(`${scenario.path}?${scenario.objectType}=APP.${objectName}`);

    const errorRegion = page.getByTestId(`${scenario.path.slice(1)}-detail-error`);
    await expect(errorRegion.getByRole("alert")).toContainText("詳細の取得に失敗しました");
    await expect(page.locator("main").getByRole("alert")).toHaveCount(1);
    failDetail = false;
    const attemptsBeforeRetry = detailAttempts;
    const retry = errorRegion.getByRole("button", { name: "再試行" });
    await retry.focus();
    await expect(retry).toBeFocused();
    await retry.press("Enter");

    await expect(page.getByRole("heading", { name: objectName })).toBeVisible();
    await expect(page.getByTestId("db-admin-detail-columns")).toContainText("識別子");
    // 再試行は 1 回だけ取り直す。
    expect(detailAttempts).toBe(attemptsBeforeRetry + 1);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      ),
    ).toBe(false);
  });
}

test("テーブル詳細は A→B→A の遅延応答でも最後に選んだ A だけを表示する", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile-375", "一覧と詳細を並べて選び直す流れは 2 ペイン（xl 以上）で確かめる（#2281）");
  const tableNames = ["TABLE_A", "TABLE_B"];
  let releaseTableB: (() => void) | undefined;
  let markTableBStarted: (() => void) | undefined;
  const tableBRelease = new Promise<void>((resolve) => {
    releaseTableB = resolve;
  });
  const tableBStarted = new Promise<void>((resolve) => {
    markTableBStarted = resolve;
  });
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: tableNames.map((name) => ({
        name,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
      })),
      refreshed_at: "2026-07-21T00:00:00+00:00",
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: tableNames.map((name) => ({
        name,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
      })),
      total: tableNames.length,
      table_count: tableNames.length,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-07-21T00:00:00+00:00",
      catalog_version: 1,
      warnings: [],
    }),
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", async (route) => {
    const tableName = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    if (tableName === "TABLE_B") {
      markTableBStarted?.();
      await tableBRelease;
    }
    try {
      await fulfillJson(route, {
        name: tableName,
        owner: "APP",
        object_type: "table",
        row_count: 1,
        comment: "",
        columns: [
          {
            column_name: `${tableName}_ID`,
            logical_name: `${tableName} の識別子`,
            data_type: "NUMBER",
            nullable: false,
            comment: "",
            sample_values: ["1"],
          },
        ],
        ddl: "",
        warnings: [],
      });
    } catch {
      // TABLE_B は後続選択時に client 側で abort されるため fulfill 不可でも正常。
    }
  });

  await page.goto("/table-management?table=APP.TABLE_A");
  await expect(page.getByRole("heading", { name: "TABLE_A" })).toBeVisible();
  await page.getByRole("button", { name: "TABLE_B を表示" }).click();
  await tableBStarted;
  await page.getByRole("button", { name: "TABLE_A を表示" }).click();
  await expect(page.getByRole("heading", { name: "TABLE_A" })).toBeVisible();
  releaseTableB?.();
  await page.waitForTimeout(100);

  await expect(page.getByRole("heading", { name: "TABLE_B" })).toHaveCount(0);
  await expect(page.getByTestId("db-admin-detail-columns")).toContainText("TABLE_A の識別子");
});

test("Excel/CSV 取込フォームは取込方法を表示せずファイル選択を全幅で表示する", { tag: "@responsive" }, async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");
  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );

  const importPanel = page.locator("#table-management-panel-import");
  await expect(importPanel).toBeVisible();
  const fileField = importPanel.getByTestId("table-import-file-field");
  await expect(fileField).toBeVisible();
  await expect(importPanel.getByText(/必須入力項目です。/)).toHaveCount(0);
  await expect(importPanel.locator('label[for="table-import-table-name"] span[aria-hidden="true"]')).toHaveText("必須");
  await expect(importPanel.locator('label[for="table-import-sheet-name"] span[aria-hidden="true"]')).toHaveText("必須");
  await expect(importPanel.getByTestId("table-import-file-field-input")).toHaveAttribute("aria-required", "true");
  await expect(importPanel.getByTestId("table-import-mode-field")).toHaveCount(0);
  await expect(importPanel.getByText("取込方法", { exact: true })).toHaveCount(0);
  await expect(importPanel.getByRole("combobox")).toHaveCount(0);

  const fileFieldBox = await fileField.boundingBox();
  const filePickerBox = await fileField.getByTestId("table-import-file-field-dropzone").boundingBox();
  const clearButtonBox = await fileField.getByRole("button", { name: "取込ファイル選択を解除" }).boundingBox();
  const fillsAvailableWidth = await fileField.evaluate((element) => {
    const parent = element.parentElement;
    return Boolean(
      parent &&
        Math.abs(element.getBoundingClientRect().width - parent.getBoundingClientRect().width) <= 1
    );
  });

  expect(fileFieldBox).not.toBeNull();
  expect(filePickerBox).not.toBeNull();
  expect(clearButtonBox).not.toBeNull();
  expect(fillsAvailableWidth).toBe(true);
  // ファイルの選択と、その横のクリアは同じ md の高さ（タッチ端末は 44px。#613）。
  const expectedHeight = await expectedControlHeight(page);
  expect(filePickerBox!.height).toBeGreaterThanOrEqual(expectedHeight);
  expect(clearButtonBox!.height).toBeGreaterThanOrEqual(expectedHeight);
  // ファイルの選択とクリアの間は gap-2（0.5rem。14px ルートで 7px）。px で書かず rem から求める（#800）。
  const controlGap = (await remToPx(page, 0.5)) - SUBPIXEL_TOLERANCE;
  if ((page.viewportSize()?.width ?? 0) < 640) {
    expect(clearButtonBox!.y).toBeGreaterThanOrEqual(filePickerBox!.y + filePickerBox!.height + controlGap);
    expect(filePickerBox!.width).toBeGreaterThanOrEqual(fileFieldBox!.width - 1);
  } else {
    expect(clearButtonBox!.x).toBeGreaterThanOrEqual(filePickerBox!.x + filePickerBox!.width + controlGap);
  }
  await expectNoHorizontalScroll(page);
});

test("テーブル管理は一覧と作成・取込パネルを同じ外枠で表示する", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");

  // 一覧 + 詳細（ListDetailLayout。#2281）は一覧と詳細の列がそれぞれ枠（カード）を持ち、外側の枠を付けない。
  const listShellStyle = await topLevelPanelStyle(page, "list");
  expect(listShellStyle.borderTopWidth).toBe("0px");
  expect(Number.parseFloat(listShellStyle.paddingTop)).toBe(0);
  for (const column of ["list", "detail"]) {
    const columnStyle = await page.getByTestId(`list-detail-table-management-${column}`).evaluate((node) => {
      const computed = window.getComputedStyle(node);
      return { backgroundColor: computed.backgroundColor, borderTopWidth: computed.borderTopWidth };
    });
    expect(columnStyle).toEqual({ backgroundColor: "rgb(255, 255, 255)", borderTopWidth: "1px" });
  }
  let listStyle: Awaited<ReturnType<typeof topLevelPanelStyle>> | null = null;

  // トップレベルはタブではなくツールバーのアクション。一覧が既定で、作成/取込は往復。
  for (const target of [
    { id: "create", buttonName: "テーブル作成" },
    { id: "import", buttonName: "Excel/CSV 取込(新規テーブル)" },
  ] as const) {
    await clickPageHeaderAction(page, "table-management-actions", target.buttonName);
    const panel = page.locator(`#table-management-panel-${target.id}`);
    await expect(panel).toBeVisible();
    // 作業（作成・取込）のパネルは同じ外枠（カード）。
    const taskStyle = await topLevelPanelStyle(page, target.id);
    expect(taskStyle.backgroundColor).toBe("rgb(255, 255, 255)");
    expect(taskStyle.borderTopWidth).toBe("1px");
    expect(Number.parseFloat(taskStyle.paddingTop)).toBeGreaterThan(0);
    if (listStyle) expect(taskStyle).toEqual(listStyle);
    listStyle = taskStyle;

    const hasPageHorizontalScroll = await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1
    );
    expect(hasPageHorizontalScroll).toBe(false);
    await page.getByRole("button", { name: "一覧へ戻る" }).click();
  }
});

test("テーブル作成フォームの見出し・実行ボタン・ステップはExcel/CSV取込と同じ階層で表示する", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");

  await page.getByTestId("table-management-actions").getByRole("button", { name: "テーブル作成" }).click();
  const createPanel = page.locator("#table-management-panel-create");
  const createHeading = createPanel.getByRole("heading", { name: "テーブル作成", level: 2 });
  const createButton = createPanel.getByRole("button", { name: "SQL 実行" });
  const createSteps = createPanel.getByTestId("table-create-steps");
  await expect(createHeading).toBeVisible();
  await expect(createPanel.getByText("実行できるのは CREATE TABLE / COMMENT ON / DROP TABLE のみです。")).toBeVisible();
  await expect(createPanel.getByText("Oracle への SQL 実行")).toBeVisible();
  await expect(createButton).toBeDisabled();
  // 実行ボタンは実行確認語フィールドの直下(同一枠内)に配置する統一レイアウト
  await expect(createPanel.getByTestId("execution-confirmation-field").getByRole("button", { name: "SQL 実行" })).toBeVisible();
  await expect(createSteps.getByText("SQL 入力")).toBeVisible();
  await expect(createSteps.getByText("実行確認")).toBeVisible();

  const createStyle = await createHeading.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      tagName: node.tagName,
      display: computed.display,
      alignItems: computed.alignItems,
      gap: computed.gap,
      fontSize: computed.fontSize,
      fontWeight: computed.fontWeight,
      color: computed.color,
    };
  });
  const createButtonStyle = await compactVisualStyle(createButton);
  const createStepStyle = await compactVisualStyle(createSteps.locator("li").first());

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );
  const importPanel = page.locator("#table-management-panel-import");
  const importHeading = importPanel.getByRole("heading", { name: "Excel/CSV 取込(新規テーブル)", level: 2 });
  const importButton = importPanel.getByRole("button", { name: "取込を実行" });
  const importSteps = importPanel.getByTestId("table-import-steps");
  await expect(importHeading).toBeVisible();
  await expect(importButton).toBeDisabled();
  await expect(importSteps.getByText("ファイル選択")).toBeVisible();
  await expect(importSteps.getByText("実行確認")).toBeVisible();

  const importStyle = await importHeading.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      tagName: node.tagName,
      display: computed.display,
      alignItems: computed.alignItems,
      gap: computed.gap,
      fontSize: computed.fontSize,
      fontWeight: computed.fontWeight,
      color: computed.color,
    };
  });
  const importButtonStyle = await compactVisualStyle(importButton);
  const importStepStyle = await compactVisualStyle(importSteps.locator("li").first());

  expect(createStyle).toEqual(importStyle);
  expect(createButtonStyle).toEqual(importButtonStyle);
  expect(createStepStyle).toEqual(importStepStyle);
});

test("テーブル管理はアクションボタンで作成・取込を開閉できる", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");

  const actions = page.getByTestId("table-management-actions");
  await expect(page.locator("#table-management-panel-list")).toBeVisible();

  await actions.getByRole("button", { name: "テーブル作成" }).click();
  await expect(page.locator("#table-management-panel-create")).toBeVisible();
  await expect(page.locator("#table-management-panel-list")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.locator("#table-management-panel-list")).toBeVisible();

  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );
  await expect(page.locator("#table-management-panel-import")).toBeVisible();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.locator("#table-management-panel-list")).toBeVisible();
});

for (const scenario of metadataScenarios) {
  test(`${scenario.title}は共通タブと単一カード枠で工程を切り替える`, async ({ page }) => {
    await mockMetadataManagementApi(page);
    await page.goto(scenario.path);

    const targetsStyle = await topLevelPanelStyle(page, "targets", scenario.idPrefix);
    expect(targetsStyle.backgroundColor).toBe("rgb(255, 255, 255)");
    expect(targetsStyle.borderTopWidth).toBe("1px");
    expect(Number.parseFloat(targetsStyle.paddingTop)).toBeGreaterThan(0);

    // データ管理系と同じく、工程はタブで切り替えてページ末尾の余分なスクロール余白を防ぐ。
    await expect(page.getByTestId(`${scenario.idPrefix}-steps`)).toBeVisible();
    await expect(page.getByRole("tab")).toHaveCount(3);
    await expect(page.getByRole("tab", { name: "対象選択", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(`#${scenario.idPrefix}-panel-input`)).toBeHidden();
    await expect(page.locator(`#${scenario.idPrefix}-panel-execute`)).toBeHidden();

    for (const target of [
      { id: "input", tabName: "入力確認・SQL生成" },
      { id: "execute", tabName: "SQL実行" },
    ] as const) {
      await page.getByRole("tab", { name: target.tabName, exact: true }).click();
      const panel = page.locator(`#${scenario.idPrefix}-panel-${target.id}`);
      await expect(panel).toBeVisible();
      expect(await topLevelPanelStyle(page, target.id, scenario.idPrefix)).toEqual(targetsStyle);
      await expect(page.locator(`#${scenario.idPrefix}-panel-targets`)).toBeHidden();
    }

    await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
    const executePanel = page.locator(`#${scenario.idPrefix}-panel-execute`);
    await expect(executePanel.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
    await expect(executePanel.getByLabel("Oracle に実行する")).toHaveCount(0);
    await expect(executePanel.getByRole("button", { name: "SQL 実行" })).toBeDisabled();
    await expectMainScrollDoesNotExposeTrailingBlank(executePanel);

    await page.getByRole("tab", { name: "対象選択", exact: true }).click();
    for (const id of ["targets"] as const) {
      const panel = page.locator(`#${scenario.idPrefix}-panel-${id}`);
      await expect(panel).toBeVisible();
      expect(await topLevelPanelStyle(page, id, scenario.idPrefix)).toEqual(targetsStyle);
      // 一覧が内部スクロールを持つ工程でも、ページ末尾に空白を作らない(#664)。
      await expectMainScrollDoesNotExposeTrailingBlank(panel);
    }

    const hasPageHorizontalScroll = await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1
    );
    expect(hasPageHorizontalScroll).toBe(false);
  });

  test(`${scenario.title}は 150% zoom 相当でも工程・対象名・操作ボタンを折り返さない`, async ({ page }) => {
    await page.setViewportSize({ width: 1365, height: 900 });
    await mockMetadataManagementApi(page);
    await page.goto(scenario.path);

    await expect(page.getByTestId(`${scenario.idPrefix}-steps`)).toBeVisible();

    const grid = page.getByTestId(`${scenario.idPrefix}-target`);
    await expect(grid.getByRole("option")).toHaveCount(30);
    await expectSingleLine(grid.getByText("META_TABLE_01"));
    await expectSingleLine(page.getByRole("button", { name: "情報を取得", exact: true }).locator("span").first());

    const scroll = await page.getByTestId(`${scenario.idPrefix}-target-scroll-region`).evaluate((node) => ({
      internalWidthStable: node.scrollWidth >= node.clientWidth,
      pageHorizontal:
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1,
    }));
    expect(scroll.internalWidthStable).toBe(true);
    expect(scroll.pageHorizontal).toBe(false);
  });
}

for (const scenario of prepareManagementScenarios) {
  test(`${scenario.title}はテーブル管理と同じタブ・パネル構造で表示する`, async ({ page }) => {
    await mockPrepareManagementApi(page, scenario.path);
    await page.goto(scenario.path);

    const isListDetail = (target: (typeof scenario.tabs)[number]) => "listDetail" in target && target.listDetail;
    const first = scenario.tabs.find((target) => !isListDetail(target))!;
    await page.getByRole("tab", { name: first.tabName }).click();
    const baseStyle = await topLevelPanelStyle(page, first.id, scenario.idPrefix);
    expect(baseStyle.backgroundColor).toBe("rgb(255, 255, 255)");
    expect(baseStyle.borderTopWidth).toBe("1px");
    expect(Number.parseFloat(baseStyle.paddingTop)).toBeGreaterThan(0);

    for (const target of scenario.tabs) {
      await page.getByRole("tab", { name: target.tabName }).click();
      const panel = page.locator(`#${scenario.idPrefix}-panel-${target.id}`);
      await expect(panel).toBeVisible();
      if (isListDetail(target)) {
        const style = await topLevelPanelStyle(page, target.id, scenario.idPrefix);
        expect(style.borderTopWidth).toBe("0px");
        expect(style.paddingTop).toBe("0px");
        await expect(panel.getByTestId(`list-detail-${scenario.idPrefix}-${target.id}`)).toBeVisible();
        await expectSingleLine(page.getByRole("tab", { name: target.tabName }).locator("span").last());
        continue;
      }
      expect(await topLevelPanelStyle(page, target.id, scenario.idPrefix)).toEqual(baseStyle);
      await expectSingleLine(page.getByRole("tab", { name: target.tabName }).locator("span").last());
    }

    const hasPageHorizontalScroll = await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1
    );
    expect(hasPageHorizontalScroll).toBe(false);
  });

  test(`${scenario.title}トップレベルタブはキーボードで切り替えられる`, async ({ page }) => {
    await mockPrepareManagementApi(page, scenario.path);
    await page.goto(scenario.path);

    const first = scenario.tabs[0]!;
    const second = scenario.tabs[1]!;
    const firstTab = page.getByRole("tab", { name: first.tabName });
    const secondTab = page.getByRole("tab", { name: second.tabName });

    await expect(firstTab).toHaveAttribute("aria-selected", "true");
    await firstTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(secondTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(`#${scenario.idPrefix}-panel-${second.id}`)).toBeVisible();

    await page.keyboard.press("Home");
    await expect(firstTab).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("End");
    await expect(page.getByRole("tab", { name: scenario.tabs[scenario.tabs.length - 1].tabName })).toHaveAttribute(
      "aria-selected",
      "true"
    );
  });

  test(`${scenario.title}は375px幅でもページ全体の横スクロールを出さない`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockPrepareManagementApi(page, scenario.path);
    await page.goto(scenario.path);

    await expect(page.getByRole("tab", { name: scenario.tabs[0].tabName })).toHaveAttribute("aria-selected", "true");
    const hasPageHorizontalScroll = await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1
    );
    expect(hasPageHorizontalScroll).toBe(false);
  });
}

test("コメント管理の対象選択は空状態を表示する", async ({ page }) => {
  await mockMetadataManagementApi(page, { empty: true });
  await page.goto("/comment-management");

  await expect(page.getByTestId("comment-management-steps")).toBeVisible();
  await expect(page.getByText("対象がありません")).toBeVisible();
  await expect(page.getByText("DB 構造を再取得してから確認してください。")).toBeVisible();
});

test("ビュー管理は一覧と作成・JOIN/WHERE パネルを同じ外枠で表示する", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[1]);
  await page.goto("/view-management");

  // 一覧 + 詳細（ListDetailLayout。#2281）は一覧と詳細の列がそれぞれ枠（カード）を持ち、外側の枠を付けない。
  const listShellStyle = await topLevelPanelStyle(page, "list", "view-management");
  expect(listShellStyle.borderTopWidth).toBe("0px");
  expect(Number.parseFloat(listShellStyle.paddingTop)).toBe(0);
  for (const column of ["list", "detail"]) {
    const columnStyle = await page.getByTestId(`list-detail-view-management-${column}`).evaluate((node) => {
      const computed = window.getComputedStyle(node);
      return { backgroundColor: computed.backgroundColor, borderTopWidth: computed.borderTopWidth };
    });
    expect(columnStyle).toEqual({ backgroundColor: "rgb(255, 255, 255)", borderTopWidth: "1px" });
  }
  let listStyle: Awaited<ReturnType<typeof topLevelPanelStyle>> | null = null;

  for (const target of [
    { id: "create", buttonName: "ビュー作成" },
    { id: "joinWhere", buttonName: "JOIN/WHERE 条件抽出" },
  ] as const) {
    await clickPageHeaderAction(page, "view-management-actions", target.buttonName);
    const panel = page.locator(`#view-management-panel-${target.id}`);
    await expect(panel).toBeVisible();
    const taskStyle = await topLevelPanelStyle(page, target.id, "view-management");
    expect(taskStyle.backgroundColor).toBe("rgb(255, 255, 255)");
    expect(taskStyle.borderTopWidth).toBe("1px");
    if (listStyle) expect(taskStyle).toEqual(listStyle);
    listStyle = taskStyle;
    await page.getByRole("button", { name: "一覧へ戻る" }).click();
  }

  const grid = page.getByTestId("view-management-grid");
  await expect(grid.locator("tbody tr")).toHaveCount(30);
  const ownerHeader = grid.getByRole("columnheader", { name: /所有者/ });
  if (await ownerHeader.count()) {
    await expectSingleLine(ownerHeader.locator("span").first());
  }
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /操作: APP\.VIEW_01/ })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: "詳細" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: "削除" })).toHaveCount(0);
});

test("ビュー管理はアクションボタンで作成・JOIN/WHERE を開閉できる", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[1]);
  await page.goto("/view-management");

  const actions = page.getByTestId("view-management-actions");
  await expect(page.locator("#view-management-panel-list")).toBeVisible();

  await actions.getByRole("button", { name: "ビュー作成" }).click();
  await expect(page.locator("#view-management-panel-create")).toBeVisible();
  await expect(page.locator("#view-management-panel-list")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.locator("#view-management-panel-list")).toBeVisible();

  await clickPageHeaderAction(page, "view-management-actions", "JOIN/WHERE 条件抽出");
  await expect(page.locator("#view-management-panel-joinWhere")).toBeVisible();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.locator("#view-management-panel-list")).toBeVisible();
});

test("Excel/CSV 取込の実行確認語は ADMIN_EXECUTE 固定で判定する", async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");
  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );

  const importPanel = page.locator("#table-management-panel-import");
  const confirmationField = importPanel.getByTestId("execution-confirmation-field");
  const executeButton = importPanel.getByRole("button", { name: "取込を実行" });
  await expect(importPanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  // 実行ボタンは実行確認語フィールドの直下(同一枠内)に配置する統一レイアウト
  await expect(confirmationField.getByRole("button", { name: "取込を実行" })).toBeVisible();

  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(importPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await expect(executeButton).toBeDisabled();

  await importPanel.getByLabel("Oracle 表名").fill("IMPORTED_ORDERS");
  await importPanel.getByLabel("実行確認語").fill("IMPORTED_ORDERS");
  await expect(confirmationField.getByText("不一致")).toBeVisible();
  await expect(executeButton).toBeDisabled();
});

// #1891: DB オブジェクトの一覧（追加読み込みの例外）の件数は ListToolbar の ListCount 1 か所。
// 絞り込みなし「全 N 件」、絞り込み中「N 件（全 M 件）」（M は絞り込みの無い同じ問い合わせの total）。
// 横あふれ・placeholder の切れ・欄の幅の段を 1280 / 1920px（desktop）と 375px（mobile-375）で確かめる。
const dbObjectListScreens = [
  {
    title: "テーブル管理",
    path: "/table-management",
    toolbar: "table-management-toolbar",
    query: "PAGED_TABLE_00",
    total: "全 149 件",
    filtered: "9 件（全 149 件）",
  },
  {
    title: "ビュー管理",
    path: "/view-management",
    toolbar: "view-management-toolbar",
    query: "PAGED_VIEW_001",
    total: "全 2 件",
    filtered: "1 件（全 2 件）",
  },
  {
    title: "データ管理のプレビュー",
    path: "/data-management",
    toolbar: "data-management-preview-toolbar",
    query: "PAGED_VIEW",
    total: "全 151 件",
    filtered: "2 件（全 151 件）",
  },
  {
    title: "コメント管理の対象",
    path: "/comment-management",
    toolbar: "comment-management-target-toolbar",
    query: "PAGED_VIEW",
    total: "全 151 件",
    filtered: "2 件（全 151 件）",
  },
] as const;

async function expectDbObjectLayoutChecks(page: Page) {
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  expect(await findClippedPlaceholders(page)).toEqual([]);
  if (page.viewportSize()!.width >= 640) expect(await findFieldsOffWidthScale(page)).toEqual([]);
}

for (const screen of dbObjectListScreens) {
  test(`${screen.title}の一覧は件数を ListToolbar の ListCount に出し、幅・placeholder が崩れない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    // 画面幅ごとに開き直して測るので、既定の 30 秒より長く取る。
    test.setTimeout(120_000);
    await mockPagedDbAdminObjectsApi(page);
    const widths = testInfo.project.name === "mobile-375" ? [375] : [1280, 1920];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(screen.path);
      const toolbar = page.getByTestId(screen.toolbar);
      const count = toolbar.locator("[data-list-count]");
      await expect(count).toHaveText(screen.total);
      // 件数は 1 か所（見出しのバッジに「N 件」を出さない）。
      await expect(page.getByText(/^全?\s?\d+\s?件$/u)).toHaveCount(1);
      await expectDbObjectLayoutChecks(page);
      await page.screenshot({ path: testInfo.outputPath(`${screen.toolbar}-${width}-light.png`) });
    }

    const toolbar = page.getByTestId(screen.toolbar);
    await toolbar.getByRole("searchbox", { name: "検索" }).fill(screen.query);
    await expect(toolbar.locator("[data-list-count]")).toHaveText(screen.filtered);
    await expectDbObjectLayoutChecks(page);
  });
}

test("DB オブジェクトの一覧は読み込み中に件数を出さず、スケルトンで覆う", { tag: "@responsive" }, async ({ page }) => {
  await mockPagedDbAdminObjectsApi(page);
  const gate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    await gate.promise;
    await route.fallback();
  });
  await page.goto("/table-management");
  await expect(page.getByTestId("table-management-list-skeleton")).toBeVisible();
  await expect(page.getByTestId("table-management-toolbar").locator("[data-list-count]")).toHaveCount(0);
  await expectDbObjectLayoutChecks(page);
  gate.release();
  await expect(page.getByTestId("table-management-toolbar").locator("[data-list-count]")).toHaveText("全 149 件");
  // 追加読み込みのフッターは「N / M 件を表示」のまま残す（page-archetypes の例外）。
  await expect(page.getByTestId("table-management-footer")).toContainText("100 / 149 件を表示");
});

test("データ管理の CSV アップロードは件数を ListCount に出し、欄の幅の段と placeholder が崩れない", { tag: "@responsive" }, async ({ page }) => {
  test.setTimeout(90_000);
  await mockPagedDbAdminObjectsApi(page);
  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const toolbar = page.getByTestId("data-csv-table-toolbar");
  await expect(toolbar.locator("[data-list-count]")).toHaveText("全 149 件");
  await expect(page.getByTestId("data-csv-mode-field").getByRole("combobox")).toBeVisible();
  await expectDbObjectLayoutChecks(page);
  // 検索中は「N 件（全 M 件）」（M は表の全件。検索語の無いオブジェクトの一覧の table_count）。
  await page.getByTestId("data-csv-table-toolbar").getByRole("searchbox").fill("PAGED_TABLE_00");
  await expect(page.getByTestId("data-csv-table-toolbar").locator("[data-list-count]")).toHaveText("9 件（全 149 件）");
});

test("テーブル取込のフォームは FormGrid の欄の幅の段と placeholder が崩れない", { tag: "@responsive" }, async ({ page }) => {
  await mockObjectManagementApi(page, scenarios[0]);
  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const importPanel = page.locator("#table-management-panel-import");
  await expect(importPanel.getByLabel("Oracle 表名")).toBeVisible();
  // 表名・シート名は名前なので md（20rem。375px は全幅）。
  const tableName = importPanel.getByLabel("Oracle 表名");
  const expectedWidth = page.viewportSize()!.width >= 640 ? 20 * 14 : null;
  if (expectedWidth) {
    expect(Math.abs((await tableName.boundingBox())!.width - expectedWidth)).toBeLessThanOrEqual(2);
  }
  await expectDbObjectLayoutChecks(page);
});
