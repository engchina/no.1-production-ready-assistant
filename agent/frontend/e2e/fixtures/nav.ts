import { expect, type Locator, type Page } from "@playwright/test";

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
  // 画面は route ごとに読む（React.lazy。#818）。読み込みの間は React の transition で前の画面とナビ（開いたドロワー）が残り、
  // 読み終えてから閉じる。ナビの現在地が URL に追いつくまで待ってから、ドロワーの開閉を見る。
  await expect
    .poll(() =>
      page.evaluate(() => {
        const current = document.querySelector('aside a[aria-current="page"]');
        const href = current?.getAttribute("href");
        const path = window.location.pathname;
        // 子の画面で親の項目が現在地になる場合も、追いついたとみなす。
        return !href || href === path || path.startsWith(`${href}/`) || href.startsWith(`${path}/`);
      })
    )
    .toBe(true);
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

/**
 * サイドナビのセクションを開く。保存値が無いときは「AI 活用」と今のページのセクションだけが開いている（#1563）。
 * `titles` を渡すとその見出しのセクションだけ、省くと閉じているすべてのセクションを開く（開いていれば何もしない）。
 * 開閉は ui-store（localStorage）に保存されるので、同じテストの中の再読み込みでも開いたまま。
 */
export async function expandSidebarSections(sidebar: Locator, ...titles: string[]) {
  if (titles.length > 0) {
    for (const title of titles) {
      const toggle = sidebar.getByRole("button", { name: new RegExp(`^${title} を(展開|折りたたむ)`) });
      if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
    }
    return;
  }
  await expect(sidebar.locator('button[aria-controls^="nav-section-"]').first()).toBeVisible();
  const collapsed = sidebar.locator('button[aria-controls^="nav-section-"][aria-expanded="false"]');
  while ((await collapsed.count()) > 0) {
    await collapsed.first().click();
  }
}
