import { expect, type Page, test } from "./fixtures/test";

import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  openAnswerProcessDetails,
  openChatHistory,
  selectSearchAnswerProfile,
} from "./_helpers";

/**
 * RAG 検索・回答の不足の回帰テスト（#304）。
 * 参照 KB が 0 件の検索・回答プロファイルでは検索・チャットしない、
 * チャットは会話の回答の trace_id で保存済みの回答を引き当て、開き直せる。
 */

const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

const view = (id: string, name: string, knowledgeBaseCount: number) => ({
  id,
  name,
  description: null,
  status: "ACTIVE",
  knowledge_base_count: knowledgeBaseCount,
  created_at: "2026-06-19T00:00:00Z",
  updated_at: "2026-06-19T00:00:00Z",
  archived_at: null,
});

const VIEWS = [view("bv-1", "経理ビュー", 1), view("bv-empty", "準備中ビュー", 0)];
const NO_KB_MESSAGE =
  "この検索・回答プロファイルには参照するナレッジベースがありません。検索・回答プロファイルの設定でナレッジベースを追加してください。";

const summary = (index: number) => ({
  trace_id: `trace-${index}`,
  search_answer_profile_id: "bv-1",
  surface: "search",
  answer_engine: "grounded",
  question: `質問 ${index} 番`,
  rewritten_question: null,
  confidence: null,
  created_at: "2026-09-25T01:00:00Z",
});

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/approved-faq/suggest")) {
      await route.fulfill(envelope({ suggestions: [] }));
      return;
    }
    await route.fulfill(
      envelope({ items: VIEWS, total: VIEWS.length, limit: 50, offset: 0, has_next: false })
    );
  });
  await page.route("**/api/settings/answer-records", (route) =>
    route.fulfill(envelope({ retention_days: 30 }))
  );
  await page.route("**/api/feedback/current**", (route) => route.fulfill(envelope([])));
});

async function mockAnswerHistory(page: Page, total: number) {
  const offsets: number[] = [];
  await page.route("**/api/search/answers**", async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit"));
    const offset = Number(url.searchParams.get("offset"));
    offsets.push(offset);
    const items = Array.from(
      { length: Math.max(0, Math.min(limit, total - offset)) },
      (_, index) => summary(offset + index + 1)
    );
    await route.fulfill(
      envelope({ items, total, limit, offset, has_next: offset + items.length < total })
    );
  });
  return offsets;
}

test("対象の検索・回答プロファイルは 1 つを選ぶ欄で、検索・参照 KB の件数・キーボードで選べる（#635）", async ({
  page,
}) => {
  await mockAnswerHistory(page, 0);
  await page.goto("/search");

  const trigger = page.getByRole("button", { name: /検索・回答プロファイル/ });
  await expect(trigger).toContainText("検索・回答プロファイルを検索して選択…");
  // 必須はラベルの「必須」のタグで読み上げる（ボタンは aria-required を持てない。#1595）。
  await expect(trigger).not.toHaveAttribute("aria-required");
  await expect(trigger).toHaveAccessibleName(/検索・回答プロファイル\s*必須/);
  // 欄の下に説明文（helper）は出さない（#664）。
  await expect(page.getByText("選んだ検索・回答プロファイルが参照するナレッジベースを検索し")).toHaveCount(0);
  // 閉じたボタンに虫眼鏡は無く、値と開閉の印だけ（#1771）。
  await expect(trigger.locator("svg.lucide-search")).toHaveCount(0);
  await expect(trigger.locator("svg.lucide-chevron-down")).toBeVisible();

  await trigger.click();
  // 欄のラベルは RAG 検索・チャットで同じ「検索・回答プロファイル」（#860）。検索欄の名前は「<ラベル>を検索」。
  const search = page.getByRole("combobox", { name: "検索・回答プロファイルを検索" });
  const listbox = page.getByRole("listbox", { name: /検索・回答プロファイル/ });
  await expect(search).toBeFocused();
  await expect(search).toHaveAttribute("placeholder", "検索・回答プロファイルの名前・説明で検索…");
  // 虫眼鏡は開いたパネルの検索欄だけ（ボタンと検索欄で 2 つ並ばない。#1771）。
  await expect(page.locator("[data-searchable-select-popover] svg.lucide-search")).toHaveCount(1);
  await expect(trigger.locator("svg.lucide-search")).toHaveCount(0);
  // 参照 KB が 0 件の検索・回答プロファイルも隠さず、件数を出して後ろに並べる。
  await expect(listbox.getByRole("option")).toHaveText([/経理ビュー.*参照 KB 1 件/, /準備中ビュー.*参照 KB 0 件/]);
  await expect(page.getByRole("checkbox", { name: "参照 KB なしを隠す" })).toHaveCount(0);

  await search.fill("経理");
  await expect(listbox.getByRole("option")).toHaveCount(1);
  await search.press("Enter");
  await expect(listbox).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toContainText("経理ビュー");
  // 選択済みの chip は出さない（単一選択）。
  await expect(page.getByLabel("経理ビュー を選択から外す")).toHaveCount(0);

  // 別の検索・回答プロファイルへ置き換える（追加にならない）。Esc で閉じるとボタンへ戻る。
  await trigger.press("ArrowDown");
  await expect(search).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(listbox).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await selectSearchAnswerProfile(page, /準備中ビュー/);
  await expect(trigger).toContainText("準備中ビュー");
  await expect(trigger).not.toContainText("経理ビュー");
  await expectNoPageOverflow(page);
});

