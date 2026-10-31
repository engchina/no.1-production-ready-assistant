import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { clickOntologyEdge, expectLegacyOntologyControls } from "./_helpers/ontology-controls";
import { chooseSelectFieldOption } from "./_helpers/select-field";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

const profile = {
  id: "default",
  name: "既定プロファイル",
  category: "既定",
  description: "",
  allowed_tables: ["ORDERS"],
  allowed_views: [],
  glossary: {},
  sql_rules: [],
  default_row_limit: 100,
  safety_policy: "select_only",
  few_shot_examples: [],
  select_ai_config: null,
  archived: false,
};

const nodes = [
  {
    id: "e1",
    kind: "business_entity",
    business_name_ja: "顧客",
    technical_name: "APP.CUSTOMERS",
    review_status: "approved",
    validation_status: "passed",
    aliases: ["得意先"],
  },
  {
    id: "e2",
    kind: "business_entity",
    business_name_ja: "注文",
    technical_name: "APP.ORDERS",
    review_status: "approved",
  },
  {
    id: "m1",
    kind: "metric",
    business_name_ja: "売上合計",
    technical_name: "SUM(AMOUNT)",
    review_status: "approved",
  },
  {
    id: "c1",
    kind: "column",
    business_name_ja: "顧客ID",
    technical_name: "APP.CUSTOMERS.ID",
    review_status: "approved",
  },
  {
    id: "c2",
    kind: "column",
    business_name_ja: "注文金額",
    technical_name: "APP.ORDERS.AMOUNT",
    review_status: "approved",
  },
];
const edges = [
  {
    id: "r1",
    kind: "business_relationship",
    source_node_id: "e1",
    target_node_id: "e2",
    relationship_name_ja: "注文する",
    cardinality: "one_to_many",
    review_status: "approved",
  },
  {
    id: "r4",
    kind: "maps_to",
    source_node_id: "m1",
    target_node_id: "e2",
    relationship_name_ja: "集計対象",
    review_status: "approved",
  },
  {
    id: "r5",
    kind: "maps_to",
    source_node_id: "c1",
    target_node_id: "e1",
    relationship_name_ja: "列",
    review_status: "approved",
  },
  {
    id: "r6",
    kind: "maps_to",
    source_node_id: "c2",
    target_node_id: "e2",
    relationship_name_ja: "列",
    review_status: "approved",
  },
];

const employeeOntologyGraph = {
  nodes: [
    {
      id: "employee-business",
      kind: "business_entity",
      business_name_ja: "従業員",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      validation_status: "passed",
      aliases: ["社員"],
      physical_mappings: [
        {
          object_ref: {
            node_id: "employee-table",
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            object_type: "table",
          },
        },
      ],
    },
    {
      id: "employee-table",
      kind: "table",
      business_name_ja: "従業員情報",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      validation_status: "passed",
      physical_mappings: [
        {
          object_ref: {
            node_id: "employee-table",
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            object_type: "table",
          },
        },
      ],
    },
  ],
  edges: [
    {
      id: "employee-maps-to",
      kind: "maps_to",
      source_node_id: "employee-business",
      target_node_id: "employee-table",
      relationship_name_ja: "物理マッピング",
      review_status: "approved",
      validation_status: "passed",
    },
  ],
};

const erDetailOntologyGraph = {
  nodes: [
    {
      id: "employee-business",
      kind: "business_entity",
      business_name_ja: "従業員",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      validation_status: "passed",
      physical_mappings: [
        {
          object_ref: {
            node_id: "employee-table",
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            object_type: "table",
          },
        },
      ],
    },
    {
      id: "employee-table",
      kind: "table",
      business_name_ja: "従業員情報",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      validation_status: "passed",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        object_type: "TABLE",
      },
    },
    {
      id: "department-table",
      kind: "table",
      business_name_ja: "部署情報",
      technical_name: "ADMIN.DEPARTMENT",
      review_status: "approved",
      validation_status: "passed",
      metadata: {
        owner: "ADMIN",
        object_name: "DEPARTMENT",
        object_type: "TABLE",
      },
    },
    {
      id: "employee-id",
      kind: "column",
      business_name_ja: "従業員ID",
      technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_ID",
      review_status: "approved",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "EMPLOYEE_ID",
        data_type: "NUMBER",
        ordinal: 1,
        primary_key: true,
      },
    },
    {
      id: "employee-department-id",
      kind: "column",
      business_name_ja: "所属部署ID",
      technical_name: "ADMIN.EMPLOYEE.DEPARTMENT_ID",
      review_status: "approved",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "DEPARTMENT_ID",
        data_type: "NUMBER",
        ordinal: 2,
      },
    },
    {
      id: "employee-name",
      kind: "column",
      business_name_ja: "従業員氏名",
      technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
      description_ja: "従業員の氏名。",
      review_status: "approved",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "EMPLOYEE_NAME",
        data_type: "VARCHAR2",
        ordinal: 3,
      },
    },
    {
      id: "employee-hire-date",
      kind: "column",
      business_name_ja: "入社日",
      technical_name: "ADMIN.EMPLOYEE.HIRE_DATE",
      review_status: "approved",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "HIRE_DATE",
        data_type: "DATE",
        ordinal: 4,
      },
    },
    {
      id: "department-id",
      kind: "column",
      business_name_ja: "部署ID",
      technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_ID",
      review_status: "approved",
      metadata: {
        owner: "ADMIN",
        object_name: "DEPARTMENT",
        column_name: "DEPARTMENT_ID",
        data_type: "NUMBER",
        ordinal: 1,
        primary_key: true,
      },
    },
  ],
  edges: [
    {
      id: "employee-maps-to",
      kind: "maps_to",
      source_node_id: "employee-business",
      target_node_id: "employee-table",
      relationship_name_ja: "物理マッピング",
      review_status: "approved",
      validation_status: "passed",
    },
    {
      id: "employee-department-fk",
      kind: "foreign_key",
      source_node_id: "employee-table",
      target_node_id: "department-table",
      relationship_name_ja: "所属部署を参照",
      cardinality: "many_to_one",
      review_status: "approved",
      validation_status: "passed",
      join_conditions: [
        {
          left: {
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            column_name: "DEPARTMENT_ID",
          },
          right: {
            owner: "ADMIN",
            object_name: "DEPARTMENT",
            column_name: "DEPARTMENT_ID",
          },
          operator: "=",
          ordinal: 1,
        },
      ],
    },
  ],
};

const relationshipRows = Array.from({ length: 30 }, (_, index) => {
  const source = edges[index % edges.length];
  return {
    ...source,
    id: `relationship-row-${index + 1}`,
    relationship_name_ja: `${source.relationship_name_ja} ${index + 1}`,
  };
});

const nodePickerOverflowGraph = {
  nodes: Array.from({ length: 14 }, (_, index) => ({
    id: `picker-node-${index + 1}`,
    kind: index % 3 === 0 ? "business_entity" : index % 3 === 1 ? "business_term" : "metric",
    business_name_ja: `選択候補 ${index + 1}`,
    technical_name: `ADMIN.PICKER_NODE_${index + 1}`,
    review_status: "approved",
  })),
  edges: [],
};

async function expectNoHorizontalScroll(page: Page) {
  const size = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(size.scrollWidth).toBeLessThanOrEqual(size.width + 1);
}

async function openGraphIfCollapsed(page: Page, playground: Locator) {
  await expect(playground.getByTestId("ontology-playground-question")).toBeVisible();
  const viewportWidth = page.viewportSize()?.width ?? 0;
  if (viewportWidth >= 1280) return;
  const toggle = playground.getByRole("button", { name: "グラフを表示" });
  if (await toggle.isVisible()) {
    await toggle.click();
  }
}

