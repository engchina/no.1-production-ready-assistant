import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME } from "./_helpers";

// md 未満のナビのドロワー（#367。platform の AppShell）。desktop は従来のサイドバーのまま。

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }
    await route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } });
  });
}

const focusedInsideDrawer = (page: Page) =>
  page.evaluate(() => Boolean(document.activeElement?.closest('[data-testid="nav-drawer"]')));

test.describe("md 未満のナビのドロワー", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockApi(page);
    await page.goto("/settings/appearance");
  });

  test("閉じている間はサイドバーを出さず、本文が画面の全幅を使う", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
    await expect(page.getByTestId("nav-drawer-bar").getByText("RAG", { exact: true })).toBeVisible();
    const main = await page.locator("#pr-main").boundingBox();
    expect(main?.x).toBe(0);
    expect(main?.width).toBe(375);
  });

  test("開くと閉じるボタンへフォーカスし、Tab はドロワーの中で回り、Escape で閉じてボタンへ戻る", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "メニュー" });
    await expect(dialog).toBeVisible();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: "メニューを閉じる" })).toBeFocused();
    // 背面は inert（本文を操作・スクロールできない）
    await expect(page.locator("#pr-main")).toHaveAttribute("inert", "");

    // Shift+Tab で末尾へ回り、Tab で先頭へ戻る
    await page.keyboard.press("Shift+Tab");
    expect(await focusedInsideDrawer(page)).toBe(true);
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "メニューを閉じる" })).toBeFocused();
    // 何度 Tab を押してもドロワーの外へ出ない
    for (let index = 0; index < 40; index += 1) await page.keyboard.press("Tab");
    expect(await focusedInsideDrawer(page)).toBe(true);

    // Tab の回数で止まる位置はナビの項目数で変わる（#412 で「改善・運用」が増え、40 回でちょうど閉じるボタンへ戻るようになった）。
    // 閉じるボタンはアイコンだけのボタンで、キーボードのフォーカスで Tooltip が出て、1 回目の Escape は吹き出しだけを閉じる（#372）。
    // ここではナビのリンク（Tooltip なし）から Escape で閉じることを確かめる。閉じるボタンの場合は下のテストで確かめる。
    await page.getByRole("complementary", { name: "サイドナビゲーション" }).getByRole("link", { name: "外観と証明書" }).focus();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await expect(trigger).toBeFocused();
  });

  test("閉じるボタンの Tooltip が出ている間は、1 回目の Escape で吹き出しだけを閉じ、2 回目でドロワーを閉じる", async ({
    page,
    hasTouch,
  }) => {
    test.skip(hasTouch, "タッチ端末（pointer: coarse）では Tooltip を出さない（#372）");
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "メニュー" });
    const close = page.getByRole("button", { name: "メニューを閉じる" });
    await expect(close).toBeFocused();
    // キーボードで閉じるボタンへ戻ると Tooltip が出る（読み上げ名と同じ文言なので aria-hidden。role では引けない）
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
    const tooltip = page.locator('[role="tooltip"]', { hasText: "メニューを閉じる" });
    await expect(tooltip).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(tooltip).toHaveCount(0);
    await expect(dialog).toBeVisible();
    await expect(close).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("scrim のタップで閉じる", async ({ page }) => {
    await page.getByRole("button", { name: "メニュー", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "メニュー" })).toBeVisible();
    await page.getByTestId("nav-drawer-scrim").click({ position: { x: 360, y: 400 } });
    await expect(page.getByRole("dialog", { name: "メニュー" })).toBeHidden();
  });

  test("ナビのリンクを選ぶと移動して閉じ、フォーカスは画面遷移の規則どおり本文へ移る", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await trigger.click();
    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    // ドロワーの中は展開して描く（折りたたみボタンの代わりに閉じるボタン）
    await expect(sidebar).toHaveAttribute("data-state", "expanded");
    await expect(sidebar.getByRole("button", { name: "サイドバーを展開" })).toHaveCount(0);
    await sidebar.getByRole("link", { name: "モデル設定" }).click();
    await expect(page).toHaveURL(/\/settings\/models?$/);
    await expect(page.getByRole("dialog", { name: "メニュー" })).toBeHidden();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    // 画面を移ったときは製品の画面遷移のフォーカス（本文 #pr-main）に従う。背面の inert は外れている。
    await expect(page.locator("#pr-main")).toBeFocused();
    await expect(page.locator("#pr-main")).not.toHaveAttribute("inert", "");
  });

  test("今のページのリンクを選んでも閉じ、メニューボタンへフォーカスを戻す", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "メニュー", exact: true });
    await trigger.click();
    await page.getByRole("complementary", { name: "サイドナビゲーション" }).getByRole("link", { name: "外観と証明書" }).click();
    await expect(page.getByRole("dialog", { name: "メニュー" })).toBeHidden();
    await expect(page).toHaveURL(/\/settings\/appearance$/);
    await expect(trigger).toBeFocused();
  });

  test("prefers-reduced-motion では開閉を動かさない", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByRole("button", { name: "メニュー", exact: true }).click();
    const duration = await page
      .getByTestId("nav-drawer")
      .evaluate((element) => parseFloat(getComputedStyle(element).transitionDuration) || 0);
    expect(duration).toBeLessThan(0.02);
  });
});

test("desktop（1280px）は従来どおりサイドバーを本文の左に置き、メニューボタンを出さない", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockApi(page);
  await page.goto("/settings/appearance");
  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  await expect(sidebar).toBeVisible();
  await expect(page.getByTestId("nav-drawer-trigger")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const box = await sidebar.boundingBox();
  expect(box?.x).toBe(0);
  const main = await page.locator("#pr-main").boundingBox();
  expect(main?.x).toBeCloseTo(box!.width, 0);
});
