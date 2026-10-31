import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectListColumnFitsViewport, expectListColumnStaysFitWhileScrolling } from "./_helpers/list-detail-fit";
import { measureHorizontalOverflow } from "../../../../platform/packages/ui/e2e/layout-checks";

// データの管理の「テーブル・ビューデータの表示」の一覧 + 詳細（共有 ListDetailLayout。#2315）。
// xl 以上は「狭い一覧 | 広い詳細（データの結果）」、xl 未満は 1 ペイン（一覧 → 詳細 → 「一覧へ戻る」）。
// 選択は URL の ?object= が持ち、先頭を自動で選ばない。行を選ぶとその対象のデータを表示する。

const OBJECTS = Array.from({ length: 10 }, (_, index) => ({
  name: index < 8 ? `TABLE_${String(index + 1).padStart(2, "0")}` : `VIEW_${String(index - 7).padStart(2, "0")}`,
  owner: "APP",
  object_type: index < 8 ? "table" : "view",
  row_count: index < 8 ? (index + 1) * 10 : null,
  comment: index < 8 ? `合成のテーブル ${index + 1}` : `合成のビュー ${index - 7}`,
}));

// 列の多い結果（詳細の列の幅を使うことを確かめる）。
const RESULT_COLUMNS = Array.from({ length: 14 }, (_, index) => `COLUMN_${String(index + 1).padStart(2, "0")}`);

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });
}

async function mockDataManagement(page: Page) {
  const previewRequests: string[] = [];
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const type = url.searchParams.get("type") ?? "all";
    const items = OBJECTS.filter((item) => type === "all" || item.object_type === type);
    return fulfillJson(route, {
      runtime: "oracle",
      owner: "APP",
      items,
      total: items.length,
      table_count: items.filter((item) => item.object_type === "table").length,
      view_count: items.filter((item) => item.object_type === "view").length,
      next_cursor: null,
      refreshed_at: "2026-10-18T09:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/preview-data", (route) => {
    const body = route.request().postDataJSON() as { object_name: string; owner: string; limit: number };
    previewRequests.push(`${body.owner}.${body.object_name}`);
    return fulfillJson(route, {
      runtime: "oracle",
      sql: `SELECT * FROM "${body.owner}"."${body.object_name}" FETCH FIRST ${body.limit} ROWS ONLY`,
      results: {
        columns: RESULT_COLUMNS,
        rows: Array.from({ length: 12 }, (_, row) =>
          Object.fromEntries(RESULT_COLUMNS.map((column, col) => [column, `値 ${row + 1}-${col + 1}`]))
        ),
        total: 12,
      },
      warnings: [],
    });
  });
  return { previewRequests };
}

// 一覧の列に収まらない数の対象と続き（「さらに読み込む」）。一覧の列の高さ（#2330）を確かめる。
async function mockManyObjects(page: Page) {
  const items = Array.from({ length: 40 }, (_, index) => ({
    name: `TABLE_${String(index + 1).padStart(2, "0")}`,
    owner: "APP",
    object_type: "table",
    row_count: (index + 1) * 10,
    comment: `合成のテーブル ${index + 1}`,
  }));
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) =>
    fulfillJson(route, {
      runtime: "oracle",
      owner: "APP",
      items,
      total: 80,
      table_count: 80,
      view_count: 0,
      next_cursor: "cursor-2",
      refreshed_at: "2026-10-18T09:00:00.000Z",
      catalog_version: 1,
      warnings: [],
    })
  );
}

async function clearListDetailStorage(page: Page) {
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("list-detail-test-cleared")) return;
    window.localStorage.removeItem("production-ready-nl2sql.listDetail.data-management-preview");
    window.sessionStorage.setItem("list-detail-test-cleared", "true");
  });
}

function rowButton(page: Page, name: string) {
  return page.getByRole("button", { name: `APP.${name} を選択` });
}

