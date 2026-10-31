import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { expectSingleSpinner } from "./_helpers/single-spinner";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

const schemaCatalog = {
  refreshed_at: "2026-06-21T10:00:00.000Z",
  tables: [
    ...Array.from({ length: 32 }, (_, index) => {
      const count = String(index + 1).padStart(2, "0");
      return {
        table_name: `TABLE_${count}`,
        logical_name: `表論理名_${count}`,
        owner: "APP",
        table_type: "TABLE",
        comment: `表コメント_${count}`,
        row_count: null,
        columns: [],
        constraints: [],
      };
    }),
    ...Array.from({ length: 32 }, (_, index) => {
      const count = String(index + 1).padStart(2, "0");
      return {
        table_name: `VIEW_${count}`,
        logical_name: `ビュー論理名_${count}`,
        owner: "APP",
        table_type: "VIEW",
        comment: `ビューコメント_${count}`,
        row_count: null,
        columns: [],
        constraints: [],
      };
    }),
    {
      table_name: "SYS$AUDIT",
      logical_name: "システム監査",
      owner: "SYS",
      table_type: "TABLE",
      comment: "system table",
      row_count: null,
      columns: [],
      constraints: [],
    },
    {
      table_name: "V_$SESSION",
      logical_name: "システムセッション",
      owner: "SYS",
      table_type: "VIEW",
      comment: "system view",
      row_count: null,
      columns: [],
      constraints: [],
    },
  ],
};

const profiles = [
  {
    id: "default",
    name: "既定プロファイル",
    category: "既定プロファイル",
    description: "許可オブジェクトの表示確認",
    allowed_tables: ["APP.TABLE_01"],
    allowed_views: ["APP.VIEW_02"],
    glossary: {},
    sql_rules: ["SELECT のみ"],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    archived: false,
  },
];

type MockDbObjectSummary = {
  name: string;
  owner: string;
  object_type: string;
  row_count: number | null;
  comment: string;
};

// backend GET /nl2sql/profiles/{id}/ontology-view の profile スコープ済み応答(縮約)。
// 列・schema ノードは画面側で省かれるため、表示は表 2 + ビュー 1 = 3 ノード、FK 1 関係になる。
const profileOntologyView = {
  profile_ontology_view: {
    id: "profile-view:default",
    profile_id: "default",
    ontology_revision_id: "ontology_revision:fp:3",
    etag: "view-etag-1",
    node_ids: [
      "table:APP:TABLE_01",
      "table:APP:TABLE_03",
      "view:APP:VIEW_02",
      "column:APP:TABLE_01:ID",
    ],
    edge_ids: ["fk:APP:TABLE_03:FK_T3_T1", "contains:APP:TABLE_01:ID"],
    allowed_path_ids: [],
    table_usages_ja: {},
    draft_node_overrides: [],
    draft_edge_overrides: [],
  },
  ontology_graph: {
    revision: {
      id: "ontology_revision:fp:3",
      version: 3,
      status: "published",
      schema_fingerprint: "fp",
      etag: "rev-etag-3",
    },
    nodes: [
      {
        id: "table:APP:TABLE_01",
        kind: "table",
        business_name_ja: "表論理名_01",
        review_status: "approved",
        physical_mappings: [
          { object_ref: { owner: "APP", object_name: "TABLE_01", object_type: "table" } },
        ],
      },
      {
        id: "table:APP:TABLE_03",
        kind: "table",
        business_name_ja: "表論理名_03",
        review_status: "approved",
        physical_mappings: [
          { object_ref: { owner: "APP", object_name: "TABLE_03", object_type: "table" } },
        ],
      },
      {
        id: "view:APP:VIEW_02",
        kind: "view",
        business_name_ja: "ビュー論理名_02",
        review_status: "approved",
        physical_mappings: [
          { object_ref: { owner: "APP", object_name: "VIEW_02", object_type: "view" } },
        ],
      },
      {
        id: "column:APP:TABLE_01:ID",
        kind: "column",
        business_name_ja: "ID",
        review_status: "approved",
      },
    ],
    edges: [
      {
        id: "fk:APP:TABLE_03:FK_T3_T1",
        kind: "foreign_key",
        source_node_id: "table:APP:TABLE_03",
        target_node_id: "table:APP:TABLE_01",
        relationship_name_ja: "表論理名_01 を参照",
        cardinality: "many_to_one",
        review_status: "approved",
      },
      {
        id: "contains:APP:TABLE_01:ID",
        kind: "contains",
        source_node_id: "table:APP:TABLE_01",
        target_node_id: "column:APP:TABLE_01:ID",
        relationship_name_ja: "含む",
        review_status: "approved",
      },
    ],
  },
};

async function mockProfileApi(
  page: Page,
  options: {
    catalog?: typeof schemaCatalog;
    tableItems?: MockDbObjectSummary[];
    viewItems?: MockDbObjectSummary[];
    profileItems?: typeof profiles;
    schemaObjectTotals?: Partial<Record<"TABLE" | "VIEW", number>>;
    profileSearchTotal?: number;
  } = {}
) {
  const tableItems = options.tableItems ?? [
    { name: "TABLE_01", owner: "APP", object_type: "TABLE", row_count: null, comment: "table" },
    { name: "SYS$AUDIT", owner: "SYS", object_type: "TABLE", row_count: null, comment: "system" },
  ];
  const viewItems = options.viewItems ?? [
    { name: "VIEW_02", owner: "APP", object_type: "VIEW", row_count: null, comment: "view" },
    { name: "V_$SESSION", owner: "SYS", object_type: "VIEW", row_count: null, comment: "system" },
  ];
  const catalog = options.catalog ?? schemaCatalog;
  const profileItems = options.profileItems ?? profiles;
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, options.catalog ?? schemaCatalog));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "cross-schema-test",
      refreshed_at: catalog.refreshed_at,
      object_count: catalog.tables.length,
      column_count: 0,
      change_token: 1,
      etag: "cross-schema-test",
    })
  );
  await page.route("**/api/schema/owners", (route) => {
    const counts = new Map<string, { table_count: number; view_count: number }>();
    for (const object of catalog.tables.filter((item) => !item.table_name.includes("$"))) {
      const current = counts.get(object.owner) ?? { table_count: 0, view_count: 0 };
      if (["VIEW", "MATERIALIZED VIEW"].includes(object.table_type.toUpperCase())) {
        current.view_count += 1;
      } else {
        current.table_count += 1;
      }
      counts.set(object.owner, current);
    }
    return fulfillJson(route, {
      current_owner: "APP",
      owners: [...counts.entries()].map(([owner, value]) => ({
        owner,
        is_current: owner === "APP",
        ...value,
      })),
      excluded_oracle_maintained_count: 2,
    });
  });
  await page.route("**/api/schema/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "";
    // backend と同じく owner は Oracle の引用規則で解釈する（`"Sales"` は小文字を含む user、#563）。
    const rawOwner = (url.searchParams.get("owner") ?? "").trim();
    const owner = /^".*"$/u.test(rawOwner) ? rawOwner.slice(1, -1) : rawOwner.toUpperCase();
    const query = (url.searchParams.get("q") ?? "").toLowerCase();
    const items = catalog.tables
      .filter((object) => !owner || object.owner === owner)
      .filter((object) => !type || object.table_type.toUpperCase() === type.toUpperCase())
      .filter((object) =>
        [
          object.owner,
          `${object.owner}.${object.table_name}`,
          object.table_name,
          object.logical_name,
          object.comment,
        ]
          .join(" ")
          .toLowerCase()
          .includes(query)
      )
      .map((object) => ({
        owner: object.owner,
        object_name: object.table_name,
        object_type: object.table_type,
        logical_name: object.logical_name,
        comment: object.comment,
        row_count: object.row_count,
        column_count: object.columns.length,
        last_ddl_at: "",
      }));
    const total = options.schemaObjectTotals?.[type.toUpperCase() as "TABLE" | "VIEW"] ?? items.length;
    return fulfillJson(route, {
      items,
      next_cursor: null,
      total,
      catalog_version: 1,
    });
  });
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
  await page.route("**/api/nl2sql/profiles", async (route) => {
    if (route.request().method() === "GET") {
      await fulfillJson(route, profileItems);
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/nl2sql/profiles/search?*", (route) => {
    const query = (new URL(route.request().url()).searchParams.get("q") ?? "").trim().toLowerCase();
    const filteredProfiles = profileItems.filter(
      (profile) =>
        !profile.archived &&
        (!query ||
          profile.name.toLowerCase().includes(query) ||
          (profile.category ?? "").toLowerCase().includes(query) ||
          (profile.description ?? "").toLowerCase().includes(query))
    );
    const total = query ? filteredProfiles.length : (options.profileSearchTotal ?? filteredProfiles.length);
    return fulfillJson(route, {
      items: filteredProfiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-19T00:00:00Z",
      })),
      next_cursor: total > filteredProfiles.length ? "profile-page-2" : null,
      total,
      change_token: 1,
    });
  });
  await page.route("**/api/nl2sql/profiles/*", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/search")) return route.fallback();
    const detailMatch = pathname.match(/^\/api\/nl2sql\/profiles\/([^/]+)$/);
    if (!detailMatch) return route.fallback();
    const profileId = detailMatch[1];
    const profile = profileItems.find((item) => item.id === profileId) ?? profileItems[0];
    return fulfillJson(route, { ...profile, etag: `etag-${profile.id}` });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", (route) =>
    fulfillJson(route, { source_documents: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) =>
    fulfillJson(route, profileOntologyView)
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "",
      draft_revision: null,
      published_revision: null,
      draft_etag: "",
      published_at: null,
    })
  );
}

/**
 * 一覧の高さは手書きの rem ではなく、DataTable の visibleRows（md 未満 5 行・md 以上 8 行）の実測で決まる（#403）。
 * 行が表示行数より少なければ、その行数ぶんの高さになる。
 */
async function expectVisibleRowsMaxHeight(region: Locator, rows: number) {
  await expect
    .poll(() =>
      region.evaluate((node, limit) => {
        const scroller = node as HTMLElement;
        const table = scroller.querySelector("table");
        const dataRows = Array.from(table?.tBodies[0]?.rows ?? []).filter(
          (row) => row.dataset.rowKind === "data"
        );
        const last = dataRows.slice(0, limit).at(-1);
        if (!table || !last) return Number.NaN;
        const chrome = scroller.offsetHeight - scroller.clientHeight;
        const expected = Math.ceil(last.getBoundingClientRect().bottom - table.getBoundingClientRect().top + chrome);
        return Math.abs(Number.parseFloat(getComputedStyle(scroller).maxHeight) - expected);
      }, rows)
    )
    .toBeLessThanOrEqual(1);
}

async function expectNoDocumentHorizontalOverflow(page: Page) {
  const width = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    bodyScrollWidth: document.body.scrollWidth,
    bodyClientWidth: document.body.clientWidth,
  }));
  expect(width.scrollWidth).toBeLessThanOrEqual(width.clientWidth + 1);
  expect(width.bodyScrollWidth).toBeLessThanOrEqual(width.bodyClientWidth + 1);
}

