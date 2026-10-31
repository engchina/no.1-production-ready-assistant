import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";
import { expandSidebarSections } from "./fixtures/nav";

// md 未満のナビのドロワー（#367。platform の AppShell）。desktop は従来のサイドバーのまま。

const focusedInsideDrawer = (page: Page) =>
  page.evaluate(() => Boolean(document.activeElement?.closest('[data-testid="nav-drawer"]')));

test.describe("md 未満のナビのドロワー (mobile-375)", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/settings/appearance");
    await expect(page.getByRole("heading", { name: "外観と証明書", level: 1 })).toBeVisible();
  });

  test("閉じている間はサイドバーを出さず、上端のバーに「メニュー」と製品名を出す", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await expect(trigger).toHaveAttribute("aria-controls", /.+/);
    await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
    await expect(page.getByTestId("nav-drawer-bar").getByText("Agent", { exact: true })).toBeVisible();
    const main = await page.locator("#pr-main").boundingBox();
    expect(main?.width).toBe(375);
  });

  test("開閉・フォーカスの閉じ込め・Escape・scrim", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "メニュー" });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("button", { name: "メニューを閉じる" })).toBeFocused();
    for (let index = 0; index < 30; index += 1) await page.keyboard.press("Tab");
    expect(await focusedInsideDrawer(page)).toBe(true);
    for (let index = 0; index < 5; index += 1) await page.keyboard.press("Shift+Tab");
    expect(await focusedInsideDrawer(page)).toBe(true);

    // Tab の回数で止まる位置はナビの項目数で変わる。閉じるボタン（アイコンだけ）に止まると Tooltip が出て、
    // 1 回目の Escape は吹き出しだけを閉じる（#372）。項目数に左右されないよう、ナビのリンクから閉じる（#426）。
    // 監査ログは既定で閉じている「改善・運用」にある（#1563）。
    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    await expandSidebarSections(sidebar, "改善・運用");
    await sidebar.getByRole("link", { name: "監査ログ", exact: true }).focus();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();

    await trigger.click();
    await page.getByTestId("nav-drawer-scrim").click({ position: { x: 360, y: 400 } });
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("ナビの選択で移動して閉じる", async ({ page }) => {
    await page.getByRole("button", { name: "メニュー", exact: true }).click();
    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    await expandSidebarSections(sidebar, "改善・運用");
    await sidebar.getByRole("link", { name: "監査ログ", exact: true }).click();
    await expect(page).toHaveURL(/\/audit$/);
    await expect(page.getByRole("dialog", { name: "メニュー" })).toBeHidden();
    await expect(page.locator("#pr-main")).not.toHaveAttribute("inert", "");
  });
});

test("desktop（1280px）は従来どおりサイドバーを出し、メニューボタンを出さない", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/settings/appearance");
  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await expect(sidebar).toBeVisible();
  await expect(page.getByTestId("nav-drawer-trigger")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /サイドバーを(展開|折りたたむ)/ })).toBeVisible();
});
