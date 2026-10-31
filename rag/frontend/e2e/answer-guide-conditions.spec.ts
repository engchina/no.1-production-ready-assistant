import { expect, type Locator, type Page, test } from "./fixtures/test";
import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  openAnswerProcessDetails,
  selectSearchAnswerProfile,
} from "./_helpers";

/**
 * 回答の詳細に業務ガイドの条件の状態（確かめた / 分からない / 質問から複数の候補が読み取れた）・
 * 適用範囲・読み込めなかったことを出し、引用に資料に印刷されたページ番号を物理頁と並べる（#1287）。
 * desktop / 375px は playwright の project、light / dark はテストごとに切り替える。
 */

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });

const summary = {
  id: "bv-1",
  name: "受注サポート",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-09-25T00:00:00Z",
  updated_at: "2026-09-25T00:00:00Z",
  archived_at: null,
};

const GUIDE = {
  guide_id: "guide-order-cancel",
  revision: 4,
  title: "受注の取消",
  decision: "branch",
  known_conditions: [
    { id: "order_type", label: "受注の種類", value: "通常受注", source: "question", state: "known" },
    { id: "shipped", label: "出荷済みか", value: "いいえ", source: "user", state: "known" },
    // 回答の根拠の画面の図の項目の種類で決めた条件（#2404）。
    { id: "field_kind", label: "項目の種類", value: "選択", source: "document", state: "known" },
  ],
  unknown_conditions: [
    {
      id: "contract",
      label: "契約の種類",
      handling: "branch",
      state: "unknown",
      candidates: [],
    },
    {
      id: "version",
      label: "システムの版",
      handling: "ask",
      state: "conflicting",
      candidates: ["v1", "v2"],
    },
  ],
  applicability: { business_domains: "matched", versions: "unverified" },
};

const citation = (chunkId: string, metadata: Record<string, unknown>) => ({
  document_id: "doc-1",
  chunk_id: chunkId,
  text: "受注一覧で対象の受注を選び、取消を押します。出荷済みの受注は取り消せません。",
  score: 0.05,
  rerank_score: 0.82,
  file_name: "受注マニュアル.pdf",
  category_name: null,
  metadata: { context_role: "evidence", content_kind: "text", ...metadata },
});

async function setTheme(page: Page, theme: "light" | "dark") {
  // 外観の選好（共有 UI の ui-store が保存する値）を読み込みの前に入れておく。
  await page.addInitScript((value) => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { theme: value }, version: 0 })
    );
  }, theme);
}

async function mockApi(page: Page, answer: Record<string, unknown>, citations: unknown[]) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/approved-faq/suggest")) {
      return route.fulfill({ json: envelope({ suggestions: [] }) });
    }
    return route.fulfill({
      json: envelope({ items: [summary], total: 1, limit: 50, offset: 0, has_next: false }),
    });
  });
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        `event: metadata\ndata: ${JSON.stringify({
          trace_id: "trace-guide",
          elapsed_ms: 20,
          guardrail_warnings: [],
          diagnostics: {
            search_answer_profile_applied: "bv-1",
            retrieval_strategy: "hybrid",
            answer: {
              confidence: "medium",
              execution_steps: [
                { name: "質問の理解", status: "complete", elapsed_seconds: 0.3, llm_calls: 1 },
              ],
              evidence_tree: [],
              ...answer,
            },
          },
        })}\n\n`,
        `event: delta\ndata: ${JSON.stringify({ text: "通常受注で未出荷なら、受注一覧で取消を押します。" })}\n\n`,
        `event: citations\ndata: ${JSON.stringify(citations)}\n\n`,
        `event: done\ndata: ${JSON.stringify({ trace_id: "trace-guide" })}\n\n`,
      ].join(""),
    })
  );
}

async function ask(page: Page) {
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /受注サポート/);
  await page.keyboard.press("Escape");
  await enableSearchAnswer(page);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("受注を取り消すには？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  // 回答の記録の詳細は、処理の経過の中の「処理の詳細」に畳む（#2096 / #2325）。
  const panel = await openAnswerProcessDetails(page, "search-run-progress");
  await expect(panel).toBeVisible();
  return panel;
}

