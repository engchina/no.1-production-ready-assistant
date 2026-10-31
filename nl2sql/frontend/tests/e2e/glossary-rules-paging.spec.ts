import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

/**
 * 用語・同義語 / 共通ルールの一覧（#1891）: 件数はツールバーの ListCount 1 か所、ページ送りは共通の advanced の
 * Pagination（表示件数 10 / 25 / 50 / 100・ページ番号の入力）で、ページと表示件数は作業状態に残す。
 */

function envelope(route: Route, data: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data, error_messages: [], warning_messages: [] }),
  });
}

const TERM_COUNT = 137;
const RULE_COUNT = 84;

async function mockMaterial(page: Page) {
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/legacy-learning-material", (route) =>
    envelope(route, {
      glossary: Object.fromEntries(
        Array.from({ length: TERM_COUNT }, (_, index) => [`用語${index + 1}`, `TABLE_${index + 1}.COLUMN`])
      ),
      rules: Array.from({ length: RULE_COUNT }, (_, index) => `ルール${index + 1}: 集計は確定済みの行だけを使う`),
    })
  );
}

const LISTS = [
  {
    path: "/glossary-rules",
    total: TERM_COUNT,
    toolbar: "glossary-terms-toolbar",
    pagination: "glossary-terms-pagination",
    rowNumber: "glossary-terms-row-number",
  },
  {
    path: "/global-rules",
    total: RULE_COUNT,
    toolbar: "global-rules-toolbar",
    pagination: "global-rules-pagination",
    rowNumber: "global-rules-row-number",
  },
] as const;

for (const list of LISTS) {
  test(`${list.path} は件数を ListCount に出し、ページ番号の入力・表示件数で移り、作業状態に残す`, async ({ page }) => {
    await mockMaterial(page);
    await page.goto(list.path);

    await expect(page.getByText(/見出しだけの Excel を取り込むと、すべて削除できます/)).toBeVisible();
    const toolbar = page.getByTestId(list.toolbar);
    const pagination = page.getByTestId(list.pagination);
    await expect(toolbar.getByRole("status")).toHaveText(`全 ${list.total} 件`);
    await expect(pagination).toContainText(`1–25 / ${list.total} 件`);
    await expect(page.getByTestId(list.rowNumber).first()).toHaveText("1");

    // ページ番号を入れて移る（行番号は全体の通し番号）。
    await jumpToPage(pagination, 3);
    await expect(pagination).toContainText(`3 / ${Math.ceil(list.total / 25)} ページ`);
    await expect(page.getByTestId(list.rowNumber).first()).toHaveText("51");

    // 再読込のあとも同じページに戻る（作業状態）。
    await page.reload();
    await expect(pagination).toContainText(`3 / ${Math.ceil(list.total / 25)} ページ`);
    await expect(page.getByTestId(list.rowNumber).first()).toHaveText("51");

    // 表示件数を変えると 1 ページ目へ戻る。
    const pageSize = pagination.getByRole("combobox", { name: "表示件数" });
    await pageSize.click();
    await page.getByRole("option", { name: "50 件" }).click();
    await expect(pagination).toContainText(`1–50 / ${list.total} 件`);
    await expect(pagination).toContainText(`1 / ${Math.ceil(list.total / 50)} ページ`);
    await expect(page.getByTestId(list.rowNumber)).toHaveCount(50);
    await jumpToPage(pagination, 2);
    await expect(page.getByTestId(list.rowNumber).first()).toHaveText("51");

    // 表示件数も作業状態に残る。
    await page.reload();
    await expect(pagination).toContainText(`51–${Math.min(100, list.total)} / ${list.total} 件`);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });
}

test("用語・同義語 / 共通ルールの一覧は 1280 / 1920 / 375pxで崩れない", { tag: "@responsive" }, async ({ page }) => {
  // 2 画面 × 幅を 1 つの test で見る（画面の移動が多いので時間を延ばす）。
  test.slow();
  await mockMaterial(page);
  const widths = test.info().project.name === "mobile-375" ? [375] : [1280, 1920];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    for (const list of LISTS) {
      await page.goto(list.path);
      await expect(page.getByTestId(list.toolbar).getByRole("status")).toHaveText(`全 ${list.total} 件`);
      await expect(page.getByTestId(list.pagination)).toBeVisible();
      const label = `${list.path} ${width}px`;
      expect(await measureHorizontalOverflow(page), label).toBeLessThanOrEqual(1);
      expect(await findClippedPlaceholders(page), label).toEqual([]);
      if (width >= 640) expect(await findFieldsOffWidthScale(page), label).toEqual([]);
    }
  }
});
