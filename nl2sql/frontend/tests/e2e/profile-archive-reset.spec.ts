import { FONT_SANS_PREFIX } from "../../../../platform/packages/ui/e2e/font-checks";
import {
  expectDeferredCjkFontFaces,
  expectLangFonts,
  expectLocalCjkFallbackFonts,
  expectLocalJapaneseFonts,
  expectLocalUiFonts,
  recordFirstRenderFontFaces,
} from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectCompactSortHeaders } from "./_helpers/sort-header";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

async function expectUnifiedConfirmDialogSurface(dialog: Locator) {
  await expect(dialog).toHaveClass(/max-w-md/);
  // ダイアログ面は共有トークン bg-surface-overlay(旧 bg-card)。
  await expect(dialog).toHaveClass(/\bbg-surface-overlay\b/);
  await expect(dialog).toHaveClass(/border-border/);
  await expect(dialog).not.toHaveClass(/border-l-danger/);
  await expect(dialog.locator(".border-l-danger")).toHaveCount(0);
  // 淡い danger 面は共有 ConfirmDialog のアイコンチップ（文字を持たない）だけに限る。
  for (const subtle of await dialog.locator(".bg-danger-subtle").all()) {
    await expect(subtle).toHaveText("");
  }
  await expect(dialog.getByRole("button", { name: "閉じる", exact: true })).toHaveCount(0);
}

async function expectProfileListNoHorizontalOverflow(page: Page) {
  const viewportWidth = page.viewportSize()?.width ?? 0;
  const metrics = await page.getByTestId("profile-management-list").evaluate((node) => {
    const list = node as HTMLElement;
    const grid = list.querySelector('[data-testid="profile-management-grid"]') as HTMLElement | null;
    const lastHeader = grid?.querySelector("thead th:last-child") as HTMLElement | null;
    const listRect = list.getBoundingClientRect();
    const gridRect = grid?.getBoundingClientRect();
    const lastHeaderRect = lastHeader?.getBoundingClientRect();
    const listStyle = window.getComputedStyle(list);
    return {
      overflowX: listStyle.overflowX,
      overflowY: listStyle.overflowY,
      listOffsetWidth: list.offsetWidth,
      listClientWidth: list.clientWidth,
      listScrollWidth: list.scrollWidth,
      listLeft: listRect.left,
      listRight: listRect.right,
      gridOffsetWidth: grid?.offsetWidth ?? 0,
      gridScrollWidth: grid?.scrollWidth ?? 0,
      gridLeft: gridRect?.left ?? 0,
      gridRight: gridRect?.right ?? 0,
      gridWidth: gridRect?.width ?? 0,
      lastHeaderRight: lastHeaderRect?.right ?? 0,
      pageHorizontal:
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1,
    };
  });
  expect(metrics.overflowX).toBe("hidden");
  expect(metrics.overflowY).toBe("auto");
  expect(metrics.pageHorizontal).toBe(false);
  expect(metrics.listScrollWidth).toBeLessThanOrEqual(metrics.listClientWidth + 1);
  expect(metrics.gridScrollWidth).toBeLessThanOrEqual(metrics.gridOffsetWidth + 1);
  expect(metrics.gridLeft).toBeGreaterThanOrEqual(metrics.listLeft - 1);
  expect(metrics.gridRight).toBeLessThanOrEqual(metrics.listRight + 1);
  // 名前セルの内容（名前・カテゴリ）が狭い幅でも許可表の列へはみ出さない（#535）。
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
  if (viewportWidth >= 768) {
    // wide 画面ではカードの中身も 100% を使う（#575）。一覧の表は幅を止めず、一覧の枠いっぱいに広げる。
    // 枠いっぱい = 枠線と、スクロールバーのために予約した幅（scrollbar-gutter: stable。#867）を除いた内幅。
    expect(Math.abs(metrics.gridLeft - metrics.listLeft)).toBeLessThanOrEqual(1);
    expect(metrics.gridWidth).toBeGreaterThanOrEqual(metrics.listClientWidth - 1);
    expect(metrics.lastHeaderRight).toBeLessThanOrEqual(metrics.listRight + 1);
  }
}

async function expectNoPageHorizontalOverflow(page: Page) {
  const hasPageHorizontalScroll = await page.evaluate(
    () =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
      document.body.scrollWidth > document.body.clientWidth + 1
  );
  expect(hasPageHorizontalScroll).toBe(false);
}

