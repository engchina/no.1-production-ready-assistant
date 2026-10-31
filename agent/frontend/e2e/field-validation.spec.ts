import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

// #540 / #541: 入力のエラーは欄の直下（FieldError）に出し、送信時は最初のエラーの欄へフォーカスする。
// 押せないボタンだけで済ませず、空の数値を 0 として送らない。文言は「〇〇を入力してください。」などの型。

async function expectFieldError(page: Page, fieldId: string, message: string) {
  const field = page.locator(`#${fieldId}`);
  await expect(field).toHaveAttribute("aria-invalid", "true");
  // エラーは aria-describedby で結んだ要素（共有の TextField / TextareaField は `<id>-<生成 id>-error`。#584 / #631）。
  await expect(field).toHaveAttribute("aria-describedby", new RegExp(`${fieldId}-\\S*error`));
  const describedBy = (await field.getAttribute("aria-describedby")) ?? "";
  const errorId = describedBy.split(/\s+/).find((id) => id.startsWith(`${fieldId}-`) && id.endsWith("-error"));
  const error = page.locator(`[id="${errorId}"]`);
  await expect(error).toHaveText(message);
  await expect(error).toHaveAttribute("role", "alert");
}

async function expectNoHorizontalOverflow(page: Page) {
  const hasNoOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth
  );
  expect(hasNoOverflow).toBe(true);
}

function patchCount(mockApi: { requests: Array<{ method: string; path: string }> }, suffix: string, method = "PATCH") {
  return mockApi.requests.filter((request) => request.method === method && request.path.endsWith(suffix)).length;
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test.describe(`欄のエラー (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("Run のゴールは必須で、未入力は欄の下に出してフォーカスする", async ({ page, mockApi }) => {
      await page.goto("/runs?id=new");
      const goal = page.locator("#run-goal");
      await expect(goal).toHaveAttribute("aria-required", "true");
      await expect(page.locator('label[for="run-goal"]')).toContainText("必須");
      await goal.fill("   ");
      await page.getByRole("button", { name: "実行を作成" }).click();

      await expectFieldError(page, "run-goal", "目標を入力してください。");
      await expect(goal).toBeFocused();
      expect(patchCount(mockApi, "/api/runs", "POST")).toBe(0);
      await expectNoHorizontalOverflow(page);
    });

    test("MCP 接続のタイムアウトは必須で、空を 0 として保存しない", async ({ page, mockApi }) => {
      await page.goto("/settings/mcp-connections?id=new");
      await page.locator("#mcp-server-id").fill("crm");
      await page.locator("#mcp-server-timeout").fill("");
      await page.getByRole("button", { name: "作成" }).click();

      await expectFieldError(page, "mcp-server-timeout", "タイムアウト秒を入力してください。");
      await expect(page.locator("#mcp-server-timeout")).toBeFocused();
      await page.locator("#mcp-server-timeout").fill("901");
      await page.getByRole("button", { name: "作成" }).click();
      await expectFieldError(page, "mcp-server-timeout", "タイムアウト秒は 0 より大きく 900 以下の数値を入力してください。");
      expect(patchCount(mockApi, "/settings/mcp-connections", "POST")).toBe(0);
      await expectNoHorizontalOverflow(page);
    });

    test("Skill の JSON の形式エラーは欄の下に出す", async ({ page, mockApi }) => {
      await page.goto("/skills?id=new");
      await page.locator("#skill-id").fill("e2e_json");
      await page.locator("#skill-name").fill("JSON 確認");
      await page.locator("#skill-mcp-requirements").fill("{");
      await page.getByRole("button", { name: "作成" }).click();

      await expectFieldError(page, "skill-mcp-requirements", "MCP 依存 (JSON) は有効な JSON で入力してください。");
      await expect(page.locator("#skill-mcp-requirements")).toBeFocused();
      expect(patchCount(mockApi, "/api/skills", "POST")).toBe(0);
    });
  });
}
