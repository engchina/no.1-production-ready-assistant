import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

import { APP_ROUTES, AUTH_ROUTE_PATHS } from "../src/lib/routes";
import { findEscapedAbsoluteElements, measureShellHiddenOverflow } from "../../../platform/packages/ui/e2e/layout-checks";

const CONTENT_MAX_WIDTH = 1440;

/** PageHeader の計測枠と、その直後の PageBody の位置・幅を返す。 */
async function measureLayout(page: Page) {
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  return page.evaluate(() => {
    const header = document.querySelector("main header");
    const headerMeasure = header?.firstElementChild;
    const body = header?.nextElementSibling;
    const main = document.querySelector("main");
    if (!headerMeasure || !body || !main) {
      throw new Error("PageHeader / PageBody が見つかりません");
    }
    const h = headerMeasure.getBoundingClientRect();
    const b = body.getBoundingClientRect();
    return {
      headerLeft: Math.round(h.left),
      headerWidth: Math.round(h.width),
      bodyLeft: Math.round(b.left),
      bodyWidth: Math.round(b.width),
      mainWidth: main.clientWidth,
      documentOverflow: document.documentElement.scrollHeight - window.innerHeight,
    };
  });
}

// Agent の全画面は画面幅いっぱい（共有 PageHeader / PageBody の `wide`）で統一する（#28。#13 の Run・監査だけの wide を置き換え）。
test.describe("画面幅（PageHeader / PageBody の wide）", { tag: "@responsive" }, () => {
  for (const { width, height } of [
    { width: 1920, height: 1080 },
    { width: 2560, height: 1200 },
  ]) {
    test(`${width}px で全画面の本文が 1440px を超えて広がり、タイトルと本文の左端がそろう`, async ({ page }) => {
      test.setTimeout(90_000);
      await page.setViewportSize({ width, height });

      // ログイン・パスワード変更・権限なしは AppShell の外の画面なので対象外（#215）。
      for (const route of Object.values(APP_ROUTES).filter((path) => !AUTH_ROUTE_PATHS.includes(path))) {
        await page.goto(route);
        const layout = await measureLayout(page);
        const label = `${route} ${JSON.stringify(layout)}`;

        expect(layout.headerLeft, label).toBe(layout.bodyLeft);
        expect(layout.headerWidth, label).toBe(layout.bodyWidth);
        expect(layout.bodyWidth, label).toBe(layout.mainWidth);
        expect(layout.bodyWidth, label).toBeGreaterThan(CONTENT_MAX_WIDTH);
        expect(layout.documentOverflow, label).toBeLessThanOrEqual(1);
        // AppShell の外枠に隠れたあふれが無い（本文の sr-only などが外枠を包含ブロックにすると、外枠ごとずれて
        // 本文の下端が画面の外に切れる。#2064）。
        const shell = await measureShellHiddenOverflow(page);
        expect(shell.vertical, `${route} 外枠の縦の隠れたあふれ`).toBeLessThanOrEqual(1);
        expect(shell.horizontal, `${route} 外枠の横の隠れたあふれ`).toBeLessThanOrEqual(1);
        // スクロール領域から抜け出した absolute の要素（sr-only など）が本文の末尾に空白を作らない（#2064）。
        expect(await findEscapedAbsoluteElements(page), `${route} スクロール領域から抜け出した absolute の要素`).toEqual([]);
      }
    });
  }
});

test.describe("操作前の案内", { tag: "@responsive" }, () => {
  test("MCP 接続の未設定は、ツールを取得できない理由として関連付ける", async ({ page }) => {
    await page.goto("/settings/mcp-connections?id=rag");

    const hint = page.getByText("接続を使えるように設定すると、ツールを取得できます。");
    await expect(hint).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "接続を使えるように設定すると" })).toHaveCount(0);

    const fetchTools = page.getByRole("button", { name: "ツールを取得" });
    await expect(fetchTools).toBeDisabled();
    await expect(fetchTools).toHaveAccessibleDescription("接続を使えるように設定すると、ツールを取得できます。");
  });
});