async function expectQuestionActionLayoutWithClear(page: Page, playground: Locator) {
  const input = playground.getByTestId("ontology-playground-question");
  const runButton = playground.getByTestId("ontology-playground-run");
  const clearButton = playground.getByTestId("ontology-playground-clear");
  await expect(clearButton).toHaveText("質問・確認結果をクリア");
  await expect(clearButton).toHaveAccessibleName("質問・確認結果をクリア");
  const [inputBox, runBox, clearBox] = await Promise.all([
    input.boundingBox(),
    runButton.boundingBox(),
    clearButton.boundingBox(),
  ]);
  expect(inputBox).not.toBeNull();
  expect(runBox).not.toBeNull();
  expect(clearBox).not.toBeNull();
  // 質問の行は lg（40px）。タッチ端末（pointer: coarse）では 44px（#613）。
  const coarse = await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches);
  for (const box of [inputBox, runBox, clearBox]) {
    expect(box!.height).toBeGreaterThanOrEqual(coarse ? 43 : 39);
    expect(box!.height).toBeLessThanOrEqual(coarse ? 45 : 41);
  }

  const viewportWidth = page.viewportSize()?.width ?? 0;
  if (viewportWidth >= 640) {
    expect(runBox!.x).toBeGreaterThan(inputBox!.x + inputBox!.width);
    expect(clearBox!.x).toBeGreaterThan(runBox!.x + runBox!.width);
    expect(Math.abs(runBox!.y - inputBox!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(clearBox!.y - inputBox!.y)).toBeLessThanOrEqual(1);
    return;
  }

  expect(runBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
  expect(clearBox!.y).toBeGreaterThan(runBox!.y + runBox!.height);
  expect(Math.abs(runBox!.x - inputBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(clearBox!.x - inputBox!.x)).toBeLessThanOrEqual(1);
  expect(runBox!.width).toBeGreaterThanOrEqual(inputBox!.width - 1);
  expect(clearBox!.width).toBeGreaterThanOrEqual(inputBox!.width - 1);
}

function hasVisibleBoxShadow(value: string) {
  if (value === "none") return false;
  return value
    .replace(/rgba\(0, 0, 0, 0\) 0px 0px 0px 0px/g, "")
    .replace(/,\s*/g, "")
    .trim().length > 0;
}

async function expectGraphSearchFieldLayout(page: Page, playground: Locator) {
  const field = playground.getByTestId("ontology-graph-search-field");
  const input = playground.getByTestId("ontology-graph-search");
  const modeControl = playground.getByTestId("ontology-graph-view-mode");
  const detailsToggleField = playground.getByTestId("ontology-graph-details-toggle-field");

  await expect(field).toBeVisible();
  await expect(input).toBeVisible();

  const [fieldBox, inputBox, modeBox, detailsBox] = await Promise.all([
    field.boundingBox(),
    input.boundingBox(),
    modeControl.boundingBox(),
    detailsToggleField.boundingBox(),
  ]);
  expect(fieldBox).not.toBeNull();
  expect(inputBox).not.toBeNull();
  expect(modeBox).not.toBeNull();
  expect(detailsBox).not.toBeNull();

  const viewportWidth = page.viewportSize()?.width ?? 0;
  // グラフのツールバーは lg（40px）。画面幅ではなく入力方式で、タッチ端末は 44px（#613）。
  const coarse = await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches);
  expect(fieldBox!.height).toBeGreaterThanOrEqual(coarse ? 43 : 39);
  expect(fieldBox!.height).toBeLessThanOrEqual(45);
  expect(Math.abs(fieldBox!.height - modeBox!.height)).toBeLessThanOrEqual(1);
  expect(Math.abs(fieldBox!.height - detailsBox!.height)).toBeLessThanOrEqual(1);

  const fieldCenterY = fieldBox!.y + fieldBox!.height / 2;
  const inputCenterY = inputBox!.y + inputBox!.height / 2;
  expect(Math.abs(fieldCenterY - inputCenterY)).toBeLessThanOrEqual(1.5);
  expect(inputBox!.x).toBeGreaterThan(fieldBox!.x + 28);
  expect(inputBox!.x + inputBox!.width).toBeLessThanOrEqual(fieldBox!.x + fieldBox!.width - 10);
  expect(inputBox!.height).toBeLessThan(fieldBox!.height - 8);

  const inputFrameStyles = await input.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      borderTopWidth: style.borderTopWidth,
      borderRightWidth: style.borderRightWidth,
      borderBottomWidth: style.borderBottomWidth,
      borderLeftWidth: style.borderLeftWidth,
      boxShadow: style.boxShadow,
      outlineStyle: style.outlineStyle,
      paddingTop: style.paddingTop,
      paddingBottom: style.paddingBottom,
    };
  });
  expect(inputFrameStyles).toMatchObject({
    borderTopWidth: "0px",
    borderRightWidth: "0px",
    borderBottomWidth: "0px",
    borderLeftWidth: "0px",
    outlineStyle: "none",
    paddingTop: "0px",
    paddingBottom: "0px",
  });
  expect(hasVisibleBoxShadow(inputFrameStyles.boxShadow)).toBe(false);

  const kindFilter = playground.getByRole("combobox", { name: "概念の種類", exact: true });
  await kindFilter.focus();
  await page.keyboard.press("Tab");
  await expect(playground.getByTestId("ontology-graph-mode-grounding")).toBeFocused();
  await playground.getByTestId("ontology-graph-mode-physical_er").focus();
  await page.keyboard.press("Tab");
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.getAttribute("data-testid")))
    .toBe("ontology-graph-search");

  const focusedInputStyles = await input.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      borderTopWidth: style.borderTopWidth,
      boxShadow: style.boxShadow,
      outlineStyle: style.outlineStyle,
    };
  });
  expect(focusedInputStyles).toMatchObject({
    borderTopWidth: "0px",
    outlineStyle: "none",
  });
  expect(hasVisibleBoxShadow(focusedInputStyles.boxShadow)).toBe(false);

  const focusedFieldShadow = await field.evaluate(
    (element) => window.getComputedStyle(element).boxShadow
  );
  expect(hasVisibleBoxShadow(focusedFieldShadow)).toBe(true);

  if (viewportWidth >= 640) {
    const laneBox = await playground.getByTestId("ontology-graph-lane-business").boundingBox();
    expect(laneBox).not.toBeNull();
    const focusedFieldBox = await field.boundingBox();
    expect(focusedFieldBox!.y + focusedFieldBox!.height + 4).toBeLessThanOrEqual(laneBox!.y);
  }
}

type MockApiOptions = {
  revisionVersion?: number;
  ontologyGraph?: {
    nodes: unknown[];
    edges: unknown[];
  };
};

async function mockApi(page: Page, options: MockApiOptions = {}) {
  const ontologyGraph = options.ontologyGraph ?? {
    nodes,
    edges: relationshipRows,
  };

  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, { refreshed_at: "2026-07-12T00:00:00Z", tables: [] })
  );
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, [profile]));
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: "default",
          name: "既定プロファイル",
          category: "既定",
          description: "",
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "e",
          updated_at: "2026-07-12T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route(/\/api\/nl2sql\/profiles\/[^/?]+$/, (route) =>
    fulfillJson(route, profile)
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) =>
    fulfillJson(route, {
      materialized: true,
      stale: false,
      profile_ontology_view: {
        id: "v",
        profile_id: "default",
        ontology_revision_id: "rev1",
        etag: "ve",
      },
      ontology_graph: {
        revision: {
          id: "rev1",
          version: options.revisionVersion ?? 1,
          status: "published",
          schema_fingerprint: "fp",
          etag: "re",
        },
        nodes: ontologyGraph.nodes,
        edges: ontologyGraph.edges,
      },
    })
  );
  await page.route("**/api/nl2sql/ontology/revisions", (route) =>
    fulfillJson(route, { revisions: [], active_revision_id: "" })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-proposals", (route) =>
    fulfillJson(route, { proposals: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "# 公開済みオントロジー",
      draft_revision: null,
      published_revision: { id: "rev1", version: options.revisionVersion ?? 1, status: "published", etag: "re", schema_fingerprint: "fp" },
      draft_etag: "",
      published_at: null,
    })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", (route) =>
    fulfillJson(route, { source_documents: [] })
  );
}

