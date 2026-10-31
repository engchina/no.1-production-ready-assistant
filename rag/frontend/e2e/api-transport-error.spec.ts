import { expect, type Locator, type Route, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// Issue 906: 通信断（ブラウザの `TypeError: Failed to fetch`）・timeout のとき、英語の文をそのまま出さず、
// 利用者向けの日本語の文（何が起きたか + 次の操作）を出し、元の文は「詳細」にだけ出す。
// desktop / mobile（375px）の両 project で動く。

/** TanStack Query の既定の再試行（3 回・計 7 秒）を待つ。 */
const AFTER_RETRIES = { timeout: 25_000 };

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

/** 要約・次の操作は日本語で、ブラウザの英語の文は開いた「詳細」の中にだけある。 */
async function expectEnglishOnlyInDetails(failure: Locator, request: string) {
  const details = failure.locator("details");
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText(request);
  const outside = await failure.evaluate((element) => {
    const clone = element.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("details").forEach((node) => node.remove());
    return clone.textContent ?? "";
  });
  expect(outside).not.toMatch(/Failed to fetch|signal timed out|aborted|TypeError|AbortError|NetworkError/u);
}

test("ナレッジベースの一覧の取得が通信断で失敗したら、日本語の文と再試行と「詳細」を出す", async ({ page }) => {
  test.setTimeout(60_000);
  await page.route("**/api/knowledge-bases**", (route) => route.abort("failed"));

  await page.goto("/knowledge-bases");

  const failure = page.getByRole("alert").filter({ hasText: "サーバーに接続できませんでした。" });
  await expect(failure).toBeVisible(AFTER_RETRIES);
  await expect(failure).toContainText("ネットワークの接続とサーバーの起動状態を確かめてから");
  await expectEnglishOnlyInDetails(failure, "GET /api/knowledge-bases");
  await expect(failure.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("ナレッジベースの一覧の取得が画面の待ち時間の上限を超えたら、秒数と次の操作を日本語で出す", async ({ page }) => {
  test.setTimeout(60_000);
  await page.addInitScript(() => {
    (window as unknown as { __RAG_API_TIMEOUT_MS__: number }).__RAG_API_TIMEOUT_MS__ = 1_000;
  });
  const held: Route[] = [];
  await page.route("**/api/knowledge-bases**", (route) => {
    held.push(route);
  });

  await page.goto("/knowledge-bases");

  const failure = page.getByRole("alert").filter({ hasText: "サーバーの応答が 1 秒以内に返りませんでした。" });
  await expect(failure).toBeVisible(AFTER_RETRIES);
  await expect(failure).toContainText("少し待ってから画面を更新して結果を確かめ");
  await expectEnglishOnlyInDetails(failure, "GET /api/knowledge-bases");
  await expect(failure.locator("details")).toContainText("1 秒");
  await expectNoPageOverflow(page);
  await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
});
