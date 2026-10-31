import { expect, test } from "./fixtures/mock-api";
import { openSidebarNav } from "./fixtures/nav";

/**
 * 共通のシステム設定の画面で、設定の取得中に別の画面へ移ったら取得の要求を止める（#1117）。
 * 共有の画面は TanStack Query の `signal` を `api` に渡す。製品の `api` がそれを fetch へ渡すこと。
 */
const CASES = [
  { path: "/settings/upload-storage", api: "/api/settings/upload-storage" },
  { path: "/settings/oci", api: "/api/settings/oci" },
  { path: "/settings/model", api: "/api/settings/model" },
  { path: "/settings/database", api: "/api/settings/database" },
] as const;

for (const { path, api } of CASES) {
  test(`${path} の取得中に別の画面へ移ると、設定の取得を止める`, async ({ page }) => {
    // 応答を返さず、取得中のまま止めておく（mock-api の既定の応答より後に登録して上書きする）。
    await page.route(`**${api}`, () => undefined);
    const requested = page.waitForRequest((request) => new URL(request.url()).pathname === api);
    const aborted = page.waitForEvent("requestfailed", (request) => new URL(request.url()).pathname === api);

    await page.goto(path);
    await requested;
    const nav = await openSidebarNav(page);
    await nav.getByRole("link", { name: "外観と証明書" }).click();
    await expect(page).toHaveURL(/\/settings\/appearance$/);

    const failed = await aborted;
    expect(failed.failure()?.errorText ?? "").toMatch(/abort|cancel/i);
  });
}
