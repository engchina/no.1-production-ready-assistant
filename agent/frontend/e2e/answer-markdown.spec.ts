import type { Locator, Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1808: チャットと実行の詳細の回答の Markdown（見出し・太字・斜体・箇条書き・番号付き・インラインコード・コードブロック・
// リンク・引用）を、記号のまま出さずに共通の部品（MarkdownText）で描く。表は今までどおり共通の結果の表（#1158）。
// 生の HTML は要素にせず文字のまま出す。

const THREAD_ID = `thread_${"c".repeat(32)}`;
const RUN_ID = "run-answer-markdown";
const QUESTION = "この関数の構成を教えて";
const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];
const LONG_CODE_LINE = `result = build_pipeline(${"argument_with_a_long_name, ".repeat(8)}final_argument)`;
const ANSWER = [
  "**関数シグネチャを解析して、呼び出しの流れをまとめました。**",
  "",
  "### 主な構成要素",
  "",
  "| 要素 | 役割 |",
  "|---|---|",
  "| `parse_signature` | 引数の型を読む |",
  "| `dispatch` | 呼び先を選ぶ |",
  "",
  "### フロー概要",
  "",
  "1. `parse_signature` で引数を読む",
  "2. *必要なら* 既定値を補う",
  "   - 型が合わないときは例外にする",
  "3. 結果を返す",
  "",
  "```python",
  LONG_CODE_LINE,
  "```",
  "",
  "> 注意: 非同期の呼び出しには対応していません。",
  "",
  "詳しくは [設計の資料](https://example.com/docs/design) を参照してください。",
  "",
  '<script>window.__xss = 1</script><img src="x" onerror="window.__xss = 2">',
].join("\n");

function seedRun(mockApi: MockApi) {
  mockApi.state.runs.push({
    id: RUN_ID,
    goal: QUESTION,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [{ id: "answer", name: "回答", kind: "answer", content: { text: ANSWER }, created_at: MOCK_NOW }],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** 回答の Markdown が要素で描かれ、記号・生の HTML が要素にならないことを確かめる。 */
async function expectMarkdownAnswer(page: Page, answer: Locator, testId: string, headingLevel: number) {
  await expect(answer.locator("strong").first()).toHaveText("関数シグネチャを解析して、呼び出しの流れをまとめました。");
  await expect(answer.getByRole("heading", { level: headingLevel, name: "主な構成要素" })).toBeVisible();
  await expect(answer.getByRole("heading", { level: headingLevel, name: "フロー概要" })).toBeVisible();
  // 表は今までどおり共通の結果の表。セルの中の `code` は文字のまま（表の部品は Markdown を描かない）。
  await expect(answer.getByTestId(`${testId}-table-1-summary`)).toContainText("2 行");
  // 番号付き（入れ子の箇条書きを含む）・斜体・インラインコード。
  const ordered = answer.locator("ol").first();
  await expect(ordered.locator(":scope > li")).toHaveCount(3);
  await expect(ordered.locator("ul > li")).toHaveText("型が合わないときは例外にする");
  await expect(ordered.locator("em")).toHaveText("必要なら");
  await expect(ordered.locator("code").first()).toHaveText("parse_signature");
  // コードブロックは枠の中で横にスクロールする（ページは横にはみ出さない）。
  const code = answer.locator("pre[data-language='python']");
  await expect(code).toHaveText(LONG_CODE_LINE);
  await expect.poll(() => code.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await expect(answer.locator("blockquote")).toHaveText("注意: 非同期の呼び出しには対応していません。");
  const link = answer.getByRole("link", { name: /設計の資料/ });
  await expect(link).toHaveAttribute("href", "https://example.com/docs/design");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  // 記号のまま出さない。生の HTML は文字のまま（要素にしない・実行しない）。
  await expect(answer).not.toContainText("**");
  await expect(answer).not.toContainText("###");
  await expect(answer).not.toContainText("```");
  await expect(answer).toContainText("<script>window.__xss = 1</script>");
  await expect(answer.locator("script, img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as { __xss?: number }).__xss)).toBeUndefined();
}

for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`チャットの回答の Markdown を記号のまま出さずに描く (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      seedRun(mockApi);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      await page.getByTestId("chat-history-toggle").click();
      if (viewport.width >= 1024) {
        await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${QUESTION}`) }).click();
      } else {
        await page.getByRole("dialog", { name: "会話の履歴" }).getByText(QUESTION).click();
      }
      const answer = page.getByTestId(`chat-answer-${RUN_ID}`);
      // 回答の本文の前に見出し「回答」（h3。会話の名前 h2 の下。#3433）を出し、回答の中の見出しはその下の h4 から。
      const body = page.getByTestId(`chat-answer-body-${RUN_ID}`);
      await expect(body.getByRole("heading", { level: 3, name: "回答", exact: true })).toBeVisible();
      await expectMarkdownAnswer(page, answer, `chat-answer-${RUN_ID}`, 4);
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`chat-${viewport.name}-${theme}.png`), fullPage: true });
    });

    test(`実行の詳細の回答の Markdown を記号のまま出さずに描く (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      seedRun(mockApi);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto(`/runs?id=${RUN_ID}`);
      const answer = page.getByTestId("run-answer-answer");
      // カードの見出し（h2）の下なので、回答の見出しは h3。
      await expectMarkdownAnswer(page, answer, "run-answer-answer", 3);
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`run-${viewport.name}-${theme}.png`), fullPage: true });
    });
  }
}
