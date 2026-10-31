import { expect, type Page, type Route, test } from "./fixtures/test";

import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";

/**
 * ナレッジベースごとの項目抽出の定義（#548）と、抽出項目の値による検索の絞り込み（#549）。
 */

const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

const DEFAULT_FIELDS = [{ name: "請求書番号", description: "", value_type: "string" }];
const CONTRACT_FIELDS = [
  { name: "契約番号", description: "", value_type: "string" },
  { name: "金額", description: "契約金額", value_type: "number" },
  { name: "契約日", description: "", value_type: "date" },
  { name: "自動更新", description: "", value_type: "bool" },
];

const KB = {
  id: "kb-1",
  name: "契約書",
  description: "契約書の文書",
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 0,
  indexed_document_count: 0,
  error_document_count: 0,
  searchable_chunk_count: 0,
  created_at: "2026-06-15T00:00:00Z",
  updated_at: "2026-06-15T00:00:00Z",
  archived_at: null,
  retrieval_config: {},
  adapter_config: { version: 1, ingestion: {}, query: {} },
};

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

async function mockKnowledgeBase(page: Page, { delayMs = 0 } = {}) {
  let own: typeof CONTRACT_FIELDS | null = null;
  const puts: unknown[] = [];
  await page.route("**/api/documents**", (route) =>
    route.fulfill(envelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }))
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill(envelope({ items: [KB], total: 1, limit: 20, offset: 0, has_next: false }))
  );
  await page.route("**/api/knowledge-bases/kb-1", (route) => route.fulfill(envelope(KB)));
  await page.route("**/api/knowledge-bases/kb-1/extraction-fields", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as { fields: typeof CONTRACT_FIELDS | null };
      puts.push(body);
      own = body.fields;
    } else if (delayMs) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
    await route.fulfill(
      envelope({ inherits_default: own === null, fields: own ?? DEFAULT_FIELDS })
    );
  });
  return puts;
}

test(`KB の抽出する項目: 既定を使い、KB の定義を作って編集し、既定に戻す`, async ({
  page,
}) => {
  const puts = await mockKnowledgeBase(page);
  await page.goto("/knowledge-bases/kb-1");

  const section = page.getByTestId("knowledge-base-extraction-fields");
  await expect(section.getByRole("heading", { name: "抽出する項目" })).toBeVisible();
  await expect(section).toContainText("全体の既定の項目（1 件）を使っています");
  await expect(section.getByRole("list", { name: "抽出する項目" })).toContainText("請求書番号");
  await expectNoPageOverflow(page);

  await section.getByRole("button", { name: "既定をもとに項目を定義" }).click();
  await expect(section.getByTestId("knowledge-base-extraction-fields-editor")).toBeVisible();
  expect(puts).toEqual([{ fields: DEFAULT_FIELDS }]);

  // KB の定義を編集して保存する（全体の既定の編集欄と同じ部品）。
  await section.getByRole("button", { name: "項目を追加" }).click();
  await section.getByRole("textbox", { name: /項目名/ }).last().fill("契約日");
  await section.getByRole("combobox", { name: "値の型" }).last().click();
  await page.getByRole("option", { name: "日付" }).click();
  await section.getByRole("button", { name: "項目の定義を保存" }).click();
  await expect(section).toContainText("項目の定義を保存しました。");
  expect(puts.at(-1)).toEqual({
    fields: [
      { name: "請求書番号", description: "", value_type: "string" },
      { name: "契約日", description: "", value_type: "date" },
    ],
  });
  await expectNoPageOverflow(page);
  await section.screenshot({ path: test.info().outputPath("kb-extraction-fields.png") });

  await section.getByRole("button", { name: "全体の既定に戻す" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("全体の既定に戻しますか？");
  await dialog.getByRole("button", { name: "全体の既定に戻す" }).click();
  await expect(section).toContainText("全体の既定の項目（1 件）を使っています");
  expect(puts.at(-1)).toEqual({ fields: null });
});

test("KB の抽出する項目: 読み込み中は経過時間と形の Skeleton を出す", async ({ page }) => {
  await mockKnowledgeBase(page, { delayMs: 1500 });
  await page.goto("/knowledge-bases/kb-1");
  await expect(page.getByTestId("knowledge-base-extraction-fields-loading")).toBeVisible();
  await expect(page.getByTestId("knowledge-base-extraction-fields")).toContainText(
    "全体の既定の項目"
  );
});

const VIEWS = [
  {
    id: "bv-1",
    name: "契約ビュー",
    description: null,
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: "2026-06-19T00:00:00Z",
    updated_at: "2026-06-19T00:00:00Z",
    archived_at: null,
  },
];

async function mockSearchPage(page: Page) {
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/approved-faq/suggest")) {
      await route.fulfill(envelope({ suggestions: [] }));
      return;
    }
    await route.fulfill(envelope({ items: VIEWS, total: 1, limit: 50, offset: 0, has_next: false }));
  });
  await page.route("**/api/search/answers**", (route) =>
    route.fulfill(envelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }))
  );
  await page.route("**/api/settings/answer-records", (route) =>
    route.fulfill(envelope({ retention_days: 30 }))
  );
  await page.route("**/api/feedback/current**", (route) => route.fulfill(envelope([])));
  await page.route("**/api/search/extraction-fields**", (route) =>
    route.fulfill(envelope({ fields: CONTRACT_FIELDS }))
  );
  const bodies: Array<{ filters?: Record<string, string> }> = [];
  await page.route("**/api/search/stream", async (route: Route) => {
    bodies.push(route.request().postDataJSON() as { filters?: Record<string, string> });
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        'event: metadata\ndata: {"trace_id":"t-1","elapsed_ms":10,"guardrail_warnings":[],"diagnostics":{}}',
        "event: citations\ndata: []",
        'event: delta\ndata: {"text":"回答"}',
        'event: done\ndata: {"trace_id":"t-1"}',
        "",
      ].join("\n\n"),
    });
  });
  return bodies;
}

