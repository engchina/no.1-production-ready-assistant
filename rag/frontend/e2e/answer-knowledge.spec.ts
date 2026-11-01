import { expect, type Page, type Route, test } from "./fixtures/test";
import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  openAnswerProcessDetails,
  selectSearchAnswerProfile,
} from "./_helpers";

// rag_poc からの移植: プロファイルの知識(登録済みQ&A / 用語・同義語 / ドメインキーワード / 確認・回答ルール)、
// 検索前の類似問提示、回答の根拠パネル。

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

const detail = {
  ...summary,
  config: {
    version: 1,
    knowledge_base_ids: ["kb-1"],
    query: {
      guardrail_policy: null,
      answer_engine: "grounded",
    },
  },
  knowledge_bases: [{ id: "kb-1", name: "受注マニュアル" }],
};

const faqSuggestion = {
  id: "faq-1",
  question: "受注を取り消すには？",
  matched_question: "受注を取り消すには？",
  answer: "受注一覧で取消ボタンを押します。",
  score: 0.97,
  direct: true,
};

async function mockCommon(page: Page) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: envelope({ items: [], total: 0, limit: 50, offset: 0, has_next: false }),
    })
  );
}

async function mockSearchAnswerProfileApi(
  page: Page,
  { suggestions = [] as unknown[] }: { suggestions?: unknown[] } = {}
) {
  await page.route("**/api/search-answer-profiles**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/domain-keywords")) {
      return route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", keywords: ["受注番号"] }) });
    }
    if (path.endsWith("/approved-faq/suggest")) {
      return route.fulfill({ json: envelope({ suggestions }) });
    }
    if (path.endsWith("/approved-faq")) {
      return route.fulfill({
        json: envelope({
          search_answer_profile_id: "bv-1",
          records: [
            {
              id: "faq-1",
              question: faqSuggestion.question,
              answer: faqSuggestion.answer,
              alternate_questions: [],
              status: "approved",
            },
          ],
        }),
      });
    }
    if (path.endsWith("/runtime-knowledge")) {
      return route.fulfill({
        json: envelope({
          search_answer_profile_id: "bv-1",
          terms: [{ term: "受注", aliases: ["オーダー"], description: "注文", status: "approved" }],
          rules: [],
        }),
      });
    }
    if (path.endsWith("/bv-1")) {
      return route.fulfill({ json: envelope(detail) });
    }
    return route.fulfill({
      json: envelope({ items: [summary], total: 1, limit: 50, offset: 0, has_next: false }),
    });
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`プロファイルの知識パネルでキーワード・FAQ・用語ルールを管理できる (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockCommon(page);
    await mockSearchAnswerProfileApi(page);

    await page.goto("/search-answer-profiles");
    await page.getByRole("link", { name: "受注サポート を編集" }).click();
    await expect(page).toHaveURL(/\/search-answer-profiles\?id=bv-1$/);

    const panel = page.getByRole("heading", { name: "プロファイルの知識" });
    await expect(panel).toBeVisible();
    // 登録済みQ&A を既定とし、主な利用段階に沿って並べる。
    const tabs = page.getByRole("tablist", { name: "プロファイルの知識" }).getByRole("tab");
    await expect(tabs).toHaveText([
      "登録済みQ&A",
      "質問の理解",
      "用語・同義語",
      "確認・回答ルール",
      "業務ガイド",
      "ドメインキーワード",
    ]);
    await expect(page.getByRole("tab", { name: "登録済みQ&A" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
    await expect(page.getByRole("rowheader", { name: faqSuggestion.question })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Excel 取込" })).toBeVisible();
    await expect(page.getByRole("switch", { name: "回答の前に関連するQ&Aを提示する" })).toBeVisible();
    await page.getByRole("switch", { name: "回答の前に関連するQ&Aを提示する" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("registered-qa.png") });

    await page.getByRole("tab", { name: "ドメインキーワード" }).click();
    await expect(page.getByLabel("登録キーワード（1 行に 1 語）")).toHaveValue("受注番号");
    await expect(page.getByRole("heading", { name: "Excel 取込" })).toBeVisible();
    await expect(page.getByLabel("取込む Excel ファイル")).toBeVisible();
    await expect(page.getByRole("button", { name: "取込", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "候補を生成" })).toHaveCount(0);

    await page.getByRole("tab", { name: "用語・同義語" }).click();
    await expect(page.getByRole("rowheader", { name: "受注" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "照合テスト" })).toBeVisible();
    // 種類の選択欄は無く、タブの種類だけを扱う（#682）。
    await expect(page.getByRole("heading", { name: "用語・同義語を追加" })).toBeVisible();
    await expect(page.locator("#runtime-knowledge-kind")).toHaveCount(0);
    // 用語・同義語は行のクリック（キーボードは名前のボタン）でフォームへ読み込み、選んだ行を aria-current で示す（#147）。
    const termRow = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "受注" }) });
    await termRow.getByText("オーダー").click();
    await expect(termRow).toHaveAttribute("aria-current", "true");
    await expect(page.getByRole("heading", { name: "編集中: 受注" })).toBeVisible();
    await expect(page.getByLabel("同義語（1 行に 1 つ）")).toHaveValue("オーダー");
    await expect(page.getByRole("button", { name: "受注 を編集" })).toBeVisible();
    // 削除は確認ダイアログを通す（キャンセルでは送らない）。
    await page.getByRole("button", { name: "削除", exact: true }).click();
    const deleteDialog = page.getByRole("alertdialog", { name: "この用語・同義語を削除しますか？" });
    await expect(deleteDialog).toBeVisible();
    await deleteDialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(deleteDialog).toHaveCount(0);
    await expectNoPageOverflow(page);
  });
}

async function selectSearchAnswerProfileAndAsk(page: Page, question: string) {
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /受注サポート/);
  await enableSearchAnswer(page);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill(question);
  await page.getByRole("button", { name: "検索", exact: true }).click();
}

test("質問履歴の候補を取得せず、質問欄から検索できる", async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  const candidateRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/query-suggestions")) candidateRequests.push(request.url());
  });
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /受注サポート/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("合成の質問");
  await expect(page.getByRole("list", { name: "よく聞かれている質問" })).toHaveCount(0);
  expect(candidateRequests).toEqual([]);
  await expectNoPageOverflow(page);
});
test("類似する登録済みQ&Aを提示し、FAQ の回答を LLM なしで表示する", async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page, { suggestions: [faqSuggestion] });
  let streamed = false;
  await page.route("**/api/search/stream", (route) => {
    streamed = true;
    return route.fulfill({ status: 500 });
  });

  await selectSearchAnswerProfileAndAsk(page, "受注を取り消すには？");

  await expect(page.getByRole("heading", { name: "質問に近い登録済みQ&Aがあります" })).toBeVisible();
  await page.getByRole("button", { name: "このQ&Aで回答する" }).click();
  await expect(page.getByRole("heading", { name: "登録済みQ&Aの回答" })).toBeVisible();
  await expect(page.getByText(faqSuggestion.answer)).toBeVisible();
  expect(streamed).toBe(false);
});

test("類似問を使わない場合は回答と根拠パネルを表示する", { tag: "@responsive" }, async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page, { suggestions: [faqSuggestion] });
  await mockAnswerStream(page);

  await selectSearchAnswerProfileAndAsk(page, "受注を取り消すには？");
  await page.getByRole("button", { name: "どれでもない（資料を検索する）" }).click();

  // 回答の記録の詳細は、処理の経過の中の「処理の詳細」に畳む（#2096 / #2325。旧「回答の根拠と実行記録」）。
  const process = page.getByRole("region", { name: "処理の経過" });
  await expect(process).toBeVisible();
  const panel = await openAnswerProcessDetails(page, "search-run-progress");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("信頼度: high")).toHaveCount(0);
  await expect(panel.getByText("人手確認が必要")).toHaveCount(0);
  // 回答の根拠は、チャット・Agent と同じ折りたたみ（既定は閉じる）。まとまりの表示・見方の切り替えは持たない（#2388）。
  const citations = page.getByTestId("answer-citations");
  const citationSummary = citations
    .locator("summary")
    .filter({ hasText: "AI に渡した 1 件 ／ 回答で引用 1 件 ／ うち該当確認済み 1 件" })
    .first();
  await expect(citationSummary).toBeVisible();
  await expect(citations.getByTestId("citation-text")).toBeHidden();
  await citationSummary.click();
  await expect(citations.getByRole("tab")).toHaveCount(0);
  const card = citations.getByTestId("evidence-card");
  await expect(card.getByText("回答で引用", { exact: true })).toBeVisible();
  await expect(panel.getByText(/^根拠の構成/)).toHaveCount(0);
  await expect(panel.getByTestId("answer-graph-expansion")).toHaveCount(0);
  // 「詳細」はチャットと同じ回答の記録の詳細だけ（開始・トレース ID・旧「処理時間: …」の行は出さない。#2118）。
  await expect(process.getByText("トレース ID")).toHaveCount(0);
  await expect(page.getByText(/^処理時間:/)).toHaveCount(0);
  // 処理の経過 → 回答の本文 → AI に渡した箇所 → 評価 の順（処理の経過は回答の上。チャットと同じ。#2115 / #2097 / #1420）。
  const feedback = page.getByRole("group", { name: "この回答は役に立ちましたか？" });
  await expect(feedback).toBeVisible();
  const [answerBox, processBox, feedbackBox] = await Promise.all([
    page.getByText("受注一覧で取消を押します。").first().boundingBox(),
    process.boundingBox(),
    feedback.boundingBox(),
  ]);
  expect(answerBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(
    (processBox?.y ?? Number.POSITIVE_INFINITY) + (processBox?.height ?? 0) - 1
  );
  const citationBox = await citationSummary.boundingBox();
  expect(citationBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(answerBox?.y ?? Number.POSITIVE_INFINITY);
  expect(feedbackBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(citationBox?.y ?? Number.POSITIVE_INFINITY);
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
]) {
  test(`回答の詳細に標準回答の評価操作を表示しない (${viewport.name})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockCommon(page);
    await mockSearchAnswerProfileApi(page);
    await mockAnswerStream(page);
    let evaluationCalls = 0;
    await page.route("**/api/search/answers/*/evaluation", (route) => {
      evaluationCalls += 1;
      return route.fulfill({ status: 404 });
    });
    await selectSearchAnswerProfileAndAsk(page, "合成の質問");
    await openAnswerProcessDetails(page, "search-run-progress");
    await expect(page.getByTestId("answer-graph-expansion")).toHaveCount(0);
    await expect(page.getByRole("region", { name: "標準回答による評価" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "標準回答で評価" })).toHaveCount(0);
    await expect(page.getByLabel("標準回答", { exact: true })).toHaveCount(0);
    expect(evaluationCalls).toBe(0);
    await page.screenshot({ path: testInfo.outputPath("answer-details-without-evaluation.png") });
    await expectNoPageOverflow(page);
  });
}