for (const [label, grants, showsAccess] of [
  ["ロール管理だけ", ["menu.profiles", "menu.security_roles"], false],
  ["権限管理あり", ["menu.profiles", "menu.security_permissions"], true],
] as const) {
  test(`業務プロファイルの利用可能ロールは権限管理の権限でだけ取得する（${label}。#206）`, async ({ page }) => {
    await page.unroute("**/api/auth/me");
    await page.route("**/api/auth/me", (route) =>
      fulfillJson(route, {
        ...systemAdminMe,
        role_codes: ["PROFILE_EDITOR"],
        is_system_admin: false,
        permissions: [...grants],
      })
    );
    let accessRequests = 0;
    await page.unroute("**/api/security/profile-access/profiles**");
    await page.route("**/api/security/profile-access/profiles**", (route) => {
      accessRequests += 1;
      // 選択中の業務プロファイルだけを ids で読む（#608）。
      expect(new URL(route.request().url()).searchParams.getAll("ids")).toEqual([profiles[0].id]);
      return fulfillJson(route, {
        items: [
          {
            id: profiles[0].id,
            name: profiles[0].name,
            category: "",
            description: "",
            archived: false,
            // ロールは名前とコードを添えて返る。画面はロール名を出し、ID（UUID）・コードは出さない（#1682）。
            allowed_roles: [
              {
                role_id: "00000000-0000-0000-0000-000000000001",
                role_code: "SYSTEM_ADMIN",
                display_name: "システム管理者",
              },
              { role_id: "6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5", role_code: "ROLE_A", display_name: "ロール A" },
            ],
          },
        ],
        total: 1,
        limit: 1,
        offset: 0,
        has_next: false,
      });
    });
    await mockProfileApi(page);

    await page.goto(`/profiles?profile=${profiles[0].id}`);

    await expect(page.locator("#profile-name")).toHaveValue(profiles[0].name);
    await expect(page).not.toHaveURL(/\/forbidden$/u);
    if (showsAccess) {
      await expect(page.getByText("利用可能ロール", { exact: true })).toBeVisible();
      const accessRoles = page.getByRole("list", { name: "利用可能ロール" });
      await expect(accessRoles.getByRole("listitem")).toHaveText(["システム管理者", "ロール A"]);
      // ロールの ID（UUID）とコードは文字にも属性にも出さない（#1682）。
      await expect(accessRoles).not.toContainText("SYSTEM_ADMIN");
      await expect(accessRoles).not.toContainText("ROLE_A");
      await expect(page.locator("main")).not.toContainText(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/u);
      expect(await page.locator("main").innerHTML()).not.toContain("00000000-0000-0000-0000-000000000001");
      expect(accessRequests).toBeGreaterThan(0);
    } else {
      // ロール管理だけの利用者は API が 403 になるため取りに行かない（403 は画面全体を権限エラーへ切り替える）。
      await expect(page.getByText("利用可能ロール", { exact: true })).toHaveCount(0);
      expect(accessRequests).toBe(0);
    }
  });
}

test("業務プロファイル一覧検索はAPI totalを件数表示に使い検索結果だけを表示する", { tag: "@responsive" }, async ({ page }) => {
  const searchableProfiles = [
    {
      ...profiles[0],
      id: "profile-dept",
      name: "PROFILE_DEPT",
      category: "DEPT",
      description: "部署 profile",
      allowed_tables: ["APP.TABLE_01"],
      allowed_views: [],
    },
    {
      ...profiles[0],
      id: "profile-emp",
      name: "PROFILE_EMP",
      category: "EMPLOYEE",
      description: "社員 profile",
      allowed_tables: ["APP.TABLE_02"],
      allowed_views: [],
    },
  ];
  await mockProfileApi(page, {
    profileItems: searchableProfiles,
    profileSearchTotal: 128,
  });

  await page.goto("/profiles");

  const listPanel = page.locator("#profile-management-panel-list");
  const profileList = page.getByTestId("profile-management-list");
  // 件数はツールバーの ListCount 1 か所（#1891）。
  await expect(page.getByTestId("profile-list-toolbar").getByRole("status")).toHaveText("全 128 件");
  await expect(listPanel.getByText("128 件", { exact: true })).toHaveCount(0);
  await expect(profileList).toHaveAttribute("role", "region");
  await expect(profileList).toHaveAttribute(
    "aria-label",
    "プロファイル一覧。必要に応じて縦方向にスクロールできます。"
  );
  await expect(profileList).toHaveAttribute("tabindex", "0");
  await expectVisibleRowsMaxHeight(profileList, (page.viewportSize()?.width ?? 0) >= 768 ? 8 : 5);
  await expect(page.getByText("2 / 128 件を表示")).toHaveCount(0);
  await expect(
    page.getByTestId("profile-management-load-more").getByRole("button", { name: "さらに読み込む" })
  ).toBeVisible();
  await expect(page.getByTestId("profile-management-load-more")).not.toContainText("件を表示");
  await expect(listPanel.getByText("PROFILE_DEPT", { exact: true })).toBeVisible();
  await expect(listPanel.getByText("PROFILE_EMP", { exact: true })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);

  const search = page.getByRole("searchbox", { name: "プロファイル検索" });
  await search.fill("PROFILE_DEPT");

  await expect(listPanel.getByText("PROFILE_DEPT", { exact: true })).toBeVisible();
  await expect(listPanel.getByText("PROFILE_EMP", { exact: true })).toHaveCount(0);
  // 絞り込み中は「一致した件数（全 M 件）」。M は検索なしの同じ問い合わせの total（#1891）。
  await expect(page.getByTestId("profile-list-toolbar").getByRole("status")).toHaveText("1 件（全 128 件）");
  await expect(page.getByText("1 / 1 件を表示")).toHaveCount(0);
  await expect(page.getByTestId("profile-management-load-more")).toHaveCount(0);

  await search.fill("NO_MATCH_PROFILE");
  await expect(listPanel.getByText("一致するプロファイルがありません")).toBeVisible();
  await expect(page.getByTestId("profile-management-load-more")).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 900 });
  await search.fill("PROFILE_EMP");
  await expect(listPanel.getByText("PROFILE_EMP", { exact: true })).toBeVisible();
  await expect(listPanel.getByText("PROFILE_DEPT", { exact: true })).toHaveCount(0);
  await expectVisibleRowsMaxHeight(profileList, 5);
  await expectNoDocumentHorizontalOverflow(page);
  // 区切りのない名前でも名前セルの内容が許可表の列へはみ出さない（#535）。
  const nameCellOverflow = await page.getByTestId("profile-management-grid").locator("tbody td:first-child").evaluateAll((cells) =>
    cells.flatMap((cell) => {
      const cellRight = cell.getBoundingClientRect().right;
      return Array.from(cell.querySelectorAll("button, button > span"))
        .map((node) => node.getBoundingClientRect().right)
        .filter((right) => right > cellRight + 1)
        .map((right) => `${cell.textContent}: ${right} > ${cellRight}`);
    })
  );
  expect(nameCellOverflow).toEqual([]);
});

test("業務プロファイルの対象オブジェクト件数は取得済み件数と API total を分けて表示する", async ({ page }) => {
  const largeCatalog = {
    ...schemaCatalog,
    tables: [
      ...Array.from({ length: 102 }, (_, index) => {
        const count = String(index + 1).padStart(2, "0");
        return {
          table_name: `TABLE_${count}`,
          logical_name: `表論理名_${count}`,
          owner: "APP",
          table_type: "TABLE",
          comment: `表コメント_${count}`,
          row_count: null,
          columns: [],
          constraints: [],
        };
      }),
      ...Array.from({ length: 2 }, (_, index) => {
        const count = String(index + 1).padStart(2, "0");
        return {
          table_name: `VIEW_${count}`,
          logical_name: `ビュー論理名_${count}`,
          owner: "APP",
          table_type: "VIEW",
          comment: `ビューコメント_${count}`,
          row_count: null,
          columns: [],
          constraints: [],
        };
      }),
    ],
  };
  const countProfiles = [
    {
      ...profiles[0],
      id: "count-profile",
      name: "件数確認プロファイル",
      allowed_tables: ["APP.TABLE_01"],
      allowed_views: ["APP.VIEW_01", "APP.VIEW_02"],
    },
  ];

  await mockProfileApi(page, {
    catalog: largeCatalog,
    profileItems: countProfiles,
    schemaObjectTotals: { TABLE: 310, VIEW: 125 },
  });

  await page.goto("/profiles?profile=count-profile");

  await expect(page.getByTestId("profile-object-search-toolbar")).toContainText(
    "104 / 435 件を表示、選択 3 件"
  );
  await expect(page.getByTestId("profile-allowed-table-list-footer")).toContainText(
    "102 / 310 件を表示、選択 1 件"
  );
  await expect(page.getByTestId("profile-allowed-view-list-footer")).toContainText(
    "2 / 125 件を表示、選択 2 件"
  );
});

