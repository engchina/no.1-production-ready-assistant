import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME, expandSidebarSections, openSidebarNav } from "./_helpers";

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

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 667 },
]) {
  test(`RAG navigation does not expose SQL or Select AI product routes on ${viewport.name}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockApi(page);
    await page.goto("/settings/database");

    const sidebar = await openSidebarNav(page);
    // 既定では「AI 活用」と今のページのセクションだけが開いている（#1563）。すべて開いて項目を確かめる。
    await expandSidebarSections(sidebar);
    await expect(sidebar).toContainText("ナレッジ構築");
    await expect(sidebar).toContainText("取込・検索・回答設定");
    await expect(sidebar.getByRole("link", { name: "RAG 検索" })).toBeVisible();
    await expect(sidebar.getByRole("link", { name: "検索・回答プロファイル" })).toBeVisible();
    await expect(sidebar.getByRole("link", { name: /NL2SQL|SQL コンソール|Select AI/ })).toHaveCount(0);
  });
}