async function mockAnswerStream(page: Page, graphExpansion?: Record<string, unknown>) {
  const answerDiagnostics = {
    confidence: "high",
    needs_human_review: true,
    insufficient_reason: "",
    generated_queries: ["受注 取消 手順"],
    execution_steps: [{ name: "質問の理解", status: "complete", elapsed_seconds: 0.3, llm_calls: 1 }],
    // 文書構造による検索拡張で加えた根拠（#2055）。処理の経過の「詳細」に件数と使った見出しを出す。
    graph_expansion: graphExpansion ?? {
      added_count: 2,
      entities: [{ entity: "manual.pdf > 第 3 章 取消の手順", added_count: 2 }],
    },
    evidence_tree: [
      {
        parent_id: "doc-1:p1",
        source: "manual.pdf",
        page: 3,
        children: [{ chunk_id: "doc-1:c1", role: "retrieved_anchor", is_model_used: true, page: 3 }],
      },
    ],
  };
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        `event: metadata\ndata: ${JSON.stringify({
          trace_id: "trace-answer",
          elapsed_ms: 20,
          diagnostics: { search_answer_profile_applied: "bv-1", retrieval_strategy: "hybrid", answer: answerDiagnostics },
        })}\n\n`,
        `event: delta\ndata: ${JSON.stringify({ text: "受注一覧で取消を押します。" })}\n\n`,
        `event: citations\ndata: ${JSON.stringify([
          {
            document_id: "doc-1",
            chunk_id: "doc-1:c1",
            text: "受注一覧で対象の受注を選び、取消を押します。",
            score: 0.8,
            rerank_score: 0.9,
            file_name: "manual.pdf",
            category_name: null,
            metadata: {
              page_start: 3,
              evidence_role: "retrieved_anchor",
              evidence_model_used: true,
              evidence_answer_use: "confirmed",
            },
          },
        ])}\n\n`,
        `event: done\ndata: ${JSON.stringify({ trace_id: "trace-answer" })}\n\n`,
      ].join(""),
    })
  );
}