test("業務プロファイル基本情報は名称とカテゴリの行を揃えて表示する", { tag: "@responsive" }, async ({ page }) => {
  await mockProfileApi(page);
  await page.goto("/profiles?profile=new");

  await expect(page.getByRole("heading", { name: "新規プロファイル" })).toBeVisible();

  const nameInput = page.locator("#profile-name");
  const categoryInput = page.locator("#profile-category");
  await expect(nameInput).toBeVisible();
  await expect(categoryInput).toBeVisible();
  // 必須バッジは aria-hidden（入力側の aria-required で伝える）なので、読み上げ名はラベルだけになる。
  await expect(nameInput).toHaveAccessibleName("名称");
  await expect(categoryInput).toHaveAccessibleName("カテゴリ");

  const layout = await page.evaluate(() => {
    const box = (selector: string) => {
      const rect = document.querySelector(selector)?.getBoundingClientRect();
      if (!rect) return null;
      return {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        right: rect.right,
        bottom: rect.bottom,
      };
    };
    return {
      viewportWidth: window.innerWidth,
      nameLabel: box('label[for="profile-name"]'),
      categoryLabel: box('label[for="profile-category"]'),
      nameInput: box("#profile-name"),
      categoryInput: box("#profile-category"),
      helper: box("#profile-name-helper"),
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  });

  expect(layout.nameLabel).not.toBeNull();
  expect(layout.categoryLabel).not.toBeNull();
  expect(layout.nameInput).not.toBeNull();
  expect(layout.categoryInput).not.toBeNull();
  expect(layout.helper).not.toBeNull();
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth + 1);

  if (layout.viewportWidth >= 768) {
    expect(Math.abs(layout.nameLabel!.y - layout.categoryLabel!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(layout.nameInput!.y - layout.categoryInput!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(layout.nameInput!.height - layout.categoryInput!.height)).toBeLessThanOrEqual(1);
    expect(Math.abs(layout.nameInput!.width - layout.categoryInput!.width)).toBeLessThanOrEqual(1);
    expect(layout.helper!.y).toBeGreaterThan(layout.nameInput!.bottom - 1);
    expect(layout.helper!.height).toBeLessThanOrEqual(22);
    expect(layout.helper!.right).toBeGreaterThanOrEqual(layout.categoryInput!.right - 1);
  } else {
    expect(layout.nameLabel!.y).toBeLessThan(layout.nameInput!.y);
    expect(layout.nameInput!.y).toBeLessThan(layout.helper!.y);
    expect(layout.helper!.y).toBeLessThan(layout.categoryLabel!.y);
    expect(layout.categoryLabel!.y).toBeLessThan(layout.categoryInput!.y);
  }
});

test("業務プロファイルの更新操作はテーブル管理と同じ文言・順序・階層で動作する", { tag: "@responsive" }, async ({ page }) => {
  let profileSearchRequests = 0;
  let schemaRefreshSubmissions = 0;
  let schemaRefreshPolls = 0;

  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      new URL(request.url()).pathname === "/api/nl2sql/profiles/search"
    ) {
      profileSearchRequests += 1;
    }
  });
  await mockProfileApi(page);
  await page.route("**/api/schema/refresh-jobs**", async (route) => {
    // coordinator の active-job discovery はポーリング回数に数えない(即 done 化を防ぐ)。
    if (route.request().url().includes("/refresh-jobs/active")) {
      await fulfillJson(route, { active_job: null });
      return;
    }
    if (route.request().method() === "POST") {
      schemaRefreshSubmissions += 1;
      await fulfillJson(route, {
        job_id: "profile-schema-refresh",
        status: "pending",
        created_at: "2026-07-23T00:00:00Z",
        scanned_objects: 0,
        changed_objects: 0,
        deleted_objects: 0,
        catalog_version: 1,
        error_code: "",
      });
      return;
    }

    schemaRefreshPolls += 1;
    const done = schemaRefreshPolls >= 2;
    await fulfillJson(route, {
      job_id: "profile-schema-refresh",
      status: done ? "done" : "running",
      created_at: "2026-07-23T00:00:00Z",
      scanned_objects: done ? schemaCatalog.tables.length : 10,
      changed_objects: done ? 2 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 1,
      error_code: "",
    });
  });
  await page.goto("/profiles");

  await expect(page.getByRole("region", { name: "業務プロファイル状態" })).toHaveCount(0);
  await expect(page.getByText("許可オブジェクト", { exact: true })).toHaveCount(0);

  const actions = page.getByTestId("profile-management-actions");
  // 共有 PageHeader(@production-ready/ui)の並び順は danger → utility → secondary → primary(主操作が右端、
  // docs/design-system/README.md §7 #11)。lg 未満では主操作以外を「その他の操作」メニュー(上から重要な順)へ畳む。
  const isCompactHeader = (page.viewportSize()?.width ?? 0) < 1024;
  const createButton = actions.getByRole("button", { name: "新規作成", exact: true });
  await expect(createButton).toHaveClass(/\bbg-accent-emphasis\b/);

  if (isCompactHeader) {
    const moreButton = actions.getByRole("button", { name: "その他の操作" });
    await expect(actions.getByRole("button")).toHaveText(["その他の操作", "新規作成"]);
    await expect(moreButton).toHaveAttribute("aria-haspopup", "menu");
    await expect(moreButton).toHaveAttribute("aria-expanded", "false");
    await createButton.focus();
    await page.keyboard.press("Shift+Tab");
    await expect(moreButton).toBeFocused();
    await moreButton.press("Enter");

    const menu = page.getByRole("menu", { name: "その他の操作" });
    await expect(moreButton).toHaveAttribute("aria-expanded", "true");
    await expect(menu.getByRole("menuitem")).toHaveText([
      "最新情報を取得",
      "DB 構造を再取得",
    ]);
    await expect(menu.getByRole("menuitem", { name: "最新情報を取得" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByRole("menuitem", { name: "DB 構造を再取得" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByRole("menuitem", { name: "最新情報を取得" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(moreButton).toBeFocused();
    await expect(moreButton).toHaveAttribute("aria-expanded", "false");

    const requestsBeforeRefresh = profileSearchRequests;
    await moreButton.click();
    await menu.getByRole("menuitem", { name: "最新情報を取得" }).click();
    await expect.poll(() => profileSearchRequests).toBeGreaterThan(requestsBeforeRefresh);
    expect(schemaRefreshSubmissions).toBe(0);

    await moreButton.click();
    await menu.getByRole("menuitem", { name: "DB 構造を再取得" }).click();
    await expect.poll(() => schemaRefreshSubmissions).toBe(1);
    await expect(page.getByText(/DB 構造(再取得|差分同期): /).first()).toBeVisible();
    await moreButton.click();
    await expect(menu.getByRole("menuitem", { name: "DB 構造を再取得" })).toBeDisabled();
    await expect.poll(() => schemaRefreshPolls).toBeGreaterThanOrEqual(2);
    await expect(page.getByText(/DB 構造(再取得|差分同期): 完了/)).toHaveCount(0);
    await expect(menu.getByRole("menuitem", { name: "DB 構造を再取得" })).toBeEnabled();

  } else {
    const actionButtons = actions.getByRole("button");
    const refreshButton = actions.getByRole("button", { name: "最新情報を取得", exact: true });
    const schemaRefreshButton = actions.getByRole("button", {
      name: "DB 構造を再取得",
      exact: true,
    });

    await expect(actionButtons).toHaveText([
      "最新情報を取得",
      "DB 構造を再取得",
      "新規作成",
    ]);
    // 更新系は utility。共有 PageHeader は utility を secondary と同じ枠付きの見た目で描く（UX 契約 buttons.md §5）。
    for (const button of [refreshButton, schemaRefreshButton]) {
      await expect(button).toHaveClass(/\bborder-border-control\b/);
      await expect(button).toHaveClass(/\bbg-surface\b/);
      await expect(button).not.toHaveClass(/\bbg-transparent\b/);
    }

    const [createBox, refreshBox, schemaRefreshBox] = await Promise.all([
      createButton.boundingBox(),
      refreshButton.boundingBox(),
      schemaRefreshButton.boundingBox(),
    ]);
    expect(createBox).not.toBeNull();
    expect(refreshBox).not.toBeNull();
    expect(schemaRefreshBox).not.toBeNull();
    const visuallyAfter = (
      previous: NonNullable<typeof createBox>,
      next: NonNullable<typeof createBox>
    ) => next.y > previous.y + previous.height / 2 || next.x > previous.x;
    expect(visuallyAfter(refreshBox!, schemaRefreshBox!)).toBe(true);
    expect(visuallyAfter(schemaRefreshBox!, createBox!)).toBe(true);

    await refreshButton.focus();
    await page.keyboard.press("Tab");
    await expect(schemaRefreshButton).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(createButton).toBeFocused();

    const requestsBeforeRefresh = profileSearchRequests;
    await refreshButton.click();
    await expect.poll(() => profileSearchRequests).toBeGreaterThan(requestsBeforeRefresh);
    expect(schemaRefreshSubmissions).toBe(0);

    await schemaRefreshButton.click();
    await expect.poll(() => schemaRefreshSubmissions).toBe(1);
    await expect(page.getByText(/DB 構造(再取得|差分同期): /).first()).toBeVisible();
    await expect(schemaRefreshButton).toBeDisabled();
    await expect.poll(() => schemaRefreshPolls).toBeGreaterThanOrEqual(2);
    await expect(page.getByText(/DB 構造(再取得|差分同期): 完了/)).toHaveCount(0);
    await expect(schemaRefreshButton).toBeEnabled();

  }

  const viewportWidth = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(viewportWidth.scrollWidth).toBeLessThanOrEqual(viewportWidth.clientWidth + 1);
});

test("業務プロファイルは表とビューを固定高リストで管理できる", { tag: "@responsive" }, async ({ page }) => {
  let savedPayload: Record<string, unknown> | null = null;
  await mockProfileApi(page);
  await page.route("**/api/nl2sql/profiles/default**", async (route) => {
    if (route.request().url().includes("/ontology-view")) {
      await route.fallback();
      return;
    }
    // 保存の本文は PATCH だけから取る（GET は詳細の取り直し）。
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    }
    await fulfillJson(route, { ...profiles[0], ...savedPayload, id: "default" });
  });

  await page.goto("/profiles");

  const actions = page.getByTestId("profile-management-actions");
  await expect(actions).toBeVisible();
  const refreshButton = actions.getByRole("button", { name: "最新情報を取得", exact: true });
  if (await refreshButton.isVisible()) {
    await expect(refreshButton).toBeVisible();
  } else {
    const moreButton = actions.getByRole("button", { name: "その他の操作" });
    await moreButton.click();
    await expect(page.getByRole("menuitem", { name: "最新情報を取得", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
  }

  await expect(page.getByRole("tab", { name: "一覧", exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "一覧と詳細" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Oracle Profile", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await expect(page.getByRole("heading", { name: "新規プロファイル" })).toBeVisible();
  await expect(page.getByText("Oracle Profile 反映結果", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("profile-oracle-result")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  const profileRow = page.getByRole("row").filter({ hasText: "既定プロファイル" });
  await profileRow.locator("td").nth(1).click();
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 既定プロファイル" })
  ).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("既定プロファイル");
  await expect(page.getByLabel("Oracle Profile 名")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Drop 実行" })).toHaveCount(0);

  await expect(page.getByText("語彙・few-shot", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "語彙・同義語" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "few-shot 例" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "SQL ルール" })).toHaveCount(0);
  await expect(page.locator("#profile-sql-rules")).toBeVisible();
  await expect(page.locator("#profile-sql-rules-input")).toBeVisible();

  // 対象オブジェクト選択はタブなしで常時表示される
  const tableList = page.getByTestId("profile-allowed-table-list");
  const viewList = page.getByTestId("profile-allowed-view-list");
  await expect(page.getByTestId("profile-allowed-table-list-footer")).toContainText("選択");
  await expect(page.getByTestId("profile-allowed-view-list-footer")).toContainText("選択");
  await expect(tableList.getByText("APP.TABLE_01", { exact: true })).toBeVisible();
  await expect(viewList.getByText("APP.VIEW_02", { exact: true })).toBeVisible();
  await expect(tableList.getByText("APP.VIEW_02", { exact: true })).toHaveCount(0);
  await expect(viewList.getByText("APP.TABLE_01", { exact: true })).toHaveCount(0);
  await expect(page.getByText("SYS$AUDIT", { exact: true })).toHaveCount(0);
  await expect(page.getByText("V_$SESSION", { exact: true })).toHaveCount(0);
  await expect(tableList.getByText("表論理名_01", { exact: true })).toBeVisible();
  await expect(viewList.getByText("ビュー論理名_02", { exact: true })).toBeVisible();
  const appTableBulkActions = tableList.getByTestId("profile-allowed-table-list-app-schema-bulk-actions");
  const appTableSchemaHeading = tableList.getByTestId("profile-allowed-table-list-app-schema-heading");
  await expect
    .poll(async () => {
      const [actionsBox, headingBox] = await Promise.all([
        appTableBulkActions.boundingBox(),
        appTableSchemaHeading.boundingBox(),
      ]);
      if (!actionsBox || !headingBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - headingBox.x);
    })
    .toBeLessThanOrEqual(1);
  await expect(appTableBulkActions.getByRole("button", { name: "APP をすべて選択" })).toBeEnabled();
  await expect(appTableBulkActions.getByRole("button", { name: "APP の選択をすべて解除" })).toBeEnabled();
  await expect(appTableBulkActions.getByText("すべて選択", { exact: true })).toBeVisible();
  await expect(appTableBulkActions.getByText("選択をすべて解除", { exact: true })).toBeVisible();
  const appViewBulkActions = viewList.getByTestId("profile-allowed-view-list-app-schema-bulk-actions");
  const appViewSchemaHeading = viewList.getByTestId("profile-allowed-view-list-app-schema-heading");
  await expect
    .poll(async () => {
      const [actionsBox, headingBox] = await Promise.all([
        appViewBulkActions.boundingBox(),
        appViewSchemaHeading.boundingBox(),
      ]);
      if (!actionsBox || !headingBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - headingBox.x);
    })
    .toBeLessThanOrEqual(1);
  await expect(appViewBulkActions.getByRole("button", { name: "APP をすべて選択" })).toBeEnabled();
  await expect(appViewBulkActions.getByRole("button", { name: "APP の選択をすべて解除" })).toBeEnabled();
  await expect(appViewBulkActions.getByText("すべて選択", { exact: true })).toBeVisible();
  await expect(appViewBulkActions.getByText("選択をすべて解除", { exact: true })).toBeVisible();

  const objectSection = page.getByTestId("profile-allowed-object-list");
  const objectSearchToolbar = page.getByTestId("profile-object-search-toolbar");
  const objectSearch = page.getByRole("searchbox", { name: "オブジェクト検索" });
  await expect(objectSearch).toHaveAttribute("placeholder", "表・ビュー名で検索");
  await expect(objectSearchToolbar.locator("svg.lucide-search")).toBeVisible();

  const [headingBox, toolbarBox, listsBox, searchBox] = await Promise.all([
    objectSection.getByText("対象オブジェクト", { exact: true }).boundingBox(),
    objectSearchToolbar.boundingBox(),
    tableList.boundingBox(),
    objectSearch.boundingBox(),
  ]);
  expect(headingBox).not.toBeNull();
  expect(toolbarBox).not.toBeNull();
  expect(listsBox).not.toBeNull();
  expect(searchBox).not.toBeNull();
  expect(toolbarBox!.y).toBeGreaterThan(headingBox!.y);
  expect(listsBox!.y).toBeGreaterThan(toolbarBox!.y + toolbarBox!.height - 1);
  expect(searchBox!.x).toBeGreaterThanOrEqual(toolbarBox!.x + 11);
  expect(searchBox!.x).toBeLessThanOrEqual(toolbarBox!.x + 14);

  await expect(tableList.getByLabel("APP.TABLE_01")).toBeChecked();
  await expect(viewList.getByLabel("APP.VIEW_02")).toBeChecked();
  await objectSearch.fill("03");
  await expect(tableList.getByText("APP.TABLE_03", { exact: true })).toBeVisible();
  await expect(viewList.getByText("APP.VIEW_03", { exact: true })).toBeVisible();
  await expect(tableList.getByText("APP.TABLE_01", { exact: true })).toHaveCount(0);
  await expect(viewList.getByText("APP.VIEW_02", { exact: true })).toHaveCount(0);
  await objectSearch.clear();
  await expect(tableList.getByLabel("APP.TABLE_01")).toBeChecked();
  await expect(viewList.getByLabel("APP.VIEW_02")).toBeChecked();

  // 行は共通の ListPicker の選択肢（role=option。#600）。表とビューの一覧は同じ高さ（28rem）で中をスクロールする。
  const tableScrollRegion = page.getByTestId("profile-allowed-table-list-scroll-region");
  const viewScrollRegion = page.getByTestId("profile-allowed-view-list-scroll-region");
  const [tablePanelBox, viewPanelBox] = await Promise.all([tableList.boundingBox(), viewList.boundingBox()]);
  expect(Math.abs(tablePanelBox!.height - viewPanelBox!.height)).toBeLessThanOrEqual(1);
  expect(await viewScrollRegion.evaluate((node) => node.clientHeight)).toBeGreaterThan(0);
  const fit = await tableScrollRegion.evaluate((node) => {
    const listBox = node.getBoundingClientRect();
    const rows = Array.from(node.querySelectorAll('[role="option"]')).map((row) => row.getBoundingClientRect());
    const visibleRows = rows.filter((row) => row.bottom <= listBox.bottom + 1 && row.top >= listBox.top - 1).length;
    return {
      listHeight: listBox.height,
      visibleRows,
      totalRows: rows.length,
      noHorizontalOverflow: node.scrollWidth <= node.clientWidth + 1,
    };
  });
  const scrollMetrics = await tableScrollRegion.evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: window.getComputedStyle(node).overflowY,
  }));
  // md 未満は 5 行（17.5rem）、md 以上は 8 行（28rem）の高さ（一覧の表示密度。#265 / #600）。
  const expectedListHeight = page.viewportSize()!.width < 768 ? 247 : 394;
  expect(Math.abs(fit.listHeight - expectedListHeight)).toBeLessThanOrEqual(4);
  expect(fit.visibleRows).toBeGreaterThan(0);
  expect(fit.visibleRows).toBeLessThan(fit.totalRows);
  expect(scrollMetrics.scrollHeight).toBeGreaterThan(scrollMetrics.clientHeight);
  expect(scrollMetrics.overflowY).toBe("auto");
  expect(fit.noHorizontalOverflow).toBe(true);

  await tableList.getByLabel("APP.TABLE_03").check();
  await viewList.getByLabel("APP.VIEW_04").check();
  const instructionsField = page.getByLabel("追加指示", { exact: true });
  await expect(instructionsField).toHaveValue("SELECT のみ");
  const nameField = page.getByLabel("名称");
  await nameField.fill("sales_profile");
  await expect(nameField).toHaveValue("SALES_PROFILE");
  await instructionsField.fill("日付は DATE 型で返す。\n日付は DATE 型で返す。\n金額は円単位で表示する。");
  // 保存に確認語は要らない（業務プロファイルの表への保存だけ）。
  const saveButton = page.getByRole("button", { name: "保存", exact: true });
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  await expect(page.getByText("業務プロファイルを保存しました。", { exact: true })).toBeVisible();
  const payload = savedPayload as {
    name: string;
    allowed_tables: string[];
    allowed_views: string[];
  } | null;
  expect(payload?.name).toBe("SALES_PROFILE");
  expect(payload?.allowed_tables).toEqual(["APP.TABLE_01", "APP.TABLE_03"]);
  expect(payload?.allowed_views).toEqual(["APP.VIEW_02", "APP.VIEW_04"]);
  // ルールは 1 行 1 件で、空行と重複を除いて保存する。
  expect(payload).toHaveProperty("sql_rules", ["日付は DATE 型で返す。", "金額は円単位で表示する。"]);

  await page.setViewportSize({ width: 375, height: 900 });
  const [mobileToolbarBox, mobileSearchBox] = await Promise.all([
    objectSearchToolbar.boundingBox(),
    objectSearch.boundingBox(),
  ]);
  expect(mobileToolbarBox).not.toBeNull();
  expect(mobileSearchBox).not.toBeNull();
  expect(mobileSearchBox!.width).toBeGreaterThanOrEqual(mobileToolbarBox!.width - 26);

  const bodyWidth = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(bodyWidth.scrollWidth).toBeLessThanOrEqual(bodyWidth.clientWidth + 1);
});


test("回答前の確認の方針と確認の上限（往復）を業務プロファイルに保存する (#1839)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockProfileApi(page);
  let savedPayload: { clarification_policy?: string; clarification_max_rounds?: number } | null = null;
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    if (route.request().method() === "GET") {
      await route.fallback();
      return;
    }
    savedPayload = route.request().postDataJSON() as typeof savedPayload;
    await fulfillJson(route, { ...profiles[0], ...savedPayload, id: "default", etag: "etag-default-2" });
  });
  await page.goto("/profiles?profile=default");

  const section = page.getByTestId("profile-clarification-settings");
  await expect(section.getByRole("heading", { name: "回答前の確認" })).toBeVisible();
  const policy = section.getByRole("combobox", { name: "曖昧な質問の扱い" });
  const maxRounds = section.getByRole("combobox", { name: "確認の上限（往復）" });
  // 方針を持たない業務プロファイルは「確認してから SQL を生成する」・上限はシステムの既定。
  await expectSelectFieldValue(policy, "confirm");
  await expectSelectFieldValue(maxRounds, "0");
  await chooseSelectFieldOption(maxRounds, "2");
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme });
    await page.evaluate((dark) => document.documentElement.classList.toggle("dark", dark), colorScheme === "dark");
    await testInfo.attach(`clarification-settings-${colorScheme}`, {
      body: await section.screenshot(),
      contentType: "image/png",
    });
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.evaluate(() => document.documentElement.classList.remove("dark"));
  await chooseSelectFieldOption(policy, "assume");
  // 前提を示すときは確認しないので、上限は選べない。
  await expect(maxRounds).toBeDisabled();

  await page.getByLabel("名称").fill("SALES_PROFILE");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("業務プロファイルを保存しました。", { exact: true })).toBeVisible();
  expect(savedPayload).toMatchObject({ clarification_policy: "assume", clarification_max_rounds: 2 });
  await expectNoDocumentHorizontalOverflow(page);
});

test("異なる schema の同名表を別々に選択できる", async ({ page }) => {
  const duplicateCatalog = {
    ...schemaCatalog,
    tables: [
      ...schemaCatalog.tables,
      {
        table_name: "ORDERS",
        qualified_name: "APP.ORDERS",
        logical_name: "アプリ受注",
        owner: "APP",
        table_type: "TABLE",
        comment: "APP の受注",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "ORDERS",
        qualified_name: "SH.ORDERS",
        logical_name: "販売受注",
        owner: "SH",
        table_type: "TABLE",
        comment: "SH の受注",
        row_count: null,
        columns: [],
        constraints: [],
      },
    ],
  };
  const duplicateProfiles = [
    {
      ...profiles[0],
      allowed_tables: ["APP.ORDERS", "SH.ORDERS"],
      allowed_views: [],
    },
  ];
  await mockProfileApi(page, {
    catalog: duplicateCatalog,
    profileItems: duplicateProfiles,
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  await expect(tableList.getByLabel("APP.ORDERS")).toBeChecked();
  await expect(tableList.getByLabel("SH.ORDERS")).toBeChecked();
  await expect(tableList.getByText("APP", { exact: true })).toBeVisible();
  await expect(tableList.getByText("SH", { exact: true })).toBeVisible();

  await tableList.getByLabel("SH.ORDERS").uncheck();
  await expect(tableList.getByLabel("APP.ORDERS")).toBeChecked();
  await expect(tableList.getByLabel("SH.ORDERS")).not.toBeChecked();

  const shBulkActions = tableList.getByTestId("profile-allowed-table-list-sh-schema-bulk-actions");
  await expect(shBulkActions.getByRole("button", { name: "SH の選択をすべて解除" })).toBeDisabled();
  await shBulkActions.getByRole("button", { name: "SH をすべて選択" }).click();
  await expect(tableList.getByLabel("SH.ORDERS")).toBeChecked();
  await expect(shBulkActions.getByRole("button", { name: "SH の選択をすべて解除" })).toBeEnabled();
  await shBulkActions.getByRole("button", { name: "SH の選択をすべて解除" }).click();
  await expect(tableList.getByLabel("SH.ORDERS")).not.toBeChecked();
  await shBulkActions.getByRole("button", { name: "SH をすべて選択" }).click();
  await expect(tableList.getByLabel("SH.ORDERS")).toBeChecked();

  await page.getByRole("searchbox", { name: "オブジェクト検索" }).fill("SH.ORDERS");
  await expect(tableList.getByLabel("SH.ORDERS")).toBeVisible();
  await expect(tableList.getByLabel("APP.ORDERS")).toHaveCount(0);
});

test("引用が必要な表名は大文字化せず、大文字の同名表と別に選択・保存する (#561, #564)", async ({ page }) => {
  const quotedCatalog = {
    ...schemaCatalog,
    tables: [
      ...schemaCatalog.tables,
      {
        table_name: "MIXED_CASE",
        logical_name: "大文字の受注",
        owner: "SALES",
        table_type: "TABLE",
        comment: "大文字の同名表",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "Mixed_Case",
        logical_name: "引用名の受注",
        owner: "SALES",
        table_type: "TABLE",
        comment: "引用が必要な表",
        row_count: null,
        columns: [],
        constraints: [],
      },
    ],
  };
  const quotedProfiles = [{ ...profiles[0], allowed_tables: [], allowed_views: [] }];
  await mockProfileApi(page, { catalog: quotedCatalog, profileItems: quotedProfiles });
  let savedPayload: { allowed_tables: string[] } | null = null;
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    if (route.request().method() === "GET") {
      await route.fallback();
      return;
    }
    savedPayload = route.request().postDataJSON() as { allowed_tables: string[] };
    await fulfillJson(route, { ...quotedProfiles[0], ...savedPayload, id: "default" });
  });
  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  const quoted = tableList.getByLabel('SALES."Mixed_Case"', { exact: true });
  const upper = tableList.getByLabel("SALES.MIXED_CASE", { exact: true });
  await expect(quoted).toBeVisible();
  await expect(upper).toBeVisible();
  await expect(tableList.getByText("引用名の受注", { exact: true })).toBeVisible();
  await expect(tableList.getByText("大文字の受注", { exact: true })).toBeVisible();

  // 引用名の表を選んでも、大文字の同名表はチェックされない。
  await quoted.check();
  await expect(quoted).toBeChecked();
  await expect(upper).not.toBeChecked();
  await expect(tableList).toContainText("選択 1 件");
  await upper.check();
  await upper.uncheck();
  await expect(quoted).toBeChecked();
  await expect(upper).not.toBeChecked();
  await expect(tableList).toContainText("選択 1 件");

  // スキーマ単位の一括解除は SALES の両方を外し、再選択は snapshot の表記を保つ。
  const salesBulkActions = tableList.getByTestId("profile-allowed-table-list-sales-schema-bulk-actions");
  await salesBulkActions.getByRole("button", { name: "SALES をすべて選択" }).click();
  await expect(quoted).toBeChecked();
  await expect(upper).toBeChecked();
  await expect(tableList).toContainText("選択 2 件");
  await upper.uncheck();
  await expect(tableList).toContainText("選択 1 件");

  await page.getByLabel("名称").fill("SALES_PROFILE");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  // #564: 引用名の表は `"Mixed_Case"` の表記のまま保存する。
  await expect(page.getByText("業務プロファイルを保存しました。", { exact: true })).toBeVisible();
  const payload = savedPayload as { allowed_tables: string[] } | null;
  expect(payload?.allowed_tables).toEqual(['SALES."Mixed_Case"']);
  await expectNoDocumentHorizontalOverflow(page);
});

test("小文字を含む owner は大文字の同名 owner と別のスキーマとして件数・一括選択する (#563)", async ({ page }) => {
  const ownerCatalog = {
    ...schemaCatalog,
    tables: [
      {
        table_name: "ORDERS",
        logical_name: "大文字 owner の受注",
        owner: "SALES",
        table_type: "TABLE",
        comment: "",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "ORDERS",
        logical_name: "小文字を含む owner の受注",
        owner: "Sales",
        table_type: "TABLE",
        comment: "",
        row_count: null,
        columns: [],
        constraints: [],
      },
    ],
  };
  const ownerProfiles = [{ ...profiles[0], allowed_tables: [], allowed_views: [] }];
  await mockProfileApi(page, { catalog: ownerCatalog, profileItems: ownerProfiles });
  const snapshotOwners: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/schema/objects" && url.searchParams.get("include_counts") === "false") {
      snapshotOwners.push(url.searchParams.get("owner") ?? "");
    }
  });
  let savedPayload: { allowed_tables: string[] } | null = null;
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    if (route.request().method() === "GET") {
      await route.fallback();
      return;
    }
    savedPayload = route.request().postDataJSON() as { allowed_tables: string[] };
    await fulfillJson(route, { ...ownerProfiles[0], ...savedPayload, id: "default" });
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  // グループは見出し（件数・一括選択）と listbox「テーブル選択: <owner>」（共通の ListPicker。#600）。
  const mixedGroup = tableList.getByTestId('profile-allowed-table-list-"sales"-schema');
  const upperGroup = tableList.getByTestId("profile-allowed-table-list-sales-schema");
  await expect(mixedGroup).toBeVisible();
  await expect(upperGroup).toBeVisible();
  const mixed = tableList
    .getByRole("listbox", { name: 'テーブル選択: "Sales"', exact: true })
    .getByLabel('"Sales".ORDERS', { exact: true });
  const upper = tableList
    .getByRole("listbox", { name: "テーブル選択: SALES", exact: true })
    .getByLabel("SALES.ORDERS", { exact: true });
  await expect(mixed).toBeVisible();
  await expect(upper).toBeVisible();

  // 引用名の owner の表を選んでも、大文字 owner のグループは 0 件のまま。
  await mixed.check();
  await expect(mixedGroup).toContainText("1 / 1");
  await expect(upperGroup).toContainText("0 / 1");

  // 大文字 owner の一括選択は、小文字を含む owner の表を巻き込まずに選び・外す。
  await upperGroup.getByRole("button", { name: "SALES をすべて選択" }).click();
  await expect(upper).toBeChecked();
  await expect(mixed).toBeChecked();
  await expect(tableList).toContainText("選択 2 件");
  await upperGroup.getByRole("button", { name: "SALES の選択をすべて解除" }).click();
  await expect(upper).not.toBeChecked();
  await expect(mixed).toBeChecked();
  await expect(upperGroup).toContainText("0 / 1");

  // 小文字を含む owner の一括操作は、その owner の表だけを snapshot（owner=`"Sales"`）から選ぶ。
  await mixedGroup.getByRole("button", { name: '"Sales" の選択をすべて解除' }).click();
  await expect(mixed).not.toBeChecked();
  await mixedGroup.getByRole("button", { name: '"Sales" をすべて選択' }).click();
  await expect(mixed).toBeChecked();
  await expect(upper).not.toBeChecked();
  await expect(tableList).toContainText("選択 1 件");
  expect(snapshotOwners).toEqual(["SALES", '"Sales"']);

  await page.getByLabel("名称").fill("SALES_PROFILE");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => savedPayload?.allowed_tables).toEqual(['"Sales".ORDERS']);
  await expectNoDocumentHorizontalOverflow(page);
});

