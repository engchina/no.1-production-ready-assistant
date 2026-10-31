import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

// #780: 業種テンプレートから業務 Agent を作る。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`テンプレートを選ぶとフォームに入り、そのまま作成できる (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/agents?id=new");

      await expect(page.getByRole("heading", { name: "テンプレートから始める" })).toBeVisible();
      const template = page.getByTestId("agent-template-sales-analytics");
      await expect(template).toHaveAttribute("aria-pressed", "false");
      await template.click();

      await expect(template).toHaveAttribute("aria-pressed", "true");
      await expect(template).toContainText("適用中");
      await expect(page.getByText("「営業分析」をフォームに入れました")).toBeVisible();
      await expect(page.locator("#new-agent-name")).toHaveValue("営業分析");
      await expect(page.locator("#new-agent-instructions")).toHaveValue(/構造化データ照会で集計し/);
      await expect(page.getByRole("option", { name: /^構造化データ照会/ })).toBeChecked();
      await expect(page.getByRole("option", { name: /^業務 RAG 調査/ })).not.toBeChecked();
      await expect(page.getByTestId("agent-template-samples")).toContainText("今月の地域別の売上を教えてください。");
      // テンプレートの評価ケースで評価セットを作る（既定はオン。#810）。
      await expect(page.getByRole("checkbox", { name: /テンプレートの評価ケース（1 件）で評価セットを作る/ })).toBeChecked();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`agent-templates-${viewport.name}-${theme}.png`), fullPage: true });

      await page.getByRole("button", { name: "作成", exact: true }).first().click();
      await expect(page.getByText("Agent を作成しました")).toBeVisible();
      const body = mockApi.lastRequest("POST", "/api/agents")?.body as Record<string, unknown>;
      expect(body.name).toBe("営業分析");
      expect(body.skill_ids).toEqual(["structured-data-query"]);
      expect(String(body.instructions)).toContain("あなたは営業企画の分析担当です。");
      expect(body.template_id).toBe("sales-analytics");
      await expect(page.getByText("評価セット「営業分析（テンプレート）」を作りました")).toBeVisible();
      expect(mockApi.lastRequest("POST", "/api/evaluation-sets/from-template")?.body).toEqual({
        agent_id: String(body.id ?? "agent-4"),
      });
      expect(mockApi.state.evaluationSets[0].cases).toEqual([
        {
          id: "case-1",
          question: "今月の地域別の売上は？",
          expected: "地域ごとの金額",
          expected_tools: [],
          expected_tools_in_order: false,
          clarification_answers: [],
          source_run_id: null,
        },
      ]);
    });
  }
}

test("入力した内容があるときは、確認してからテンプレートで置き換える", async ({ page }) => {
  await page.goto("/agents?id=new");
  await page.locator("#new-agent-name").fill("自分で考えた Agent");

  await page.getByTestId("agent-template-internal-policy-helpdesk").click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog.getByText("入力した内容をテンプレートで置き換えますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page.locator("#new-agent-name")).toHaveValue("自分で考えた Agent");

  await page.getByTestId("agent-template-internal-policy-helpdesk").click();
  await dialog.getByRole("button", { name: "置き換える" }).click();
  await expect(page.locator("#new-agent-name")).toHaveValue("社内規程の問い合わせ");
  await expect(page.getByRole("option", { name: /^業務 RAG 調査/ })).toBeChecked();
});

test("使えない Skill は外して知らせ、既存の業務 Agent の編集にはテンプレートを出さない", async ({ page }) => {
  await page.goto("/agents?id=new");
  await page.getByTestId("agent-template-manufacturing-quality").click();
  await expect(page.getByText("使えないスキルは外しました: quality_lab_only")).toBeVisible();
  await expect(page.getByRole("option", { name: /^業務 RAG 調査/ })).toBeChecked();

  await page.goto("/agents?id=default");
  await expect(page.getByRole("heading", { name: "資料・データ横断アシスタント", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "テンプレートから始める" })).toHaveCount(0);
});

test("テンプレートの評価ケースで評価セットを作らないこともできる（#810）", async ({ page, mockApi }) => {
  await page.goto("/agents?id=new");
  await page.getByTestId("agent-template-internal-policy-helpdesk").click();
  const checkbox = page.getByRole("checkbox", { name: /テンプレートの評価ケース（1 件）で評価セットを作る/ });
  await checkbox.uncheck();
  await page.getByRole("button", { name: "作成", exact: true }).first().click();
  await expect(page.getByText("Agent を作成しました")).toBeVisible();
  expect((mockApi.lastRequest("POST", "/api/agents")?.body as Record<string, unknown>).template_id).toBe(
    "internal-policy-helpdesk"
  );
  expect(mockApi.lastRequest("POST", "/api/evaluation-sets/from-template")).toBeUndefined();
});