async function expectEntireGraphInCanvas(playground: Locator) {
  const canvas = playground.getByTestId("ontology-graph-canvas");
  await expect.poll(() => canvas.evaluate(element => {
    const frame = element.getBoundingClientRect();
    const content = [...element.querySelectorAll("[data-ontology-lane-label], .react-flow__node")];
    return content.length > 0 && content.every(item => {
      const rect = item.getBoundingClientRect();
      return rect.left >= frame.left && rect.right <= frame.right && rect.top >= frame.top && rect.bottom <= frame.bottom;
    });
  })).toBe(true);
}

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 1920]) {
    test(`概念の種類を先頭にして操作枠を整列する ${theme} ${width}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop" && width !== 1280, "モバイルは375幅で確認");
      if (testInfo.project.name === "desktop") await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript((theme) => {
        localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({
          state: { sidebarCollapsed: false, collapsedSections: {}, theme }, version: 0,
        }));
      }, theme);
      await mockApi(page, { ontologyGraph: erDetailOntologyGraph });
      await page.goto("/ontology-build?profile=default");
      await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
      await page.keyboard.press("Tab");
      await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
      const heading = page.getByRole("heading", { level: 1 });
      const bodyStart = page.locator("main header + div > :first-child");
      const headingBox = await heading.boundingBox();
      const bodyBox = await bodyStart.boundingBox();
      expect(headingBox).not.toBeNull();
      expect(bodyBox).not.toBeNull();
      expect(Math.abs(headingBox!.x - bodyBox!.x)).toBeLessThanOrEqual(1);
      await page.getByTestId("ontology-view-fetch").click();
      const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
      await openGraphIfCollapsed(page, playground);
      await expectGraphSearchFieldLayout(page, playground);
      const toolbar = playground.getByTestId("ontology-graph-toolbar");
      const filter = toolbar.getByRole("combobox", { name: "概念の種類", exact: true });
      await expect(toolbar.locator("select, button, input").first()).toHaveAttribute("aria-label", "概念の種類");
      const controls = [filter, ...["ontology-graph-view-mode", "ontology-graph-search-field", "ontology-graph-details-toggle-field"].map(id => toolbar.getByTestId(id))];
      const boxes = await Promise.all(controls.map(control => control.boundingBox()));
      for (const box of boxes) {
        expect(box).not.toBeNull();
        expect(Math.abs(box!.height - boxes[0]!.height)).toBeLessThanOrEqual(1);
        if (testInfo.project.name === "desktop" && width === 1920) expect(Math.abs(box!.y - boxes[0]!.y)).toBeLessThanOrEqual(1);
      }
      if (testInfo.project.name === "desktop" && width === 1280) {
        // 右側の詳細パネルを含む狭いグラフでは2行。各行で上下端を揃える。
        expect(Math.abs(boxes[0]!.y - boxes[1]!.y)).toBeLessThanOrEqual(1);
        expect(Math.abs(boxes[2]!.y - boxes[3]!.y)).toBeLessThanOrEqual(1);
        expect(boxes[2]!.y).toBeGreaterThanOrEqual(boxes[0]!.y + boxes[0]!.height);
      }
      const canvas = playground.getByTestId("ontology-graph-canvas");
      expect(await canvas.evaluate(el => el.getBoundingClientRect().height / parseFloat(getComputedStyle(document.documentElement).fontSize))).toBeCloseTo(56, 0);
      await expectEntireGraphInCanvas(playground);
      await playground.getByRole("button", { name: "グラフを拡大", exact: true }).click();
      const fit = playground.getByRole("button", { name: "グラフ全体を表示", exact: true });
      await fit.focus();
      await page.keyboard.press("Enter");
      await expectEntireGraphInCanvas(playground);
      await canvas.screenshot({ path: testInfo.outputPath(`fit-labels-${theme}-${width}.png`) });
      await filter.selectOption("object_type");
      await expect(filter).toHaveValue("object_type");
      await filter.selectOption("");
      await expectNoHorizontalScroll(page);
      await toolbar.screenshot({ path: testInfo.outputPath(`toolbar-${theme}-${width}.png`) });
    });
  }
}

for (const version of [1, 2]) {
  test(`公開版 v${version} の接地確認に件数と版番号を表示しない`, async ({ page }, testInfo) => {
    await mockApi(page, { revisionVersion: version });
    await page.goto("/ontology-build?profile=default");
    await page.getByTestId("ontology-view-fetch").click();
    const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
    await expect(playground.getByTestId("ontology-playground-question")).toBeVisible();
    await expect(playground.getByTestId("ontology-playground-graph-summary")).toHaveCount(0);
    await expect(playground.getByTestId("ontology-playground-version")).toHaveCount(0);
    await expect(playground).not.toContainText("確認対象:");
    await expect(playground).not.toContainText("公開済みバージョン:");
    await expect(playground.getByTestId("ontology-playground-ready-state")).toHaveText(
      "質問を入力すると、一致したノードと関係をグラフで強調表示します。"
    );
    await expectNoHorizontalScroll(page);
    await playground.getByRole("heading", { name: "質問のオントロジー接地確認用グラフ", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`grounding-without-summary-v${version}.png`) });
  });
}

test("グラフ操作部は旧版の外観でモード・凡例・ズーム・検索を操作できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockApi(page, { ontologyGraph: erDetailOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await openGraphIfCollapsed(page, playground);
  await expectLegacyOntologyControls(page, playground);
  await expectGraphSearchFieldLayout(page, playground);

  const viewport = playground.locator(".react-flow__viewport");
  const initialTransform = await viewport.getAttribute("style");
  await playground.getByRole("button", { name: "グラフを拡大", exact: true }).click();
  await expect.poll(() => viewport.getAttribute("style")).not.toBe(initialTransform);
  const zoomedTransform = await viewport.getAttribute("style");
  await playground.getByRole("button", { name: "グラフを縮小", exact: true }).click();
  await expect.poll(() => viewport.getAttribute("style")).not.toBe(zoomedTransform);
  await playground.getByRole("button", { name: "グラフ全体を表示", exact: true }).click();
  await expect(playground.getByTestId("ontology-graph-reset-layout")).toBeDisabled();

  const search = playground.getByTestId("ontology-graph-search");
  await search.fill("従業員");
  const nav = playground.getByTestId("ontology-graph-search-nav");
  await expect(nav).toHaveCSS("height", testInfo.project.name === "mobile-375" ? "44px" : "40px");
  const next = playground.getByTestId("ontology-graph-search-next");
  const prev = playground.getByTestId("ontology-graph-search-prev");
  await expect(next).toBeEnabled();
  const firstMatch = await nav.innerText();
  await next.click();
  await expect(nav).not.toHaveText(firstMatch);
  await prev.click();
  await expect(nav).toHaveText(firstMatch);
  await search.fill("no-such-ontology-node");
  await expect(next).toBeDisabled();
  await expect(prev).toBeDisabled();
  await search.press("Escape");
  await expect(nav).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await playground.screenshot({ path: testInfo.outputPath("ontology-controls-restored.png") });
});

test("グラフはカード表示 + 検索 + 詳細ノードの折畳ができる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1440, height: 900 });
  }
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-nl2sql.ui",
      JSON.stringify({
        state: { sidebarCollapsed: false, collapsedSections: {}, theme: "dark" },
        version: 0,
      })
    );
  });
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await expect(
    playground.getByText("質問を入力すると、一致したノードと関係をグラフで強調表示します。")
  ).toBeVisible();
  await expect(playground).not.toContainText("確認対象:");
  await expect(playground).not.toContainText("公開済みバージョン:");
  await expect(playground.getByTestId("ontology-playground-clear")).toBeDisabled();
  await expectQuestionActionLayoutWithClear(page, playground);
  await expectNoHorizontalScroll(page);
  await openGraphIfCollapsed(page, playground);
  await expectLegacyOntologyControls(page, playground);
  await expect(playground.getByTestId("ontology-graph-view-mode")).toBeVisible();
  await expect(playground.getByTestId("ontology-graph-mode-all")).toHaveAttribute("aria-pressed", "true");
  await expect(playground.getByTestId("ontology-graph-lane-business")).toContainText("業務概念");
  await expect(playground.getByTestId("ontology-graph-lane-attribute")).toContainText("属性・指標");
  // 詳細(列・列挙値)ノードが畳まれている間は空レーンのラベルを出さない
  await expect(playground.getByTestId("ontology-graph-lane-detail")).toHaveCount(0);
  await expectGraphSearchFieldLayout(page, playground);
  await expectNoHorizontalScroll(page);

  // カードノード: 業務名 + 技術名の 2 段表示
  const customer = playground.locator(".react-flow__node", { hasText: "顧客" }).first();
  await expect(customer).toBeVisible();
  await expect(customer).toContainText("APP.CUSTOMERS");
  await expect(playground.getByTestId("ontology-node-card-e1")).toHaveCSS(
    "background-color",
    "rgb(23, 58, 94)"
  );
  await expect(playground.getByTestId("ontology-node-card-e1")).toHaveCSS(
    "color",
    "rgb(239, 243, 247)"
  );
  await expect(playground.getByTestId("ontology-node-card-m1")).toHaveCSS(
    "background-color",
    "rgb(54, 42, 89)"
  );

  // 既定では列・列挙値ノード(2 件)は畳まれる
  await expect(playground.locator(".react-flow__node")).toHaveCount(3);
  await playground.getByTestId("ontology-graph-details-toggle").check();
  await expect(playground.locator(".react-flow__node")).toHaveCount(5);
  // 展開すると詳細レーンのラベルが現れる
  await expect(playground.getByTestId("ontology-graph-lane-detail")).toContainText("物理列・列挙値");
  await expect(playground.getByTestId("ontology-node-card-c1")).toHaveCSS(
    "background-color",
    "rgb(36, 43, 53)"
  );
  await playground.getByTestId("ontology-graph-details-toggle").uncheck();

  // 検索は一致ノードを強調し、非一致を減光する(opacity)
  await playground.getByTestId("ontology-graph-search").fill("顧客");
  const orderCard = playground
    .locator(".react-flow__node", { hasText: "売上合計" })
    .locator("div")
    .first();
  await expect(orderCard).toHaveCSS("opacity", "0.35");
  await playground.getByTestId("ontology-graph-search").fill("");
  await expect(orderCard).toHaveCSS("opacity", "1");

  const modeButtons = playground.getByTestId("ontology-graph-view-mode").getByRole("button");
  for (const button of await modeButtons.all()) {
    const geometry = await button.evaluate(node => ({
      right: node.getBoundingClientRect().right,
      clipped: node.scrollWidth > node.clientWidth,
      viewport: window.innerWidth,
    }));
    expect(geometry.right).toBeLessThanOrEqual(geometry.viewport);
    expect(geometry.clipped).toBe(false);
  }
  await page.screenshot({ path: testInfo.outputPath(`ontology-graph-dark-${testInfo.project.name}.png`) });
});

test("同じ物理名の業務概念と物理表をカード上で区別できる", async ({ page }) => {
  await mockApi(page, { ontologyGraph: employeeOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  const businessCard = playground.getByTestId("ontology-node-card-employee-business");
  await expect(businessCard).toBeVisible();
  await expect(businessCard).toHaveAttribute("data-ontology-node-kind", "business_entity");
  await expect(businessCard).toContainText("業務概念");
  await expect(businessCard).toContainText("従業員");
  await expect(businessCard).toContainText("対応表: ADMIN.EMPLOYEE");

  const tableCard = playground.getByTestId("ontology-node-card-employee-table");
  await expect(tableCard).toBeVisible();
  await expect(tableCard).toHaveAttribute("data-ontology-node-kind", "table");
  await expect(tableCard).toContainText("物理表");
  await expect(tableCard).toContainText("従業員情報");
  await expect(tableCard).toContainText("物理名: ADMIN.EMPLOYEE");

  const legend = playground.getByTestId("ontology-graph-legend");
  await expect(legend).toContainText("業務概念");
  await expect(legend).toContainText("物理表・ビュー");
  await expect(legend).toContainText("物理マッピング = 業務概念と物理表・ビューの対応");
  await expectNoHorizontalScroll(page);
});

test("大文字小文字だけが異なる物理表は別カード・別 cluster として SQL と同じ表記で表示する (#563)", { tag: "@responsive" }, async ({
  page,
}) => {
  const physicalTable = (id: string, objectName: string, technicalName: string, label: string) => ({
    id,
    kind: "table",
    business_name_ja: label,
    technical_name: technicalName,
    review_status: "approved",
    validation_status: "passed",
    metadata: { owner: "ADMIN", object_name: objectName, object_type: "table" },
    physical_mappings: [
      { object_ref: { node_id: id, owner: "ADMIN", object_name: objectName, object_type: "table" } },
    ],
  });
  await mockApi(page, {
    ontologyGraph: {
      nodes: [
        physicalTable("upper-table", "MIXED_CASE", "ADMIN.MIXED_CASE", "大文字の受注"),
        physicalTable("quoted-table", "Mixed_Case", 'ADMIN."Mixed_Case"', "引用名の受注"),
      ],
      edges: [],
    },
  });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  const upperCard = playground.getByTestId("ontology-node-card-upper-table");
  const quotedCard = playground.getByTestId("ontology-node-card-quoted-table");
  await expect(upperCard).toContainText("物理名: ADMIN.MIXED_CASE");
  await expect(quotedCard).toContainText('物理名: ADMIN."Mixed_Case"');
  // 大文字化した cluster に潰れると同じ列に重なる。別 cluster なら横位置が異なる。
  const upperBox = await upperCard.boundingBox();
  const quotedBox = await quotedCard.boundingBox();
  expect(upperBox).not.toBeNull();
  expect(quotedBox).not.toBeNull();
  expect(Math.abs((upperBox?.x ?? 0) - (quotedBox?.x ?? 0))).toBeGreaterThan(1);
  await expectNoHorizontalScroll(page);
});

test("質問接地グラフで選択した物理オブジェクトの ER 詳細を段階表示する", async ({ page }) => {
  await mockApi(page, { ontologyGraph: erDetailOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);
  await expect(playground.getByTestId("ontology-er-details-panel")).toHaveCount(0);

  await expect(playground.getByTestId("ontology-inspector-node-picker")).toBeVisible();
  await playground.getByTestId("ontology-inspector-node-employee-business").click();

  const details = playground.getByTestId("ontology-er-details-panel");
  await expect(details).toBeVisible();
  await expect(details.getByRole("heading", { name: "ER 詳細" })).toBeVisible();
  await expect(details.getByTestId("ontology-er-detail-object-name")).toContainText("ADMIN.EMPLOYEE");
  await expect(details).toContainText("列 4");

  const columns = details.getByTestId("ontology-er-columns");
  await expect(columns).toContainText("EMPLOYEE_ID");
  await expect(columns).toContainText("DEPARTMENT_ID");
  await expect(columns).toContainText("EMPLOYEE_NAME");
  await expect(columns).toContainText("HIRE_DATE");
  await expect(columns).toContainText("NUMBER");
  await expect(columns).toContainText("VARCHAR2");
  await expect(columns).toContainText("DATE");
  await expect(columns).toContainText("PK");
  await expect(columns).toContainText("FK");

  const joins = details.getByTestId("ontology-er-joins");
  await expect(joins).toContainText("所属部署を参照");
  await expect(joins).toContainText("ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID");
  await expect(playground.getByTestId("ontology-node-card-employee-table")).toContainText("列 4");
  await expect(playground.getByTestId("ontology-node-card-employee-table")).toContainText("Join 1");

  await playground.getByTestId("ontology-graph-details-toggle").check();
  await expect(playground.getByTestId("ontology-node-card-employee-department-id")).toBeVisible();
  await expect(details).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("質問を接地すると分類とグラフ強調が表示され、入力削除とクリアで reset できる", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);
  await expect(playground.getByTestId("ontology-playground-clear")).toBeDisabled();
  await playground.getByTestId("ontology-playground-question").fill("顧客と注文の関係は?");
  await expect(playground.getByTestId("ontology-playground-clear")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-clear")).toBeEnabled();
  await expectQuestionActionLayoutWithClear(page, playground);
  await expectNoHorizontalScroll(page);
  await playground.getByTestId("ontology-playground-run").click();

  const result = playground.getByTestId("ontology-playground-result");
  await expect(result).toContainText("関係の一致");
  await expect(result).toContainText("注文する");
  await expect(playground.getByTestId("ontology-playground-ready-state")).toHaveCount(0);
  await expect(playground.getByTestId("ontology-graph-mode-grounding")).toHaveAttribute("aria-pressed", "true");
  await expect(playground.getByTestId("ontology-grounding-path-panel")).toContainText("接地パス");
  await expect(playground.getByTestId("ontology-grounding-path-panel")).toContainText("顧客");
  await expect(playground.getByTestId("ontology-grounding-path-panel")).toContainText("注文");
  await expect(playground.getByTestId("ontology-inspector-relationships")).toContainText("注文する");

  const metricCard = playground
    .locator(".react-flow__node", { hasText: "売上合計" })
    .locator("div")
    .first();
  await expect(metricCard).toHaveCSS("opacity", "0.35");
  await expect(playground.getByTestId("ontology-playground-clear")).toBeVisible();
  await expectQuestionActionLayoutWithClear(page, playground);
  await expectNoHorizontalScroll(page);

  await playground.getByTestId("ontology-playground-question").fill("");
  await expect(playground.getByTestId("ontology-playground-question")).toHaveValue("");
  await expect(playground.getByTestId("ontology-playground-ready-state")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-result")).toHaveCount(0);
  await expect(playground.getByTestId("ontology-graph-mode-all")).toHaveAttribute("aria-pressed", "true");
  await expect(metricCard).toHaveCSS("opacity", "1");
  await expect(playground.getByTestId("ontology-selection-details-panel")).toContainText(
    "グラフ上のノードや関係の線を選択"
  );
  await expect(playground.getByTestId("ontology-playground-clear")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-clear")).toBeDisabled();

  await playground.getByTestId("ontology-playground-question").fill("顧客と注文の関係は?");
  await expect(playground.getByTestId("ontology-playground-clear")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-clear")).toBeEnabled();
  await expectQuestionActionLayoutWithClear(page, playground);
  await playground.getByTestId("ontology-playground-run").click();
  await expect(playground.getByTestId("ontology-playground-result")).toContainText("関係の一致");
  await expect(metricCard).toHaveCSS("opacity", "0.35");
  await playground.getByTestId("ontology-playground-clear").focus();
  await page.screenshot({ path: testInfo.outputPath("grounding-clear.png") });
  await playground.getByTestId("ontology-playground-clear").press("Enter");
  await expect(playground.getByTestId("ontology-playground-question")).toHaveValue("");
  await expect(playground.getByTestId("ontology-playground-ready-state")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-result")).toHaveCount(0);
  await expect(playground.getByTestId("ontology-graph-mode-all")).toHaveAttribute("aria-pressed", "true");
  await expect(metricCard).toHaveCSS("opacity", "1");
  await expect(playground.getByTestId("ontology-selection-details-panel")).toContainText(
    "グラフ上のノードや関係の線を選択"
  );
  await expect(playground.getByTestId("ontology-playground-clear")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-clear")).toBeDisabled();
  await expectNoHorizontalScroll(page);
});

test("選択候補一覧は標準の最大表示件数で縦スクロールする", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockApi(page, { ontologyGraph: nodePickerOverflowGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  const nodePicker = playground.getByTestId("ontology-inspector-node-picker");
  await expect(nodePicker).toBeVisible();
  const scrollRegion = nodePicker.getByTestId("ontology-inspector-node-picker-scroll-region");
  await expect(scrollRegion).toHaveAttribute("role", "region");
  await expect(scrollRegion).toHaveAttribute("aria-label", "選択候補");
  await expect(scrollRegion.getByRole("button")).toHaveCount(12);
  await expect(playground.getByTestId("ontology-inspector-node-picker-node-13")).toHaveCount(0);

  const metrics = await scrollRegion.evaluate((el) => ({
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
    overflowY: getComputedStyle(el).overflowY,
    maxHeightPx: Number.parseFloat(getComputedStyle(el).maxHeight),
    remPx: Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
  }));
  const expectedVisibleRows = testInfo.project.name === "mobile-375" ? 5 : 8;
  expect(metrics.overflowY).toBe("auto");
  expect(metrics.maxHeightPx).toBeCloseTo(expectedVisibleRows * 3.5 * metrics.remPx, 0);
  expect(metrics.clientHeight).toBeLessThanOrEqual(metrics.maxHeightPx + 1);
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  await scrollRegion.focus();
  await expect(scrollRegion).toBeFocused();
  await scrollRegion.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  expect(await scrollRegion.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  await expectNoHorizontalScroll(page);
});

test("公開済み Ontology がない場合は準備手順を表示する", async ({ page }) => {
  await mockApi(page, { ontologyGraph: { nodes: [], edges: [] } });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await expect(playground.getByText("公開済みオントロジーがまだありません")).toBeVisible();
  await expect(
    playground.getByText("AI 構築の Markdown 下書きを確認して公開すると、質問がどの業務モデルに接地するかをここで確認できます。")
  ).toBeVisible();
  await expect(playground.getByText("準備: AI 構築 → Markdown 下書き確認 → オントロジー公開")).toBeVisible();
  await expect(playground.getByTestId("ontology-playground-question")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("Ontology グラフはデスクトップとモバイルで主要ノード・凡例を表示し横スクロールしない", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();

  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await expect(playground.getByTestId("ontology-playground-inspector")).toBeVisible();
  await openGraphIfCollapsed(page, playground);
  await expect(playground.locator(".react-flow")).toBeVisible();
  await expect(playground.locator(".react-flow__node")).toHaveCount(3);
  await expect(playground.getByTestId("ontology-graph-lane-business")).toContainText("業務概念");
  await expect(playground.getByTestId("ontology-graph-mode-all")).toHaveAttribute("aria-pressed", "true");
  await expect(playground.getByTestId("ontology-graph-legend")).toBeVisible();
  await expect(playground.locator(".react-flow__node", { hasText: "顧客" })).toBeVisible();
  await expect(playground.locator(".react-flow__node", { hasText: "注文" })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("業務概念→物理表の対応エッジは縦ハンドルで自己ループしない + FK はカーディナリティ常時表示", async ({ page }) => {
  await mockApi(page, { ontologyGraph: employeeOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  // maps_to(業務概念→物理表)エッジは縦方向ハンドル(下→上)で接続される
  const mappingEdge = playground.locator(
    '.react-flow__edge[data-testid="rf__edge-employee-maps-to"]'
  );
  // 縦優勢ルーティング → smoothstep エッジとして描画される(SVG <g> は visibility 判定不可)
  await expect(mappingEdge).toHaveClass(/react-flow__edge-smoothstep/);
  const path = playground.locator(
    '[data-testid="rf__edge-employee-maps-to"] path.react-flow__edge-path'
  );
  const d = await path.getAttribute("d");
  // 始点と終点の座標: 縦接続なら y 差が x 差より大きい
  const points = [...(d ?? "").matchAll(/(-?\d+(?:\.\d+)?)[, ](-?\d+(?:\.\d+)?)/g)];
  expect(points.length).toBeGreaterThanOrEqual(2);
  const [startX, startY] = points[0]!.slice(1).map(Number);
  const [endX, endY] = points[points.length - 1]!.slice(1).map(Number);
  expect(Math.abs(endY - startY)).toBeGreaterThan(Math.abs(endX - startX));
});

test("FK エッジは hover なしでカーディナリティ短縮ラベルを常時表示する", async ({ page }) => {
  await mockApi(page, { ontologyGraph: erDetailOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  // many_to_one の FK は hover なしで "N:1" を表示する(ラベルはエッジ SVG 内の text)
  await expect(
    playground.locator('[data-testid="rf__edge-employee-department-fk"]')
  ).toContainText("N:1");
  // maps_to(対応)エッジは点線で FK と区別される
  const mappingPath = playground.locator(
    '[data-testid="rf__edge-employee-maps-to"] path.react-flow__edge-path'
  );
  await expect(mappingPath).toHaveAttribute("style", /dasharray/i);
});

const schemaContainsOntologyGraph = {
  nodes: [
    {
      id: "schema-admin",
      kind: "schema",
      business_name_ja: "ADMIN スキーマ",
      technical_name: "ADMIN",
      review_status: "approved",
      validation_status: "passed",
      metadata: { owner: "ADMIN" },
    },
    {
      id: "employee-table",
      kind: "table",
      business_name_ja: "従業員情報",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      validation_status: "passed",
      metadata: { owner: "ADMIN", object_name: "EMPLOYEE", object_type: "TABLE" },
    },
    {
      id: "department-table",
      kind: "table",
      business_name_ja: "部署情報",
      technical_name: "ADMIN.DEPARTMENT",
      review_status: "approved",
      validation_status: "passed",
      metadata: { owner: "ADMIN", object_name: "DEPARTMENT", object_type: "TABLE" },
    },
    {
      id: "employee-business",
      kind: "business_entity",
      business_name_ja: "従業員",
      technical_name: "ADMIN.EMPLOYEE",
      review_status: "approved",
      physical_mappings: [
        {
          object_ref: {
            node_id: "employee-table",
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            object_type: "table",
          },
        },
      ],
    },
    {
      id: "department-business",
      kind: "business_entity",
      business_name_ja: "部署",
      technical_name: "ADMIN.DEPARTMENT",
      review_status: "approved",
      physical_mappings: [
        {
          object_ref: {
            node_id: "department-table",
            owner: "ADMIN",
            object_name: "DEPARTMENT",
            object_type: "table",
          },
        },
      ],
    },
  ],
  edges: [
    {
      id: "contains-employee",
      kind: "contains",
      source_node_id: "schema-admin",
      target_node_id: "employee-table",
      relationship_name_ja: "含む",
      cardinality: "one_to_many",
      review_status: "approved",
    },
    {
      id: "contains-department",
      kind: "contains",
      source_node_id: "schema-admin",
      target_node_id: "department-table",
      relationship_name_ja: "含む",
      cardinality: "one_to_many",
      review_status: "approved",
    },
    {
      id: "belongs-to",
      kind: "business_relationship",
      source_node_id: "employee-business",
      target_node_id: "department-business",
      relationship_name_ja: "所属する",
      review_status: "approved",
    },
    {
      id: "manages",
      kind: "business_relationship",
      source_node_id: "employee-business",
      target_node_id: "department-business",
      relationship_name_ja: "管理される",
      review_status: "approved",
    },
  ],
};

test("schema→表エッジは縦 smoothstep で貫通せず、並行エッジは経路が分離される", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockApi(page, { ontologyGraph: schemaContainsOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  // schema ノードは表と同じ行ではなく専用上段行に置かれる
  const schemaBox = await playground
    .locator('.react-flow__node[data-id="schema-admin"]')
    .boundingBox();
  const tableBox = await playground
    .locator('.react-flow__node[data-id="employee-table"]')
    .boundingBox();
  expect(schemaBox).not.toBeNull();
  expect(tableBox).not.toBeNull();
  expect(schemaBox!.y + schemaBox!.height).toBeLessThanOrEqual(tableBox!.y + 1);

  // contains エッジは縦優先の smoothstep(横一列の bezier 貫通の再発防止)。
  // 経路全体が schema 下端〜表上端の余白帯内に収まる = 表ノードを貫通しない。
  const containsPaths: string[] = [];
  for (const edgeId of ["contains-employee", "contains-department"]) {
    const edge = playground.locator(`.react-flow__edge[data-testid="rf__edge-${edgeId}"]`);
    await expect(edge).toHaveClass(/react-flow__edge-smoothstep/);
    const d = await playground
      .locator(`[data-testid="rf__edge-${edgeId}"] path.react-flow__edge-path`)
      .getAttribute("d");
    const points = [...(d ?? "").matchAll(/(-?\d+(?:\.\d+)?)[, ](-?\d+(?:\.\d+)?)/g)];
    expect(points.length).toBeGreaterThanOrEqual(2);
    const startY = Number(points[0]![2]);
    const endY = Number(points[points.length - 1]![2]);
    expect(endY).toBeGreaterThan(startY);
    for (const point of points) {
      const y = Number(point[2]);
      expect(y).toBeGreaterThanOrEqual(startY - 1);
      expect(y).toBeLessThanOrEqual(endY + 1);
    }
    containsPaths.push(d ?? "");
  }
  // stepPosition の扇形分離で 2 本の経路は一致しない
  expect(containsPaths[0]).not.toEqual(containsPaths[1]);

  // 同一ノードペア間の並行(horizontal)エッジは専用エッジ型の弧で分離される
  const parallelPaths: string[] = [];
  for (const edgeId of ["belongs-to", "manages"]) {
    const edge = playground.locator(`.react-flow__edge[data-testid="rf__edge-${edgeId}"]`);
    await expect(edge).toHaveClass(/react-flow__edge-ontologyParallel/);
    parallelPaths.push(
      (await playground
        .locator(`[data-testid="rf__edge-${edgeId}"] path.react-flow__edge-path`)
        .getAttribute("d")) ?? ""
    );
  }
  expect(parallelPaths[0]).not.toEqual(parallelPaths[1]);
});

test("Join 条件を持たない関係カードは空枠ではなく物理対応名を表示する", async ({ page }) => {
  await mockApi(page, { ontologyGraph: schemaContainsOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();

  const containsCard = playground.getByTestId("ontology-inspector-relationship-contains-employee");
  await containsCard.scrollIntoViewIfNeeded();
  await expect(containsCard).toBeVisible();
  // contains は構造上 Join 条件を持たないので、物理対応名へ縮退する。
  await expect(containsCard).toContainText("物理対応");
  await expect(containsCard.getByTestId("ontology-inspector-relationship-detail")).toHaveText(
    "ADMIN.EMPLOYEE"
  );

  // ハイフンだけの空枠(旧フォールバック)が残っていないこと。
  const details = playground.getByTestId("ontology-inspector-relationship-detail");
  for (const text of await details.allInnerTexts()) {
    expect(text.trim()).not.toBe("-");
  }
});

test("ノードはドラッグで移動でき、リセットで決定論レイアウトに戻る", async ({ page }, testInfo) => {
  // タッチエミュレーション環境でのマウス合成ドラッグは React Flow のジェスチャ判定と
  // 相性が悪く不安定なため、ドラッグ検証はデスクトップのみで行う(タッチ操作は
  // React Flow がネイティブ対応)。
  test.skip(testInfo.project.name === "mobile-375", "ドラッグ検証はデスクトップのみ");
  await mockApi(page, { ontologyGraph: employeeOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  const node = playground.locator('.react-flow__node[data-id="employee-table"]');
  await expect(node).toBeVisible();
  // 初期 fitView(80ms 遅延 + 300ms アニメーション)が完全に終わってから座標を取る。
  // アニメーション中に boundingBox を取るとポインタ位置からノードがずれ、
  // ノードドラッグではなくキャンバスのパンになってしまう。
  await page.waitForTimeout(700);
  // カードの実測高さで物理表が画面下へ移る場合も、ドラッグ対象を表示して座標を取る。
  await node.scrollIntoViewIfNeeded();
  const initialTransform = await node.evaluate((el) => el.style.transform);
  const resetButton = playground.getByTestId("ontology-graph-reset-layout");
  await expect(resetButton).toBeDisabled();

  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  // hover 再レンダが落ち着いてから押す(直後の mousedown はパン判定になりうる)
  await page.waitForTimeout(120);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 120, box!.y + box!.height / 2 + 80, {
    steps: 8,
  });
  await page.mouse.up();

  // ドラッグ後は transform が変わり、自動 refit にスナップバックされない
  await expect.poll(() => node.evaluate((el) => el.style.transform)).not.toBe(initialTransform);
  await page.waitForTimeout(500);
  expect(await node.evaluate((el) => el.style.transform)).not.toBe(initialTransform);

  // ドラッグ後もクリック選択は生きている(aria-live で通知される)
  await node.click();
  await expect(playground.getByTestId("ontology-graph-selection-announcement")).toContainText(
    "選択中"
  );

  // リセットで決定論レイアウトの座標に戻る
  await expect(resetButton).toBeEnabled();
  await resetButton.click();
  await expect.poll(() => node.evaluate((el) => el.style.transform)).toBe(initialTransform);
});

test("ノードを hover してもエッジが 1 フレームも消えない(点滅回帰防止)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile-375", "hover 検証はマウス前提のためデスクトップのみ");
  await mockApi(page, { ontologyGraph: schemaContainsOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);
  const node = playground.locator('.react-flow__node[data-id="employee-table"]');
  await expect(node).toBeVisible();
  await page.waitForTimeout(700);

  // 毎フレームのエッジ数を記録(handleBounds リセットが起きると 1 フレーム 0 になる)
  await page.evaluate(() => {
    const w = window as unknown as { __edgeCounts: number[]; __raf: number };
    w.__edgeCounts = [];
    const tick = () => {
      w.__edgeCounts.push(document.querySelectorAll(".react-flow__edge").length);
      w.__raf = requestAnimationFrame(tick);
    };
    tick();
  });
  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  for (let i = 0; i < 3; i += 1) {
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2, { steps: 2 });
    await page.waitForTimeout(200);
    await page.mouse.move(box!.x - 60, box!.y - 60, { steps: 2 });
    await page.waitForTimeout(200);
  }
  const counts = await page.evaluate(() => {
    const w = window as unknown as { __edgeCounts: number[]; __raf: number };
    cancelAnimationFrame(w.__raf);
    return w.__edgeCounts;
  });
  expect(counts.length).toBeGreaterThan(10);
  expect(Math.min(...counts)).toBe(4);
  expect(Math.max(...counts)).toBe(4);
});

test("サーバ検索結果のヒット一覧は最大高さを超えると縦スクロールになる", async ({ page }, testInfo) => {
  const invalidCoordinates: string[] = [];
  page.on("console", (message) => { if (message.type() === "error" && /NaN|Infinity/.test(message.text())) invalidCoordinates.push(message.text()); });
  await mockApi(page, { ontologyGraph: employeeOntologyGraph });
  await page.route("**/api/nl2sql/profiles/*/ontology-context/search", (route) =>
    fulfillJson(route, {
      profile_id: "default",
      profile_view_id: "v",
      ontology_revision_id: "rev1",
      hits: Array.from({ length: 8 }, (_, index) => ({
        node: {
          id: `hit-${index + 1}`,
          kind: index < 2 ? "business_entity" : "column",
          business_name_ja: `ヒット概念 ${index + 1}`,
          review_status: "approved",
        },
        score: 1 - index * 0.08,
        matched_terms: ["従業員"],
        sources: ["lexical"],
        inference_source: "asserted",
      })),
      nodes: [],
      edges: [],
      mermaid: "",
      llm_markdown: "",
      context_hash: "hash",
    })
  );
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await playground.getByTestId("ontology-playground-question").fill("従業員");
  await playground.getByTestId("ontology-playground-server-search").click();

  const hits = playground.getByTestId("ontology-playground-server-hits");
  await expect(hits.locator("li")).toHaveCount(8);
  // 関係一覧と同じ標準: max-h-80(20rem) + overflow-y-auto でリスト内スクロール
  const metrics = await hits.evaluate((el) => ({
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
    overflowY: getComputedStyle(el).overflowY,
    maxHeightPx: Number.parseFloat(getComputedStyle(el).maxHeight),
    remPx: Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
  }));
  expect(metrics.overflowY).toBe("auto");
  // max-h-80 = 20rem(root font-size 14px なら 280px)
  expect(metrics.maxHeightPx).toBeCloseTo(20 * metrics.remPx, 0);
  expect(metrics.clientHeight).toBeLessThanOrEqual(metrics.maxHeightPx + 1);
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  await hits.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  expect(await hits.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  // 見出しとヒット件数バッジはスクロール外で常時見える
  await expect(
    playground.getByTestId("ontology-playground-server-result").getByText("8 件ヒット")
  ).toBeVisible();
  expect(invalidCoordinates).toEqual([]);
  const expand = playground.getByRole("button", { name: "グラフを表示", exact: true });
  if (await expand.isVisible()) {
    await expand.click();
    await expect(playground.locator(".react-flow__viewport")).toBeVisible();
    await playground.getByRole("button", { name: "グラフを隠す", exact: true }).click();
    await expand.click();
    await expect(playground.locator(".react-flow__viewport")).toBeVisible();
  }
  await expect(playground.locator(".react-flow__viewport")).not.toHaveAttribute("style", /NaN|Infinity/);
  await playground.getByTestId("ontology-playground-graph-region").screenshot({ path: testInfo.outputPath("ontology-visible-viewport.png") });
  expect(invalidCoordinates).toEqual([]);
});

const typedSemanticGraph = {
  nodes: [
    { id: "object", kind: "object_type", business_name_ja: "受注", technical_name: "Order" },
    { id: "property", kind: "property", business_name_ja: "状態", technical_name: "Order.status" },
    { id: "value-type", kind: "value_type", business_name_ja: "状態コード型", technical_name: "StatusCode" },
    { id: "enumeration", kind: "enumeration", business_name_ja: "受注状態", technical_name: "OrderStatus" },
    { id: "member", kind: "enum_value", business_name_ja: "受付済", technical_name: "OrderStatus:OPEN", metadata: { derived_from_definition_id: "enumeration" }, enum_value_definition: { code: "OPEN", label_ja: "受付済", data_type: "string", physical_literal: "OPEN", property_node_id: "property" } },
  ],
  edges: [
    { id: "domain", kind: "domain", source_node_id: "property", target_node_id: "object", relationship_name_ja: "所属オブジェクト", metadata: { semantic_predicate: "rdfs:domain" } },
    { id: "range", kind: "range", source_node_id: "property", target_node_id: "value-type", relationship_name_ja: "値型", metadata: { semantic_predicate: "rdfs:range" } },
    { id: "constraint", kind: "uses", source_node_id: "enumeration", target_node_id: "property", relationship_name_ja: "対象プロパティ" },
    { id: "membership", kind: "has_value", source_node_id: "enumeration", target_node_id: "member", relationship_name_ja: "列挙メンバー", metadata: { semantic_predicate: "skos:hasTopConcept" } },
  ],
};

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 1920]) {
    test(`型付き意味グラフの列挙展開とキーボード選択 ${theme} ${width}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop" && width !== 1280, "モバイルは375幅で確認");
      if (testInfo.project.name === "desktop") await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript((theme) => {
        localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({ state: { sidebarCollapsed: false, collapsedSections: {}, theme }, version: 0 }));
      }, theme);
      await mockApi(page, { ontologyGraph: typedSemanticGraph });
      await page.goto("/ontology-build?profile=default");
      await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
      await page.keyboard.press("Tab");
      await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
      await page.getByTestId("ontology-view-fetch").click();
      const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
      await openGraphIfCollapsed(page, playground);
      await expect(playground.locator('.react-flow__edge[data-id="domain"]')).toHaveCount(1);
      await expect(playground.locator('.react-flow__edge[data-id="range"]')).toHaveCount(1);
      await playground.getByTestId("ontology-graph-details-toggle").check();
      const filter = playground.getByRole("combobox", { name: "概念の種類", exact: true });
      await filter.selectOption("enumeration");
      await expect(playground.locator(".react-flow__node")).toHaveCount(2);
      await expect(playground.locator('.react-flow__edge[data-id="membership"]')).toHaveCount(1);
      const member = playground.locator('.react-flow__node[data-id="member"]');
      await member.focus();
      await page.keyboard.press("Enter");
      await expect(member).toBeFocused();
      await expect(playground.getByTestId("ontology-node-card-member")).toContainText("受付済");
      await expectNoHorizontalScroll(page);
      await playground.getByTestId("ontology-playground-graph-region").screenshot({ path: testInfo.outputPath(`typed-graph-${theme}-${width}.png`) });
    });
  }
}

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 1920]) {
    test(`公開版のない Profile に物理 graph を表示しない ${theme} ${width}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop" && width !== 1280, "モバイルは375幅で確認");
      if (testInfo.project.name === "desktop") await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript(theme => localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({state:{theme},version:0})), theme);
      await mockApi(page, { ontologyGraph: employeeOntologyGraph });
      let published = false;
      let wrongRevision = false;
      let fail = false;
      await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async route => {
        await new Promise(resolve => setTimeout(resolve, 300));
        if (fail) { await route.fulfill({status:500,contentType:"application/json",body:'{"detail":"Markdown unavailable"}'}); return; }
        await fulfillJson(route, {draft_markdown:"# 下書き",draft_revision:null,draft_etag:"draft",published_markdown:published ? "# 公開済み" : "",published_revision:published ? {id:wrongRevision ? "different" : "rev1",version:1,status:"published",etag:"re",schema_fingerprint:"fp"} : null});
      });
      const panel = page.locator("#ontology-query-playground-panel");
      await page.goto("/ontology-build?profile=default");
      await page.getByTestId("ontology-view-fetch").click();
      await expect(panel.getByText("公開済みオントロジーがまだありません", {exact:true})).toBeVisible();
      await expect(panel.locator(".react-flow")).toHaveCount(0);
      await expect(panel.getByTestId("ontology-playground-question")).toHaveCount(0);
      await expect(panel.getByTestId("ontology-playground-server-search")).toHaveCount(0);
      await panel.screenshot({path:testInfo.outputPath(`unpublished-${theme}-${width}.png`)});
      // API が公開版を返して初めて表示する。物理 graph 単体の取得では公開判定しない。
      published = true;
      await page.reload(); await page.getByTestId("ontology-view-fetch").click();
      await expect(panel.getByTestId("ontology-playground-question")).toBeVisible();
      wrongRevision = true;
      await page.reload(); await page.getByTestId("ontology-view-fetch").click();
      await expect(panel.getByText("公開版に対応するグラフを取得できませんでした。再試行してください。", {exact:true})).toBeVisible();
      await expect(panel.locator(".react-flow")).toHaveCount(0);
      wrongRevision = false;
      await panel.getByRole("button", {name:"再試行",exact:true}).click();
      await expect(panel.getByTestId("ontology-playground-question")).toBeVisible();
      fail = true;
      await page.reload(); await page.getByTestId("ontology-view-fetch").click();
      await expect(page.getByText("Markdown オントロジーを読み込めませんでした。", {exact:true})).toBeVisible();
      await expect(panel.locator(".react-flow")).toHaveCount(0);
      await expect(panel.getByTestId("ontology-playground-question")).toHaveCount(0);
    });
  }
}

test("公開済み Profile から未公開 Profile へ切り替えると graph と質問を残さない", async ({ page }) => {
  await mockApi(page);
  await page.route("**/api/nl2sql/profiles/search?*", route => fulfillJson(route, {
    items: ["default", "unpublished"].map(id => ({...profile,id,name:id,allowed_table_count:1,allowed_view_count:0,glossary_count:0,few_shot_count:0,version:1,etag:id,updated_at:"2026-07-12T00:00:00Z"})), next_cursor:null,total:2,change_token:1,
  }));
  await page.route("**/api/nl2sql/profiles/unpublished/ontology-markdown", route => fulfillJson(route, {draft_markdown:"# 別の下書き",draft_revision:null,draft_etag:"draft",published_markdown:"",published_revision:null}));
  await page.goto("/ontology-build?profile=default");
  await page.getByTestId("ontology-view-fetch").click();
  const panel = page.locator("#ontology-query-playground-panel");
  await panel.getByTestId("ontology-playground-question").fill("公開版の顧客");
  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "unpublished");
  await page.getByTestId("ontology-view-fetch").click();
  await expect(panel.getByText("公開済みオントロジーがまだありません", {exact:true})).toBeVisible();
  await expect(panel.locator(".react-flow")).toHaveCount(0);
  await expect(panel.getByTestId("ontology-playground-question")).toHaveCount(0);
  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "default");
  await page.getByTestId("ontology-view-fetch").click();
  await expect(panel.getByTestId("ontology-playground-question")).toHaveValue("");
});

test("ノード種別は共有バッジで長い日本語名を欠けずに表示する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockApi(page, { ontologyGraph: typedSemanticGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByTestId("ontology-view-fetch").click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await openGraphIfCollapsed(page, playground);
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      for (const [id, label] of [["object", "オブジェクト型（Object Type）"], ["property", "プロパティ（Property）"]]) {
        await playground.getByRole("combobox", { name: "概念の種類", exact: true }).selectOption(id === "object" ? "object_type" : "property");
        const card = playground.getByTestId(`ontology-node-card-${id}`);
        const badge = card.getByTestId("ontology-node-kind-label").locator("[data-status-variant]");
        await expect(badge).toHaveText(label);
        await expect(badge).toHaveAttribute("data-status-variant", "info");
        const geometry = await badge.evaluate(element => {
          const box = element.getBoundingClientRect();
          const card = element.closest("[data-ontology-node-kind]")!.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(element);
          const text = range.getBoundingClientRect();
          return {
            clipped: element.scrollWidth > element.clientWidth || element.scrollHeight > element.clientHeight,
            fits: text.left >= box.left && text.right <= box.right && text.top >= box.top && text.bottom <= box.bottom,
            inside: box.left >= card.left && box.right <= card.right && box.top >= card.top && box.bottom <= card.bottom,
            opacity: getComputedStyle(element.parentElement!).opacity,
          };
        });
        expect(geometry).toEqual({ clipped: false, fits: true, inside: true, opacity: "1" });
        await playground.getByTestId("ontology-playground-graph-region").screenshot({ path: testInfo.outputPath(`node-kind-${id}-${width}-${theme}.png`) });
      }
      await expectNoHorizontalScroll(page);
      await playground.getByTestId("ontology-playground-graph-region").screenshot({ path: testInfo.outputPath(`node-kind-badges-${width}-${theme}.png`) });
    }
  }
  await playground.getByRole("combobox", { name: "概念の種類", exact: true }).selectOption("object_type");
  const object = playground.locator('.react-flow__node[data-id="object"]');
  await object.focus();
  await page.keyboard.press("Enter");
  await expect(object).toBeFocused();
});

// 線（リンク型）の詳細は、ノードと同じ右（375px ではグラフの上）の詳細のパネルに出す（#1788）。
const linkTypeOntologyGraph = {
  nodes: [
    {
      id: "project",
      kind: "object_type",
      business_name_ja: "プロジェクト",
      technical_name: "ADB_APP_USER.PROJECT",
      review_status: "approved",
      validation_status: "passed",
      metadata: {
        definition: {
          kind: "object_type",
          api_name: "project",
          name_ja: "プロジェクト",
          primary_key: ["project_id"],
          lifecycle_ja: "",
          implements: [],
        },
      },
    },
    {
      id: "department",
      kind: "object_type",
      business_name_ja: "部門",
      technical_name: "ADB_APP_USER.DEPARTMENT",
      review_status: "approved",
      validation_status: "passed",
    },
  ],
  edges: [
    {
      id: "project-department",
      kind: "link_type",
      source_node_id: "project",
      target_node_id: "department",
      relationship_name_ja: "プロジェクト所属部門",
      cardinality: "many_to_one",
      review_status: "approved",
      validation_status: "passed",
      metadata: {
        definition: {
          kind: "link_type",
          api_name: "project_department",
          name_ja: "プロジェクト所属部門",
          source: "project",
          target: "department",
          inverse_name_ja: "部門所属プロジェクト",
          cardinality: "many_to_one",
          optional: false,
          join_expression_sql:
            "ADB_APP_USER.PROJECT.DEPARTMENT_ID = ADB_APP_USER.DEPARTMENT.DEPARTMENT_ID",
          intermediary_object: "",
          valid_time_ja: "",
          mappings: [],
          missing_information_ja: [],
          evidence: [
            {
              source_id: "schema",
              locator: "FK_PROJECT_DEPARTMENT",
              source_kind: "database",
              source_sha256: "",
              verified: true,
            },
          ],
        },
      },
    },
  ],
};

test("線をクリックすると関係の詳細をノードと同じ詳細のパネルに出し、グラフの上には出さない (#1788)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockApi(page, { ontologyGraph: linkTypeOntologyGraph });
  await page.goto("/ontology-build?profile=default");
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const playground = page.getByRole("region", { name: "質問のオントロジー接地確認用グラフ" });
  await playground.scrollIntoViewIfNeeded();
  await openGraphIfCollapsed(page, playground);

  const details = playground.getByTestId("ontology-selection-details-panel");
  await expect(details.getByRole("heading", { name: "詳細", exact: true })).toBeVisible();
  await expect(details).toContainText("グラフ上のノードや関係の線を選択");

  const edge = playground.locator('.react-flow__edge[data-id="project-department"]');
  await expect(edge).toHaveCount(1);
  await clickOntologyEdge(page, edge);

  await expect(details.getByRole("heading", { name: "関係の詳細", exact: true })).toBeVisible();
  const edgeDetails = details.getByTestId("ontology-edge-details");
  await expect(edgeDetails).toContainText("プロジェクト所属部門");
  await expect(edgeDetails).toContainText("リンク型（Link Type）");
  // 関係を理解するのに要る項目（結合のキー・起点・終点・多重度）を先に出す。
  const labels = await edgeDetails.locator("dl > div > dt").allTextContents();
  expect(labels.slice(0, 4)).toEqual([
    "結合のキー（Join Key）",
    "起点（Source）",
    "終点（Target）",
    "多重度（Cardinality）",
  ]);
  await expect(edgeDetails).toContainText(
    "ADB_APP_USER.PROJECT.DEPARTMENT_ID = ADB_APP_USER.DEPARTMENT.DEPARTMENT_ID"
  );
  await expect(edgeDetails).toContainText("N:1");
  await expect(edgeDetails).toContainText("部門所属プロジェクト");
  // 値の無い項目（未設定）は出さない。
  await expect(edgeDetails).not.toContainText("未設定");
  await expect(edgeDetails).not.toContainText("中間のオブジェクト型");
  await expect(edgeDetails).not.toContainText("有効期間");
  // グラフの上（質問の欄の下）には出さない。
  await expect(playground.getByText("逆向きの名前（Inverse Name）")).toHaveCount(1);
  await expect(playground.getByRole("region", { name: "リンク型（Link Type）" })).toHaveCount(0);

  // 詳細のパネルは、ノードの詳細と同じ所（desktop は右、375px はグラフの上）にある。
  const graphBox = await playground.getByTestId("ontology-playground-graph-region").boundingBox();
  const detailsBox = await details.boundingBox();
  expect(graphBox).not.toBeNull();
  expect(detailsBox).not.toBeNull();
  if ((page.viewportSize()?.width ?? 0) >= 1280) {
    expect(detailsBox!.x).toBeGreaterThanOrEqual(graphBox!.x + graphBox!.width);
  } else {
    expect(detailsBox!.y + detailsBox!.height).toBeLessThanOrEqual(graphBox!.y);
  }
  await expectNoHorizontalScroll(page);
  await details.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("edge-details.png") });

  // ノードのクリックは今どおりノードの詳細を出す。
  await playground.getByTestId("ontology-node-card-project").click();
  await expect(details.getByRole("heading", { name: "ノード詳細", exact: true })).toBeVisible();
  await expect(details.getByTestId("ontology-edge-details")).toHaveCount(0);
  await expect(details).toContainText("プロジェクト");
  await expect(details).toContainText("主キー（Primary Key）");
  await expect(details).not.toContainText("ライフサイクル");
  await expect(details).not.toContainText("未設定");
  await details.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("node-details.png") });
});