test("$ と NL2SQL_ のシステム object は業務プロファイルの対象オブジェクトに表示しない", async ({ page }) => {
  const catalog = {
    ...schemaCatalog,
    tables: [
      {
        table_name: "ORDERS",
        qualified_name: "APP.ORDERS",
        logical_name: "受注",
        owner: "APP",
        table_type: "TABLE",
        comment: "業務受注",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "V_ORDERS",
        qualified_name: "APP.V_ORDERS",
        logical_name: "受注ビュー",
        owner: "APP",
        table_type: "VIEW",
        comment: "業務受注ビュー",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "NL2SQL_SCHEMA_OBJECTS",
        qualified_name: "APP.NL2SQL_SCHEMA_OBJECTS",
        logical_name: "NL2SQL schema objects",
        owner: "APP",
        table_type: "TABLE",
        comment: "system table",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "NL2SQL_SYSTEM_VIEW",
        qualified_name: "APP.NL2SQL_SYSTEM_VIEW",
        logical_name: "NL2SQL system view",
        owner: "APP",
        table_type: "VIEW",
        comment: "system view",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "ORDERS",
        qualified_name: "NL2SQL_APP.ORDERS",
        logical_name: "NL2SQL owner business table",
        owner: "NL2SQL_APP",
        table_type: "TABLE",
        comment: "business table",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "RC_BACKUP_ARCHIVELOG_DETAILS",
        qualified_name: "RMAN$CATALOG.RC_BACKUP_ARCHIVELOG_DETAILS",
        logical_name: "RMAN backup details",
        owner: "RMAN$CATALOG",
        table_type: "VIEW",
        comment: "system view",
        row_count: null,
        columns: [],
        constraints: [],
      },
      {
        table_name: "RC_ARCHIVED_LOG",
        qualified_name: "RMAN$CATALOG.RC_ARCHIVED_LOG",
        logical_name: "RMAN archived log",
        owner: "RMAN$CATALOG",
        table_type: "TABLE",
        comment: "system table",
        row_count: null,
        columns: [],
        constraints: [],
      },
    ],
  };
  await mockProfileApi(page, {
    catalog,
    profileItems: [
      {
        ...profiles[0],
        allowed_tables: [
          "APP.ORDERS",
          "APP.NL2SQL_SCHEMA_OBJECTS",
          "NL2SQL_APP.ORDERS",
          "RMAN$CATALOG.RC_ARCHIVED_LOG",
        ],
        allowed_views: [
          "APP.V_ORDERS",
          "APP.NL2SQL_SYSTEM_VIEW",
          "RMAN$CATALOG.RC_BACKUP_ARCHIVELOG_DETAILS",
        ],
      },
    ],
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  const viewList = page.getByTestId("profile-allowed-view-list");
  await expect(tableList.getByLabel("APP.ORDERS", { exact: true })).toBeChecked();
  await expect(tableList.getByLabel("NL2SQL_APP.ORDERS", { exact: true })).toBeChecked();
  await expect(viewList.getByLabel("APP.V_ORDERS", { exact: true })).toBeChecked();
  await expect(tableList.getByText("RMAN$CATALOG")).toHaveCount(0);
  await expect(viewList.getByText("RMAN$CATALOG")).toHaveCount(0);
  await expect(tableList.getByText("APP.NL2SQL_SCHEMA_OBJECTS", { exact: true })).toHaveCount(0);
  await expect(viewList.getByText("APP.NL2SQL_SYSTEM_VIEW", { exact: true })).toHaveCount(0);
  await expect(tableList.getByText("RC_ARCHIVED_LOG", { exact: true })).toHaveCount(0);
  await expect(viewList.getByText("RC_BACKUP_ARCHIVELOG_DETAILS", { exact: true })).toHaveCount(0);

  await page.getByRole("searchbox", { name: "オブジェクト検索" }).fill("NL2SQL_SCHEMA");
  await expect(tableList.getByText("選択できるテーブルがありません。")).toBeVisible();
  await expect(viewList.getByText("選択できるビューがありません。")).toBeVisible();

  await page.getByRole("searchbox", { name: "オブジェクト検索" }).fill("RMAN");
  await expect(tableList.getByText("選択できるテーブルがありません。")).toBeVisible();
  await expect(viewList.getByText("選択できるビューがありません。")).toBeVisible();
});

test("業務プロファイル必須項目は空欄保存を止める", async ({ page }) => {
  await mockProfileApi(page);
  let saveRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/nl2sql/profiles" && request.method() === "POST") {
      saveRequests += 1;
    }
  });
  await page.goto("/profiles");

  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await page.getByLabel("名称").fill("SALES_PROFILE");
  await page.getByLabel("カテゴリ").fill("");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByRole("alert").filter({ hasText: "カテゴリを入力してください。" })).toBeVisible();
  await expect(page.getByLabel("カテゴリ")).toHaveAttribute("aria-invalid", "true");
  expect(saveRequests).toBe(0);

  await page.getByLabel("カテゴリ").fill("販売");
  await expect(page.getByRole("alert").filter({ hasText: "カテゴリを入力してください。" })).toHaveCount(0);
});

