import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME, expandSidebarSections, openSidebarNav } from "./_helpers";

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());

    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }

    // 戻る前の画面は、内容が画面より長い検索方法（OCI 認証の画面は #11 で削除した）。
    // 検索方法はデータベースの状態の確認を通る画面なので、準備済みを返す。
    const settingsData: Record<string, unknown> = {
      "/api/ready/database": { status: "ok", check: "ok", detail: null },
      "/api/settings/answer-prompts": { prompts: [], stages: [] },
      "/api/settings/answering": {
        query_strategy: "auto_routing",
        answer_flow: "crag",
        neighbor_child_count: 3,
        rerank_enabled: true,
        screen_linking_enabled: false,
        request_coverage_retrieval_enabled: false,
        auto_field_filter_enabled: false,
        config_source: "runtime",
      },
      "/api/settings/upload-storage": {
        local_storage_dir: "/u01/data/production-ready-rag",
        readiness: "ok",
        max_upload_bytes: 209715200,
        config_source: "runtime",
      },
    };
    if (url.pathname in settingsData) {
      await route.fulfill({
        json: { data: settingsData[url.pathname], error_messages: [], warning_messages: [] },
      });
      return;
    }

    await route.fulfill({
      json: { data: null, error_messages: [], warning_messages: [] },
    });
  });
}

test("sidebar route changes reset the main pane while browser back restores it", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/settings/retrieval");

  const main = page.getByRole("main");
  await expect(page.getByRole("heading", { name: "検索方法", level: 1 })).toBeVisible();

  const startScrollTop = await main.evaluate((element) => {
    element.scrollTo({
      top: Math.min(620, element.scrollHeight - element.clientHeight),
      left: 0,
      behavior: "auto",
    });
    element.dispatchEvent(new Event("scroll"));
    return element.scrollTop;
  });
  expect(startScrollTop).toBeGreaterThan(100);

  const sidebar = await openSidebarNav(page);
  await expandSidebarSections(sidebar, "システム設定");
  await sidebar.getByRole("link", { name: "アップロード保存先" }).click();

  await expect(page).toHaveURL(/\/settings\/upload-storage$/);
  await expect(
    page.getByRole("heading", { name: "アップロード保存先", level: 1 })
  ).toBeVisible();
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(0);

  await page.goBack();

  await expect(page).toHaveURL(/\/settings\/retrieval$/);
  await expect(page.getByRole("heading", { name: "検索方法", level: 1 })).toBeVisible();
  await expect
    .poll(() => main.evaluate((element) => element.scrollTop))
    .toBeGreaterThanOrEqual(startScrollTop - 1);
});
