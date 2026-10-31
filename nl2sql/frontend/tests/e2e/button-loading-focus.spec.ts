import { expect, test, type Page } from "./_helpers/test";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #355: 共有 Button は loading 中もフォーカスを保ち（ネイティブの disabled ではなく aria-disabled）、
// クリック・Enter / Space・form の暗黙の送信による二重送信を止める。
// あわせて PageHeader の操作のグループの区切りと、フォーカスの表示が outline 1 つであることを確かめる。
async function openFixture(page: Page, theme: "light" | "dark") {
  await page.route("**/__button-loading-focus", (route) => route.fulfill({ contentType: "text/html", body:
    `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/button-loading-focus.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（button-standards.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/button-loading-focus\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/button-loading-focus.tsx";' }));
  await page.goto("/__button-loading-focus");
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme}: loading 中もフォーカスを保ち、二重に送信しない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await openFixture(page, theme);
    const save = page.getByTestId("save");
    const copy = page.getByTestId("copy");
    const submits = page.getByTestId("submits");
    const clicks = page.getByTestId("clicks");

    // Enter → loading。フォーカスはボタンに残る（以前は disabled で body へ外れていた）。
    await page.getByLabel("名前").fill("売上");
    await page.keyboard.press("Tab");
    await expect(save).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(submits).toHaveText("1");
    await expect(save).toHaveAttribute("aria-busy", "true");
    await expect(save).toHaveAttribute("aria-disabled", "true");
    await expect(save).not.toHaveAttribute("disabled", /.*/);
    await expect(save).toBeDisabled(); // Playwright は aria-disabled も無効と判定する
    await expect(save).toBeFocused();

    // loading 中の Enter / Space / クリックは送信しない。
    await page.keyboard.press("Enter");
    await page.keyboard.press("Space");
    await save.click({ force: true });
    await expect(save).toBeFocused();
    await copy.click({ force: true });
    await expect(submits).toHaveText("1");
    await expect(clicks).toHaveText("0");

    // 見た目は disabled と同じ（ラベルと幅は変えない）。
    const look = (testId: string) =>
      page.getByTestId(testId).evaluate((element) => {
        const style = getComputedStyle(element);
        return { background: style.backgroundColor, color: style.color, cursor: style.cursor };
      });
    // Button は transition-colors で色を変えるため、loading に入った直後（CI では約 100ms 後に読んでいた）は
    // まだ途中の色になっている。遷移が終わるのを待って比べる（#391）。
    const disabledLook = await look("disabled");
    await expect.poll(() => look("save")).toEqual(disabledLook);
    await expect(save).toContainText("保存");
    await page.screenshot({ path: testInfo.outputPath(`button-loading-${theme}.png`) });

    // 入力欄で Enter（form の暗黙の送信）も、既定のボタンが loading の間は送信しない。
    await page.getByLabel("名前").press("Enter");
    await expect(submits).toHaveText("1");

    // 完了後もフォーカスはボタンのまま（入力欄へ移した場合を除き、利用者の位置を失わない）。
    await save.focus();
    await page.evaluate(() => window.__finishSave?.());
    await expect(save).not.toHaveAttribute("aria-busy", /.*/);
    await expect(save).not.toHaveAttribute("aria-disabled", /.*/);
    await expect(save).toBeEnabled();
    await expect(save).toBeFocused();
    expect(await page.evaluate(() => document.activeElement?.getAttribute("data-testid"))).toBe("save");

    // 完了後は再び送信できる。
    await page.keyboard.press("Enter");
    await expect(submits).toHaveText("2");
    await page.evaluate(() => window.__finishSave?.());
    await copy.click();
    await expect(clicks).toHaveText("1");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });

  test(`${theme}: フォーカスの表示は outline 1 つで、PageHeader の操作はグループごとに区切る`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const mobile = testInfo.project.name === "mobile-375";
    await openFixture(page, theme);

    // Switch: 以前は ring（box-shadow）とグローバルの outline が二重に出ていた。
    const toggle = page.getByTestId("switch");
    await page.getByTestId("copy").focus();
    await page.keyboard.press("Tab");
    await expect(toggle).toBeFocused();
    const focusStyle = await toggle.evaluate((element) => {
      const style = getComputedStyle(element);
      return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, boxShadow: style.boxShadow };
    });
    expect(focusStyle.outlineStyle).toBe("solid");
    expect(focusStyle.outlineWidth).toBe("2px");
    expect(focusStyle.boxShadow).toBe("none");

    const header = page.getByRole("group", { name: "ページ操作" });
    if (!mobile) {
      // danger | utility | secondary + primary の 3 グループ → 区切りは 2 つ
      await expect(header.getByTestId("page-actions-separator")).toHaveCount(2);
      await expect(header.getByRole("button")).toHaveText(["すべて削除", "最新情報を取得", "取込", "新規作成"]);
    } else {
      // 狭い画面は主操作 1 つ +「その他の操作」。メニューでは危険操作を区切り線の下に置く。
      await expect(header.getByTestId("page-actions-separator")).toHaveCount(0);
      await header.getByRole("button", { name: "その他の操作" }).click();
      const menu = page.getByRole("menu", { name: "その他の操作" });
      await expect(menu.getByRole("separator")).toHaveCount(1);
      await expect(menu.getByRole("menuitem")).toHaveText(["取込", "最新情報を取得", "すべて削除"]);
      await page.keyboard.press("Escape");
    }
    await page.screenshot({ path: testInfo.outputPath(`page-header-groups-${theme}.png`) });
  });
}