test("参照 KB が 0 件の検索・回答プロファイルでは理由を示し、検索を送らない", async ({ page }) => {
  let streamRequests = 0;
  await mockAnswerHistory(page, 0);
  await page.route("**/api/search/stream", async (route) => {
    streamRequests += 1;
    await route.fulfill({ status: 500, json: { data: null, error_messages: [], warning_messages: [] } });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /準備中ビュー/);
  await expect(page.getByRole("alert").filter({ hasText: NO_KB_MESSAGE })).toBeVisible();
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  // 送信せず、検索前の案内のまま（回答の生成を始めない）。
  await expect(page.getByText("索引済みドキュメントを自然言語で検索します。").nth(1)).toBeVisible();
  await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
  expect(streamRequests).toBe(0);
  await expectNoPageOverflow(page);

  // KB のある検索・回答プロファイルを足せば理由は消える（KB のある検索・回答プロファイルの KB だけを検索する）。
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await expect(page.getByText(NO_KB_MESSAGE)).toHaveCount(0);
});

test("backend が参照 KB のない検索・回答プロファイルを 409 で断ったら、その理由を表示する", async ({ page }) => {
  await mockAnswerHistory(page, 0);
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 409,
      json: { data: null, error_messages: [NO_KB_MESSAGE], warning_messages: [] },
    })
  );

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect(page.getByText(NO_KB_MESSAGE)).toBeVisible();
});

test("RAG 検索画面には回答履歴の一覧を出さない（#444）", async ({ page }) => {
  await mockAnswerHistory(page, 4);

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await expect(page.getByRole("textbox", { name: "RAG 検索" })).toBeVisible();
  await expect(page.getByText("DocRAG の回答履歴")).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "回答履歴のページ" })).toHaveCount(0);
});

// 会話の回答の引用は、画面と評価が使う項目だけを保存した形で返る（#1371。親の本文・索引の内部の
// 項目は無く、要素の表示領域は display_regions）。
const citationChunk = {
  document_id: "d1",
  chunk_id: "ch1",
  text: "経費の上限は 10 万円です。",
  score: 0.91,
  rerank_score: 0.82,
  file_name: "経費規程.pdf",
  category_name: null,
  metadata: {
    page_start: 2,
    page_end: 2,
    page_number: 2,
    section_path: "第3条 経費の上限",
    content_kind: "text",
    evidence_model_used: true,
    evidence_role: "retrieved_anchor",
    rerank_rank: 1,
    bbox: "[120.0, 340.0, 980.0, 420.0]",
    bbox_unit: "absolute",
    page_width: 1240,
    page_height: 1754,
    display_regions: [
      {
        page: 2,
        boxes: [
          {
            record_id: "docling-p2-3",
            seq_no: 3,
            category: "Text",
            bbox: [120.0, 340.0, 980.0, 420.0],
            text_preview: "経費の上限は 10 万円です。",
          },
        ],
      },
    ],
  },
};