test("業務プロファイル重複名称は名称欄のエラーとして表示する", async ({ page }) => {
  await mockProfileApi(page);
  let saveRequests = 0;
  await page.route("**/api/nl2sql/profiles", async (route) => {
    const request = route.request();
    if (new URL(request.url()).pathname !== "/api/nl2sql/profiles" || request.method() !== "POST") {
      await route.fallback();
      return;
    }
    saveRequests += 1;
    await route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["業務プロファイル名「SALES_PROFILE」は既に使用されています。"],
        warning_messages: [],
        error_code: "NL2SQL_PROFILE_NAME_CONFLICT",
        problem: {
          type: "urn:nl2sql:problem:nl2sql-profile-name-conflict",
          title: "入力内容を確認してください",
          status: 422,
          detail: "業務プロファイル名「SALES_PROFILE」は既に使用されています。",
          code: "NL2SQL_PROFILE_NAME_CONFLICT",
          request_id: "req-duplicate-profile-name",
          retryable: false,
          field_errors: [
            {
              pointer: "/name",
              code: "profile_name_conflict",
              message: "業務プロファイル名「SALES_PROFILE」は既に使用されています。",
            },
          ],
        },
      }),
    });
  });

  await page.goto("/profiles");
  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await page.getByLabel("名称").fill("sales_profile");
  await page.getByLabel("カテゴリ").fill("販売");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  const duplicateError = page
    .getByRole("alert")
    .filter({ hasText: "同じ名称の業務プロファイルが既に存在します。" });
  await expect(duplicateError).toBeVisible();
  // 名称の欄は共有の TextField（#631）。エラーは欄の外枠（input → 枠の div → 外枠）の中の alert。
  await expect(page.locator("#profile-name").locator("xpath=../..").getByRole("alert")).toContainText(
    "同じ名称の業務プロファイルが既に存在します。"
  );
  await expect(page.getByLabel("名称")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel("名称")).toBeFocused();
  expect(saveRequests).toBe(1);
});

