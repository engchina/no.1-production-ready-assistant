import { expect, type Page } from "@playwright/test";

/**
 * サイドナビ（`<aside>`）を返す。md 未満（767px 以下）ではナビがドロワーのため（#367）、
 * 閉じていれば上端のバーの「メニュー」を押して開いてから返す。md 以上はそのまま返す。
 * ナビのリンクを押すとドロワーは閉じるので、続けて操作するときは再び呼ぶ。
 */
export async function openSidebarNav(page: Page) {
  const trigger = page.getByTestId("nav-drawer-trigger");
  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  // 画面の描画を待ってから、どちらの形かを見る（シェルが出る前に判定しない）。
  await expect(trigger.or(sidebar).first()).toBeVisible();
  if ((await trigger.isVisible()) && (await trigger.getAttribute("aria-expanded")) !== "true") {
    await trigger.click();
    await expect(page.getByTestId("nav-drawer")).toHaveAttribute("data-state", "open");
  }
  await expect(sidebar).toBeVisible();
  return sidebar;
}

/** md 未満でドロワーが開いていれば Escape で閉じる（離脱の確認をキャンセルしたあとなど）。 */
export async function closeSidebarNav(page: Page) {
  const trigger = page.getByTestId("nav-drawer-trigger");
  if ((await trigger.count()) > 0 && (await trigger.getAttribute("aria-expanded")) === "true") {
    await page.keyboard.press("Escape");
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
  }
}
