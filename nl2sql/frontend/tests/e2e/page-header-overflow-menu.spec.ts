import { expect, test, type Locator, type Page } from "./_helpers/test";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// PageHeader の「その他の操作」メニューが画面の外に切れないこと（#363）。
// 375px では操作が折り返して「その他の操作」が左端に来る。右端揃え固定だとメニューが左外に切れていた。

async function openFixture(page: Page) {
  await page.route("**/__header-overflow-menu", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: `<html data-theme="light" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/header-utilities.tsx"></script></body></html>`,
    })
  );
  await page.goto("/__header-overflow-menu");
}

async function box(locator: Locator) {
  const rect = await locator.boundingBox();
  expect(rect).not.toBeNull();
  return rect!;
}

async function expectInsideViewport(page: Page, locator: Locator) {
  const viewport = page.viewportSize()!;
  const rect = await box(locator);
  expect(rect.x).toBeGreaterThanOrEqual(0);
  expect(rect.y).toBeGreaterThanOrEqual(0);
  expect(rect.x + rect.width).toBeLessThanOrEqual(viewport.width);
  expect(rect.y + rect.height).toBeLessThanOrEqual(viewport.height);
}

test("375px: 折り返して左端に来た「その他の操作」は、メニューを左端揃えで画面内に開く", { tag: "@responsive" }, async ({ page, isMobile }) => {
  test.skip(!isMobile, "375px の project で検証する");
  await openFixture(page);
  const header = page.locator("header").first();
  const heading = header.getByRole("heading", { name: "テーブルの管理" });
  const trigger = header.getByTestId("page-actions-more");
  const primary = header.getByRole("button", { name: "テーブル作成" });

  // 操作がタイトルの下に折り返し、「その他の操作」が左寄りにある状態。
  const headingRect = await box(heading);
  const triggerRect = await box(trigger);
  expect(triggerRect.y).toBeGreaterThanOrEqual(headingRect.y + headingRect.height);
  expect(triggerRect.x).toBeLessThan(375 / 2);

  await trigger.click();
  const menu = page.getByRole("menu", { name: "その他の操作" });
  await expect(menu).toBeVisible();
  await expectInsideViewport(page, menu);
  await expect(menu).toHaveAttribute("data-floating-menu-align", "start");
  const items = menu.getByRole("menuitem");
  await expect(items).toHaveText(["Excel/CSV 取込(新規テーブル)", "最新情報を取得", "DB 構造を再取得"]);
  for (const item of await items.all()) await expectInsideViewport(page, item);

  // ボタンの左端にそろえ、ボタンの下に開く。
  const menuRect = await box(menu);
  expect(Math.abs(menuRect.x - triggerRect.x)).toBeLessThanOrEqual(1);
  expect(menuRect.y).toBeGreaterThanOrEqual(triggerRect.y + triggerRect.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);

  // キーボード操作（WAI-ARIA Menu Button）は従来どおり。
  await expect(items.first()).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(items.nth(1)).toBeFocused();
  await page.keyboard.press("End");
  await expect(items.last()).toBeFocused();
  await page.keyboard.press("Home");
  await expect(items.first()).toBeFocused();
  await expect(items.first()).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");

  // Tab はメニューを閉じ、ボタンの次（主操作）へ進む。Shift+Tab はボタンへ戻る。
  await page.keyboard.press("Enter");
  await expect(items.first()).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(menu).toHaveCount(0);
  await expect(primary).toBeFocused();
  await trigger.click();
  await expect(items.first()).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // 外側のクリックで閉じ、メニューの中のクリックでは閉じない（項目を押すと閉じる）。
  await trigger.click();
  await expect(menu).toBeVisible();
  await page.mouse.click(4, 800);
  await expect(menu).toHaveCount(0);
  await trigger.click();
  await items.nth(1).click();
  await expect(menu).toHaveCount(0);
  await expect(page.locator("output")).toHaveText("更新回数: 1");
});

test("右寄りの「その他の操作」は、従来どおりメニューの右端をボタンの右端にそろえる", { tag: "@responsive" }, async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop の project で検証する");
  // lg 未満（「その他の操作」を出す幅）で、操作が折り返さずヘッダーの右端にある状態。
  await page.setViewportSize({ width: 900, height: 900 });
  await openFixture(page);
  const header = page.locator("header").first();
  const trigger = header.getByTestId("page-actions-more");
  const heading = header.getByRole("heading", { name: "テーブルの管理" });
  const triggerRect = await box(trigger);
  expect(triggerRect.y).toBeLessThan((await box(heading)).y + (await box(heading)).height);
  expect(triggerRect.x).toBeGreaterThan(900 / 2);

  await trigger.click();
  const menu = page.getByRole("menu", { name: "その他の操作" });
  await expect(menu).toBeVisible();
  await expectInsideViewport(page, menu);
  await expect(menu).toHaveAttribute("data-floating-menu-align", "end");
  const menuRect = await box(menu);
  expect(Math.abs(menuRect.x + menuRect.width - (triggerRect.x + triggerRect.width))).toBeLessThanOrEqual(1);
  expect(menuRect.y).toBeGreaterThanOrEqual(triggerRect.y + triggerRect.height);
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});