// #540 / #541: FAQ の追加と確認・回答ルールの保存は、押せる状態のまま未入力を欄の直下に出す。
// ルールは backend と同じく「ルール名」「照合キーワード」「ルール内容」が必須。
test("FAQ の追加と確認・回答ルールの保存は、未入力を欄の下に出す", async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /approved-faq$|runtime-knowledge\/edit$/.test(request.url())) {
      writes.push(request.url());
    }
  });

  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "登録済みQ&A" }).click();
  const add = page.getByRole("button", { name: "追加", exact: true });
  await expect(add).toBeEnabled();
  await add.click();
  await expect(page.locator("#approved-faq-question")).toHaveAccessibleDescription(/質問を入力してください。/);
  await expect(page.locator("#approved-faq-answer")).toHaveAccessibleDescription(/回答を入力してください。/);
  await expect(page.locator("#approved-faq-question")).toBeFocused();

  await page.getByRole("tab", { name: "確認・回答ルール" }).click();
  await expect(page.getByRole("heading", { name: "確認・回答ルールを追加" })).toBeVisible();
  await expect(page.locator('label[for="runtime-knowledge-title"]')).toContainText("必須");
  await expect(page.locator('label[for="runtime-knowledge-content"]')).toContainText("必須");
  // ヘッダーの「保存」（検索・回答プロファイルの設定。#618）と区別し、確認・回答ルールのタブの中の保存を押す。
  await page
    .getByRole("tabpanel", { name: "確認・回答ルール" })
    .getByRole("button", { name: "保存", exact: true })
    .click();
  await expect(page.locator("#runtime-knowledge-name")).toHaveCount(0);
  await expect(page.locator("#runtime-knowledge-labels")).toHaveAccessibleDescription(/照合キーワードを入力してください。/);
  await expect(page.locator("#runtime-knowledge-title")).toHaveAccessibleDescription(/ルール名を入力してください。/);
  await expect(page.locator("#runtime-knowledge-content")).toHaveAccessibleDescription(/ルール内容を入力してください。/);
  await expect(page.locator("#runtime-knowledge-title")).toBeFocused();
  expect(writes).toHaveLength(0);
  await expectNoPageOverflow(page);
});

