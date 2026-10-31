import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectListColumnFitsViewport, expectListColumnStaysFitWhileScrolling } from "./_helpers/list-detail-fit";
import { measureHorizontalOverflow } from "../../../../platform/packages/ui/e2e/layout-checks";

// テーブルの管理・ビューの管理の一覧 + 詳細（共有 ListDetailLayout。#2281）。
// xl 以上は「狭い一覧 | 広い詳細」、xl 未満は 1 ペイン（一覧 → 詳細 → 「一覧へ戻る」）。選択は URL が持ち、先頭を自動で選ばない。

const TABLES = Array.from({ length: 12 }, (_, index) => ({
  name: `TABLE_${String(index + 1).padStart(2, "0")}`,
  owner: "APP",
  qualified_name: `APP.TABLE_${String(index + 1).padStart(2, "0")}`,
  object_type: "table",
  row_count: (index + 1) * 10,
  comment: `合成のテーブル ${index + 1}`,
}));

const COLUMN_TYPES = ["NUMBER", "VARCHAR2(200)", "DATE", "TIMESTAMP(6)", "VARCHAR2(4000)", "NUMBER(10,2)"];

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });
}

async function mockTableManagement(page: Page, options: { tableCount?: number; columnCount?: number } = {}) {
  const tables = options.tableCount
    ? Array.from({ length: options.tableCount }, (_, index) => ({
        ...TABLES[0],
        name: `TABLE_${String(index + 1).padStart(2, "0")}`,
        qualified_name: `APP.TABLE_${String(index + 1).padStart(2, "0")}`,
        row_count: (index + 1) * 10,
        comment: `合成のテーブル ${index + 1}`,
      }))
    : TABLES;
  const columnTypes = options.columnCount
    ? Array.from({ length: options.columnCount }, (_, index) => COLUMN_TYPES[index % COLUMN_TYPES.length])
    : COLUMN_TYPES;
  const detailRequests: string[] = [];
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/synthetic-data/runs", (route) => fulfillJson(route, []));
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "oracle",
      owner: "APP",
      items: tables,
      total: tables.length,
      table_count: tables.length,
      view_count: 0,
      next_cursor: null,
      refreshed_at: "2026-10-18T09:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/tables/*", (route) => {
    const url = new URL(route.request().url());
    const name = decodeURIComponent(url.pathname.split("/").pop() ?? "");
    detailRequests.push(name);
    const table = tables.find((item) => item.name === name) ?? tables[0];
    return fulfillJson(route, {
      ...table,
      columns: columnTypes.map((dataType, index) => ({
        column_name: `COLUMN_${index + 1}`,
        logical_name: `列 ${index + 1}`,
        data_type: dataType,
        nullable: index % 2 === 0,
        comment: `合成の列のコメント ${index + 1}`,
        sample_values: [`値 ${index + 1}-a`, `値 ${index + 1}-b`],
      })),
      ddl: `CREATE TABLE "APP"."${table.name}" ("COLUMN_1" NUMBER)`,
      warnings: [],
    });
  });
  return { detailRequests };
}

async function clearListDetailStorage(page: Page) {
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("list-detail-test-cleared")) return;
    window.localStorage.removeItem("production-ready-nl2sql.listDetail.table-management");
    window.sessionStorage.setItem("list-detail-test-cleared", "true");
  });
}

function rowButton(page: Page, name: string) {
  return page.getByRole("button", { name: `APP.${name} を表示` });
}

