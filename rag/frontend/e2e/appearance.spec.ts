import { expect, test, type Page } from "./fixtures/test";

import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// 外観（テーマ切替）は platform の共有パッケージの画面を使う（#95）。

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockDatabaseReady(page);
});

async function isDark(page: Page) {
  return page.evaluate(() => document.documentElement.classList.contains("dark"));
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`外観と証明書でライト / ダーク / 自動を切り替え、再読込後も保持する (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/settings/appearance");

    await expect(page.getByRole("heading", { name: "外観と証明書", level: 1 })).toBeVisible();
    const toggle = page.getByTestId("appearance-theme-toggle");
    await expect(toggle.getByRole("button", { name: "ライト" })).toHaveAttribute("aria-pressed", "true");
    expect(await isDark(page)).toBe(false);

    await toggle.getByRole("button", { name: "ダーク" }).click();
    await expect(toggle.getByRole("button", { name: "ダーク" })).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => isDark(page)).toBe(true);

    await page.reload();
    await expect(page.getByTestId("appearance-theme-toggle").getByRole("button", { name: "ダーク" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect.poll(() => isDark(page)).toBe(true);

    // 自動は OS 設定に追従する
    await page.getByTestId("appearance-theme-toggle").getByRole("button", { name: "自動（OS 設定）" }).click();
    await expect.poll(() => isDark(page)).toBe(false);
    await page.emulateMedia({ colorScheme: "dark" });
    await expect.poll(() => isDark(page)).toBe(true);

    await expectNoPageOverflow(page);
  });
}

test("サイドナビのシステム設定に外観と証明書がある", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/settings/appearance");
  await expect(page.getByRole("link", { name: "外観と証明書" })).toHaveAttribute("href", "/settings/appearance");
});

// HTTPS の証明書（#1316）: 1 台の Compute の Nginx が /platform/ca.crt で CA の証明書を配る。製品は /<製品>/ の下でも
// サイトの root の /platform/ca.crt を見る。ローカルの開発（Vite）は配っていないので説明だけを出す。
for (const theme of ["ライト", "ダーク"] as const) {
  test(`外観と証明書の HTTPS の証明書（${theme}）`, { tag: "@responsive" }, async ({ page }) => {
    await page.goto("/settings/appearance");
    await page.getByTestId("appearance-theme-toggle").getByRole("button", { name: theme }).click();
    await expect(page.locator("html")).toHaveClass(theme === "ダーク" ? /dark/ : /^(?!.*\bdark\b).*$/);

    const card = page.getByTestId("appearance-ca-certificate");
    await expect(card.getByText("HTTPS の証明書", { exact: true })).toBeVisible();
    await expect(card.getByText("この環境では HTTPS の自己署名の証明書を使っていません。")).toBeVisible();
    await expect(card.getByRole("link", { name: "CA の証明書をダウンロード" })).toHaveCount(0);

    await page.route("**/platform/ca.crt", (route) =>
      route.fulfill({ status: 200, headers: { "Content-Type": "application/x-x509-ca-cert" }, body: "" }),
    );
    await page.reload();
    await expect(card.getByRole("link", { name: "CA の証明書をダウンロード" })).toHaveAttribute("href", "/platform/ca.crt");
    await card.getByText("端末への取り込み方").click();
    // OS の見出しは使うブラウザを添え、ブラウザの項は Edge → Chrome → Firefox の順（#1662）。
    await expect(card.getByTestId("appearance-ca-certificate-steps").locator("dt")).toHaveText([
      "Windows（Edge・Chrome）",
      "macOS（Safari・Chrome・Edge）",
      "iPhone / iPad（すべてのブラウザ）",
      "Android（Chrome）",
      "Edge",
      "Chrome",
      "Firefox",
    ]);
    await expect(card.getByText("Firefox", { exact: true })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
