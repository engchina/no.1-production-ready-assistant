import { expect, test } from "./_helpers/test";

import { expectedControlHeight } from "./_helpers/control-height";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

for (const theme of ["light", "dark"]) {
  test(`${theme}: 実行確認語の統一表示・状態・キーボード操作`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.route("**/__execution-confirmation**", route => route.fulfill({
      contentType: "text/html",
      body: `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/execution-confirmation.tsx"></script></body></html>`,
    }));
    await page.goto("/__execution-confirmation");
    const field = page.getByTestId("execution-confirmation-field");
    const input = field.getByRole("textbox", { name: "実行確認語" });
    const run = field.getByRole("button", { name: "SQL 実行", exact: true });
    const clear = field.getByRole("button", { name: "SQL 入力・結果をリセット" });
    await expect(input).toBeVisible();
    await expect(input).toHaveAttribute("aria-required", "true");
    await expect(input).toHaveAccessibleDescription(/非 SELECT/);
    await expect(field.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
    await expect(field.getByText("未入力", { exact: true })).toBeVisible();
    await expect(run).toBeDisabled();

    const styles = await field.evaluate(el => {
      const label = el.querySelector("label")!;
      const helper = el.querySelector("p")!;
      const input = el.querySelector("input")!;
      const actions = el.lastElementChild!;
      const expected = label.parentElement!.lastElementChild!;
      const style = getComputedStyle(el);
      return {
        label: getComputedStyle(label).color,
        helper: getComputedStyle(helper).color,
        danger: style.getPropertyValue("--color-danger-fg").trim(),
        muted: style.getPropertyValue("--color-fg-muted").trim(),
        fg: style.getPropertyValue("--color-fg").trim(),
        background: style.backgroundColor,
        inputBackground: getComputedStyle(input).backgroundColor,
        inputHeight: input.getBoundingClientRect().height,
        labelY: label.getBoundingClientRect().y,
        expectedY: expected.getBoundingClientRect().y,
        actionsY: actions.getBoundingClientRect().y,
        helperBottom: helper.getBoundingClientRect().bottom,
        separator: getComputedStyle(actions).borderTopWidth,
        direction: getComputedStyle(actions).flexDirection,
      };
    });
    const resolveColor = (color: string) => page.evaluate(value => {
      const el = document.createElement("span");
      el.style.color = value;
      document.body.append(el);
      const resolved = getComputedStyle(el).color;
      el.remove();
      return resolved;
    }, color);
    const dangerColor = await resolveColor(styles.danger);
    // 入力前は danger 色にしない（操作前のエラー表示に見えるため）。ラベルは通常の文字色、説明は補助色。
    expect(styles.label).toBe(await resolveColor(styles.fg));
    expect(styles.helper).toBe(await resolveColor(styles.muted));
    expect(styles.background).not.toBe(styles.inputBackground);
    // 確認語の欄は下の操作行（lg）と同じ lg（タッチ端末は 44px。#613）。
    expect(styles.inputHeight).toBe(await expectedControlHeight(page, "lg"));
    expect(styles.actionsY).toBeGreaterThanOrEqual(styles.helperBottom);
    expect(styles.separator).toBe("1px");
    const mobile = testInfo.project.name === "mobile-375";
    expect(styles.direction).toBe(mobile ? "column" : "row");
    if (!mobile) expect(Math.abs(styles.labelY - styles.expectedY)).toBeLessThan(8);
    await field.screenshot({ path: testInfo.outputPath(`confirmation-${theme}.png`) });

    await input.focus();
    await input.pressSequentially("wrong");
    await expect(field.getByText("不一致", { exact: true })).toBeVisible();
    await expect(input).toHaveAttribute("aria-invalid", "true");
    // 一致しない語を入力したときだけ、説明文を danger 色にする。
    await expect(field.locator("p").first()).toHaveCSS("color", dangerColor);
    await expect(run).toBeDisabled();
    await input.fill("ADMIN_EXECUTE");
    await expect(field.getByText("確認済み", { exact: true })).toBeVisible();
    await expect(input).not.toHaveAttribute("aria-invalid", "true");
    await input.press("Tab");
    await expect(run).toBeFocused();
    await run.press("Tab");
    await expect(clear).toBeFocused();
    await clear.press("Enter");
    await expect(input).toHaveValue("");
    await expect(run).toBeDisabled();
    await input.fill("ADMIN_EXECUTE");
    await run.click();
    await expect(run).toHaveAttribute("aria-busy", "true");
    await expect(input).toBeDisabled();
    await expect(clear).toBeDisabled();

    await page.goto(`/__execution-confirmation?phrase=${"LONG_TABLE_NAME_".repeat(8)}`);
    await expect(input).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(field.getByText("未入力", { exact: true })).toBeVisible();
  });
}
