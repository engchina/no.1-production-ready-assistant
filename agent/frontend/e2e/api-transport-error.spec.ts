import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

// #906: 通信断（ブラウザの `TypeError: Failed to fetch`）のとき、英語の文をそのまま出さず、
// 利用者向けの日本語の文（何が起きたか + 次の操作）を出し、元の文は「詳細」にだけ出す。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

/** TanStack Query の既定の再試行（3 回・計 7 秒）を待つ。 */
const AFTER_RETRIES = { timeout: 20_000 };

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** 要約・次の操作は日本語で、英語の元の文は開いた「詳細」の中にだけある。 */
async function expectNetworkFailure(failure: Locator, request: string) {
  await expect(failure).toContainText("サーバーに接続できませんでした。");
  await expect(failure).toContainText("ネットワークの接続とサーバーの起動状態を確かめてから");
  const details = failure.locator("details");
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText(request);
  await expect(details).toContainText("TypeError");
  const outside = await failure.evaluate((element) => {
    const clone = element.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("details").forEach((node) => node.remove());
    return clone.textContent ?? "";
  });
  expect(outside).not.toMatch(/Failed to fetch|TypeError|NetworkError/u);
}

for (const viewport of VIEWPORTS) {
  test(`業務 Agent の一覧の取得が通信断で失敗したら、日本語の文と再試行と「詳細」を出す (${viewport.name})`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route(
      (url) => url.pathname === "/api/agents",
      (route) => (route.request().method() === "GET" ? route.abort("failed") : route.fallback())
    );
    await page.goto("/agents");

    const failure = page.getByRole("alert").filter({ hasText: "サーバーに接続できませんでした。" }).first();
    await expect(failure).toBeVisible(AFTER_RETRIES);
    await expectNetworkFailure(failure, "GET /api/agents");
    await expect(failure.getByRole("button", { name: "再試行" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agents-network-${viewport.name}.png`), fullPage: true });
  });

  test(`テンプレートの取得が通信断で失敗したら、Banner に日本語の文と「詳細」を出す (${viewport.name})`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route("**/api/agent-templates", (route) => route.abort("failed"));
    await page.goto("/agents?id=new");

    await expect(page.getByRole("heading", { name: "テンプレートから始める" })).toBeVisible();
    const failure = page.getByRole("alert").filter({ hasText: "サーバーに接続できませんでした。" });
    await expect(failure).toBeVisible(AFTER_RETRIES);
    await expectNetworkFailure(failure, "GET /api/agent-templates");
    await expectNoHorizontalOverflow(page);
    await failure.screenshot({ path: testInfo.outputPath(`agent-templates-network-${viewport.name}.png`) });
  });
}
