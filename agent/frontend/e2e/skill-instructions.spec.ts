import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

// #1795: 組み込みのスキルの指示は手順で進める Markdown。詳細画面で共有の MarkdownText（#1808）が見出し・箇条書き・`code` として表示し、
// Markdown の記号（## や - ）を文字のまま出さない。375px でも横にあふれない。

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test.describe("スキルの指示の表示", { tag: "@responsive" }, () => {
  test("組み込みのスキルの指示を見出し・入れ子の箇条書き・code で表示する", async ({ page }) => {
    await page.goto("/skills?id=business-rag-research");
    await expect(page.getByRole("heading", { name: "業務 RAG 調査", level: 1 })).toBeVisible();

    const view = page.getByTestId("skill-instructions-view");
    await expect(view).toBeVisible();
    // 画面の「指示」（Section の h2）の下に、## は h3、### は h4 で並ぶ。
    await expect(page.getByRole("heading", { name: "指示", level: 2 })).toBeVisible();
    await expect(view.getByRole("heading", { level: 3 })).toHaveText(["使う MCP ツール", "手順", "結果ごとの対応"]);
    await expect(view.getByRole("heading", { name: "Step 1: 検索の前に確かめる", level: 4 })).toBeVisible();
    await expect(view.getByRole("heading", { name: "Step 3: rag_search で答えを得る", level: 4 })).toBeVisible();

    // 「次へ進む」の行き先は入れ子の箇条書き、needs_clarification の手順は番号付き。
    const next = view.getByRole("listitem").filter({ hasText: /^次へ進む:/ });
    await expect(next.getByRole("listitem")).toHaveText([
      "複数の根拠をつないで初めて答えられる質問（多段の質問）→ Step 2",
      "それ以外 → Step 3",
    ]);
    const clarification = view.getByRole("listitem").filter({ hasText: /^needs_clarification:/ });
    await expect(clarification.locator("ol > li")).toHaveText([
      "答えを得たら conditions（条件の id → 値）に入れる。",
      "rag_search を呼び直す。",
    ]);

    // 識別子は code、Markdown の記号は文字のまま残らない。
    await expect(view.locator("code", { hasText: "rag_list_search_answer_profiles" }).first()).toBeVisible();
    await expect(view).not.toContainText("## ");
    await expect(view).not.toContainText("`");
    await expect(view).not.toContainText("- 対象");

    // 長い識別子（code）も枠の中で折り返す。
    const viewBox = await view.boundingBox();
    const codeBox = await view.locator("code", { hasText: "rag_list_search_answer_profiles" }).first().boundingBox();
    expect(viewBox && codeBox).toBeTruthy();
    if (viewBox && codeBox) expect(codeBox.x + codeBox.width).toBeLessThanOrEqual(viewBox.x + viewBox.width + 1);
    await expectNoHorizontalOverflow(page);
  });
});
