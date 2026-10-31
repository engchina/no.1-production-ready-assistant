import { expect, test } from "./fixtures/mock-api";

// 通常の scrollbar が幅を取る環境を再現する。無頭ブラウザーの既定では scrollbar が隠れる。
test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

for (const theme of ["light", "dark"] as const) {
  for (const width of [1280, 1920, 375]) {
    test(`スキル一覧の件数とページのスクロールが変わっても幅と行高が安定する (${width}px, ${theme})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
      const base = mockApi.state.skills[0];
      const rows = Array.from({ length: 10 }, (_, index) => ({
        ...base, id: `stable-skill-${index}`, name: `業務の問い合わせに回答するスキル ${index}`,
        tags: ["ナレッジ検索", "業務問い合わせ"],
      }));
      // 表示上限ちょうど → 上限を超える件数で、表の縦 scrollbar の有無が変わる。
      const visibleRows = width < 768 ? 5 : 8;
      mockApi.state.skills = rows.slice(0, visibleRows);
      await page.setViewportSize({ width, height: 1200 });
      await page.addInitScript((value) => {
        localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
      }, theme);
      await page.goto("/skills");
      await expect(page.locator("tbody tr")).toHaveCount(visibleRows);
      await page.evaluate(() => document.fonts.ready);
      const tableRegion = page.getByRole("region", { name: "スキル一覧。スクロールできます。" });
      const measure = () => tableRegion.evaluate((element) => ({
        width: element.clientWidth,
        height: element.clientHeight,
        rowHeight: element.querySelector("tbody tr")!.getBoundingClientRect().height,
      }));
      const before = await measure();
      mockApi.state.skills = rows;
      const reload = page.getByRole("button", { name: "最新情報を取得", exact: true });
      if (await reload.isVisible()) {
        await reload.click();
      } else {
        await page.getByRole("button", { name: "その他の操作", exact: true }).click();
        await page.getByRole("menuitem", { name: "最新情報を取得", exact: true }).click();
      }
      await expect(page.locator("tbody tr")).toHaveCount(10);
      // 一度の初期測定ではなく、連続フレームで周期的な高さの切り替わりがないことを確かめる。
      await expect.poll(async () => (await measure()).width).toBe(before.width);
      await expect.poll(async () => (await measure()).rowHeight).toBe(before.rowHeight);
      const frames = await tableRegion.evaluate(async (element) => {
        const frames: string[] = [];
        for (let index = 0; index < 30; index += 1) {
          await new Promise(requestAnimationFrame);
          frames.push(JSON.stringify([element.clientWidth, element.clientHeight, (element as HTMLElement).style.maxHeight]));
        }
        return [...new Set(frames)];
      });
      expect(frames).toHaveLength(1);
      // 短い画面で main の scrollbar が現れても本文の内幅を変えない。
      const main = page.locator("#pr-main");
      const mainWidth = await main.evaluate((element) => element.clientWidth);
      await page.setViewportSize({ width, height: 480 });
      await expect.poll(() => main.evaluate((element) => element.clientWidth)).toBe(mainWidth);
      await tableRegion.focus();
      await page.keyboard.press("End");
      await expect(tableRegion).toBeFocused();
      await expect.poll(() => tableRegion.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
      await page.screenshot({ path: testInfo.outputPath(`skills-scroll-${width}-${theme}.png`) });
    });
  }
}
