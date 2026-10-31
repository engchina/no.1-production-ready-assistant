import { expect, test } from "./_helpers/test";
import { expectCompactSortHeaders } from "./_helpers/sort-header";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

for (const theme of ["light", "dark"]) {
  test(`${theme}: 共通ボタンの実寸・状態・配置・キーボード操作`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.route("**/__button-standards", route => route.fulfill({ contentType: "text/html", body:
      `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/button-standards.tsx"></script></body></html>` }));
    // fixture はトップレベルで createRoot() する。@vitejs/plugin-react は react-refresh 用に自分自身を
    // import するが、並行作業中のファイル更新で Vite が HMR timestamp を付けると自己 import が
    // `button-standards.tsx?t=…` に書き換わり、ここで静的に読む `button-standards.tsx` と別モジュールとして
    // 二重実行される（root と Toaster portal「通知」が重複する）。?t= 版は元モジュールの再 export に差し替える。
    await page.route(/\/tests\/fixtures\/button-standards\.tsx\?t=\d+/, route => route.fulfill({
      contentType: "text/javascript", body: 'export * from "/tests/fixtures/button-standards.tsx";' }));
    await page.goto("/__button-standards");
    const mobile = testInfo.project.name === "mobile-375";
    for (const [size, height] of [["sm", 32], ["md", 36], ["lg", 40]] as const) {
      for (const variant of ["primary", "secondary", "ghost", "danger", "disabled", "loading"]) {
        const button = page.getByTestId(`${size}-${variant}`);
        await expect(button).toBeVisible();
        const style = await button.evaluate(el => {
          const s = getComputedStyle(el);
          return { height: el.getBoundingClientRect().height, radius: s.borderRadius, border: s.borderTopWidth,
            font: s.fontSize, clipped: el.scrollWidth > el.clientWidth, color: s.color, bg: s.backgroundColor };
        });
        expect(style.height).toBe(mobile ? 44 : height);
        expect(style.radius).toBe("6px");
        expect(style.border).toBe("1px");
        expect(style.font).toBe("14px");
        expect(style.clipped).toBe(false);
        if (["disabled", "loading"].includes(variant)) await expect(button).toBeDisabled();
        if (variant === "loading") {
          await expect(button).toHaveAttribute("aria-busy", "true");
          await expect(button.locator("svg:visible")).toHaveCount(1);
        }
      }
    }
    const sortTable = page.getByTestId("sort-header-table");
    await expectCompactSortHeaders(sortTable);
    const sortButton = sortTable.getByRole("button", { name: "名称" });
    await sortButton.focus();
    await expect(sortButton).toBeFocused();
    await expect(sortButton).toHaveCSS("outline-style", "solid");
    await sortButton.press("Enter");
    await expect(sortTable.getByRole("columnheader", { name: "名称" })).toHaveAttribute("aria-sort", "descending");
    await expect(sortTable.locator("tbody tr").first()).toContainText("B");
    // 見出しセル全体が押せることを hover の地で示す（platform README §7 #13）。
    if (!mobile) {
      await sortButton.hover();
      await expect(sortButton).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    } else {
      await sortButton.tap();
      await expect(sortTable.getByRole("columnheader", { name: "名称" })).toHaveAttribute("aria-sort", "ascending");
    }
    await sortTable.locator("thead").screenshot({ path: testInfo.outputPath(`column-font-${theme}.png`) });

    // 実際に合成された色を sRGB に変換して通常文字の 4.5:1 を検証する。
    // 共有 Button には nl2sql-button クラスが無いため、fixture の共有 Button を data-testid で対象にする。
    const sharedButtons = page.locator(
      ['sm-', 'md-', 'lg-'].map(prefix => `button[data-testid^="${prefix}"]:not(:disabled):not([aria-disabled="true"])`)
        .concat(['[data-testid="icon"]', '[data-testid="field"]', '[data-testid="danger-trigger"]'])
        .join(", ")
    );
    expect(await sharedButtons.count()).toBeGreaterThanOrEqual(15);
    const contrasts = await sharedButtons.evaluateAll(buttons => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext("2d")!;
      function luminance(color: string, background: string) {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = background;
        ctx.fillRect(0, 0, 1, 1);
        ctx.fillStyle = color;
        ctx.fillRect(0, 0, 1, 1);
        const rgb = Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3).map(value => {
          const c = value / 255;
          return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        });
        return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
      }
      return buttons.map(button => {
        const style = getComputedStyle(button);
        // dev サーバでは CSS 変数が light-dark() 文字列のまま返るため、変数ではなく
        // 祖先で実際に塗られている背景色（最も近い不透明な background-color）を下地にする。
        let surface = "rgb(255, 255, 255)";
        for (let node = button.parentElement; node; node = node.parentElement) {
          const bgColor = getComputedStyle(node).backgroundColor;
          if (bgColor !== "rgba(0, 0, 0, 0)" && bgColor !== "transparent") { surface = bgColor; break; }
        }
        const fg = luminance(style.color, surface);
        const bg = luminance(style.backgroundColor, surface);
        return { label: button.textContent, ratio: (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05) };
      });
    });
    for (const contrast of contrasts) expect(contrast.ratio, contrast.label ?? "icon").toBeGreaterThanOrEqual(4.5);
    await expect(page.getByRole("link", { name: "結果へ移動" })).toHaveCSS("height", mobile ? "44px" : "32px");
    await page.emulateMedia({ reducedMotion: "reduce" });
    // スピナーは reduced motion でも回転を止めない（#440）。
    await expect(page.getByTestId("sm-loading").locator("svg:visible")).toHaveCSS("animation-name", "spin");
    await expect(page.getByTestId("sm-loading").locator("svg:visible")).toHaveCSS("animation-duration", "1s");
    const primary = page.getByTestId("sm-primary");
    if (!mobile) {
      const initial = await primary.evaluate(el => getComputedStyle(el).backgroundColor);
      await primary.hover();
      const hover = await primary.evaluate(el => getComputedStyle(el).backgroundColor);
      expect(hover).not.toBe(initial);
      await page.mouse.down();
      const active = await primary.evaluate(el => getComputedStyle(el).backgroundColor);
      expect(active).not.toBe(hover);
      await page.mouse.move(0, 0);
      await page.mouse.up();
    }
    const icon = page.getByTestId("icon");
    const rect = await icon.boundingBox();
    expect(rect?.height).toBe(mobile ? 44 : 36);
    expect(rect?.width).toBe(rect?.height);
    // 入力欄と横の操作は同じ高さ（md 36px、タッチ端末は 44px。#613）。
    await expect(page.getByTestId("field")).toHaveCSS("height", mobile ? "44px" : "36px");
    await expect(page.getByTestId("field-input")).toHaveCSS("height", mobile ? "44px" : "36px");
    await page.getByTestId("sm-primary").focus();
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("sm-secondary")).toBeFocused();
    await expect(page.getByTestId("sm-secondary")).toHaveCSS("outline-style", "solid");
    await page.keyboard.press("Enter");
    await expect(page.locator("output")).toHaveText("操作回数: 1");
    await page.getByRole("button", { name: "選択状態", exact: true }).click();
    await expect(page.getByRole("button", { name: "選択状態", exact: true })).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "次へ", exact: true }).click();
    await expect(page.getByRole("navigation")).toContainText("2 / 3");
    await page.getByRole("button", { name: "再試行", exact: true }).click();
    await expect(page.locator("output")).toHaveText("操作回数: 2");
    await page.getByTestId("danger-trigger").click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    // 共有 Button の danger variant（bg-danger-emphasis）。実際の塗りも fixture の danger ボタンと一致する
    const dialogDelete = dialog.getByRole("button", { name: "削除する" });
    await expect(dialogDelete).toHaveClass(/(^|\s)bg-danger-emphasis(\s|$)/);
    await expect(dialogDelete).toHaveCSS("background-color",
      await page.getByTestId("sm-danger").evaluate(el => getComputedStyle(el).backgroundColor));
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    const overflow = page.getByRole("button", { name: "対象の操作", exact: true });
    await overflow.click();
    // 共有 Button の tone="danger"（text-danger-fg）。文字色は secondary + tone=danger のトリガーと同じ
    const menuDelete = page.getByRole("menuitem", { name: "削除", exact: true });
    await expect(menuDelete).toHaveClass(/(^|\s)text-danger-fg(\s|$)/);
    await expect(menuDelete).toHaveCSS("color",
      await page.getByTestId("danger-trigger").evaluate(el => getComputedStyle(el).color));
    await page.keyboard.press("Escape");
    await expect(overflow).toBeFocused();
    const form = page.getByTestId("form-actions");
    for (const action of await form.locator("button, a").all()) {
      await expect(action).toHaveCSS("height", mobile ? "44px" : "40px");
      expect(await action.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
      if (mobile) expect((await action.boundingBox())?.width).toBe((await form.boundingBox())?.width);
    }
    await form.getByRole("button", { name: "その他の操作" }).click();
    const formDelete = page.getByRole("menuitem", { name: "保存済みの内容を削除" });
    await expect(formDelete).toHaveCSS("height", mobile ? "44px" : "32px");
    expect(await formDelete.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "保存通知", exact: true }).click();
    const notifications = page.getByRole("region", { name: "通知", exact: true });
    await expect(notifications).toContainText("保存しました");
    await expect(notifications.getByRole("button", { name: "閉じる" })).toHaveCSS("height", "44px");
    await notifications.getByRole("button", { name: "閉じる" }).click();
    await expect(notifications.getByRole("button")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`buttons-${theme}.png`), fullPage: true });
    await testInfo.attach(`buttons-${theme}`, { path: testInfo.outputPath(`buttons-${theme}.png`), contentType: "image/png" });
  });
}
