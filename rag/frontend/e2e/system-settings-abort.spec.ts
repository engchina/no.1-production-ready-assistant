import { expect, test } from "./fixtures/test";
import { mockLocalAuth } from "./_helpers";

/**
 * 共通のシステム設定の画面で、設定の取得中に別の画面へ移ったら取得の要求を止める（#1117）。
 * 共有の画面は TanStack Query の `signal` を `api` に渡す。製品の `api` がそれを fetch へ渡すこと。
 */
// `alsoHold` は同じ画面が同時に取得する API。応答を返さずに取得中のままにする。未モックの 404 で先に失敗すると、
// 画面は取得中ではなく失敗の表示になり、移っても止める要求が残らない（dev server の StrictMode の
// マウントのやり直しだけで中断が起きて通っていた。PR の CI は build の成果物で実行する。#1583）。
// `to` は移り先のサイドナビの項目（同じシステム設定の別の画面）。
const CASES: ReadonlyArray<{
  path: string;
  api: string;
  alsoHold?: readonly string[];
  to: { link: string; url: RegExp };
}> = [
  { path: "/settings/upload-storage", api: "/api/settings/upload-storage", to: { link: "データベース設定", url: /\/settings\/database$/ } },
  { path: "/settings/model", api: "/api/settings/model", to: { link: "データベース設定", url: /\/settings\/database$/ } },
  { path: "/settings/database", api: "/api/settings/database", to: { link: "モデル設定", url: /\/settings\/model$/ } },
];

test.beforeEach(async ({ page }, testInfo) => {
  // 画面内の移動（サイドナビ）で確かめる。画面の幅に関係しないので desktop だけで実行する。
  test.skip(testInfo.project.name !== "desktop", "画面の幅に関係しない");
  await mockLocalAuth(page);
});

for (const { path, api, alsoHold = [], to } of CASES) {
  test(`${path} の取得中に別の画面へ移ると、設定の取得を止める`, async ({ page }) => {
    // 応答を返さず、取得中のまま止めておく。
    for (const held of [api, ...alsoHold]) await page.route(`**${held}`, () => undefined);
    const requested = page.waitForRequest((request) => new URL(request.url()).pathname === api);
    const aborted = page.waitForEvent(
      "requestfailed",
      (request) => new URL(request.url()).pathname === api
    );

    await page.goto(path);
    await requested;
    await page.getByRole("link", { name: to.link, exact: true }).click();
    await expect(page).toHaveURL(to.url);

    const failed = await aborted;
    expect(failed.failure()?.errorText ?? "").toMatch(/abort|cancel/i);
  });
}