test("検索・回答プロファイルごとに類似問の提示をオン / オフできる（既定はオン。#684）", async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  const saved: unknown[] = [];
  await page.route("**/api/search-answer-profiles/bv-1/approved-faq/settings", async (route) => {
    const body = route.request().postDataJSON() as { enabled: boolean };
    saved.push(body);
    await route.fulfill({
      json: envelope({ search_answer_profile_id: "bv-1", records: [faqSuggestion], enabled: body.enabled }),
    });
  });

  await page.goto("/search-answer-profiles?id=bv-1");
  const toggle = page.getByRole("switch", { name: "回答の前に関連するQ&Aを提示する" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(saved).toEqual([{ enabled: false }]);
  await expectNoPageOverflow(page);
});


// #717: 確認・回答ルールに確認の質問と、選択肢が指す章節を設定する。
test("確認・回答ルールに回答前の確認と章節を設定して保存できる", { tag: "@responsive" }, async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  await page.route("**/api/search-answer-profiles/bv-1/runtime-knowledge", (route) =>
    route.fulfill({
      json: envelope({
        search_answer_profile_id: "bv-1",
        terms: [],
        rules: [
          { id: "R01", title: "期限の確認", triggers: ["期限"], content: "規程ごとに違う", status: "approved" },
        ],
      }),
    })
  );
  const puts: unknown[] = [];
  await page.route("**/api/search-answer-profiles/bv-1/runtime-knowledge/rules/R01/clarification", async (route) => {
    puts.push(route.request().postDataJSON());
    await route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", terms: [], rules: [] }) });
  });
  const documentQueries: URL[] = [];
  await page.route("**/api/documents?**", (route) => {
    const url = new URL(route.request().url());
    documentQueries.push(url);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const items = offset === 0
      ? Array.from({ length: 100 }, (_, index) => ({ id: `synthetic-${index + 1}`, file_name: `文書 ${index + 1}.pdf` }))
      : [{ id: "doc-travel", file_name: "文書 101.pdf" }];
    return route.fulfill({ json: envelope({ items, total: 101, limit: 100, offset, has_next: offset === 0 }) });
  });
  await page.route("**/api/documents/doc-travel/sections**", (route) =>
    route.fulfill({
      json: envelope({
        document_id: "doc-travel",
        source: "extraction",
        rules_mode: "parser",
        sections: [
          { id: "sec-1", title: "第1章 総則", level: 1, page_start: 1, page_end: 1, origin: "extraction", source_section_id: "sec-1", edited: false },
          { id: "sec-6", title: "第6条 申請と精算", level: 2, page_start: 2, page_end: 3, origin: "extraction", source_section_id: "sec-6", edited: false },
        ],
        extraction_section_count: 2,
        page_count: 3,
        revision: null,
        updated_at: null,
      }),
    })
  );

  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "確認・回答ルール" }).click();
  await page.getByRole("button", { name: /期限の確認/ }).first().click();
  await expect(page.locator("#runtime-knowledge-name")).toHaveCount(0);
  await expect(page.getByRole("switch", { name: "ルールを有効にする", exact: true })).toBeVisible();
  await expect(page.locator("#runtime-knowledge-enabled-help")).toContainText("無効にして保存すると両方を使いません");
  const editor = page.getByTestId("rule-clarification-editor");
  await editor.getByRole("button", { name: "回答前の確認を設定する" }).click();

  // 未入力は保存の前に欄の下で知らせる。
  await editor.getByRole("button", { name: "回答前の確認を保存" }).click();
  await expect(editor.getByText("確認事項を入力してください。")).toBeVisible();
  expect(puts).toHaveLength(0);

  await editor.getByRole("textbox", { name: /確認事項/ }).fill("どの規程についてのご質問ですか？");
  await editor.getByRole("textbox", { name: /表示名/ }).nth(0).fill("出張旅費");
  await editor.getByRole("textbox", { name: /表示名/ }).nth(1).fill("経費精算");
  await editor.getByRole("textbox", { name: "検索に足す語" }).nth(0).fill("出張、日当");
  await editor.getByRole("button", { name: /^章節を選ぶ文書/ }).first().click();
  const listbox = page.getByRole("listbox", { name: "章節を選ぶ文書" });
  await expect(listbox.getByRole("option")).toHaveCount(100);
  expect(documentQueries).toHaveLength(1);
  expect(documentQueries[0].searchParams.get("limit")).toBe("100");
  await expect(listbox.getByRole("option").nth(1)).toHaveText("文書 2.pdf");
  await expect(listbox.getByRole("option").nth(9)).toHaveText("文書 10.pdf");
  await listbox.evaluate((node) => { node.scrollTop = node.scrollHeight; node.dispatchEvent(new Event("scroll")); });
  await expect(page.getByRole("option", { name: "文書 101.pdf", exact: true })).toBeVisible();
  expect(documentQueries.some((url) => url.searchParams.get("offset") === "100")).toBe(true);
  await page.screenshot({ path: test.info().outputPath("document-candidates-loaded.png") });
  await page.getByRole("option", { name: "文書 101.pdf", exact: true }).click();
  const documentPicker = editor.getByRole("button", { name: /^章節を選ぶ文書/ }).first();
  await documentPicker.click();
  const search = page.getByRole("combobox", { name: /章節を選ぶ文書/ });
  await search.fill("文書");
  await expect.poll(() => documentQueries.some((url) => url.searchParams.get("q") === "文書" && url.searchParams.get("offset") === "0")).toBe(true);
  await expect(listbox.getByRole("option")).toHaveCount(100);
  await search.press("Escape");
  await expect(documentPicker).toContainText("文書 101.pdf");
  await editor.getByRole("checkbox", { name: /第6条 申請と精算/ }).check();
  await expect(editor.getByRole("list", { name: "選んだ章節" })).toContainText("第6条 申請と精算 p.2–3");

  await editor.getByRole("button", { name: "回答前の確認を保存" }).click();
  await expect(page.getByText("回答前の確認を保存しました。").first()).toBeVisible();
  expect(puts).toHaveLength(1);
  expect(puts[0]).toMatchObject({
    clarification: {
      question: "どの規程についてのご質問ですか？",
      multiple: true,
      allow_other: true,
      options: [
        {
          label: "出張旅費",
          search_terms: ["出張", "日当"],
          sections: [
            {
              document_id: "doc-travel",
              document_name: "文書 101.pdf",
              section_id: "sec-6",
              title: "第6条 申請と精算",
              page_start: 2,
              page_end: 3,
            },
          ],
        },
        { label: "経費精算", sections: [] },
      ],
    },
  });
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("rule-clarification-form.png") });
});


