import { expect, test, type Page } from "./_helpers/test";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #351: 通知はホバー・フォーカス中に消えず、離れたら残り時間から再開する。danger は 8 秒で消える（#2470）。
// タイマーは page.clock で止めて進め、実時間に依存しない。
async function openFixture(page: Page) {
  await page.route("**/__toast-pause", (route) => route.fulfill({ contentType: "text/html", body:
    `<html lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/toast-pause.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（button-standards.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/toast-pause\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/toast-pause.tsx";' }));
  // 時刻を止め、runFor で進めた分だけ経過させる（操作にかかる実時間を残り時間に混ぜない）。
  await page.clock.install({ time: new Date("2026-09-28T09:00:00+09:00") });
  await page.clock.pauseAt(new Date("2026-09-28T09:00:01+09:00"));
  await page.goto("/__toast-pause");
}

test(`通知はホバー・フォーカス中に止まり、danger は閉じるボタンでも消せる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile-375";
  await openFixture(page);
  const region = page.getByRole("region", { name: "通知", exact: true });

  await page.getByRole("button", { name: "エラー通知" }).click();
  await page.getByRole("button", { name: "成功通知" }).click();
  const danger = region.getByRole("alert");
  const success = region.getByRole("status").filter({ hasText: "保存しました" });
  await expect(danger).toBeVisible();
  await expect(success).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`toast-light.png`) });

  // ホバー（desktop）: 3 秒表示した後に乗せて 10 秒待っても消えず、離れたら残りの 1 秒で消える。
  if (!mobile) {
    await page.clock.runFor(3000);
    await success.hover();
    await page.clock.runFor(10_000);
    await expect(success).toBeVisible();
    await page.mouse.move(0, 0);
    await page.clock.runFor(900);
    await expect(success).toBeVisible();
    await page.clock.runFor(200);
    await expect(success).toHaveCount(0);
    await page.getByRole("button", { name: "成功通知" }).click();
    await expect(success).toBeVisible();
  }

  // フォーカス: 通知の中にフォーカスがある間は消えず、外へ出たら残り時間から再開する。
  await page.clock.runFor(2000);
  await success.getByRole("button", { name: "閉じる" }).focus();
  await page.clock.runFor(10_000);
  await expect(success).toBeVisible();
  await page.getByRole("button", { name: "警告通知" }).focus();
  await page.clock.runFor(1900);
  await expect(success).toBeVisible();
  await page.clock.runFor(200);
  await expect(success).toHaveCount(0);

  // warning は 4 秒で消える。danger は止まっていない時間が 8 秒を超えたので消えている（#2470）。
  await page.getByRole("button", { name: "警告通知" }).click();
  const warning = region.getByRole("status").filter({ hasText: "接続が不安定です" });
  await expect(warning).toBeVisible();
  await page.mouse.move(0, 0);
  await page.clock.runFor(4100);
  await expect(warning).toHaveCount(0);
  await expect(danger).toHaveCount(0);

  // danger は 8 秒で消え、それより前に閉じるボタンでも消せる。
  await page.getByRole("button", { name: "エラー通知" }).click();
  await expect(danger).toBeVisible();
  await page.mouse.move(0, 0);
  await page.clock.runFor(7900);
  await expect(danger).toBeVisible();
  await page.clock.runFor(200);
  await expect(danger).toHaveCount(0);
  await page.getByRole("button", { name: "エラー通知" }).click();
  await expect(danger).toBeVisible();
  await danger.getByRole("button", { name: "閉じる" }).click();
  await expect(danger).toHaveCount(0);

  // 閉じた通知の一時停止が残らない（閉じるボタンを押した通知は DOM から外れる）。
  await page.getByRole("button", { name: "成功通知" }).click();
  await page.mouse.move(0, 0);
  await page.clock.runFor(4100);
  await expect(success).toHaveCount(0);

  // Banner の閉じるボタンは共有 Button（44px、フォーカスリング）。
  const banner = page.getByRole("status").filter({ hasText: "データベースが縮退モードです" });
  const close = banner.getByRole("button", { name: "閉じる" });
  const box = await close.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(43.9);
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(43.9);
  await page.keyboard.press("Tab");
  await close.focus();
  await expect(close).toHaveCSS("outline-style", "solid");
  await banner.screenshot({ path: testInfo.outputPath(`banner-light.png`) });
  await page.keyboard.press("Enter");
  await expect(banner).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