function chatMessage(role: "USER" | "ASSISTANT", id: string, traceId: string | null) {
  return {
    message_id: id,
    conversation_id: "conv-1",
    role,
    content: role === "USER" ? "経費の上限は？" : "経費の上限は 10 万円です。",
    model: role === "USER" ? null : "m1",
    citations: role === "USER" ? [] : [citationChunk],
    guardrail_warnings: [],
    trace_id: traceId,
    status: "COMPLETE",
    reply_to_message_id: role === "USER" ? null : "u1",
    created_at: "2026-01-01T00:00:00Z",
  };
}

async function mockChat(page: Page, searchAnswerProfileId: string) {
  const messages = [chatMessage("USER", "u1", null), chatMessage("ASSISTANT", "a1", "trace-chat")];
  const conversation = {
    id: "conv-1",
    search_answer_profile_id: searchAnswerProfileId,
    title: "経費の上限は？",
    status: "ACTIVE",
    message_count: messages.length,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:02Z",
  };
  await page.route("**/api/chat/models", (route) => route.fulfill(envelope([])));
  await page.route("**/api/feedback**", (route) => route.fulfill(envelope([])));
  await page.route("**/api/chat/conversations**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/chat/conversations") {
      await route.fulfill(
        envelope({ items: [conversation], total: 1, limit: 50, offset: 0, has_next: false })
      );
      return;
    }
    await route.fulfill(envelope({ ...conversation, messages }));
  });
}

test("チャットは参照 KB が 0 件の検索・回答プロファイルで理由を示し、送信できない", async ({ page }) => {
  await mockChat(page, "bv-empty");
  await page.route("**/api/search/answers**", (route) =>
    route.fulfill(envelope({ items: [], total: 0, limit: 1, offset: 0, has_next: false }))
  );

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "準備中ビュー");
  const banner = page.getByRole("status").filter({ hasText: "参照するナレッジベースがありません" });
  await expect(banner).toBeVisible();
  await expect(banner.getByRole("button", { name: "検索・回答プロファイルの設定を開く" })).toBeVisible();
  const conversations = await openChatHistory(page);
  await conversations.getByRole("list", { name: "会話の履歴" }).getByRole("button").first().click();
  await page.getByRole("textbox", { name: "質問", exact: true }).fill("経費の上限は？");
  await expect(page.getByRole("button", { name: "送信" })).toBeDisabled();
  await expectNoPageOverflow(page);
});

