import { expect, test, type Locator, type Page } from "./_helpers/test";

import { expectToastPlacement, overlaps } from "./_helpers/toast";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #1728: 通知は画面幅ごとに決まった 1 か所に出し、ページの内容（PageHeader の操作の有無・スクロール）で動かさない。
// desktop は画面の右下（右端から 1rem、下端から --toast-offset-bottom = 8rem）、375px は上端のバー。
// 以前（#411）は PageHeader の操作の位置を測ってその左に置いたため、操作の有無で位置が変わった。
// 下端から 8rem は、ページの末尾（スクロールしきると画面の下端）の「SQL 生成」とチャットの入力欄の上で、
// ポインタが通知に乗って自動の消去が止まって（#351）も主操作を押せる。一時停止はそのまま残す。
async function openFixture(page: Page, theme: "light" | "dark") {
  await page.route(/\/__toast-placement(\?.*)?$/, (route) => route.fulfill({ contentType: "text/html", body:
    `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/toast-placement.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（toast-pause.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/toast-placement\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/toast-placement.tsx";' }));
  // 時刻を止め、runFor で進めた分だけ経過させる（通知が操作の途中で消えて、覆っていないように見えることを防ぐ）。
  await page.clock.install({ time: new Date("2026-09-28T09:00:00+09:00") });
  await page.clock.pauseAt(new Date("2026-09-28T09:00:01+09:00"));
  await page.goto("/__toast-placement");
}

/** 要素の中心を押すと、その要素に届く（通知などに覆われていない）。 */
async function expectHitsItself(target: Locator) {
  await expect(target).toBeInViewport();
  const hit = await target.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return top !== null && element.contains(top);
  });
  expect(hit).toBe(true);
}

/** 通知の領域の右端・下端（desktop）または上端・右端（375px）。決まった 1 か所かを比べる。 */
async function anchorOf(region: Locator) {
  const box = (await region.boundingBox())!;
  return { right: Math.round(box.x + box.width), bottom: Math.round(box.y + box.height), top: Math.round(box.y) };
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme}: 通知を出したまま、ページの操作と末尾の主操作を押せる（一時停止は維持）`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const mobile = testInfo.project.name === "mobile-375";
    await openFixture(page, theme);
    const region = page.getByRole("region", { name: "通知", exact: true });
    const counts = page.getByTestId("counts");
    const pageActions = page.getByRole("group", { name: "ページ操作" });
    const save = pageActions.getByRole("button", { name: "保存", exact: true });

    // ページの操作（PageHeader の右端）: 押した直後に出た通知が、押したボタンと操作の並びを覆わない。
    // desktop の通知は右下なので届かない。375px では通知が上端のバーから下へ積まれるため、スクロールしていない
    // PageHeader の操作に届かない 2 件で確かめる（3 件目は PageHeader の操作の高さに届く。UX 契約 messaging.md §3.1）。
    const stacked = mobile ? 2 : 3;
    for (let index = 0; index < stacked; index += 1) {
      await expectHitsItself(save);
      await save.click({ timeout: 3_000 });
    }
    await expect(counts).toContainText(`保存 ${stacked} 回`);
    await expect(region.getByRole("status")).toHaveCount(stacked);
    const regionBox = (await region.boundingBox())!;
    expect(overlaps(regionBox, (await pageActions.boundingBox())!)).toBe(false);
    await expectToastPlacement(page);
    const anchor = await anchorOf(region);
    // 新しい通知は下に足す（desktop は古い通知が上へ押し上げられる。375px は下へ伸びる）。
    const statuses = await region.getByRole("status").all();
    const ys = await Promise.all(statuses.map(async (status) => (await status.boundingBox())!.y));
    expect(ys).toEqual([...ys].sort((a, b) => a - b));
    if (mobile) {
      // 上端のバーに重ねるが、メニューのボタンは覆わない。
      await expectHitsItself(page.getByTestId("nav-drawer-trigger"));
    }
    // 本文の先頭の右の操作（ContentActionBar / ObjectActionBar の位置）も覆わない。
    const copy = page.getByRole("group", { name: "構造情報の操作" }).getByRole("button", { name: "コピー" });
    await expectHitsItself(copy);
    await copy.click({ timeout: 3_000 });
    await expect(counts).toContainText("コピー 1 回");
    await page.screenshot({ path: testInfo.outputPath(`toast-placement-header-${theme}.png`) });

    // ページの末尾の主操作: スクロールしきると画面の下端に来る。
    await page.locator("main").evaluate((main) => {
      main.scrollTop = main.scrollHeight;
    });
    const generate = page.getByRole("group", { name: "SQL 生成の操作" }).getByRole("button", { name: "SQL 生成" });
    const viewport = page.viewportSize()!;
    const generateBox = (await generate.boundingBox())!;
    expect(viewport.height - (generateBox.y + generateBox.height)).toBeLessThan(120);

    // 通知にポインタを乗せると止まる（desktop）。止めたままでも、覆われていない主操作を押せる。
    const first = region.getByRole("status").first();
    if (!mobile) {
      await first.hover();
      await page.clock.runFor(10_000);
      await expect(region.getByRole("status")).toHaveCount(stacked);
    }
    await expectHitsItself(generate);
    await generate.click({ timeout: 3_000 });
    await expect(counts).toContainText("生成 1 回");
    await expect(region.getByRole("status")).toHaveCount(stacked + 1);
    await expectToastPlacement(page);
    // スクロールしても、通知を足しても、通知の位置（desktop は右下の角、375px は上端）は変わらない。
    const scrolledAnchor = await anchorOf(region);
    if (mobile) expect({ top: scrolledAnchor.top, right: scrolledAnchor.right }).toEqual({ top: anchor.top, right: anchor.right });
    else expect({ bottom: scrolledAnchor.bottom, right: scrolledAnchor.right }).toEqual({ bottom: anchor.bottom, right: anchor.right });
    expect(overlaps((await region.boundingBox())!, (await generate.boundingBox())!)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`toast-placement-end-${theme}.png`) });

    // 離れたら残り時間から再開し、4 秒で消える。
    await page.mouse.move(0, 0);
    await page.clock.runFor(4_100);
    await expect(region.getByRole("status")).toHaveCount(0);

    // PageHeader にページの操作が無い画面でも、同じ位置に出る（以前は操作の有無で位置が変わった）。
    await page.goto("/__toast-placement?actions=none");
    await page.getByRole("group", { name: "SQL 生成の操作" }).getByRole("button", { name: "SQL 生成" }).click();
    await expect(region.getByRole("status")).toHaveCount(1);
    await expectToastPlacement(page);
    const noActionsAnchor = await anchorOf(region);
    if (mobile) expect({ top: noActionsAnchor.top, right: noActionsAnchor.right }).toEqual({ top: anchor.top, right: anchor.right });
    else expect({ bottom: noActionsAnchor.bottom, right: noActionsAnchor.right }).toEqual({ bottom: anchor.bottom, right: anchor.right });
    await page.screenshot({ path: testInfo.outputPath(`toast-placement-no-actions-${theme}.png`) });
    await page.clock.runFor(4_100);
    await expect(region.getByRole("status")).toHaveCount(0);

    if (!mobile) {
      // 1920px でも右下に出し、ページの操作を覆わない。
      await page.goto("/__toast-placement");
      await page.setViewportSize({ width: 1920, height: 1080 });
      await page.locator("main").evaluate((main) => {
        main.scrollTop = 0;
      });
      await save.click();
      await expect(region.getByRole("status")).toHaveCount(1);
      await expectToastPlacement(page);
      expect(overlaps((await region.boundingBox())!, (await pageActions.boundingBox())!)).toBe(false);
      await page.screenshot({ path: testInfo.outputPath(`toast-placement-1920-${theme}.png`) });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}