test.describe("データの管理のデータの表示の一覧 + 詳細（ListDetailLayout。#2315）", () => {
  test("xl 以上は狭い一覧と広い詳細を並べ、先頭を自動で選ばず、選んだ対象のデータを詳細の全幅に出す", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
    await clearListDetailStorage(page);
    const { previewRequests } = await mockDataManagement(page);
    await page.goto("/data-management");

    const layout = page.getByTestId("list-detail-data-management-preview");
    const list = page.getByTestId("list-detail-data-management-preview-list");
    const detail = page.getByTestId("list-detail-data-management-preview-detail");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "split");
    await expect(rowButton(page, "TABLE_01")).toBeVisible();
    // 左右 1:1 の分割と、ステップの表示は持たない。
    await expect(page.getByTestId("fixed-split-pane-data-management-preview")).toHaveCount(0);
    await expect(page.getByTestId("data-preview-steps")).toHaveCount(0);
    // 先頭の行を自動で選ばない（データの取得を走らせない）。
    await expect(detail).toContainText("対象が未選択です");
    expect(previewRequests).toEqual([]);
    await expect(page).not.toHaveURL(/object=/);

    // 行は名前とコメントの 2 段と、行数・種類の短い値。
    await expect(rowButton(page, "TABLE_01")).toHaveAccessibleDescription("合成のテーブル 1");
    const viewRow = list.locator("tbody tr").filter({ has: rowButton(page, "VIEW_01") });
    await expect(viewRow).toContainText("ビュー");
    await expect(viewRow).toContainText("統計未取得");

    const [listBox, detailBox] = await Promise.all([list.boundingBox(), detail.boundingBox()]);
    // 一覧は既定 20rem（14px ルートで 280px）、詳細はその 2 倍より広い。
    expect(listBox!.width).toBeCloseTo(280, 0);
    expect(detailBox!.width).toBeGreaterThan(listBox!.width * 2);

    await rowButton(page, "TABLE_03").click();
    await expect(page).toHaveURL(/\?object=APP\.TABLE_03$/);
    await expect(rowButton(page, "TABLE_03")).toHaveAttribute("aria-current", "true");
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_03");
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("合成のテーブル 3");
    const results = page.getByTestId("query-results");
    await expect(results.getByTestId("query-results-summary")).toBeVisible();
    expect(previewRequests).toEqual(["APP.TABLE_03"]);
    // データの結果は詳細の列の全幅を使う（左右 1:1 の半分ではない）。
    const [resultsBox, detailAfter] = await Promise.all([results.boundingBox(), detail.boundingBox()]);
    expect(resultsBox!.width).toBeGreaterThan(detailAfter!.width - 64);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath("data-management-list-detail-1280.png"), fullPage: true });

    // 再読込で同じ対象が開き、データを取り直す。
    await page.reload();
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_03");
    await expect(page.getByTestId("query-results-summary")).toBeVisible();

    // 前へ / 次へ は一覧の今の並びの前後へ移り、そのデータを表示する。
    await page.getByTestId("data-management-preview-detail-pager-next").click();
    await expect(page).toHaveURL(/\?object=APP\.TABLE_04$/);
    await expect(rowButton(page, "TABLE_04")).toHaveAttribute("aria-current", "true");
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_04");

    // ほかのタブへ移ると URL から対象を外し、戻ると最後の選択を URL に戻す。
    await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
    await expect(page).not.toHaveURL(/object=/);
    await page.getByRole("tab", { name: "テーブル・ビューデータの表示" }).click();
    await expect(page).toHaveURL(/\?object=APP\.TABLE_04$/);
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_04");
  });

  // 旧 fixed-split-pane.spec.ts（左右 1:1 の分割線）の置き換え。分割線は一覧の幅だけを 16〜32rem で変え、
  // 長い値の結果でも詳細の列が一覧に重ならず、ページに横のはみ出しを出さない。
  test("分割線で一覧の幅を変えても 16〜32rem に収まり、長い値の結果が一覧に重ならない", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "分割線は xl 以上で確かめる");
    await page.setViewportSize({ width: 2048, height: 1000 });
    await clearListDetailStorage(page);
    await mockDataManagement(page);
    await page.goto("/data-management?object=APP.TABLE_01");
    await expect(page.getByTestId("query-results-table")).toBeVisible();
    const list = page.getByTestId("list-detail-data-management-preview-list");
    const detail = page.getByTestId("list-detail-data-management-preview-detail");
    const resizer = page.getByTestId("list-detail-data-management-preview-resizer");
    for (const deltaX of [-4_000, 8_000]) {
      const box = (await resizer.boundingBox())!;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + deltaX, box.y + box.height / 2, { steps: 8 });
      await page.mouse.up();
      const [listBox, detailBox] = await Promise.all([list.boundingBox(), detail.boundingBox()]);
      // 14px ルートで 16rem = 224px、32rem = 448px。
      expect(listBox!.width).toBeGreaterThanOrEqual(223);
      expect(listBox!.width).toBeLessThanOrEqual(449);
      expect(listBox!.x + listBox!.width).toBeLessThanOrEqual(detailBox!.x + 1);
      expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    }
  });

  test("1920px でも一覧は狭い列のままで、タイトルと本文の左端がそろい、横にあふれない", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "1920px は desktop で確かめる");
    await page.setViewportSize({ width: 1920, height: 1000 });
    await clearListDetailStorage(page);
    await mockDataManagement(page);
    await page.goto("/data-management?object=APP.TABLE_02");
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_02");
    await expect(page.getByTestId("query-results-summary")).toBeVisible();
    const list = await page.getByTestId("list-detail-data-management-preview-list").boundingBox();
    const title = await page.getByRole("heading", { level: 1, name: "データの管理" }).boundingBox();
    expect(list!.width).toBeCloseTo(280, 0);
    expect(Math.abs(list!.x - title!.x)).toBeLessThanOrEqual(1);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath("data-management-list-detail-1920.png"), fullPage: true });
  });

  for (const viewport of [
    { width: 1280, height: 900 },
    { width: 1920, height: 1000 },
  ]) {
    test(`${viewport.width}px: 一覧の列は画面の高さに収まり、詳細が短ければ本文は縦にスクロールしない（#2330）`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
      await page.setViewportSize(viewport);
      await clearListDetailStorage(page);
      await mockDataManagement(page);
      await mockManyObjects(page);
      await page.goto("/data-management");
      await expect(rowButton(page, "TABLE_01")).toBeVisible();
      await expect(page.getByTestId("list-detail-data-management-preview-detail")).toContainText("対象が未選択です");
      const fit = await expectListColumnFitsViewport(page, "data-management-preview");
      // 行は列の中でスクロールし、列の下の「さらに読み込む」は本文をスクロールせずに見える。
      expect(fit.bodyScrolls).toBe(true);
      await expect(page.getByRole("button", { name: "さらに読み込む" })).toBeInViewport();
      await page.screenshot({ path: testInfo.outputPath(`data-management-list-fit-${viewport.width}.png`) });
    });
  }

  test("詳細が長いと本文はスクロールし、一覧の列は見出しの下に貼り付いたまま高さを変えない（#2330）", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
    // 低い画面で、データの結果（12 行）が詳細の列を画面より高くする。
    await page.setViewportSize({ width: 1280, height: 640 });
    await clearListDetailStorage(page);
    await mockDataManagement(page);
    await mockManyObjects(page);
    await page.goto("/data-management?object=APP.TABLE_02");
    await expect(page.getByTestId("query-results-summary")).toBeVisible();
    await expectListColumnStaysFitWhileScrolling(page, "data-management-preview");
    await page.screenshot({ path: testInfo.outputPath("data-management-list-fit-scrolled.png") });
  });

  test("xl 未満は 1 ペイン: 選ぶと詳細だけ、「一覧へ戻る」で一覧に戻り選んだ行へフォーカスを戻す", { tag: "@responsive" }, async ({ page }, testInfo) => {
    if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 1024, height: 900 });
    await clearListDetailStorage(page);
    await mockDataManagement(page);
    await page.goto("/data-management");
    const layout = page.getByTestId("list-detail-data-management-preview");
    const list = page.getByTestId("list-detail-data-management-preview-list");
    const detail = page.getByTestId("list-detail-data-management-preview-detail");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(page.getByTestId("data-management-detail-back")).toHaveCount(0);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);

    await rowButton(page, "TABLE_05").click();
    await expect(page).toHaveURL(/\?object=APP\.TABLE_05$/);
    await expect(list).toBeHidden();
    await expect(detail).toBeVisible();
    await expect(detail).toBeFocused();
    await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.TABLE_05");
    await expect(page.getByTestId("query-results-summary")).toBeVisible();
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`data-management-list-detail-single-${testInfo.project.name}.png`), fullPage: true });

    const back = page.getByTestId("data-management-detail-back");
    await expect(back).toBeVisible();
    await back.click();
    await expect(page).not.toHaveURL(/object=/);
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(rowButton(page, "TABLE_05")).toBeFocused();

    // ブラウザの「戻る」でも一覧へ戻る（一覧から入るときは履歴に積む）。
    await rowButton(page, "TABLE_02").click();
    await expect(detail).toBeVisible();
    await page.goBack();
    await expect(list).toBeVisible();
    await expect(page).not.toHaveURL(/object=/);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });
});