test("チャットは会話の回答の trace_id で保存済みの回答を引き当て、処理の詳細に出す（削除の操作は出さない）", { tag: "@responsive" }, async ({ page }) => {
  await mockChat(page, "bv-1");
  const requested: string[][] = [];
  await page.route("**/api/search/answers**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/answers/trace-chat")) {
      await route.fulfill(
        envelope({
          ...summary(1),
          trace_id: "trace-chat",
          surface: "chat",
          answer: "経費の上限は 10 万円です。",
          citations: [citationChunk],
          answer_diagnostics: {},
        })
      );
      return;
    }
    const traceIds = url.searchParams.getAll("trace_id");
    requested.push(traceIds);
    await route.fulfill(
      envelope({
        items:
          traceIds.includes("trace-chat")
            ? [{ ...summary(1), trace_id: "trace-chat", surface: "chat" }]
            : [],
        total: 1,
        limit: 1,
        offset: 0,
        has_next: false,
      })
    );
  });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理ビュー");
  const conversations = await openChatHistory(page);
  await conversations.getByRole("list", { name: "会話の履歴" }).getByRole("button").first().click();
  // 保存された回答の記録は、処理の経過の中の「処理の詳細」から開く（#2096 / #2325。旧「この回答の実行記録」）。
  await openAnswerProcessDetails(page, "chat-answer-progress");
  const details = page.getByTestId("chat-answer-progress-details");
  await expect(page.getByText(/実行記録/)).toHaveCount(0);
  expect(requested).toContainEqual(["trace-chat"]);

  // 保存された回答の記録には引用の一覧を出さず、引用は回答の枠の「根拠 N 件」の 1 か所だけに出す（#1202）。
  const citationSummary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" });
  await citationSummary.click();
  await expect(page.getByText("経費規程.pdf")).toHaveCount(1);
  // 保存した形の引用でも、頁・章節・回答に使った根拠の印を出す（#1371）。
  await expect(citationSummary).toContainText("回答で引用 1 件");
  await expect(page.getByText("第3条 経費の上限", { exact: true })).toBeVisible();
  await expect(page.getByText("Rerank #1", { exact: true })).toBeVisible();
  // 回答の評価は、根拠を確かめた後に答えられるよう「根拠 N 件」の後ろに出す（#1202）。
  const feedback = page.getByRole("group", { name: "この回答は役に立ちましたか？" });
  await expect(feedback).toBeVisible();
  const citationTop = (await citationSummary.boundingBox())?.y ?? Number.POSITIVE_INFINITY;
  const feedbackTop = (await feedback.boundingBox())?.y ?? Number.NEGATIVE_INFINITY;
  expect(feedbackTop).toBeGreaterThan(citationTop);
  // 処理の経過（とその中の「処理の詳細」）は回答の上（作成中の「N ステップ完了」の場所のまま。#2115）。「根拠 N 件」より前に出す。
  const recordTop = (await details.boundingBox())?.y ?? Number.POSITIVE_INFINITY;
  expect(citationTop).toBeGreaterThan(recordTop);

  // 処理の詳細は処理の経過の技術的な詳細なので、回答の記録の削除（「その他の操作」）は出さない（#2325。#147 の削除の導線を外した）。
  await expect(page.getByRole("group", { name: "保存された回答 の操作" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
  await expect(page.getByText("この回答を削除")).toHaveCount(0);
});

// 検索はフォームの幅、チャットはツールバーの幅で同じ対象選択を使う（#3918）。
for (const viewport of [
  { name: "1280", width: 1280, height: 800 },
  { name: "1920", width: 1920, height: 1000 },
  { name: "375", width: 375, height: 812 },
]) {
  test(`検索の対象はフォームに、チャットの対象はコンパクトなツールバーに置く (${viewport.name})`, {tag: "@responsive"}, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockAnswerHistory(page, 0);
    await mockChat(page, "bv-1");

    const measure = async () => {
      const trigger = page.getByRole("button", { name: /検索・回答プロファイル/ });
      await expect(trigger).toBeVisible();
      await expect(trigger).toContainText("検索・回答プロファイルを検索して選択…");
      await expect(trigger).not.toHaveAttribute("aria-required");
      await expect(trigger).toHaveAccessibleName(/検索・回答プロファイル\s*必須/);
      await expect(page.getByText("選んだ検索・回答プロファイルが参照するナレッジベースを検索し")).toHaveCount(0);
      return trigger.evaluate((button) => {
        // 欄（ラベル・ボタン）を置いた親の、内側の幅いっぱいに置く。
        const container = button.parentElement!.parentElement!;
        const style = getComputedStyle(container);
        const inner =
          container.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        const rect = button.getBoundingClientRect();
        return { width: rect.width, inner, height: rect.height };
      });
    };

    await page.goto("/search");
    const search = await measure();
    await page.goto("/chat");
    const chat = await measure();
    for (const box of [search, chat]) expect(Math.abs(box.width - box.inner)).toBeLessThanOrEqual(1);
    expect(chat.width).toBeLessThan(search.width);
    const target = page.getByRole("button", {name: /検索・回答プロファイル/});
    expect(await target.evaluate(node => node.closest("[data-chat-target-selector]") !== null)).toBe(true);
    const toolbarHeight = await page.getByRole("button", {name: "新しい会話", exact: true}).evaluate(node => node.getBoundingClientRect().height);
    expect(Math.abs(chat.height - toolbarHeight)).toBeLessThanOrEqual(0.5);

    await selectSearchAnswerProfile(page, "経理ビュー");
    await expect(page.getByRole("button", { name: /検索・回答プロファイル/ })).toContainText("経理ビュー");
    await expectNoPageOverflow(page);
  });
}

const ANSWER_MODELS = [
  { model_id: "text-m", display_name: "gpt-oss-120b", kind: "text" },
  { model_id: "vision-m", display_name: "grok-4.3", kind: "vision" },
];