for (const graph of [
  { status: "completed", graph_available: true, added_count: 0, skipped_existing_count: 3 },
  { status: "completed", graph_available: false, added_count: 0 },
]) {
  test(`関係情報の追加0件でも処理の詳細に理由を表示する (${graph.graph_available})`, { tag: "@responsive" }, async ({ page }) => {
    await mockCommon(page);
    await mockSearchAnswerProfileApi(page, { suggestions: [faqSuggestion] });
    await mockAnswerStream(page, graph);
    await selectSearchAnswerProfileAndAsk(page, "合成の操作方法を確認する");
    await page.getByRole("button", { name: "どれでもない（資料を検索する）" }).click();
    const panel = await openAnswerProcessDetails(page, "search-run-progress");
    const diagnostic = panel.getByTestId("answer-graph-expansion");
    await expect(diagnostic).toHaveCount(0);
    await expect(panel.locator("[data-run-used-models]")).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("graph-zero-diagnostic.png") });
  });
}

test("キーワードの Excel 取込はプレビュー・未保存保護・追加・全置換を共通の構成で扱う", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  let keywords = ["既存語"];
  let imports = 0;
  await page.route("**/api/search-answer-profiles/bv-1/domain-keywords**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/preview")) {
      return route.fulfill({ json: envelope({ total: 2, rows: [{ row: 2, keyword: "追加語" }, { row: 3, keyword: "条件語" }] }) });
    }
    if (path.endsWith("/import")) {
      imports++;
      keywords = route.request().postData()?.includes("REPLACE") ? ["追加語", "条件語"] : ["既存語", "追加語", "条件語"];
      return route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", keywords, inserted_count: 2 }) });
    }
    if (route.request().method() === "PUT") keywords = route.request().postDataJSON().keywords;
    return route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", keywords }) });
  });
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "ドメインキーワード" }).click();
  const editor = page.getByLabel("登録キーワード（1 行に 1 語）");
  await expect(editor).toHaveValue("既存語");
  const file = page.getByLabel("取込む Excel ファイル");
  await file.setInputFiles({ name: "keywords.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("synthetic mocked workbook") });
  await expect(page.getByText("有効なキーワード 2 件（先頭 10 件を表示）")).toBeVisible();
  const button = page.getByRole("button", { name: "取込", exact: true });
  await editor.fill("未保存語");
  await expect(button).toBeDisabled();
  await expect(page.getByText("左の変更を保存してから取り込んでください。")).toBeVisible();
  await editor.fill("既存語");
  await button.click();
  await expect(editor).toHaveValue("既存語\n追加語\n条件語");
  expect(imports).toBe(1);
  await page.getByLabel("取込モード").click();
  await page.getByRole("option", { name: "全置換（Excel の語のみ）" }).click();
  await button.click();
  const dialog = page.getByRole("alertdialog", { name: "登録キーワードを全置換しますか？" });
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  expect(imports).toBe(1);
  await button.click();
  await dialog.getByRole("button", { name: "取込", exact: true }).click();
  await expect(editor).toHaveValue("追加語\n条件語");
  expect(imports).toBe(2);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("keyword-excel.png"), fullPage: true });
});

