import { expect, test, type Locator, type Page } from "./_helpers/test";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #364: タッチ端末（pointer: coarse）でだけ、ToggleChip / Switch の当たり判定を見た目のまま 44px 以上に広げる。
// 隣り合う・折り返した部品の当たり判定が、互いの見た目を覆わないことも確かめる。
// あわせて、Tabs が横にスクロールするときはスクロールできる方向の端だけをフェードし、キーボードで選んだタブを見せる。
// mobile-375 は Pixel 5（hasTouch / isMobile）で、desktop は変化が無いことを確かめる。
async function openFixture(page: Page, theme: "light" | "dark") {
  await page.route("**/__touch-targets-tabs", (route) => route.fulfill({ contentType: "text/html", body:
    `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/touch-targets-tabs.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（button-standards.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/touch-targets-tabs\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/touch-targets-tabs.tsx";' }));
  await page.goto("/__touch-targets-tabs");
  await expect(page.getByRole("tablist", { name: "表示の切り替え" })).toBeVisible();
}

type Box = { x: number; y: number; width: number; height: number };

async function box(locator: Locator): Promise<Box> {
  const value = await locator.boundingBox();
  if (!value) throw new Error("要素が表示されていません");
  return value;
}

/** 画面上の点を押したときに届く要素の data-testid / 名前（当たり判定の実寸を elementFromPoint で測る）。 */
async function hitAt(page: Page, x: number, y: number) {
  return page.evaluate(([px, py]) => {
    const element = document.elementFromPoint(px, py)?.closest("button");
    return element ? element.getAttribute("data-testid") ?? element.textContent?.trim() ?? "" : null;
  }, [x, y] as const);
}

/** 見た目の外側で、その部品に届く範囲の縦・横の大きさ（1px 刻みで外へ探る）。 */
async function hitExtent(page: Page, locator: Locator, name: string) {
  const b = await box(locator);
  const cx = b.x + b.width / 2;
  const cy = b.y + b.height / 2;
  let up = 0;
  while (up < 40 && (await hitAt(page, cx, cy - b.height / 2 - up - 0.5)) === name) up += 1;
  let down = 0;
  while (down < 40 && (await hitAt(page, cx, cy + b.height / 2 + down + 0.5)) === name) down += 1;
  let left = 0;
  while (left < 40 && (await hitAt(page, cx - b.width / 2 - left - 0.5, cy)) === name) left += 1;
  let right = 0;
  while (right < 40 && (await hitAt(page, cx + b.width / 2 + right + 0.5, cy)) === name) right += 1;
  return { height: b.height + up + down, width: b.width + left + right, visual: b };
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme}: タッチ端末でだけ ToggleChip / Switch の当たり判定が 44px 以上になり、隣の見た目を覆わない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const mobile = testInfo.project.name === "mobile-375";
    await openFixture(page, theme);
    expect(await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches)).toBe(mobile);

    const triState = page.getByTestId("tri-state");
    const on = triState.getByRole("button", { name: "ON", exact: true });
    const off = triState.getByRole("button", { name: "OFF", exact: true });
    const inherit = triState.getByRole("button", { name: "継承" });
    const switchHistory = page.getByTestId("switch-history");
    const switchNotify = page.getByTestId("switch-notify");
    const switchSound = page.getByTestId("switch-sound");

    // 見た目の大きさは desktop と同じ（チップ 26px 前後・スイッチ 44 × 24px）。
    const onBox = await box(on);
    expect(onBox.height).toBeLessThan(30);
    expect(await box(switchHistory)).toMatchObject({ width: 44, height: 24 });

    for (const [locator, name] of [
      [on, "ON"],
      [inherit, "継承"],
      [switchHistory, "switch-history"],
    ] as const) {
      const extent = await hitExtent(page, locator, name);
      if (mobile) {
        expect(extent.height, `${name} の当たり判定の高さ`).toBeGreaterThanOrEqual(44);
        expect(extent.width, `${name} の当たり判定の幅`).toBeGreaterThanOrEqual(Math.min(44, extent.visual.width + 2 * 3.5));
      } else {
        // マウス環境は変えない（当たり判定 = 見た目）。
        expect(extent.height, `${name} の当たり判定の高さ（desktop）`).toBeLessThanOrEqual(extent.visual.height + 1);
        expect(extent.width, `${name} の当たり判定の幅（desktop）`).toBeLessThanOrEqual(extent.visual.width + 1);
      }
    }

    // 見た目の外側（下へ 8px）を実際にタップ / クリックしたとき、タッチ端末でだけ切り替わる。
    const offBox = await box(off);
    if (mobile) {
      await page.touchscreen.tap(offBox.x + offBox.width / 2, offBox.y + offBox.height + 8);
      await expect(off).toHaveAttribute("aria-pressed", "true");
      const historyBox = await box(switchHistory);
      await page.touchscreen.tap(historyBox.x + historyBox.width / 2, historyBox.y - 8);
      await expect(switchHistory).toHaveAttribute("aria-checked", "true");
    } else {
      await page.mouse.click(offBox.x + offBox.width / 2, offBox.y + offBox.height + 8);
      await expect(off).toHaveAttribute("aria-pressed", "false");
    }

    // 隣り合う・折り返した部品: 見た目の上の点（端から 1px 内側）は必ずその部品に届く。
    const wrapped = page.getByTestId("wrapped").getByRole("button");
    const count = await wrapped.count();
    const rows = new Set<number>();
    for (let index = 0; index < count; index += 1) {
      const chip = wrapped.nth(index);
      const name = (await chip.textContent())?.trim() ?? "";
      const b = await box(chip);
      rows.add(Math.round(b.y));
      for (const [x, y] of [
        [b.x + b.width / 2, b.y + 1],
        [b.x + b.width / 2, b.y + b.height - 1],
        [b.x + 1, b.y + b.height / 2],
        [b.x + b.width - 1, b.y + b.height / 2],
      ]) {
        expect(await hitAt(page, x, y), `${name} の見た目の端`).toBe(name);
      }
    }
    expect(rows.size, "チップが折り返している").toBeGreaterThan(1);
    for (const [locator, name] of [
      [switchNotify, "switch-notify"],
      [switchSound, "switch-sound"],
    ] as const) {
      const b = await box(locator);
      expect(await hitAt(page, b.x + b.width / 2, b.y + 1)).toBe(name);
      expect(await hitAt(page, b.x + b.width / 2, b.y + b.height - 1)).toBe(name);
    }
    if (mobile) {
      // 間隔（7px）が当たり判定の広がり（10px）より狭いときは、間を後ろ（下）の部品が取る。
      // 上のスイッチは上へ、下のスイッチは下へ 44px 分まで広がる。
      expect((await hitExtent(page, switchNotify, "switch-notify")).height).toBeGreaterThanOrEqual(24 + 10);
      expect((await hitExtent(page, switchSound, "switch-sound")).height).toBeGreaterThanOrEqual(24 + 10 + 7);
    }

    // 広げた当たり判定で横スクロールを作らない。
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`touch-targets-${theme}.png`), fullPage: true });
  });

  test(`${theme}: Tabs はスクロールできる方向の端だけをフェードし、キーボードで選んだタブを見せる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const mobile = testInfo.project.name === "mobile-375";
    await openFixture(page, theme);
    const tablist = page.getByRole("tablist", { name: "表示の切り替え" });
    const edges = () =>
      tablist.evaluate((element) => ({
        start: element.hasAttribute("data-scroll-start"),
        end: element.hasAttribute("data-scroll-end"),
        mask: getComputedStyle(element).maskImage || getComputedStyle(element).getPropertyValue("-webkit-mask-image"),
        overflow: element.scrollWidth > element.clientWidth,
      }));

    if (!mobile) {
      // desktop（1280px）はタブが入りきるので、フェードもスクロールバーも出さない（見た目は変わらない）。
      expect(await edges()).toEqual({ start: false, end: false, mask: "none", overflow: false });
      await page.screenshot({ path: testInfo.outputPath(`tabs-desktop-${theme}.png`) });
      return;
    }

    // 375px: 先頭にいるときは右（続き）だけをフェードする。
    await expect.poll(edges).toMatchObject({ start: false, end: true, overflow: true });
    expect((await edges()).mask).toContain("linear-gradient");
    await page.screenshot({ path: testInfo.outputPath(`tabs-start-${theme}.png`) });

    const fadeWidth = await tablist.evaluate((element) => Number.parseFloat(getComputedStyle(element).scrollPaddingInlineStart));
    expect(fadeWidth).toBeGreaterThan(0);
    /** タブがフェードの外（スクロール領域の見えている範囲の内側）にあるか。 */
    const fullyVisible = async (name: string) => {
      const list = await box(tablist);
      const tab = await box(tablist.getByRole("tab", { name }));
      const scrollLeft = await tablist.evaluate((element) => element.scrollLeft);
      const maxScroll = await tablist.evaluate((element) => element.scrollWidth - element.clientWidth);
      // 端までスクロールしたときは、その側にフェードが無いので余白は要らない。
      const startPad = scrollLeft <= 0 ? 0 : fadeWidth;
      const endPad = scrollLeft >= maxScroll - 1 ? 0 : fadeWidth;
      return tab.x >= list.x + startPad - 1 && tab.x + tab.width <= list.x + list.width - endPad + 1;
    };

    // キーボード: → で順に選ぶと、選んだタブがフェードに隠れない位置までスクロールする。
    await tablist.getByRole("tab", { name: "概要" }).focus();
    for (const name of ["テーブル", "ビュー", "列の定義", "索引", "制約"]) {
      await page.keyboard.press("ArrowRight");
      const tab = tablist.getByRole("tab", { name });
      await expect(tab).toHaveAttribute("aria-selected", "true");
      await expect(tab).toBeFocused();
      await expect.poll(() => fullyVisible(name), `${name} が見える`).toBe(true);
    }
    // 途中にいるときは両端をフェードする。
    await expect.poll(edges).toMatchObject({ start: true, end: true });
    await page.screenshot({ path: testInfo.outputPath(`tabs-middle-${theme}.png`) });

    // End: 末尾のタブを見せ、右のフェードを消す。
    await page.keyboard.press("End");
    await expect(tablist.getByRole("tab", { name: "変更履歴" })).toBeFocused();
    await expect.poll(() => fullyVisible("変更履歴")).toBe(true);
    await expect.poll(edges).toMatchObject({ start: true, end: false });
    await page.screenshot({ path: testInfo.outputPath(`tabs-end-${theme}.png`) });

    // Home: 先頭へ戻り、左のフェードを消す。
    await page.keyboard.press("Home");
    await expect(tablist.getByRole("tab", { name: "概要" })).toBeFocused();
    await expect.poll(edges).toMatchObject({ start: false, end: true });
    await expect(page.getByTestId("selected-tab")).toHaveText("overview");

    // 幅が広がって入りきるようになったら、フェードを外す（resize で出し分ける）。
    await page.setViewportSize({ width: 1280, height: 812 });
    await expect.poll(edges).toEqual({ start: false, end: false, mask: "none", overflow: false });
  });
}