test("検索とチャットの入力欄に回答するモデルを表示せず既定を使う", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const bodies: Record<string, unknown>[] = [];
  await mockAnswerHistory(page, 0);
  await page.route("**/api/search/models", (route) => route.fulfill(envelope(ANSWER_MODELS)));
  await page.route("**/api/search/stream", async (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 500, json: { data: null, error_messages: [], warning_messages: [] } });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  const modelRow = page.getByTestId("search-answer-model");
  await expect(modelRow).toHaveCount(0);
  await enableSearchAnswer(page);
  await expect(modelRow).toHaveCount(0);
  await expect(page.getByRole("button", {name: "回答するモデルの説明"})).toHaveCount(0);
  await page.screenshot({path: testInfo.outputPath("search-without-model-selection.png")});
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("図の数値は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({ generate_answer: true });
  expect(bodies[0]).not.toHaveProperty("model_id");
  await expectNoPageOverflow(page);

  await mockChat(page, "bv-1");
  await page.route("**/api/chat/models", (route) => route.fulfill(envelope(ANSWER_MODELS)));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理ビュー");
  await expect(page.getByTestId("chat-answer-model")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "回答するモデルの説明" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "gpt-oss-120b（テキスト）" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "grok-4.3（画像対応）" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("テキストと画像の既定が同じ場合もモデル選択行を表示しない", {tag: "@responsive"}, async ({
  page,
}, testInfo) => {
  // 既定のテキストモデルと既定の画像対応モデルが同じ（例: xai.grok-4.3）なら 1 件で kind は text_vision。
  const shared = [{ model_id: "xai.grok-4.3", display_name: "xai.grok-4.3", kind: "text_vision" }];
  const bodies: Record<string, unknown>[] = [];
  await mockChat(page, "bv-1");
  await page.route("**/api/chat/models", (route) => route.fulfill(envelope(shared)));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理ビュー");
  await expect(page.getByTestId("chat-answer-model")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "回答するモデルの説明" })).toHaveCount(0);
  await expectNoPageOverflow(page);

  await mockAnswerHistory(page, 0);
  await page.route("**/api/search/models", (route) => route.fulfill(envelope(shared)));
  await page.route("**/api/search/stream", async (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 500, json: { data: null, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await enableSearchAnswer(page);
  const modelRow = page.getByTestId("search-answer-model");
  await expect(modelRow).toHaveCount(0);
  await expect(page.getByRole("button", {name: "回答するモデルの説明"})).toHaveCount(0);
  await page.screenshot({path: testInfo.outputPath("search-without-model-selection.png")});
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("図の数値は？");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({ generate_answer: true });
  expect(bodies[0]).not.toHaveProperty("model_id");
  await expectNoPageOverflow(page);
});

/**
 * モデル設定（GET / PATCH /api/settings/model）と、その既定のモデルから作る回答するモデルの候補
 * （backend の answer_model_choices と同じ規則）。保存した既定のモデルを次の候補の取得で返す。
 */
async function mockModelSettingsWithAnswerModels(page: Page) {
  const models = ["openai.gpt-oss-120b", "xai.grok-4.3", "claude-opus-5"].map((modelId) => ({
    model_id: modelId,
    display_name: "",
    vision_enabled: modelId !== "openai.gpt-oss-120b",
    connection_id: "primary",
    max_output_tokens: 32768,
  }));
  const server = { defaults: { text: "openai.gpt-oss-120b", vision: "xai.grok-4.3" } };
  const settings = () => ({
    settings: {
      enterprise_ai: {
        connections: [
          {
            connection_id: "primary",
            endpoint: "https://inference.example",
            project_ocid: "ocid1.generativeaiproject.oc1..example",
            api_key: "",
            has_api_key: true,
            clear_api_key: false,
          },
        ],
        models,
        default_text_model_id: server.defaults.text,
        default_vision_model_id: server.defaults.vision,
        api_path: "/responses",
        vlm_input_mode: "auto",
        text_payload_template: "",
        vision_payload_template: "",
        text_response_path: "",
        vision_response_path: "",
        timeout_seconds: 600,
        max_retries: 3,
      },
      generative_ai: {
        embedding_model: "text-embedding-v4",
        embedding_dim: 1536,
        embedding_base_url: "",
        rerank_model: "qwen3-rerank",
        rerank_base_url: "",
      },
    },
    model_settings_file: "model-settings.json",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
  });
  const answerModels = () => {
    const { text, vision } = server.defaults;
    if (text === vision) return [{ model_id: text, display_name: text, kind: "text_vision" }];
    return [
      { model_id: text, display_name: text, kind: "text" },
      { model_id: vision, display_name: vision, kind: "vision" },
    ];
  };
  await page.route("**/api/settings/model", async (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      const payload = request.postDataJSON() as {
        enterprise_ai: { default_text_model_id: string; default_vision_model_id: string };
      };
      server.defaults = {
        text: payload.enterprise_ai.default_text_model_id,
        vision: payload.enterprise_ai.default_vision_model_id,
      };
    }
    await route.fulfill(envelope(settings()));
  });
  await page.route("**/api/chat/models", (route) => route.fulfill(envelope(answerModels())));
  return server;
}

/** 画面を読み直さずに移る（SPA の中の移動。query の cache が残る）。 */
async function navigateInApp(page: Page, path: string) {
  await page.evaluate((target) => {
    window.history.pushState({}, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

async function chooseDefaultModel(page: Page, name: string, option: string) {
  const field = page.getByRole("combobox", { name });
  await field.click();
  await page.getByRole("listbox", { name }).getByRole("option", { name: option }).click();
  await expect(field).toContainText(option);
}

test("モデル設定で既定を変更してもチャットのモデル選択行は表示しない", async ({
  page,
}) => {
  await mockChat(page, "bv-1");
  // mockChat の空の候補より後に登録し、モデル設定から作る候補を返す（後に登録した route が先に当たる）。
  const server = await mockModelSettingsWithAnswerModels(page);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理ビュー");
  const modelRow = page.getByTestId("chat-answer-model");
  await expect(modelRow).toHaveCount(0);

  // 同じタブの中でモデル設定へ移り、既定のテキストモデル・既定の画像対応モデルを claude-opus-5 にして保存する。
  await navigateInApp(page, "/settings/model");
  await chooseDefaultModel(page, "既定のテキストモデル", "claude-opus-5");
  await chooseDefaultModel(page, "既定の画像対応モデル", "claude-opus-5");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
  expect(server.defaults).toEqual({ text: "claude-opus-5", vision: "claude-opus-5" });

  await navigateInApp(page, "/chat");
  await expect(page.locator("#chat-composer")).toBeVisible();
  await expect(modelRow).toHaveCount(0);

  server.defaults = { text: "openai.gpt-oss-120b", vision: "claude-opus-5" };
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect(modelRow).toHaveCount(0);
  await expectNoPageOverflow(page);
});


test("検索前の案内の開き括弧を次の文に付ける", {tag:"@responsive"}, async ({page}) => {
  await page.goto("/search");
  const sentences=page.locator('[data-message-sentence]');
  await expect(sentences.first()).toBeVisible();
  const text=await sentences.allTextContents();
  expect(text.some(t=>/検索します。「$/.test(t))).toBe(false);
  expect(text.some(t=>t.startsWith("「") && t.includes("LLM"))).toBe(true);
  await expectNoPageOverflow(page);
});

// #3739: チャットと同じ選択部品を使う RAG 検索も、後続ページを検索対象にする。
test("RAG検索で51件目以降のプロファイルを検索し、再読込後も選択を保持する", async ({ page }, testInfo) => {
  const profiles = Array.from({ length: 50 }, (_, i) => view(`profile-${i}`, `検証用_候補${i + 1}`, 1));
  const target = view("profile-last", "検証用_最後の候補", 1);
  const offsets: number[] = [];
  await page.route("**/api/search-answer-profiles?**", async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? 0);
    offsets.push(offset);
    await route.fulfill(envelope({
      items: offset === 0 ? profiles : [target], total: 51, limit: 50, offset, has_next: offset === 0,
    }));
  });
  await page.goto("/search");
  await selectSearchAnswerProfile(page, target.name);
  await expect(page.getByRole("button", { name: new RegExp(target.name) })).toBeVisible();
  expect(offsets).toContain(50);
  await page.reload();
  await expect(page.getByRole("button", { name: new RegExp(target.name) })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("search-profile-after-first-page.png") });
});