test("キーワード Excel の遅い旧プレビューとファイル検証エラーから取込を実行しない", async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  let releaseFirst: () => void = () => {};
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  let previews = 0;
  await page.route("**/domain-keywords/import/preview", async (route) => {
    previews++;
    if (previews === 1) {
      await firstGate;
      return route.fulfill({ json: envelope({ total: 1, rows: [{ row: 2, keyword: "旧語" }] }) });
    }
    return route.fulfill({ status: 422, json: { data: null, error_messages: ["KEYWORD 列を指定してください。"], warning_messages: [] } });
  });
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "ドメインキーワード" }).click();
  const file = page.getByLabel("取込む Excel ファイル");
  const upload = { name: "keywords.xlsx", mimeType: "application/octet-stream", buffer: Buffer.from("synthetic") };
  await file.setInputFiles(upload);
  await expect(page.locator("[data-processing-label]").filter({ hasText: "Excel のキーワードを確認しています" })).toBeVisible();
  await file.setInputFiles({ ...upload, name: "second.xlsx" });
  await expect(page.getByText("KEYWORD 列を指定してください。")).toBeVisible();
  releaseFirst();
  await expect(page.getByRole("button", { name: "取込", exact: true })).toBeDisabled();
  await expect(page.getByText("旧語", { exact: true })).toHaveCount(0);
});