test("名称は英字開始の識別子だけ保存でき小文字は自動大文字化する", async ({ page }) => {
  await mockProfileApi(page);
  await page.goto("/profiles");

  await page.getByRole("button", { name: "新規作成", exact: true }).click();

  // 名称は空のまま保存する。
  await page.getByRole("button", { name: "保存", exact: true }).click();

  // spec §2 error-placement: 該当欄の直下に role=alert で表示される。
  const fieldError = page.getByRole("alert").filter({ hasText: "名称を入力してください。" });
  await expect(fieldError).toBeVisible();
  const categoryError = page.getByRole("alert").filter({ hasText: "カテゴリを入力してください。" });
  await expect(categoryError).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel("カテゴリ")).toHaveAttribute("aria-invalid", "true");

  await page.getByLabel("名称").fill("新プロファイル");
  await expect(fieldError).toHaveCount(0);
  await page.getByLabel("カテゴリ").fill("販売");
  await expect(categoryError).toHaveCount(0);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const formatError = page
    .getByRole("alert")
    .filter({ hasText: "名称は英字で開始し、英字・数字・アンダースコアのみ使用してください。" });
  await expect(formatError).toBeVisible();

  await page.getByLabel("名称").fill("1PROFILE");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(formatError).toBeVisible();

  await page.getByLabel("名称").fill("sales_profile");
  await expect(page.getByLabel("名称")).toHaveValue("SALES_PROFILE");
  await expect(formatError).toHaveCount(0);
});

test("業務プロファイルはcatalogが空のときDB管理用の現在schema一覧を混在させない", async ({ page }) => {
  await mockProfileApi(page, {
    catalog: { ...schemaCatalog, tables: [] },
    tableItems: [
      { name: "DEPARTMENT", owner: "APP", object_type: "TABLE", row_count: 10, comment: "部署" },
      { name: "EMPLOYEE", owner: "APP", object_type: "TABLE", row_count: 12, comment: "社員" },
      { name: "PROJECT", owner: "APP", object_type: "TABLE", row_count: 8, comment: "案件" },
      { name: "SYS$AUDIT", owner: "SYS", object_type: "TABLE", row_count: null, comment: "system" },
    ],
    viewItems: [
      { name: "V_EMP_DEPT", owner: "APP", object_type: "VIEW", row_count: null, comment: "社員と部署" },
    ],
    profileItems: [{ ...profiles[0], allowed_tables: [], allowed_views: [] }],
  });
  await page.goto("/profiles");
  await page.getByRole("link", { name: /^既定プロファイル/ }).click();

  const tableList = page.getByTestId("profile-allowed-table-list");
  const viewList = page.getByTestId("profile-allowed-view-list");

  await expect(tableList.getByText("選択できるテーブルがありません。")).toBeVisible();
  await expect(tableList.getByText("DEPARTMENT", { exact: true })).toHaveCount(0);
  await expect(tableList.getByText("EMPLOYEE", { exact: true })).toHaveCount(0);
  await expect(tableList.getByText("PROJECT", { exact: true })).toHaveCount(0);
  await expect(tableList.getByText("SYS$AUDIT", { exact: true })).toHaveCount(0);
  await expect(viewList.getByText("V_EMP_DEPT", { exact: true })).toHaveCount(0);
});

test("業務プロファイルの対象オブジェクト空状態はExcelプレビュー風の広い面で表示する", { tag: "@responsive" }, async ({ page }) => {
  await mockProfileApi(page, {
    catalog: { ...schemaCatalog, tables: [] },
    tableItems: [],
    viewItems: [],
  });

  await page.goto("/profiles");
  await page.getByRole("link", { name: /^既定プロファイル/ }).click();

  const tableList = page.getByTestId("profile-allowed-table-list");
  const viewList = page.getByTestId("profile-allowed-view-list");

  await expect(tableList).toHaveAccessibleName("テーブル選択");
  await expect(viewList).toHaveAccessibleName("ビュー選択");
  await expect(tableList.getByText("選択できるテーブルがありません。")).toBeVisible();
  await expect(tableList.getByText("Oracle からテーブルを読み込むとここに表示されます。")).toBeVisible();
  await expect(viewList.getByText("選択できるビューがありません。")).toBeVisible();
  await expect(viewList.getByText("Oracle からビューを読み込むとここに表示されます。")).toBeVisible();
  await expect(tableList.getByRole("option")).toHaveCount(0);
  await expect(viewList.getByRole("option")).toHaveCount(0);

  // 空の案内は一覧の領域（28rem）の中に出す（候補があるときと同じ高さ。#600）。
  const surface = await page.getByTestId("profile-allowed-table-list-scroll-region").evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      height: node.getBoundingClientRect().height,
      borderStyle: style.borderStyle,
      backgroundColor: style.backgroundColor,
      dashedDescendants: node.querySelectorAll(".border-dashed").length,
      noHorizontalOverflow: node.scrollWidth <= node.clientWidth + 1,
    };
  });
  const expectedSurfaceHeight = page.viewportSize()!.width < 768 ? 247 : 394;
  expect(Math.abs(surface.height - expectedSurfaceHeight)).toBeLessThanOrEqual(4);
  expect(surface.borderStyle).toBe("solid");
  expect(surface.backgroundColor).toBe("rgb(255, 255, 255)");
  expect(surface.dashedDescendants).toBe(0);
  expect(surface.noHorizontalOverflow).toBe(true);

  await page.setViewportSize({ width: 375, height: 900 });
  const mobileWidth = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(mobileWidth.scrollWidth).toBeLessThanOrEqual(mobileWidth.clientWidth + 1);
});

test("未解決オブジェクトの警告からスキーマ情報を更新して復旧できる", async ({ page }) => {
  await mockProfileApi(page);
  let schemaRefreshed = false;
  let refreshJobPolls = 0;
  let ontologyViewCalls = 0;
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    await fulfillJson(route, {
      draft_markdown: "# Draft",
      published_markdown: "# Published",
      draft_revision: {
        id: "revision-draft-1",
        version: 2,
        status: "draft",
        schema_fingerprint: "fp",
        etag: "draft-etag",
      },
      published_revision: {
        id: "revision-published-1",
        version: 1,
        status: "published",
        schema_fingerprint: "fp",
        etag: "published-etag",
        published_at: "2026-07-12T00:00:20Z",
      },
      draft_etag: "markdown-draft-etag",
      published_at: "2026-07-12T00:00:20Z",
    });
  });
  await page.route("**/api/schema/refresh-jobs**", async (route) => {
    // coordinator の active-job discovery はポーリング回数に数えない(即 done 化を防ぐ)。
    if (route.request().url().includes("/refresh-jobs/active")) {
      await fulfillJson(route, { active_job: null });
      return;
    }
    if (route.request().method() === "POST") {
      await fulfillJson(route, {
        job_id: "schema-refresh-1",
        status: "pending",
        created_at: "2026-07-12T00:00:00Z",
        scanned_objects: 0,
        changed_objects: 0,
        deleted_objects: 0,
        catalog_version: 1,
        error_code: "",
      });
      return;
    }
    refreshJobPolls += 1;
    const done = refreshJobPolls >= 2;
    schemaRefreshed = done;
    await fulfillJson(route, {
      job_id: "schema-refresh-1",
      status: done ? "done" : "running",
      created_at: "2026-07-12T00:00:00Z",
      scanned_objects: done ? 1 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 1,
      error_code: "",
    });
  });
  // 初回は未解決警告つき空グラフ、スキーマ更新後は解決済みグラフを返す
  await page.route("**/api/nl2sql/profiles/*/ontology-view", async (route) => {
    ontologyViewCalls += 1;
    if (!schemaRefreshed) {
      await fulfillJson(route, {
        profile_ontology_view: {
          ...profileOntologyView.profile_ontology_view,
          node_ids: [],
          edge_ids: [],
        },
        ontology_graph: { ...profileOntologyView.ontology_graph, nodes: [], edges: [] },
        warnings_ja: [
          "「TABLE_01」を公開済み Ontology(スキーマ情報)に解決できません。スキーマ情報を更新するか、オブジェクト名(owner 付き)を確認してください。",
        ],
      });
      return;
    }
    await fulfillJson(route, { ...profileOntologyView, warnings_ja: [] });
  });

  await page.goto("/ontology-build?profile=default&tab=model");
  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const unresolved = page.getByTestId("profile-ontology-unresolved");
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await expect(unresolved).toBeVisible();
  await expect(playground.getByTestId("profile-ontology-unresolved")).toBeVisible();
  await expect(unresolved.getByText("TABLE_01", { exact: false })).toBeVisible();

  const refreshButton = unresolved.getByRole("button", { name: "DB 構造を再取得" });
  await refreshButton.click();

  // 案内の中のボタンで始めた再取得の進行は、同じ案内の中のボタンの直下に出し、ページの先頭に重ねない（#2034）。
  const schemaRefreshProcessing = unresolved.getByTestId("ontology-playground-schema-refresh-processing");
  await expect(schemaRefreshProcessing).toBeVisible();
  await expect(schemaRefreshProcessing).toContainText("DB 構造を再取得しています");
  await expect(page.getByTestId("ontology-build-schema-refresh-processing")).toHaveCount(0);
  const buttonBox = await refreshButton.boundingBox();
  const processingBox = await schemaRefreshProcessing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  await expect(page.getByText("スキーマ更新: 実行中", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("profile-ontology-unresolved")).toHaveCount(0);
  await expect(schemaRefreshProcessing).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  await expect(page.getByText(/DB 構造(再取得|差分同期): 完了/)).toHaveCount(0);
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await expect(page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" })).toBeVisible();
  expect(schemaRefreshed).toBe(true);
  expect(ontologyViewCalls).toBeGreaterThanOrEqual(2);
});


