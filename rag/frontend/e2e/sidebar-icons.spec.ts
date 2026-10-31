import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME, expandSidebarSections, openSidebarNav } from "./_helpers";

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }

    await route.fulfill({
      json: { data: null, error_messages: [], warning_messages: [] },
    });
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 720 },
  { name: "mobile", width: 375, height: 667 },
]) {
  test(`settings sidebar icons are semantic on ${viewport.name}`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockApi(page);
    await page.goto("/settings/oci");

    const sidebar = await openSidebarNav(page);
    const ociSettings = sidebar.getByRole("link", { name: "OCI 認証設定" });
    const modelSettings = sidebar.getByRole("link", { name: "モデル設定" });

    await expect(ociSettings).toBeVisible();
    await expect(modelSettings).toBeVisible();
    await expect(ociSettings.locator("svg").first()).toHaveClass(/lucide-key-round/);
    // システム設定の共通5項目のアイコンは共有パッケージが決める（NL2SQL と同じ。#116）。
    await expect(modelSettings.locator("svg").first()).toHaveClass(/lucide-brain-cog/);
  });

  test(`sidebar brand and menu labels are stable on ${viewport.name}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockApi(page);
    await page.goto("/settings/oci");

    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    const brand = sidebar.locator('[title="Private RAG"]').first();
    const uploadLink = sidebar.getByRole("link", { name: "文書アップロード" });

    if (viewport.width < 768) {
      // md 未満はドロワー（#367）。上端のバーに製品名を出し、開いたドロワーは展開して描く（折りたたみボタンは無い）。
      const banner = page.getByTestId("nav-drawer-bar").locator('[title="Private RAG"]');
      await expect(banner.getByText("Private", { exact: true })).toBeVisible();
      await expect(banner.getByText("RAG", { exact: true })).toBeVisible();
      await openSidebarNav(page);
      // 文書アップロードは既定で閉じている「ナレッジ構築」にある（#1563）。
      await expandSidebarSections(sidebar, "ナレッジ構築");
      await expect(brand.getByText("Private", { exact: true })).toBeVisible();
      await expect(uploadLink.getByText("文書アップロード", { exact: true })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("document-upload-menu.png") });
      await expect(page.getByRole("button", { name: "サイドバーを展開" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "サイドバーを折りたたむ" })).toHaveCount(0);
      return;
    }

    await expect(brand).toBeVisible();
    await expect(brand.getByText("Private", { exact: true })).toBeVisible();
    await expect(brand.getByText("RAG", { exact: true })).toBeVisible();
    await expandSidebarSections(sidebar, "ナレッジ構築");
    await expect(uploadLink).toHaveAttribute("href", /\/upload$/);
    await expect(uploadLink.getByText("文書アップロード", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("document-upload-menu.png") });

    await page.getByRole("button", { name: "サイドバーを折りたたむ" }).click();

    await expect(brand).toHaveAttribute("aria-hidden", "true");
    await expect(brand.getByText("Private", { exact: true })).toBeHidden();
    await expect(page.getByRole("button", { name: "サイドバーを展開" })).toBeVisible();
    await expect(uploadLink).toBeVisible();
  });
}
