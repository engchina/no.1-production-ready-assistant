import { expect, test } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";

// 無頭ブラウザーの scrollbar 非表示を解除し、通常の scrollbar による幅変更を検出する。
test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

for (const theme of ["light", "dark"] as const) {
  for (const width of [1280, 1920, 375]) {
    test(`一覧・設定の本文は scrollbar の有無で幅が変わらない (${width}px, ${theme})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name.includes("mobile") ? width !== 375 : width === 375);
      // 画面固有の取得失敗でも AppShell の寸法を維持する。実 backend には接続しない。
      await page.route("**/api/**", (route) => route.fulfill({
        status: 404, json: { data: null, error_messages: ["検証用の取得失敗"], warning_messages: [] },
      }));
      await mockDatabaseGateReady(page);
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(({ key, theme }) => {
        localStorage.setItem(key, JSON.stringify({ state: { theme }, version: 0 }));
      }, { key: "production-ready-nl2sql.ui", theme });
      for (const route of ["/profiles", "/history", "/settings/appearance"]) {
        await page.goto(route);
        await expect(page.locator("main h1")).toBeVisible();
        await page.evaluate(() => document.fonts.ready);
        const main = page.locator("#pr-main");
        const widths = await main.evaluate(async (element) => {
          const original = element.style.overflowY;
          const widths: number[] = [];
          // 画面の縦 scrollbar と、モーダル表示中のスクロール停止を切り替えて測る。
          for (const overflow of ["auto", "scroll", "hidden", "auto"]) {
            element.style.overflowY = overflow;
            await new Promise(requestAnimationFrame);
            widths.push(element.clientWidth);
          }
          element.style.overflowY = original;
          return widths;
        });
        expect(new Set(widths).size, `${route}: ${JSON.stringify(widths)}`).toBe(1);
        await page.screenshot({ path: testInfo.outputPath(`${route.replaceAll("/", "-")}-${width}-${theme}.png`) });
      }
    });
  }
}