function profile(id: string, name: string, archived = false) {
  return {
    id,
    name,
    category: "経理",
    description: `${name} の説明`,
    allowed_tables: ["INVOICES"],
    allowed_views: [],
    glossary: {},
    sql_rules: ["SELECT のみ"],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    archived,
  };
}

async function mockProfileManagement(page: Page) {
  let deleteRequests = 0;
  const deleteIfMatchHeaders: string[] = [];
  let profiles = [
    profile("default", "標準プロファイル"),
    profile("accounting", "経理プロファイル"),
    profile("sales", "営業プロファイル"),
    profile("legacy", "旧プロファイル", true),
  ];

  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, {
      refreshed_at: "2026-07-11T00:00:00Z",
      tables: [
        {
          table_name: "INVOICES",
          logical_name: "請求",
          owner: "APP",
          table_type: "TABLE",
          comment: "",
          row_count: null,
          columns: [],
          constraints: [],
        },
      ],
    })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "catalog-v1",
      refreshed_at: "2026-07-11T00:00:00Z",
      object_count: 1,
      column_count: 0,
      change_token: 1,
      etag: "catalog-v1",
    })
  );
  await page.route("**/api/schema/objects?**", async (route) => {
    const objectType = new URL(route.request().url()).searchParams.get("type") ?? "";
    const items =
      objectType.toUpperCase() === "VIEW"
        ? []
        : [
            {
              owner: "APP",
              object_name: "INVOICES",
              object_type: "TABLE",
              logical_name: "請求",
              comment: "",
              row_count: null,
              column_count: 0,
              last_ddl_at: "2026-07-11T00:00:00Z",
            },
          ];
    await fulfillJson(route, {
      items,
      next_cursor: null,
      total: items.length,
      catalog_version: 1,
    });
  });
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [{ owner: "APP", is_current: true, table_count: 1, view_count: 0 }],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  // 編集画面マウント時の ontology 系 GET を高速 mock(未 mock だと dev proxy の
  // 失敗待ちが発生し、負荷時に flake の原因になる)
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) => fulfillJson(route, {}));
  await page.route("**/api/nl2sql/profiles/*/ontology-proposals", (route) =>
    fulfillJson(route, { proposals: [] })
  );
  await page.route("**/api/nl2sql/ontology/revisions", (route) =>
    fulfillJson(route, { revisions: [], active_revision_id: "" })
  );
  await page.route("**/api/nl2sql/profiles", async (route) => {
    if (route.request().method() === "GET") {
      await fulfillJson(route, profiles);
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/nl2sql/profiles/*", async (route) => {
    const url = new URL(route.request().url());
    const profileId = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    if (route.request().method() === "GET" && profileId === "search") {
      const items = profiles
        .filter((item) => !item.archived)
        .map((item) => ({
          id: item.id,
          name: item.name,
          category: item.category,
          description: item.description,
          archived: item.archived,
          allowed_table_count: item.allowed_tables.length,
          allowed_view_count: item.allowed_views.length,
          glossary_count: Object.keys(item.glossary).length,
          few_shot_count: item.few_shot_examples.length,
          version: 1,
          etag: `profile-${item.id}-v1`,
          updated_at: "2026-07-11T00:00:00Z",
        }));
      await fulfillJson(route, { items, next_cursor: null, total: items.length, change_token: 1 });
      return;
    }
    if (route.request().method() === "GET") {
      const target = profiles.find((item) => item.id === profileId);
      if (target) {
        await fulfillJson(route, { ...target, version: 1, etag: `profile-${target.id}-v1` });
      } else {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "指定された業務プロファイルが見つかりません。" }),
        });
      }
      return;
    }
    if (route.request().method() !== "DELETE") {
      await route.fallback();
      return;
    }
    deleteRequests += 1;
    deleteIfMatchHeaders.push(route.request().headers()["if-match"] ?? "");
    const target = profiles.find((item) => item.id === profileId);
    if (!target) {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "指定された業務プロファイルが見つかりません。" }),
      });
      return;
    }
    profiles = profiles.filter((item) => item.id !== profileId);
    await fulfillJson(route, { profile: target });
  });
  return {
    deleteRequests: () => deleteRequests,
    deleteIfMatchHeaders: () => [...deleteIfMatchHeaders],
  };
}

test("一覧の編集ボタンでエディタを開き、一覧に戻るで戻れる", { tag: "@responsive" }, async ({ page }) => {
  await mockProfileManagement(page);
  await page.goto("/profiles");

  const listPanel = page.locator("#profile-management-panel-list");

  await expect(page.getByRole("tab", { name: "一覧", exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "新規作成/編集" })).toHaveCount(0);
  await expect(page.getByTestId("fixed-split-pane-profile-management-list")).toHaveCount(0);
  await expect(listPanel).toHaveAttribute("data-management-id", "profile-management");
  await expect(listPanel.getByRole("heading", { name: "プロファイル" })).toBeVisible();
  await expect(listPanel.getByRole("heading", { name: /プロファイル編集/ })).toHaveCount(0);

  const salesRow = page.getByRole("row").filter({ hasText: "営業プロファイル" });
  await salesRow.locator("td").nth(1).click();
  await expect(page).toHaveURL(/\/profiles\?profile=sales$/);
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 営業プロファイル" })
  ).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("営業プロファイル");
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeVisible();

  const editorPanel = page.locator("#profile-management-panel-editor");
  const backButton = page.getByRole("button", { name: "一覧へ戻る", exact: true });
  await expect(editorPanel).toHaveAttribute("data-management-id", "profile-management");
  await expect(editorPanel.getByRole("button", { name: "一覧へ戻る", exact: true })).toHaveCount(0);
  const [backButtonBox, editorPanelBox] = await Promise.all([
    backButton.boundingBox(),
    editorPanel.boundingBox(),
  ]);
  expect(backButtonBox).not.toBeNull();
  expect(editorPanelBox).not.toBeNull();
  expect(backButtonBox!.y + backButtonBox!.height).toBeLessThanOrEqual(editorPanelBox!.y);
  const viewport = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth + 1);

  await backButton.click();
  await expect(page).not.toHaveURL(/profile=/);
  await expect(listPanel.getByRole("heading", { name: "プロファイル" })).toBeVisible();
  await page.getByRole("link", { name: /^経理プロファイル/ }).click();
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 経理プロファイル" })
  ).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("経理プロファイル");
});

test("プロファイル名のリンク（共有 RowTitleButton の href）は Tab で届き、Enter でエディタを開き、Ctrl / ⌘ + クリックで新しいタブに開ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  // #421: 一覧の行の題名のボタンを packages/ui の共有部品にそろえた（NL2SQL の手書きを置き換え）。
  // #583: URL（?profile=）で開くので、題名はリンクの形にして新しいタブでも開けるようにした。
  await mockProfileManagement(page);
  await page.goto("/profiles");

  const salesLink = page.getByRole("link", { name: "営業プロファイル を編集" });
  await expect(salesLink).toHaveAttribute("data-row-title-button", "");
  await expect(salesLink).toHaveAttribute("href", "/profiles?profile=sales");
  // 題名の色は 3 製品で共通の本文色（押せることは行の hover と下線で示す）。
  const titleColor = await salesLink.evaluate((node) => getComputedStyle(node.querySelector("span")!).color);
  const fgColor = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.className = "text-fg";
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });
  expect(titleColor).toBe(fgColor);

  await salesLink.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(salesLink).toBeFocused();
  await expect(salesLink).toHaveCSS("outline-style", "solid");
  await expectProfileListNoHorizontalOverflow(page);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/profiles\?profile=sales$/);
  await expect(page.getByRole("heading", { name: "プロファイル編集: 営業プロファイル" })).toBeVisible();

  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  const financeLink = page.getByRole("link", { name: /^経理プロファイル を編集/ });
  // 修飾キー付きのクリックはブラウザの既定（新しいタブ）に任せ、この画面では開かない。
  const [popup] = await Promise.all([
    page.context().waitForEvent("page"),
    financeLink.click({ modifiers: ["ControlOrMeta"] }),
  ]);
  await expect(popup).toHaveURL(/\/profiles\?profile=accounting$/);
  await popup.close();
  await expect(page).not.toHaveURL(/profile=/);
  await financeLink.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "プロファイル編集: 経理プロファイル" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`profile-row-title-${testInfo.project.name}.png`), fullPage: true });
});

test("URL 深リンクでエディタを直接開ける", async ({ page }) => {
  await mockProfileManagement(page);

  await page.goto("/profiles?profile=sales");
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 営業プロファイル" })
  ).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("営業プロファイル");

  await page.goto("/profiles?profile=new");
  await expect(page.getByRole("heading", { name: "新規プロファイル" })).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("");

  await page.goto("/profiles?profile=missing");
  await expect(page.getByText("指定された業務プロファイルが見つかりません。", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/profile=missing/);
  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  await expect(page.locator("#profile-management-panel-list")).toBeVisible();
  await expect(page).not.toHaveURL(/profile=/);
});

test("未保存の変更があるときは一覧に戻る前に確認する", async ({ page }) => {
  await mockProfileManagement(page);
  await page.goto("/profiles?profile=sales");
  await expect(page.getByLabel("名称")).toHaveValue("営業プロファイル");

  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(page.locator("#profile-management-panel-list")).toBeVisible();

  await page
    .getByRole("row")
    .filter({ hasText: "営業プロファイル" })
    .locator("td")
    .nth(0)
    .click();
  await page.getByLabel("名称").fill("営業プロファイル改");
  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: "変更を破棄しますか" });
  await expect(dialog.getByText("保存されていない変更があります。一覧へ戻ると破棄されます。")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page.getByLabel("名称")).toHaveValue("営業プロファイル改");

  await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
  await page
    .getByRole("alertdialog", { name: "変更を破棄しますか" })
    .getByRole("button", { name: "破棄して戻る", exact: true })
    .click();
  await expect(page.locator("#profile-management-panel-list")).toBeVisible();
  await expect(page).not.toHaveURL(/profile=/);
});

test("リセットとアーカイブ関連 UI は表示しない", async ({ page }) => {
  await mockProfileManagement(page);
  await page.goto("/profiles");

  const main = page.locator("main");
  await expect(main.getByRole("button", { name: "リセット", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: "アーカイブ", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: "使用中", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: "アーカイブ済み", exact: true })).toHaveCount(0);
  await expect(main.getByText("旧プロファイル")).toHaveCount(0);

  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await expect(page.getByRole("heading", { name: "新規プロファイル" })).toBeVisible();
  await expect(page.getByLabel("名称")).toHaveValue("");
  await expect(main.getByRole("button", { name: "保存", exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "リセット", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: "アーカイブ", exact: true })).toHaveCount(0);

  const viewport = await page.evaluate(() => ({ body: document.body.scrollWidth, window: window.innerWidth }));
  expect(viewport.body).toBeLessThanOrEqual(viewport.window);
});

test("ローカルフォントで全体と並べ替え列名を描画しキーボードで操作できる", { tag: "@responsive" }, async ({ page, context, baseURL }, testInfo) => {
  const origin = new URL(baseURL!).origin;
  const externalRequests: string[] = [];
  const fontResponses: { url: string; ok: boolean }[] = [];
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin !== origin) {
      externalRequests.push(route.request().url());
      await route.abort();
    } else {
      await route.fallback();
    }
  });
  page.on("response", (response) => {
    if (response.request().resourceType() === "font") {
      fontResponses.push({ url: response.url(), ok: response.ok() });
    }
  });
  await mockProfileManagement(page);
  await recordFirstRenderFontFaces(page);
  await page.goto("/profiles");
  const grid = page.getByTestId("profile-management-grid");
  await expect(grid).toBeVisible();
  // SC・TC・HK の @font-face は主の CSS に入れず、最初の描画の後に読む（#3305）。
  await expectDeferredCjkFontFaces(page);
  // check() だけでは未登録フォントの fallback を見逃すため、実際にロードした face も検証する。
  const fonts = await page.evaluate(async () => {
    const loaded = [];
    for (const family of ["Noto Sans JP Variable", "Roboto"]) {
      for (const weight of [400, 500, 600, 700]) {
        const faces = await document.fonts.load(
          `${weight} 14px "${family}"`,
          family === "Noto Sans JP Variable" ? "名称 許可表 許可ビュー" : "Production Ready NL2SQL"
        );
        loaded.push({ family, weight, count: faces.length, loaded: faces.every((face) => face.status === "loaded") });
      }
    }
    await document.fonts.ready;
    return loaded;
  });
  for (const font of fonts) {
    expect(font.count, `${font.family} ${font.weight}`).toBeGreaterThan(0);
    expect(font.loaded).toBe(true);
  }
  const bodyFont = await page.locator("body").evaluate((node) => getComputedStyle(node).fontFamily);
  expect(bodyFont.slice(0, FONT_SANS_PREFIX.length)).toBe(FONT_SANS_PREFIX);
  await expect(grid.locator("th[aria-sort] > button").first()).toHaveCSS("font-family", bodyFont);
  for (const count of await grid.locator("tbody td:nth-child(2), tbody td:nth-child(3)").all()) {
    await expect(count).toHaveCSS("font-family", bodyFont);
  }
  // Chromium が日本語列名に使った実フォントを確認（CSS 宣言だけの検証にしない）。
  const cdp = await context.newCDPSession(page);
  try {
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    const { root } = await cdp.send("DOM.getDocument");
    const { nodeId } = await cdp.send("DOM.querySelector", {
      nodeId: root.nodeId,
      selector: 'th[aria-sort] > button > span',
    });
    const { fonts: renderedFonts } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId });
    expect(renderedFonts.length).toBeGreaterThan(0);
    for (const font of renderedFonts) {
      // フォント内部名にはウェイト名が付く（例: Noto Sans JP Thin SemiBold）。
      expect(font.familyName).toMatch(/^Noto Sans JP(?: |$)/);
      expect(font.isCustomFont).toBe(true);
    }
  } finally {
    await cdp.detach();
  }
  // Noto Sans JP（可変書体）の各太さは本物の字形で描く（合成の太字にしない。#3333）。
  await expectLocalJapaneseFonts(page);
  // Noto Sans JP に無い字（合成の例）は Noto Sans SC → TC → HK で補う（#3292）。
  await expectLocalCjkFallbackFonts(page);
  // 内容の言語（lang）で共通の漢字の字形を切り替える（#3335）。
  await expectLangFonts(page);
  expect(fontResponses.some(({ url }) => url.includes("noto-sans-jp"))).toBe(true);
  for (const family of ["noto-sans-sc", "noto-sans-tc", "noto-sans-hk"]) {
    expect(fontResponses.some(({ url }) => url.includes(family)), family).toBe(true);
  }
  expect(fontResponses.some(({ url }) => url.includes("roboto"))).toBe(true);
  expect(fontResponses.every(({ url, ok }) => new URL(url).origin === origin && ok)).toBe(true);
  expect(externalRequests).toEqual([]);
  await expectCompactSortHeaders(grid);
  const nameHeader = grid.locator("th[aria-sort]").first();
  const nameSort = nameHeader.locator(":scope > button");
  await nameSort.focus();
  await expect(nameSort).toBeFocused();
  const before = await nameHeader.getAttribute("aria-sort");
  await nameSort.press("Enter");
  await expect(nameHeader).not.toHaveAttribute("aria-sort", before!);
  await expectProfileListNoHorizontalOverflow(page);
  await grid.locator("thead").screenshot({ path: testInfo.outputPath("profile-column-font.png") });
  await page.screenshot({ path: testInfo.outputPath("profile-local-fonts.png"), fullPage: true });
});

test("標準プロファイルも一覧と編集画面から確認付きで削除できる", { tag: "@responsive" }, async ({ page }) => {
  const api = await mockProfileManagement(page);
  await page.goto("/profiles");

  const grid = page.getByTestId("profile-management-grid");
  await expect(grid.getByRole("columnheader", { name: "操作", exact: true })).toHaveCount(0);
  await expect(grid.locator("thead th").last()).toContainText("許可ビュー");
  await expect(grid.getByRole("button", { name: /操作:/ })).toHaveCount(0);
  await expectProfileListNoHorizontalOverflow(page);

  const defaultRow = page.getByRole("row").filter({ hasText: "標準プロファイル" });
  await expect(defaultRow).toBeVisible();
  await expect(defaultRow.getByRole("button", { name: "編集", exact: true })).toHaveCount(0);
  await expect(defaultRow.getByRole("button", { name: "削除", exact: true })).toHaveCount(0);

  const salesRow = page.getByRole("row").filter({ hasText: "営業プロファイル" });
  await expect(salesRow.getByRole("button", { name: /操作:/ })).toHaveCount(0);

  await defaultRow.locator("td").nth(0).click();
  await expect(page).toHaveURL(/\/profiles\?profile=default$/);
  const editor = page.locator("#profile-management-panel-editor");
  await expect(
    editor.getByRole("heading", { name: "プロファイル編集: 標準プロファイル" })
  ).toBeVisible();
  await expect(editor.getByRole("button", { name: "保存", exact: true })).toBeVisible();
  const editorActions = editor.getByTestId("profile-editor-actions");
  const deleteMenuButton = editorActions.getByRole("button", { name: "その他の操作" });
  const deleteMenuChevron = deleteMenuButton.locator('svg[data-state]');
  await expect(deleteMenuButton).toBeVisible();
  await expect(deleteMenuChevron).toHaveAttribute("data-state", "collapsed");
  await expect(deleteMenuChevron).toHaveClass(/rotate-90/);
  await expect(editorActions.getByRole("button", { name: "削除", exact: true })).toHaveCount(0);

  await deleteMenuButton.click();
  await expect(deleteMenuChevron).toHaveAttribute("data-state", "expanded");
  await expect(deleteMenuChevron).toHaveClass(/rotate-0/);
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: "プロファイルを削除しますか" });
  await expect(dialog).toBeVisible();
  await expectUnifiedConfirmDialogSurface(dialog);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(deleteMenuButton).toBeVisible();
  await expect(deleteMenuChevron).toHaveAttribute("data-state", "collapsed");
  expect(api.deleteRequests()).toBe(0);

  await deleteMenuButton.click();
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  await page
    .getByRole("alertdialog", { name: "プロファイルを削除しますか" })
    .getByRole("button", { name: "削除", exact: true })
    .click();
  expect(api.deleteRequests()).toBe(1);
  expect(api.deleteIfMatchHeaders()).toEqual(['"profile-default-v1"']);
  await expect(page.getByText("「標準プロファイル」を削除しました。")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "標準プロファイル" })).toHaveCount(0);
  await expect(page.locator("#profile-management-panel-list")).toBeVisible();

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoPageHorizontalOverflow(page);
});

test("編集画面からプロファイルを確認付きで削除できる", { tag: "@responsive" }, async ({ page }) => {
  const api = await mockProfileManagement(page);
  await page.goto("/profiles");

  const grid = page.getByTestId("profile-management-grid");
  await expect(grid.getByRole("columnheader", { name: "操作", exact: true })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /操作:/ })).toHaveCount(0);
  await expectProfileListNoHorizontalOverflow(page);

  await page.getByRole("row").filter({ hasText: "営業プロファイル" }).locator("td").nth(0).click();
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 営業プロファイル" })
  ).toBeVisible();
  await page.getByTestId("profile-editor-actions").getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: "プロファイルを削除しますか" });
  await expectUnifiedConfirmDialogSurface(dialog);
  await expect(dialog.getByText("プロファイルを削除しますか")).toBeVisible();
  await expect(
    dialog.getByText(
      "「営業プロファイル」とそのすべてのオントロジー範囲設定を完全に削除します。監査履歴は削除されません。"
    )
  ).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
  expect(api.deleteRequests()).toBe(0);
  await expect(page.getByLabel("名称")).toHaveValue("営業プロファイル");

  await page.getByTestId("profile-editor-actions").getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  await page
    .getByRole("alertdialog", { name: "プロファイルを削除しますか" })
    .getByRole("button", { name: "削除", exact: true })
    .click();
  expect(api.deleteRequests()).toBe(1);
  await expect(page.getByText("「営業プロファイル」を削除しました。")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "営業プロファイル" })).toHaveCount(0);

  const accountingRow = page.getByRole("row").filter({ hasText: "経理プロファイル" });
  await accountingRow.locator("td").nth(0).click();
  await expect(
    page.getByRole("heading", { name: "プロファイル編集: 経理プロファイル" })
  ).toBeVisible();
  await page.getByTestId("profile-editor-actions").getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除", exact: true }).click();
  await page
    .getByRole("alertdialog", { name: "プロファイルを削除しますか" })
    .getByRole("button", { name: "削除", exact: true })
    .click();
  expect(api.deleteRequests()).toBe(2);
  await expect(
    page.locator("#profile-management-panel-list").getByRole("heading", { name: "プロファイル" })
  ).toBeVisible();
  await expect(page.getByText("「経理プロファイル」を削除しました。")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "経理プロファイル" })).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoPageHorizontalOverflow(page);
});

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});