test("DB 構造を再取得しても解決しない未解決の警告は、再取得を求めずに残りの対処だけを出す（#2034）", async ({ page }) => {
  await mockProfileApi(page);
  let schemaRefreshed = false;
  let refreshJobPolls = 0;
  let ontologyViewCalls = 0;
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    await fulfillJson(route, {
      draft_markdown: "# Draft",
      published_markdown: "# Published",
      draft_revision: {
        id: "revision-draft-1",
        version: 2,
        status: "draft",
        schema_fingerprint: "fp",
        etag: "draft-etag",
      },
      published_revision: {
        id: "revision-published-1",
        version: 1,
        status: "published",
        schema_fingerprint: "fp",
        etag: "published-etag",
        published_at: "2026-07-12T00:00:20Z",
      },
      draft_etag: "markdown-draft-etag",
      published_at: "2026-07-12T00:00:20Z",
    });
  });
  await page.route("**/api/schema/refresh-jobs**", async (route) => {
    // coordinator の active-job discovery はポーリング回数に数えない(即 done 化を防ぐ)。
    if (route.request().url().includes("/refresh-jobs/active")) {
      await fulfillJson(route, { active_job: null });
      return;
    }
    if (route.request().method() === "POST") {
      await fulfillJson(route, {
        job_id: "schema-refresh-1",
        status: "pending",
        created_at: "2026-07-12T00:00:00Z",
        scanned_objects: 0,
        changed_objects: 0,
        deleted_objects: 0,
        catalog_version: 1,
        error_code: "",
      });
      return;
    }
    refreshJobPolls += 1;
    const done = refreshJobPolls >= 2;
    schemaRefreshed = done;
    await fulfillJson(route, {
      job_id: "schema-refresh-1",
      status: done ? "done" : "running",
      created_at: "2026-07-12T00:00:00Z",
      scanned_objects: done ? 1 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 1,
      error_code: "",
    });
  });
  // スキーマを更新した後も、同じ未解決警告つきの空グラフを返す（公開オントロジーの側の問題）。
  await page.route("**/api/nl2sql/profiles/*/ontology-view", async (route) => {
    ontologyViewCalls += 1;
    await fulfillJson(route, {
      profile_ontology_view: {
        ...profileOntologyView.profile_ontology_view,
        node_ids: [],
        edge_ids: [],
      },
      ontology_graph: { ...profileOntologyView.ontology_graph, nodes: [], edges: [] },
      warnings_ja: [
        "「TABLE_01」を公開済み Ontology(スキーマ情報)に解決できません。スキーマ情報を更新するか、オブジェクト名(owner 付き)を確認してください。",
      ],
    });
  });

  await page.goto("/ontology-build?profile=default&tab=model");
  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const unresolved = page.getByTestId("profile-ontology-unresolved");
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await expect(unresolved).toBeVisible();
  await expect(playground.getByTestId("profile-ontology-unresolved")).toBeVisible();
  await expect(unresolved.getByText("TABLE_01", { exact: false })).toBeVisible();

  const refreshButton = unresolved.getByRole("button", { name: "DB 構造を再取得" });
  await refreshButton.click();

  // 案内の中のボタンで始めた再取得の進行は、同じ案内の中のボタンの直下に出し、ページの先頭に重ねない（#2034）。
  const schemaRefreshProcessing = unresolved.getByTestId("ontology-playground-schema-refresh-processing");
  await expect(schemaRefreshProcessing).toBeVisible();
  await expect(schemaRefreshProcessing).toContainText("DB 構造を再取得しています");
  await expect(page.getByTestId("ontology-build-schema-refresh-processing")).toHaveCount(0);
  const buttonBox = await refreshButton.boundingBox();
  const processingBox = await schemaRefreshProcessing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  await expect(schemaRefreshProcessing).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  await expect.poll(() => ontologyViewCalls).toBeGreaterThanOrEqual(2);
  // 同じ警告が残っても、再取得を求めるボタンは出さず、残りの対処を案内する。
  await expect(unresolved).toBeVisible();
  await expect(unresolved.getByText("DB の構造を取り直しても解決しませんでした。", { exact: false })).toBeVisible();
  await expect(unresolved.getByRole("button", { name: "DB 構造を再取得" })).toHaveCount(0);
  await expect(unresolved.getByText("TABLE_01", { exact: false })).toBeVisible();
  expect(schemaRefreshed).toBe(true);
});
test("Ontology 未公開のとき旧モデル編集は出さず Markdown Draft と接地確認の導線を示す", async ({ page }) => {
  await mockProfileApi(page);
  // 公開済み Ontology が無い状態では warning が返っても Draft 編集面には出さない。
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    await fulfillJson(route, {
      draft_markdown: "# Draft\n\n- TABLE_01",
      published_markdown: "",
      draft_revision: {
        id: "revision-draft-1",
        version: 1,
        status: "draft",
        schema_fingerprint: "fp",
        etag: "draft-etag",
      },
      published_revision: null,
      draft_etag: "markdown-draft-etag",
      published_at: null,
    });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-view", async (route) => {
    await fulfillJson(route, {
      ontology_graph: null,
      warnings_ja: [
        "「TABLE_01」を公開済み Ontology(スキーマ情報)に解決できません。スキーマ情報を更新するか、オブジェクト名(owner 付き)を確認してください。",
      ],
    });
  });

  await page.goto("/ontology-build?profile=default&tab=model");
  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  await expect(page.getByTestId("profile-ontology-empty")).toHaveCount(0);
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toBeVisible();
  await expect(page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" })).toBeVisible();
  await expect(page.getByText("公開済みオントロジーがまだありません")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-unresolved")).toHaveCount(0);
});

test("Ontology 構築はプロファイルごとの保存済みファイルを再表示する", async ({ page }) => {
  await mockProfileApi(page);
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", (route) =>
    fulfillJson(route, {
      source_documents: [
        {
          id: "ontology_source_rules",
          profile_id: "default",
          filename: "hr-rules.md",
          source_role: "source",
          media_type: "text/markdown",
          size_bytes: 2048,
          status: "extracted",
          extracted_chunk_count: 2,
          created_at: "2026-09-01T00:00:00.000Z",
          updated_at: "2026-09-01T00:05:00.000Z",
        },
        {
          id: "ontology_source_qa",
          profile_id: "default",
          filename: "qa_cases.csv",
          source_role: "qa",
          media_type: "text/csv",
          size_bytes: 1024,
          status: "stored",
          extracted_chunk_count: 0,
          created_at: "2026-09-01T00:01:00.000Z",
          updated_at: "2026-09-01T00:01:00.000Z",
        },
      ],
    })
  );

  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const savedFiles = page.getByTestId("ontology-build-saved-files");
  await expect(savedFiles).toBeVisible();
  await expect(savedFiles).toContainText("保存済みファイル");
  await expect(savedFiles).toContainText("hr-rules.md");
  await expect(savedFiles).toContainText("構築資料");
  await expect(savedFiles).toContainText("抽出済み");
  await expect(savedFiles).toContainText("2 チャンク");
  await expect(savedFiles).toContainText("qa_cases.csv");
  await expect(savedFiles).toContainText("Q/A");
  await expect(savedFiles).toContainText("保存済み");
});


test("検索フィルタ適用中のスキーマ一括操作はヒットした object だけに作用する", async ({ page }) => {
  await mockProfileApi(page, {
    profileItems: [{ ...profiles[0], allowed_tables: [], allowed_views: [] }],
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  const objectSearch = page.getByRole("searchbox", { name: "オブジェクト検索" });
  const appBulkActions = tableList.getByTestId(
    "profile-allowed-table-list-app-schema-bulk-actions",
  );

  await expect(tableList.getByLabel("APP.TABLE_01")).not.toBeChecked();

  // 絞り込み中の「すべて選択」はスキーマ全体へ波及しない。
  await objectSearch.fill("TABLE_01");
  await expect(tableList.getByLabel("APP.TABLE_02")).toHaveCount(0);
  await appBulkActions.getByRole("button", { name: "APP をすべて選択" }).click();
  await expect(tableList.getByLabel("APP.TABLE_01")).toBeChecked();

  await objectSearch.fill("");
  // 検索はデバウンスされるため、絞り込み解除後の一覧描画を待ってから検証する。
  await expect(tableList.getByLabel("APP.TABLE_02")).toBeVisible();
  await expect(tableList.getByLabel("APP.TABLE_01")).toBeChecked();
  await expect(tableList.getByLabel("APP.TABLE_02")).not.toBeChecked();

  // 絞り込み中の「すべて解除」もヒットした object だけを外す。
  await appBulkActions.getByRole("button", { name: "APP をすべて選択" }).click();
  await expect(tableList.getByLabel("APP.TABLE_02")).toBeChecked();
  await objectSearch.fill("TABLE_01");
  await expect(tableList.getByLabel("APP.TABLE_02")).toHaveCount(0);
  await appBulkActions.getByRole("button", { name: "APP の選択をすべて解除" }).click();
  await expect(tableList.getByLabel("APP.TABLE_01")).not.toBeChecked();

  await objectSearch.fill("");
  await expect(tableList.getByLabel("APP.TABLE_02")).toBeVisible();
  await expect(tableList.getByLabel("APP.TABLE_01")).not.toBeChecked();
  await expect(tableList.getByLabel("APP.TABLE_02")).toBeChecked();
});

test("50 件超のスキーマでも仮想スクロールの行位置がずれない", { tag: "@responsive" }, async ({ page }) => {
  const manyTables = {
    refreshed_at: schemaCatalog.refreshed_at,
    tables: Array.from({ length: 120 }, (_, index) => {
      const count = String(index + 1).padStart(3, "0");
      return {
        table_name: `TABLE_${count}`,
        logical_name: `表論理名_${count}`,
        owner: "APP",
        table_type: "TABLE",
        comment: `表コメント_${count}`,
        row_count: null,
        columns: [],
        constraints: [],
      };
    }),
  };
  await mockProfileApi(page, {
    catalog: manyTables as typeof schemaCatalog,
    profileItems: [{ ...profiles[0], allowed_tables: [], allowed_views: [] }],
  });

  await page.goto("/profiles?profile=default");

  // 共通の ListPicker は 100 行を超えると見えている行だけを描く（#600）。
  const virtualList = page.getByTestId("profile-allowed-table-list-scroll-region");
  await expect(virtualList).toBeVisible();
  expect(await virtualList.getByRole("option").count()).toBeLessThan(60);

  await virtualList.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });

  const lastRow = virtualList.getByLabel("APP.TABLE_120", { exact: true });
  await expect(lastRow).toBeVisible();

  // 行高の定数と実描画が一致していれば、末尾までスクロールした時点で
  // 最終行の下端とリスト下端が揃う(ずれていると末尾に空白が残る)。
  const listBox = await virtualList.boundingBox();
  const lastBox = await lastRow.boundingBox();
  expect(listBox).not.toBeNull();
  expect(lastBox).not.toBeNull();
  const gap = listBox!.y + listBox!.height - (lastBox!.y + lastBox!.height);
  expect(Math.abs(gap)).toBeLessThan(4);
});

test("業務プロファイル一覧の初回ロード中は空状態ではなくスケルトンを表示する", async ({ page }) => {
  await mockProfileApi(page);

  let releaseSearch: () => void = () => {};
  const searchGate = new Promise<void>((resolve) => {
    releaseSearch = resolve;
  });
  // mockProfileApi より後に登録した route が優先されるため、一覧応答だけを保留する。
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    await searchGate;
    await fulfillJson(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-19T00:00:00Z",
      })),
      next_cursor: null,
      total: profiles.length,
      change_token: 1,
    });
  });

  await page.goto("/profiles");

  await expect(page.getByTestId("profile-list-skeleton")).toBeVisible();
  await expect(page.getByText("プロファイルがありません")).toHaveCount(0);
  // 初回の読込のスピナーは一覧の読込表示の 1 つだけ（ヘッダーの「最新情報を取得」は回さない。#416）。
  await expectSingleSpinner(page, page.getByTestId("profile-list-loading"));

  releaseSearch();

  await expect(page.getByTestId("profile-management-list")).toBeVisible();
  await expect(page.getByTestId("profile-list-skeleton")).toHaveCount(0);
});

test("業務プロファイル一覧の並べ替えはサーバ順をそのまま表示する", async ({ page }) => {
  await mockProfileApi(page);

  const requestedSorts: string[] = [];
  // mockProfileApi より後に登録した route が優先される。
  // サーバ順が client 再ソートで崩れないよう、意図的に名前順と異なる順序を返す。
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    requestedSorts.push(`${params.get("sort")}:${params.get("direction")}`);
    await fulfillJson(route, {
      items: [
        { name: "ZETA", allowed_table_count: 9 },
        { name: "ALPHA", allowed_table_count: 5 },
        { name: "MIKE", allowed_table_count: 1 },
      ].map((item, index) => ({
        id: `profile-${index}`,
        name: item.name,
        category: "sales",
        description: "",
        archived: false,
        allowed_table_count: item.allowed_table_count,
        allowed_view_count: 0,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: `etag-${index}`,
        updated_at: "2026-07-19T00:00:00Z",
      })),
      next_cursor: null,
      total: 3,
      change_token: 1,
    });
  });

  await page.goto("/profiles");

  const rows = page.getByTestId("profile-management-grid").locator("tbody tr");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("ZETA");
  await expect(rows.nth(1)).toContainText("ALPHA");
  await expect(rows.nth(2)).toContainText("MIKE");
  expect(requestedSorts.at(0)).toBe("name:asc");

  await page.getByRole("button", { name: "許可表" }).click();
  await expect
    .poll(() => requestedSorts.at(-1))
    .toBe("tables:asc");

  await page.getByRole("button", { name: "許可表" }).click();
  await expect
    .poll(() => requestedSorts.at(-1))
    .toBe("tables:desc");
});