// #3664: 用途と段階別の適用は管理設定として明示し、確認だけにダミー指示を求めない。
test("確認だけのルールを空の内容で保存し、適用設定を保持する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  const saved: Record<string, unknown>[] = [];
  await page.route("**/api/search-answer-profiles/bv-1/runtime-knowledge/edit", async (route) => {
    saved.push(route.request().postDataJSON());
    await route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", terms: [], rules: [] }) });
  });
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "確認・回答ルール" }).click();
  await page.locator("#runtime-knowledge-title").fill("合成の確認");
  await page.locator("#runtime-knowledge-labels").fill("設定");
  await page.locator("#runtime-knowledge-purpose").click();
  await page.getByRole("option", { name: "回答前の確認だけ", exact: true }).click();
  await expect(page.locator('label[for="runtime-knowledge-content"]')).not.toContainText("必須");
  await expect(page.locator("#runtime-knowledge-instruction-kind")).toBeDisabled();
  await page.locator("#runtime-knowledge-exclusive-group").fill("group-a");
  await page.locator("#runtime-knowledge-priority").fill("3");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("rule-application-purpose.png"), fullPage: true });
  await page.getByRole("tabpanel", { name: "確認・回答ルール" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ purpose: "confirmation", content: "", exclusive_group: "group-a", priority: 3 });
});

test("照合テストで保留した排他設定を管理者へ示す", { tag: "@responsive" }, async ({ page }) => {
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  await page.route("**/api/search-answer-profiles/bv-1/runtime-knowledge/preview", (route) => route.fulfill({ json: envelope({ expanded_question: "設定", matched_terms: [], matched_rules: [], conflicting_rules: ["合成ルール A", "合成ルール B"] }) }));
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "確認・回答ルール" }).click();
  await page.locator("#runtime-knowledge-question").fill("設定");
  await page.getByRole("button", { name: "照合する", exact: true }).click();
  await expect(page.getByText(/排他設定の競合により保留しました: 合成ルール A、合成ルール B/)).toBeVisible();
  await expectNoPageOverflow(page);
});

test("検索の回答は同頁の説明と全確定根拠の対応を保つ", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { itemCitationText, itemCitationChunks, expectItemCitationSources } = await import("./_item-citations");
  await mockCommon(page);
  await mockSearchAnswerProfileApi(page);
  await page.route("**/api/search/stream", route => route.fulfill({
    headers: { "content-type": "text/event-stream" },
    body: [
      `event: metadata\ndata: ${JSON.stringify({ trace_id: "trace-items", elapsed_ms: 20, diagnostics: {} })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ text: itemCitationText })}\n\n`,
      `event: citations\ndata: ${JSON.stringify(itemCitationChunks)}\n\n`,
      `event: done\ndata: ${JSON.stringify({ trace_id: "trace-items" })}\n\n`,
    ].join(""),
  }));
  await selectSearchAnswerProfileAndAsk(page, "合成の説明を確認してください。");
  await expectItemCitationSources(page, page.getByTestId("answer-text"));
  await page.screenshot({ path: testInfo.outputPath("item-citations-search.png"), fullPage: true });
});