test.describe("テーブルの管理の一覧 + 詳細（ListDetailLayout。#2281）", () => {
  test("xl 以上は狭い一覧と広い詳細を並べ、先頭を自動で選ばず、選択を URL に持つ", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
    await clearListDetailStorage(page);
    const { detailRequests } = await mockTableManagement(page);
    await page.goto("/table-management");

    const layout = page.getByTestId("list-detail-table-management");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "split");
    await expect(rowButton(page, "TABLE_01")).toBeVisible();
    // 先頭の行を自動で選ばない（詳細の取得を走らせない）。
    await expect(page.getByTestId("list-detail-table-management-detail")).toContainText("オブジェクトが未選択です");
    expect(detailRequests).toEqual([]);
    await expect(page).not.toHaveURL(/table=/);

    const list = page.getByTestId("list-detail-table-management-list");
    const detail = page.getByTestId("list-detail-table-management-detail");
    const [listBox, detailBox] = await Promise.all([list.boundingBox(), detail.boundingBox()]);
    // 一覧は既定 20rem（14px ルートで 280px）、詳細はその 2 倍より広い。
    expect(listBox!.width).toBeCloseTo(280, 0);
    expect(detailBox!.width).toBeGreaterThan(listBox!.width * 2);

    await rowButton(page, "TABLE_03").click();
    await expect(page).toHaveURL(/\?table=APP\.TABLE_03$/);
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_03");
    await expect(rowButton(page, "TABLE_03")).toHaveAttribute("aria-current", "true");
    // 列情報の 6 列が 1280px の詳細の列に横スクロールなしで収まる。
    const columns = page.getByTestId("db-admin-detail-columns");
    await expect(columns.getByRole("rowheader", { name: "COLUMN_6" })).toBeVisible();
    const overflow = await columns.evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);

    // 再読込・戻る / 進むで同じ対象に戻る（2 ペインの選び直しは replace なので、戻るはこの画面の前へ）。
    await page.reload();
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_03");
    await rowButton(page, "TABLE_05").click();
    await expect(page).toHaveURL(/\?table=APP\.TABLE_05$/);
    await expect(rowButton(page, "TABLE_05")).toHaveAttribute("aria-current", "true");

    // 前へ / 次へ は一覧の今の並びの前後へ移る。
    await page.getByTestId("table-management-detail-pager-next").click();
    await expect(page).toHaveURL(/\?table=APP\.TABLE_06$/);
    await expect(rowButton(page, "TABLE_06")).toHaveAttribute("aria-current", "true");
    await page.getByTestId("table-management-detail-pager-previous").click();
    await expect(page).toHaveURL(/\?table=APP\.TABLE_05$/);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });

  for (const viewport of [
    { width: 1280, height: 900 },
    { width: 1920, height: 1000 },
  ]) {
    test(`${viewport.width}px: 一覧の列は画面の高さに収まり、詳細が短ければ本文は縦にスクロールしない（#2330）`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
      await page.setViewportSize(viewport);
      await clearListDetailStorage(page);
      await mockTableManagement(page, { tableCount: 40 });
      await page.goto("/table-management");
      await expect(rowButton(page, "TABLE_01")).toBeVisible();
      await expect(page.getByTestId("list-detail-table-management-detail")).toContainText("オブジェクトが未選択です");
      const fit = await expectListColumnFitsViewport(page, "table-management");
      expect(fit.bodyScrolls).toBe(true);
      // 一覧の中を最後までスクロールしても、本文は動かない。
      await page.getByTestId("list-detail-table-management-list").evaluate((column) => {
        column.querySelectorAll<HTMLElement>("*").forEach((node) => {
          if (node.scrollHeight > node.clientHeight + 1) node.scrollTop = node.scrollHeight;
        });
      });
      await expect(rowButton(page, "TABLE_40")).toBeInViewport();
      await expectListColumnFitsViewport(page, "table-management");
      await page.screenshot({ path: testInfo.outputPath(`table-management-list-fit-${viewport.width}.png`) });
    });
  }

  test("詳細が長いと本文はスクロールし、一覧の列は見出しの下に貼り付いたまま高さを変えない（#2330）", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
    await clearListDetailStorage(page);
    // 列の多いテーブル（列情報はページと一緒にスクロールする）。
    await mockTableManagement(page, { tableCount: 40, columnCount: 60 });
    await page.goto("/table-management?table=APP.TABLE_03");
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_03");
    await expect(page.getByTestId("db-admin-detail-columns").getByRole("rowheader", { name: "COLUMN_60" })).toBeAttached();
    await expectListColumnStaysFitWhileScrolling(page, "table-management");
    await page.screenshot({ path: testInfo.outputPath("table-management-list-fit-scrolled.png") });
  });

  test("↑ / ↓ で一覧の行の間を移り、Enter で選ぶ", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "キーボードの移動は desktop で確かめる");
    await clearListDetailStorage(page);
    await mockTableManagement(page);
    await page.goto("/table-management");
    await rowButton(page, "TABLE_01").focus();
    await page.keyboard.press("ArrowDown");
    await expect(rowButton(page, "TABLE_02")).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(rowButton(page, "TABLE_03")).toBeFocused();
    await page.keyboard.press("ArrowUp");
    await expect(rowButton(page, "TABLE_02")).toBeFocused();
    await page.keyboard.press("End");
    await expect(rowButton(page, "TABLE_12")).toBeFocused();
    await page.keyboard.press("Home");
    await expect(rowButton(page, "TABLE_01")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?table=APP\.TABLE_01$/);
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_01");
  });

  test("一覧の幅を分割線で変え、開閉ボタンで畳むと詳細が全幅になり、どちらも再読込の後に残る", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "幅の変更と開閉は xl 以上で確かめる");
    await clearListDetailStorage(page);
    await mockTableManagement(page);
    await page.goto("/table-management?table=APP.TABLE_02");
    const layout = page.getByTestId("list-detail-table-management");
    const list = page.getByTestId("list-detail-table-management-list");
    const resizer = page.getByTestId("list-detail-table-management-resizer");
    await expect(resizer).toHaveAttribute("role", "separator");
    await expect(resizer).toHaveAttribute("aria-valuenow", "20");
    await resizer.focus();
    await page.keyboard.press("Shift+ArrowRight");
    await expect(resizer).toHaveAttribute("aria-valuenow", "24");
    await expect.poll(async () => Math.round((await list.boundingBox())!.width)).toBe(336);
    await page.reload();
    await expect(resizer).toHaveAttribute("aria-valuenow", "24");
    await page.keyboard.press("Tab");
    await resizer.press("Home");
    await expect(resizer).toHaveAttribute("aria-valuenow", "20");

    const toggle = page.getByTestId("list-detail-table-management-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(toggle).toHaveAccessibleName("テーブル一覧");
    await toggle.click();
    await expect(layout).toHaveAttribute("data-list-collapsed", "true");
    await expect(list).toBeHidden();
    const layoutBox = await layout.boundingBox();
    const detailBox = await page.getByTestId("list-detail-table-management-detail").boundingBox();
    expect(Math.abs(layoutBox!.width - detailBox!.width)).toBeLessThanOrEqual(1);
    await page.reload();
    await expect(layout).toHaveAttribute("data-list-collapsed", "true");
    await toggle.click();
    await expect(list).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  test("1920px でも一覧は狭い列のままで、タイトルと本文の左端がそろい、横にあふれない", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "1920px は desktop で確かめる");
    await page.setViewportSize({ width: 1920, height: 1000 });
    await clearListDetailStorage(page);
    await mockTableManagement(page);
    await page.goto("/table-management?table=APP.TABLE_01");
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_01");
    const list = await page.getByTestId("list-detail-table-management-list").boundingBox();
    const title = await page.getByRole("heading", { level: 1, name: "テーブルの管理" }).boundingBox();
    expect(list!.width).toBeCloseTo(280, 0);
    expect(Math.abs(list!.x - title!.x)).toBeLessThanOrEqual(1);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });

  test("xl 未満は 1 ペイン: 選ぶと詳細だけ、「一覧へ戻る」で一覧に戻り選んだ行へフォーカスを戻す", { tag: "@responsive" }, async ({ page }, testInfo) => {
    if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 1024, height: 900 });
    await clearListDetailStorage(page);
    await mockTableManagement(page);
    await page.goto("/table-management");
    const layout = page.getByTestId("list-detail-table-management");
    const list = page.getByTestId("list-detail-table-management-list");
    const detail = page.getByTestId("list-detail-table-management-detail");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(page.getByTestId("list-detail-table-management-toggle")).toHaveCount(0);
    await expect(page.getByTestId("list-detail-table-management-resizer")).toHaveCount(0);
    await expect(page.getByTestId("table-management-detail-back")).toHaveCount(0);

    await rowButton(page, "TABLE_04").click();
    await expect(page).toHaveURL(/\?table=APP\.TABLE_04$/);
    await expect(list).toBeHidden();
    await expect(detail).toBeVisible();
    await expect(detail).toBeFocused();
    await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.TABLE_04");
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);

    const back = page.getByTestId("table-management-detail-back");
    await expect(back).toBeVisible();
    await back.click();
    await expect(page).not.toHaveURL(/table=/);
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(rowButton(page, "TABLE_04")).toBeFocused();

    // ブラウザの「戻る」でも一覧へ戻る（一覧から入るときは履歴に積む）。
    await rowButton(page, "TABLE_02").click();
    await expect(detail).toBeVisible();
    await page.goBack();
    await expect(list).toBeVisible();
    await expect(page).not.toHaveURL(/table=/);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });
});