test("業務プロファイル検索は打鍵ごとに API を呼ばない", async ({ page }) => {
  let searchRequests = 0;
  let objectRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/nl2sql/profiles/search") searchRequests += 1;
    if (url.pathname === "/api/schema/objects") objectRequests += 1;
  });
  await mockProfileApi(page);

  await page.goto("/profiles?profile=default");
  await expect(page.getByTestId("profile-allowed-table-list")).toBeVisible();

  const initialSearchRequests = searchRequests;
  const initialObjectRequests = objectRequests;

  await page.getByRole("searchbox", { name: "オブジェクト検索" }).pressSequentially("TABLE_01", {
    delay: 20,
  });
  await expect(page.getByTestId("profile-allowed-table-list").getByLabel("APP.TABLE_01")).toBeVisible();

  // 8 打鍵に対して TABLE / VIEW 各 1 回(= 2 リクエスト)程度に収まる。
  expect(objectRequests - initialObjectRequests).toBeLessThanOrEqual(6);
  expect(searchRequests).toBe(initialSearchRequests);
});

test("引用符付きで保存された許可オブジェクトもチェックを外せる", async ({ page }) => {
  await mockProfileApi(page, {
    profileItems: [
      {
        ...profiles[0],
        // 旧データや手入力で入り得る表記揺れ。
        allowed_tables: ['"APP"."TABLE_01"'],
        allowed_views: ["app.view_02"],
      },
    ],
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  const viewList = page.getByTestId("profile-allowed-view-list");
  const table01 = tableList.getByLabel("APP.TABLE_01");
  const view02 = viewList.getByLabel("APP.VIEW_02");

  await expect(table01).toBeChecked();
  await expect(view02).toBeChecked();
  await expect(tableList).toContainText("選択 1 件");

  await table01.uncheck();
  await expect(table01).not.toBeChecked();
  await expect(tableList).toContainText("選択 0 件");

  await view02.uncheck();
  await expect(view02).not.toBeChecked();
  await expect(viewList).toContainText("選択 0 件");
});

test("手動更新の失敗通知はクエリ再描画で消えない", { tag: "@responsive" }, async ({ page }) => {
  await mockProfileApi(page);

  // mockProfileApi より後に登録した route が優先される。
  // 初回の読込（画面の取り直しを含む）は成功させ、ヘッダーの「最新情報を取得」の後の再取得だけ失敗させる。
  // 初回の読込は catalog/head を 2 回取りうるので、回数ではなく操作の前後で切り替える（#1922）。
  let failHead = false;
  await page.route("**/api/schema/catalog/head", async (route) => {
    if (!failHead) {
      await fulfillJson(route, {
        catalog_version: 1,
        schema_fingerprint: "fingerprint",
        refreshed_at: schemaCatalog.refreshed_at,
        object_count: schemaCatalog.tables.length,
        column_count: 0,
        change_token: 1,
        etag: "head-etag",
      });
      return;
    }
    await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });

  await page.goto("/profiles");
  await expect(page.getByTestId("profile-management-list")).toBeVisible();
  const notice = page.getByText("プロファイルの読込に失敗しました。");
  await expect(notice).toHaveCount(0);

  failHead = true;
  // ヘッダーの「最新情報を取得」。lg 未満（375px）では「その他の操作」のメニューに畳まれる。
  const actions = page.getByTestId("profile-management-actions");
  const refreshButton = actions.getByRole("button", { name: "最新情報を取得", exact: true });
  if (await refreshButton.isVisible()) {
    await refreshButton.click();
  } else {
    await actions.getByRole("button", { name: "その他の操作" }).click();
    await page.getByRole("menuitem", { name: "最新情報を取得", exact: true }).click();
  }

  await expect(notice).toBeVisible();
  // 再取得完了後の再描画でも保持され続ける(別 state で持つ)。
  await expect(notice).toBeVisible({ timeout: 3_000 });
  await page.waitForTimeout(1_000);
  await expect(notice).toBeVisible();
});

test("チェックを付け外しして元に戻したら未保存確認を出さない", async ({ page }) => {
  await mockProfileApi(page, {
    profileItems: [
      { ...profiles[0], allowed_tables: ["APP.TABLE_01", "APP.TABLE_02"], allowed_views: [] },
    ],
  });

  await page.goto("/profiles?profile=default");

  const tableList = page.getByTestId("profile-allowed-table-list");
  const table01 = tableList.getByLabel("APP.TABLE_01");

  // 先頭の選択を外して付け直すと末尾へ回る。内容は読込時と同じ。
  await table01.uncheck();
  await expect(table01).not.toBeChecked();
  await table01.check();
  await expect(table01).toBeChecked();

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(page.getByTestId("profile-management-list")).toBeVisible();
});

test("未保存のままサイドナビで離脱しようとすると確認を挟む", async ({ page }) => {
  await mockProfileApi(page);

  await page.goto("/profiles?profile=default");

  await page.getByLabel("カテゴリ").fill("finance");

  // 375px ではナビがドロワー（#367）。開いてからサイドナビのリンクを押す。
  const otherPageLink = (await openSidebarNav(page)).getByRole("link", { name: "オントロジー構築" }).first();
  await otherPageLink.click();

  // 確認ダイアログを挟み、キャンセルすれば編集画面に留まる。
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\/profiles\?profile=default/);
  await expect(page.getByLabel("カテゴリ")).toHaveValue("finance");

  // 承認したときだけ遷移する（確認をキャンセルしたのでドロワーは開いたまま）。
  await openSidebarNav(page);
  await otherPageLink.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "破棄して戻る" }).click();
  await expect(page).toHaveURL(/\/ontology-build/);
});

test("保存中はプロファイル編集と競合操作を固定し失敗後に復帰する", async ({ page }, testInfo) => {
  await mockProfileApi(page);
  await page.goto("/profiles?profile=default");
  const name = page.locator("#profile-name");
  await name.fill("PENDING_PROFILE");
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let saves = 0;
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    saves += 1;
    await gate;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "保存失敗テスト" }) });
  });
  await page.getByRole("button", { name: "保存", exact: true }).click();
  try {
    await expect(name).toBeDisabled();
    await expect(page.getByRole("button", { name: "変更を破棄", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "一覧へ戻る", exact: true })).toBeDisabled();
    await expect(page.getByTestId("profile-allowed-table-list").getByRole("option").first()).toBeDisabled();
  } finally { release?.(); }
  // 保存の失敗は保存ボタンの直下の FormStatus の 1 か所だけに出し、Toast に重ねない（messaging.md §3.3.1。#585）。
  await expect(page.getByTestId("profile-save-error").getByRole("alert")).toHaveText("保存失敗テスト");
  await expect(page.getByText("保存失敗テスト", { exact: true })).toHaveCount(1);
  await expect(name).toBeEnabled();
  await expect(name).toHaveValue("PENDING_PROFILE");
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeEnabled();
  expect(saves).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("profile-save-failure-draft.png") });
});

test("保存の 409（ほかの更新）は文に合わせて「最新の内容を読み込む」を出し、確認の後に最新の版へ置き換える", async ({ page }, testInfo) => {
  const profile = { ...profiles[0], name: "DEFAULT_PROFILE" };
  await mockProfileApi(page, { profileItems: [profile] });
  let serverVersion = 1;
  const ifMatches: string[] = [];
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      if (serverVersion === 1) return route.fallback();
      return fulfillJson(route, { ...profile, category: "営業", etag: "etag-default-2" });
    }
    if (method !== "PATCH") return route.fallback();
    ifMatches.push(route.request().headers()["if-match"] ?? "");
    if (serverVersion === 1) {
      // ほかの利用者の保存で版が進んだ（ETag の不一致）。
      serverVersion = 2;
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "業務プロファイルが更新されています。再読込してください。" }),
      });
    }
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, { ...profile, ...payload, id: "default", etag: "etag-default-3" });
  });
  await page.goto("/profiles?profile=default");
  const category = page.getByLabel("カテゴリ");
  await category.fill("finance");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  const saveError = page.getByTestId("profile-save-error");
  await expect(saveError.getByRole("alert")).toContainText("ほかの操作でこの業務プロファイルが更新された");
  const reload = saveError.getByRole("button", { name: "最新の内容を読み込む" });
  await expect(reload).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("profile-save-conflict.png") });

  // 編集中の内容は破棄されるため、確認を挟む。キャンセルすれば編集を残す。
  await reload.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("最新の内容を読み込みますか");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(category).toHaveValue("finance");
  await expect(reload).toBeVisible();

  await reload.click();
  await dialog.getByRole("button", { name: "破棄して読み込む" }).click();
  await expect(category).toHaveValue("営業");
  await expect(saveError).toHaveCount(0);

  // 最新の版の ETag で保存し直せる。
  await category.fill("経理");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => ifMatches.length).toBe(2);
  expect(ifMatches).toEqual(['"etag-default"', '"etag-default-2"']);
  await expect(saveError).toHaveCount(0);
});

test("スキーマ一括選択中は保存と対象切替を固定する", async ({ page }) => {
  await mockProfileApi(page, { profileItems: [{ ...profiles[0], allowed_tables: [], allowed_views: [] }] });
  await page.goto("/profiles?profile=default");
  const list = page.getByTestId("profile-allowed-table-list");
  await expect(list.getByLabel("APP.TABLE_01")).not.toBeChecked();
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/schema/objects?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("owner") === "APP") await gate;
    await route.fallback();
  });
  await list.getByRole("button", { name: "APP をすべて選択", exact: true }).click();
  try {
    await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "一覧へ戻る", exact: true })).toBeDisabled();
    await expect(list.getByLabel("APP.TABLE_01")).toBeDisabled();
  } finally { release?.(); }
  await expect(list.getByLabel("APP.TABLE_01")).toBeChecked();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeEnabled();
});

test("プロファイル詳細取得失敗は選択 URL を保持し再試行できる", async ({ page }, testInfo) => {
  await mockProfileApi(page);
  let failed = true;
  await page.route("**/api/nl2sql/profiles/default", async (route) => {
    if (!failed) return route.fallback();
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "プロファイル詳細テストエラー" }) });
  });
  await page.goto("/profiles?profile=default");
  await expect(page.getByText("プロファイル詳細テストエラー", { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page).toHaveURL(/profile=default/);
  await expect(page.getByRole("button", { name: "保存", exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("profile-detail-retry.png") });
  failed = false;
  await page.getByRole("button", { name: "再試行", exact: true }).press("Enter");
  await expect(page.locator("#profile-name")).toBeVisible();
  await expect(page).toHaveURL(/profile=default/);
  await page.route("**/api/nl2sql/profiles/missing", (route) => route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "not found" }) }));
  await page.goto("/profiles?profile=missing");
  await expect(page.getByText("指定された業務プロファイルが見つかりません。", { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page).toHaveURL(/profile=missing/);
});

test("業務プロファイルの一覧と編集のフォームは 1280 / 1920 / 375px・ライト / ダークで欄の幅と placeholder が崩れない", { tag: "@responsive" }, async ({ page }) => {
  // 一覧の件数はツールバーの ListCount、編集のフォームは FormGrid と値の種類の幅（#1891）。
  test.slow();
  await mockProfileApi(page);
  const widths = test.info().project.name === "mobile-375" ? [375] : [1280, 1920];
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme });
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      for (const path of ["/profiles", "/profiles?profile=default"]) {
        await page.goto(path);
        if (path === "/profiles") {
          await expect(page.getByTestId("profile-list-toolbar").getByRole("status")).toHaveText(/^全 \d+ 件$/u);
        } else {
          await expect(page.locator("#profile-sql-rules-input")).toBeVisible();
          await expect(page.getByTestId("profile-allowed-table-list")).toBeVisible();
        }
        const label = `${path} ${width}px ${colorScheme}`;
        expect(await measureHorizontalOverflow(page), label).toBeLessThanOrEqual(1);
        expect(await findClippedPlaceholders(page), label).toEqual([]);
        if (width >= 640) expect(await findFieldsOffWidthScale(page), label).toEqual([]);
      }
    }
  }
});
