import { expect, type Page, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// #535: 一覧の絞り込みの検索は 3 製品で同じ部品（SearchField）と規則で動く。
// 入力に合わせて絞り込み（debounce）、検索ボタンを置かない、IME の変換中は絞り込まない、0 件は「検索語をクリア」。

const now = "2026-06-19T00:00:00Z";

const searchAnswerProfiles = [
  { id: "bv-1", name: "経理ビュー", description: "経費精算の相談" },
  { id: "bv-2", name: "人事ビュー", description: "休暇と勤怠" },
  { id: "bv-3", name: "情報システム", description: "アカウント申請" },
].map((item) => ({
  ...item,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: now,
  updated_at: now,
  archived_at: null,
}));

const knowledgeBases = [
  { id: "kb-1", name: "社内規程", description: "経費・人事・情報管理" },
  { id: "kb-2", name: "設計資料", description: "設計レビュー" },
].map((item) => ({
  ...item,
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 1,
  indexed_document_count: 1,
  error_document_count: 0,
  searchable_chunk_count: 4,
  created_at: now,
  updated_at: now,
  archived_at: null,
}));

function envelope(data: unknown) {
  return { json: { data, error_messages: [], warning_messages: [] } };
}

/** 一覧 API を検索語（q）で絞って返し、受けた検索語を記録する。 */
async function mockList(
  page: Page,
  pattern: string,
  items: { name: string; description: string | null }[]
): Promise<string[]> {
  const queries: string[] = [];
  await page.route(pattern, async (route) => {
    const url = new URL(route.request().url());
    const q = url.searchParams.get("q") ?? "";
    queries.push(q);
    const matched = items.filter((item) => !q || `${item.name} ${item.description ?? ""}`.includes(q));
    await route.fulfill(envelope({ items: matched, total: matched.length, limit: 50, offset: 0, has_next: false }));
  });
  return queries;
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

/**
 * IME の変換中の入力を再現する（compositionstart → isComposing の input → compositionend）。
 * Playwright の keyboard は IME を通さないため、React が受ける DOM event を直接出す。
 */
async function composeJapanese(page: Page, selector: string, steps: string[]) {
  await page.evaluate(
    ({ selector, steps }) => {
      const input = document.querySelector<HTMLInputElement>(selector)!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      input.focus();
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "" }));
      for (const step of steps) {
        setter.call(input, step);
        input.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true, data: step }));
      }
      // 変換を確定する Enter（isComposing=true）。
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: true }));
    },
    { selector, steps }
  );
}

async function endComposition(page: Page, selector: string, committed: string) {
  await page.evaluate(
    ({ selector, committed }) => {
      const input = document.querySelector<HTMLInputElement>(selector)!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, committed);
      input.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true, data: committed }));
      input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: committed }));
    },
    { selector, committed }
  );
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

for (const theme of ["light", "dark"] as const) {
  test(`検索・回答プロファイルの一覧は検索ボタンを置かず、入力に合わせて絞り込む (${theme})`, async ({ page }, testInfo) => {
    await useTheme(page, theme);
    await mockList(page, "**/api/knowledge-bases**", knowledgeBases);
    const queries = await mockList(page, "**/api/search-answer-profiles**", searchAnswerProfiles);
    await page.goto("/search-answer-profiles");

    const search = page.getByRole("searchbox", { name: "名前・説明で検索" });
    await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();
    // 検索ボタン（旧「名前・説明で検索」のボタン）はない。
    await expect(page.getByRole("button", { name: "名前・説明で検索" })).toHaveCount(0);

    await search.pressSequentially("経理", { delay: 30 });
    await expect(page.getByTestId("search-answer-profile-row-bv-2")).toHaveCount(0);
    await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();
    // 1 文字ずつではなく、入力が止まってから確定した値で問い合わせる。
    expect(queries.filter((q) => q !== "")).toEqual(["経理"]);
    await expect(page.getByRole("status").filter({ hasText: "1 件が一致しました" })).toHaveCount(1);
    await expectNoPageOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`search-answer-profiles-search-${testInfo.project.name}-${theme}.png`),
    });

    // 0 件は空の状態と「検索語をクリア」。押すと元の一覧へ戻る。
    await search.fill("該当なし");
    await expect(page.getByText("検索に一致する検索・回答プロファイルがありません")).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`search-answer-profiles-search-empty-${testInfo.project.name}-${theme}.png`),
    });
    await page.getByRole("main").getByRole("button", { name: "検索語をクリア" }).last().click();
    await expect(search).toHaveValue("");
    await expect(page.getByTestId("search-answer-profile-row-bv-3")).toBeVisible();
  });
}

test("検索・回答プロファイルの一覧は IME の変換中に絞り込まず、確定した値で絞り込む", async ({ page }) => {
  await mockList(page, "**/api/knowledge-bases**", knowledgeBases);
  const queries = await mockList(page, "**/api/search-answer-profiles**", searchAnswerProfiles);
  await page.goto("/search-answer-profiles");
  await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();

  await composeJapanese(page, "#search-answer-profile-search", ["j", "じ", "じん", "じんじ", "人事"]);
  await page.waitForTimeout(700);
  // 変換中の読み（じ・じん…）でも、確定の Enter でも問い合わせない。
  expect(queries.filter((q) => q !== "")).toEqual([]);
  await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();

  await endComposition(page, "#search-answer-profile-search", "人事");
  await expect(page.getByTestId("search-answer-profile-row-bv-1")).toHaveCount(0);
  await expect(page.getByTestId("search-answer-profile-row-bv-2")).toBeVisible();
  expect(queries.filter((q) => q !== "")).toEqual(["人事"]);
});

test("ナレッジベースの一覧も同じ部品で、入力に合わせて絞り込み、Enter はすぐ反映する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const queries = await mockList(page, "**/api/knowledge-bases**", knowledgeBases);
  await page.goto("/knowledge-bases");
  const search = page.getByRole("searchbox", { name: "名前・説明で検索" });
  await expect(page.getByRole("link", { name: "設計資料" })).toBeVisible();

  await search.fill("設計");
  await search.press("Enter");
  await expect(page.getByRole("link", { name: "社内規程" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "設計資料" })).toBeVisible();
  expect(queries.filter((q) => q !== "")).toEqual(["設計"]);

  // 位置は一覧のツールバーの左端（先頭）。状態のチップはその右（狭い幅では下）に並ぶ
  // （page-archetypes.md「一覧のツールバー」。#600）。375px でもはみ出さない。
  const [box, toolbarBox, chipBox] = await Promise.all([
    search.boundingBox(),
    page.getByTestId("knowledge-base-list-toolbar").boundingBox(),
    page.getByRole("group", { name: "ナレッジベース状態フィルター" }).boundingBox(),
  ]);
  expect(Math.abs(box!.x - toolbarBox!.x)).toBeLessThanOrEqual(1);
  if (testInfo.project.name === "mobile") expect(chipBox!.y).toBeGreaterThan(box!.y);
  else expect(chipBox!.x).toBeGreaterThan(box!.x + box!.width - 1);
  await expectNoPageOverflow(page);

  // × で消すと debounce を待たずに元の一覧へ戻る。
  await page.getByRole("button", { name: "検索語をクリア" }).first().click();
  await expect(page.getByRole("link", { name: "社内規程" })).toBeVisible();
  await expect(search).toBeFocused();
});