async function openFieldFilters(page: Page) {
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /契約ビュー/);
  await page.getByRole("button", { name: /詳細条件/ }).click();
  await page.getByRole("button", { name: "抽出項目の値で絞り込む" }).click();
}

async function addCondition(page: Page, fieldLabel: RegExp) {
  await page.getByRole("button", { name: "条件を追加" }).click();
  await page.getByRole("combobox", { name: "項目" }).last().click();
  await page.getByRole("option", { name: fieldLabel }).click();
}

test(`検索の詳細条件で抽出項目の値の条件を型に合った入力で足して検索する`, async ({
  page,
}) => {
  const bodies = await mockSearchPage(page);
  await openFieldFilters(page);

  await addCondition(page, /金額/);
  await page.getByRole("textbox", { name: "下限（以上）" }).fill("1000000");
  await addCondition(page, /契約日/);
  await page.getByLabel("開始日（この日を含む）").fill("2025-01-01");
  await page.getByLabel("終了日（この日を含む）").fill("2025-12-31");
  await addCondition(page, /自動更新/);
  await page.getByRole("combobox", { name: "値（一致）" }).last().click();
  await page.getByRole("option", { name: "はい" }).click();
  await addCondition(page, /契約番号/);
  await page.getByRole("textbox", { name: "値（一致）" }).fill("C-1");
  await expectNoPageOverflow(page);
  await page
    .getByTestId("search-extraction-field-filters")
    .screenshot({ path: test.info().outputPath("search-extraction-fields.png") });

  await page.getByRole("textbox", { name: "RAG 検索" }).fill("更新条件は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect(page.getByText("回答", { exact: true }).first()).toBeVisible();

  expect(JSON.parse(bodies[0].filters?.extraction_fields ?? "[]")).toEqual([
    { name: "金額", value_type: "number", op: "gte", value: "1000000" },
    { name: "契約日", value_type: "date", op: "gte", value: "2025-01-01" },
    { name: "契約日", value_type: "date", op: "lte", value: "2025-12-31" },
    { name: "自動更新", value_type: "bool", op: "eq", value: "true" },
    { name: "契約番号", value_type: "string", op: "eq", value: "C-1" },
  ]);
  const applied = page.getByLabel("適用中の詳細条件");
  await expect(applied).toContainText("金額 ≥ 1000000");
  await expect(applied).toContainText("自動更新: はい");
});

test("数値でない値の条件は誤りを出して検索しない", async ({ page }) => {
  const bodies = await mockSearchPage(page);
  await openFieldFilters(page);
  await addCondition(page, /金額/);
  await page.getByRole("textbox", { name: "上限（以下）" }).fill("百万");
  await expect(page.getByText("数値を入力してください")).toBeVisible();

  await page.getByRole("textbox", { name: "RAG 検索" }).fill("金額は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "項目" })).toBeFocused();
  expect(bodies).toEqual([]);
});
