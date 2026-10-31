import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME } from "./_helpers";

// 本文へスキップ（platform の AppShell）。URL に #pr-main を付けず（履歴を増やさず）、本文へフォーカスを移す。
// 既定のページ内リンクの移動では履歴が 1 つ増え、離脱ガードの blocker が POP の警告を出していた。

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }
    await route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } });
  });
}

// desktop（1440px）と mobile（375px・タッチ）の project で実行する。
test("本文へスキップは URL と履歴を変えずに本文へフォーカスを移す", async ({ page }) => {
  const warnings: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "warning" || message.type() === "error") warnings.push(message.text());
  });
  await mockApi(page);
  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const url = page.url();
  const historyLength = await page.evaluate(() => window.history.length);

  // キーボード: 最初の Tab でリンクへ、Enter で本文へ。
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "本文へスキップ" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  const main = page.locator("main#pr-main");
  await expect(main).toHaveAttribute("tabindex", "-1");
  await expect(main).toBeFocused();
  expect(page.url()).toBe(url);
  expect(await page.evaluate(() => window.history.length)).toBe(historyLength);

  // マウスで押しても同じ。
  await skip.focus();
  await skip.click();
  await expect(main).toBeFocused();
  expect(page.url()).toBe(url);
  expect(await page.evaluate(() => window.history.length)).toBe(historyLength);
  expect(warnings.filter((text) => text.includes("POP navigation"))).toEqual([]);
});
