import { expect, test } from "./fixtures/mock-api";
import { openSidebarNav } from "./fixtures/nav";

/**
 * 共通のシステム設定の画面で、設定の取得中に別の画面へ移ったら取得の要求を止める（#1117）。
 * 共有の画面は TanStack Query の `signal` を `api` に渡す。製品の `api` がそれを fetch へ渡すこと。
 */
const CASES = [
  { path: "/settings/upload-storage", api: "/api/settings/upload-storage", link: "アップロード保存先" },
  { path: "/settings/oci", api: "/api/settings/oci", link: "OCI 認証設定" },
  { path: "/settings/model", api: "/api/settings/model", link: "モデル設定" },
  { path: "/settings/database", api: "/api/settings/database", link: "データベース設定" },
] as const;

for (const [index, { path, api }] of CASES.entries()) {
  // 移り先は一覧の次の画面（最後は先頭）。移り先の取得は mock-api の既定の応答で返る。
  const next = CASES[(index + 1) % CASES.length];
  test(`${path} の取得中に別の画面へ移ると、設定の取得を止める`, async ({ page }) => {
    // 応答を返さず、取得中のまま止めておく（mock-api の既定の応答より後に登録して上書きする）。
    await page.route(`**${api}`, () => undefined);
    const requested = page.waitForRequest((request) => new URL(request.url()).pathname === api);
    const aborted = page.waitForEvent("requestfailed", (request) => new URL(request.url()).pathname === api);

    await page.goto(path);
    await requested;
    const nav = await openSidebarNav(page);
    await nav.getByRole("link", { name: next.link, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${next.path}$`));

    const failed = await aborted;
    expect(failed.failure()?.errorText ?? "").toMatch(/abort|cancel/i);
  });
}
