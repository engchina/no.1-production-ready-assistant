import { expect, test, type Locator, type Page } from "./_helpers/test";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #372: 共有の Tooltip。アイコンだけの Button は既定で aria-label と同じ文言を、ホバー（遅延あり）と
// キーボードのフォーカス（すぐ）で出す。Escape で閉じ、吹き出しへポインタを移しても消えない（WCAG 1.4.13）。
// 画面の端では反転・内側へずらし、タッチ端末（mobile-375 = Pixel 5、pointer: coarse）では出さない。
async function openFixture(page: Page, theme: "light" | "dark") {
  await page.route("**/__tooltip", (route) => route.fulfill({ contentType: "text/html", body:
    `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/tooltip.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（button-standards.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/tooltip\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/tooltip.tsx";' }));
  await page.goto("/__tooltip");
  await expect(page.getByTestId("close")).toBeVisible();
}

/** 見えている吹き出し（hidden の説明用の吹き出しは除く）。 */
function visibleTooltip(page: Page) {
  return page.locator('[role="tooltip"]:not([hidden])');
}

async function box(locator: Locator) {
  const value = await locator.boundingBox();
  if (!value) throw new Error("要素が表示されていません");
  return value;
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme}: ホバーで遅れて出し、吹き出しへ移っても消えず、Escape で閉じる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "ホバーは desktop だけ");
    await openFixture(page, theme);
    const close = page.getByTestId("close");
    const tooltip = visibleTooltip(page);

    await close.hover();
    await expect(tooltip).toHaveText("閉じる");
    // aria-label と同じ文言は説明として結び付けず、読み上げから外す（二重に読み上げない）。
    await expect(tooltip).toHaveAttribute("aria-hidden", "true");
    await expect(close).not.toHaveAttribute("aria-describedby", /.+/);
    await expect(close).not.toHaveAttribute("title", /.*/);
    // 既定は上に出す。トリガーの中央にそろう。
    await expect(tooltip).toHaveAttribute("data-tooltip-placement", "top");
    const trigger = await box(close);
    const bubble = await box(tooltip);
    expect(bubble.y + bubble.height).toBeLessThanOrEqual(trigger.y);
    expect(Math.abs(bubble.x + bubble.width / 2 - (trigger.x + trigger.width / 2))).toBeLessThanOrEqual(1);

    // トリガーから吹き出しへポインタを移しても消えない（WCAG 1.4.13 hoverable）。
    await page.mouse.move(trigger.x + trigger.width / 2, trigger.y + 2);
    await page.mouse.move(bubble.x + bubble.width / 2, bubble.y + bubble.height / 2, { steps: 4 });
    await page.waitForTimeout(300);
    await expect(tooltip).toBeVisible();

    // Escape は吹き出しだけを閉じ、囲む層（document の keydown）には届かない。2 回目は届く。
    await page.keyboard.press("Escape");
    await expect(tooltip).toHaveCount(0);
    await expect(page.getByTestId("escapes")).toHaveText("0");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("escapes")).toHaveText("1");

    // 押したら出さない（押した結果を隠さない）。
    await page.mouse.move(0, 0);
    await close.click();
    await expect(page.getByTestId("clicks")).toHaveText("1");
    await page.waitForTimeout(700);
    await expect(tooltip).toHaveCount(0);

    // tooltip={false} では出さない。
    await page.getByTestId("no-tooltip").hover();
    await page.waitForTimeout(700);
    await expect(tooltip).toHaveCount(0);
  });

  test(`${theme}: キーボードのフォーカスですぐに出し、違う文言は aria-describedby で説明にする`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "キーボードは desktop だけ");
    await openFixture(page, theme);
    const prev = page.getByTestId("prev");
    const tooltip = visibleTooltip(page);

    await page.getByLabel("検索語").focus();
    await page.keyboard.press("Tab");
    await expect(prev).toBeFocused();
    await expect(tooltip).toHaveText("前のページ（PageUp）");
    await expect(tooltip).not.toHaveAttribute("aria-hidden", /.*/);
    await expect(tooltip).toHaveAttribute("data-tooltip-reason", "focus");
    await expect(prev).toHaveAccessibleName("前のページ");
    await expect(prev).toHaveAccessibleDescription("前のページ（PageUp）");
    const describedBy = await prev.getAttribute("aria-describedby");
    await expect(tooltip).toHaveAttribute("id", describedBy ?? "");

    await page.keyboard.press("Escape");
    await expect(tooltip).toHaveCount(0);
    await expect(prev).toBeFocused();

    // 次のボタンへ移ると、そのボタンの吹き出しに替わる。
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("close")).toBeFocused();
    await expect(tooltip).toHaveText("閉じる");
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Shift+Tab");
    await expect(tooltip).toHaveCount(0);
  });

  test(`${theme}: 固定ヘッダーのボタンは下へ反転してヘッダーの上に出し、画面の右端では内側にずらす`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "ホバーは desktop だけ");
    await openFixture(page, theme);
    const help = page.getByTestId("help");
    const tooltip = visibleTooltip(page);

    await help.hover();
    await expect(tooltip).toHaveText("ヘルプ");
    await expect(tooltip).toHaveAttribute("data-tooltip-placement", "bottom");
    // 固定ヘッダー（--z-sticky）の中でも隠れない（ホバーで開いた吹き出しはポインタを受けるので elementFromPoint で測れる）。
    const bubble = await box(tooltip);
    const hit = await page.evaluate(
      ([x, y]) => document.elementFromPoint(x, y)?.closest('[role="tooltip"]')?.textContent ?? null,
      [bubble.x + bubble.width / 2, bubble.y + bubble.height / 2] as const
    );
    expect(hit).toBe("ヘルプ");

    // 375px の右端のボタンは、画面の内側（右 8px）に収める。
    await page.setViewportSize({ width: 375, height: 812 });
    const edge = page.getByTestId("edge");
    await edge.scrollIntoViewIfNeeded();
    await edge.focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(edge).toBeFocused();
    await expect(tooltip).toHaveText("CSV をダウンロード");
    const edgeBubble = await box(tooltip);
    expect(edgeBubble.x).toBeGreaterThanOrEqual(8);
    expect(edgeBubble.x + edgeBubble.width).toBeLessThanOrEqual(375 - 8 + 1);
  });

  test(`${theme}: タッチ端末（375px）では押しても出さない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-375", "タッチ端末は mobile-375 だけ");
    await openFixture(page, theme);
    expect(await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches)).toBe(true);
    const tooltip = visibleTooltip(page);

    await page.getByTestId("close").tap();
    await expect(page.getByTestId("clicks")).toHaveText("1");
    await page.getByTestId("prev").focus();
    await page.waitForTimeout(700);
    await expect(tooltip).toHaveCount(0);
    // 説明は見た目に出さなくても読み上げに残る。
    await expect(page.getByTestId("prev")).toHaveAccessibleDescription("前のページ（PageUp）");
  });
}