async function screenshot(target: Locator, name: string, projectName: string) {
  if (!process.env.RAG_E2E_SCREENSHOT_DIR) return;
  await target.screenshot({ path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/${name}-${projectName}.png` });
}

for (const theme of ["light", "dark"] as const) {
  test(`回答の詳細に業務ガイドの条件の状態を、引用に資料の表記のページ番号を出す (${theme})`, async ({
    page,
  }, testInfo) => {
    await setTheme(page, theme);
    await mockApi(page, { outcome: "conditional", guide: GUIDE }, [
      citation("doc-1:cs_1:0", { page_start: 12, page_label_start: "3-4", page_label_end: "3-4" }),
      citation("doc-1:cs_1:1", { page_start: 5 }),
    ]);
    const panel = await ask(page);
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(theme === "dark");

    await expect(panel.getByTestId("answer-outcome-badge")).toHaveCount(0);
    await expect(panel.getByTestId("answer-guide-badge")).toHaveCount(0);
    await expect(panel.getByRole("region", { name: "業務ガイドの条件" })).toHaveCount(0);
    await expect(panel.locator("[data-run-used-models]")).toBeVisible();

    // 印刷のページ番号があれば物理頁と並べ、無ければ物理頁だけ。
    const pages = page.locator("[data-testid=citation-main] dd").filter({ hasText: /^p\./ });
    await expect(pages).toHaveText(["p.12（資料の表記: 3-4）", "p.5"]);
    await expectNoPageOverflow(page);
    await screenshot(panel, `answer-guide-conditions-${theme}`, testInfo.project.name);
    await screenshot(
      page.locator("li").filter({ has: page.getByTestId("citation-main") }).first(),
      `citation-page-label-${theme}`,
      testInfo.project.name
    );
  });
}

test("条件を持たない業務ガイドは名前と版だけを出し、条件の欄を出さない", async ({ page }) => {
  await mockApi(
    page,
    { outcome: "answered", guide: { guide_id: "g-old", revision: 1, title: "受注の取消" } },
    [citation("doc-1:cs_1:0", { page_start: 7 })]
  );
  const panel = await ask(page);
  await expect(panel.getByTestId("answer-guide-badge")).toHaveCount(0);
  await expect(panel.getByRole("region", { name: "業務ガイドの条件" })).toHaveCount(0);
  await expect(page.locator("[data-testid=citation-main] dd").filter({ hasText: /^p\./ })).toHaveText([
    "p.7",
  ]);
  await expectNoPageOverflow(page);
});

for (const theme of ["light", "dark"] as const) {
  test(`業務ガイドを読み込めずに回答したことを、ガイドが無いときと区別して出す (${theme})`, async ({
    page,
  }, testInfo) => {
    await setTheme(page, theme);
    await mockApi(page, { outcome: "answered", guide_load_failed: true }, [
      citation("doc-1:cs_1:0", { page_start: 2, page_end: 3, page_label_start: "ii", page_label_end: "iii" }),
    ]);
    const panel = await ask(page);
    // 回答を読むときに要る案内は「詳細」に畳まず、回答の本文の直下に出す（#2096）。
    const failed = page.getByTestId("answer-guide-load-failed");
    await expect(failed).toHaveCount(1);
    await expect(panel.getByTestId("answer-guide-load-failed")).toHaveCount(0);
    await expect(failed).toContainText("業務ガイドを読み込めませんでした");
    await expect(failed).toContainText("業務ガイドの条件を確かめずに、資料だけで回答しました。");
    await expect(panel.getByTestId("answer-guide-badge")).toHaveCount(0);
    await expect(panel.getByRole("region", { name: "業務ガイドの条件" })).toHaveCount(0);
    await expect(page.locator("[data-testid=citation-main] dd").filter({ hasText: /^p\./ })).toHaveText([
      "p.2-3（資料の表記: ii〜iii）",
    ]);
    await expectNoPageOverflow(page);
    await screenshot(panel, `answer-guide-load-failed-${theme}`, testInfo.project.name);
  });
}

test("診断の照合・CRAG の情報は処理の詳細に出さず、識別子とモデルに統一する", { tag: "@responsive" }, async ({ page }) => {
  await mockApi(page, { answer_validation: { status: "completed", valid: false }, crag_attempts: [{ sufficient: true, grade_execution: "not_run" }] }, []);
  const panel = await ask(page);
  await expect(panel.getByTestId("answer-crag-details")).toHaveCount(0);
  await expect(panel.getByTestId("answer-validation-status")).toHaveCount(0);
  await expect(panel.locator("[data-process-identifier]")).toHaveCount(3);
  await expect(panel.locator("[data-run-used-models]")).toBeVisible();
  await expectNoPageOverflow(page);
});
