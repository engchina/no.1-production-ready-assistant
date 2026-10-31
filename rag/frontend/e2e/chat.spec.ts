import { inspectTraceStep } from "../../../platform/packages/ui/e2e/trace-explorer";
import {timelineSnapshot, timelineTypography} from "../../../platform/packages/ui/e2e/timeline-snapshot";
import { findIconTextMisalignment } from "../../../platform/packages/ui/e2e/layout-checks";
import { renderedFontsForSelector, waitForCjkFallbackFaces } from "../../../platform/packages/ui/e2e/font-checks";

import { expect, type Locator, type Page, test } from "./fixtures/test";
import {
  answerSpans,
  installTraceRecorder,
  mockTraceApi,
  ragSpan,
  savedSpanProgress,
  SPAN_TRACE_ID,
  traceSnapshotJson,
  traceSse,
  type ChatProgressEventJson,
} from "./_chat-progress";

import {
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  openAnswerProcessDetails,
  openChatHistory,
  selectSearchAnswerProfile,
} from "./_helpers";

const searchAnswerProfile = {
  id: "bv-1",
  name: "経理アシスタント",
  description: "経費の相談",
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const citationChunk = {
  document_id: "d1",
  chunk_id: "ch1",
  text: "経費の上限は 10 万円です。",
  score: 0.91,
  rerank_score: 0.82,
  file_name: "経費規程.pdf",
  category_name: null,
  // 回答エンジンは回答に使った根拠かを記録する（#1208）。
  metadata: { evidence_model_used: true },
};

const userMessage = {
  message_id: "u1",
  conversation_id: "conv-1",
  role: "USER",
  content: "経費の上限は？",
  model: null,
  citations: [],
  guardrail_warnings: [],
  trace_id: null,
  status: "COMPLETE",
  reply_to_message_id: null,
  created_at: "2026-01-01T00:00:00Z",
};

const assistantMessage = {
  message_id: "a1",
  conversation_id: "conv-1",
  role: "ASSISTANT",
  content: "経費の上限は 10 万円です。",
  model: "m1",
  citations: [citationChunk],
  guardrail_warnings: [],
  trace_id: "t1",
  status: "COMPLETE",
  reply_to_message_id: "u1",
  created_at: "2026-01-01T00:00:01Z",
};

const comparisonReplies = [
  {
    ...assistantMessage,
    message_id: "a-model-1",
    model: "xai.grok-4.3",
    content: "経費の上限は 10 万円です。申請前に承認者を確認してください。",
    citations: [{ ...citationChunk, chunk_id: "ch-model-1" }],
  },
  {
    ...assistantMessage,
    message_id: "a-model-2",
    model: "google.gemini-2.5-pro",
    content: "規程上の上限額は 10 万円です。例外申請には追加承認が必要です。",
    citations: [{ ...citationChunk, chunk_id: "ch-model-2" }],
  },
  {
    ...assistantMessage,
    message_id: "a-model-3",
    model: "cohere.command-a",
    content: "通常の経費上限は 10 万円で、超過する場合は事前申請が必要です。",
    citations: [{ ...citationChunk, chunk_id: "ch-model-3" }],
  },
];

const sseStart = `event: start\ndata: ${JSON.stringify({
  conversation_id: "conv-1",
  user_message: userMessage,
  columns: [{ model_id: "m1", label: "MODEL 1" }],
})}\n\n`;

const sseBody = [
  sseStart,
  `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "経費の上限は 10 万円です。" })}\n\n`,
  `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [] })}\n\n`,
  `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations: [citationChunk] })}\n\n`,
  `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  `event: all_done\ndata: ${JSON.stringify({ conversation_id: "conv-1" })}\n\n`,
].join("");

function pageEnvelope<T>(items: T[]) {
  return {
    data: { items, total: items.length, limit: 50, offset: 0, has_next: false },
    error_messages: [],
    warning_messages: [],
  };
}

function conversationDetail(messages: object[], title: string | null = null) {
  return {
    id: "conv-1",
    search_answer_profile_id: "bv-1",
    title,
    status: "ACTIVE",
    message_count: messages.length,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:02Z",
    messages,
  };
}

type ConversationListMode = "ready" | "loading" | "error";

async function mockChat(
  page: Page,
  conversationListMode: ConversationListMode = "ready",
  initialMessages: object[] = [],
  options: {
    initialTitle?: string | null;
    renameFails?: boolean;
    streamBody?: string;
    /** 送信の後に会話が返すメッセージ（既定は質問と完了した回答）。 */
    sentMessages?: object[];
  } = {}
): Promise<() => void> {
  let sent = false;
  let created = initialMessages.length > 0;
  let conversationTitle =
    options.initialTitle ?? (initialMessages.length > 0 ? userMessage.content : null);
  let releaseConversationList: () => void = () => undefined;
  const conversationListReady =
    conversationListMode === "loading"
      ? new Promise<void>((resolve) => {
          releaseConversationList = resolve;
        })
      : Promise.resolve();

  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  // 配信の中の span の記録の印（`traceSse`）を、回答の trace の API が返す（#3226）。
  await installTraceRecorder(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({ json: pageEnvelope([searchAnswerProfile]) })
  );
  await page.route("**/api/chat/models", (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  let feedbackItems: Record<string, unknown>[] = [];
  await page.route("**/api/feedback**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/feedback/current") {
      await route.fulfill({
        json: { data: feedbackItems, error_messages: [], warning_messages: [] },
      });
      return;
    }
    if (request.method() === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      feedbackItems = [
        {
          feedback_id: "feedback-1",
          created_at: "2026-07-01T00:00:00Z",
          ...payload,
        },
      ];
      await route.fulfill({
        json: {
          data: { feedback_id: "feedback-1", ...payload },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fallback();
  });

  await page.route("**/api/chat/conversations**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const trace = /\/messages\/([^/]+)\/trace$/.exec(path);
    if (trace) {
      // 回答の trace の API（共通の span の表の snapshot。#3226）。mock のメッセージの `progress` は表の span を段の
      // イベントにしたもの（会話の取得は処理の経過を返さない）。
      const messages = sent ? (options.sentMessages ?? [userMessage, assistantMessage]) : initialMessages;
      const message = messages.find((item) => (item as { message_id?: string }).message_id === trace[1]) as
        | { progress?: ChatProgressEventJson[] }
        | undefined;
      await route.fulfill({ json: traceSnapshotJson(message?.progress ?? []) });
      return;
    }
    if (path.endsWith("/messages/stream")) {
      sent = true;
      created = true;
      conversationTitle = userMessage.content;
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: options.streamBody ?? sseBody,
      });
      return;
    }
    if (path === "/api/chat/conversations") {
      if (request.method() === "POST") {
        created = true;
        await route.fulfill({
          json: {
            data: conversationDetail([], conversationTitle),
            error_messages: [],
            warning_messages: [],
          },
        });
        return;
      }
      if (conversationListMode === "error") {
        await route.fulfill({
          status: 500,
          json: { data: null, error_messages: ["会話一覧を読み込めませんでした。"], warning_messages: [] },
        });
        return;
      }
      await conversationListReady;
      const messages = sent ? (options.sentMessages ?? [userMessage, assistantMessage]) : initialMessages;
      const summaries = created
        ? [{ ...conversationDetail(messages, conversationTitle), messages: undefined }]
        : [];
      await route.fulfill({ json: pageEnvelope(summaries) });
      return;
    }
    if (request.method() === "DELETE") {
      created = false;
      sent = false;
      await route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } });
      return;
    }
    if (request.method() === "PATCH") {
      if (options.renameFails) {
        await route.fulfill({
          status: 500,
          json: {
            data: null,
            error_messages: [
              "会話名を変更できませんでした。入力内容を確認して再試行してください。",
            ],
            warning_messages: [],
          },
        });
        return;
      }
      conversationTitle = (request.postDataJSON() as { title: string }).title;
      await route.fulfill({
        json: {
          data: { ...conversationDetail([], conversationTitle), messages: undefined },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    // GET /api/chat/conversations/{id}
    const messages = sent ? (options.sentMessages ?? [userMessage, assistantMessage]) : initialMessages;
    await route.fulfill({
      json: {
        data: conversationDetail(messages, conversationTitle),
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  return releaseConversationList;
}

/** 会話の履歴は既定で閉じ、チャットが本文の幅いっぱいに出る（#664）。 */
async function expectChatWorkspaceLayout(page: Page, mode: "desktop" | "mobile") {
  const main = page.getByRole("main");
  const chat = page.getByRole("region", { name: "チャット" });
  await expect(page.getByTestId("chat-history-toggle")).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("chat-history")).toBeHidden();
  const [mainBox, chatBox] = await Promise.all([main.boundingBox(), chat.boundingBox()]);

  if (!mainBox || !chatBox) throw new Error("チャットレイアウトを計測できません。");

  const leftGutter = chatBox.x - mainBox.x;
  const rightGutter = mainBox.x + mainBox.width - (chatBox.x + chatBox.width);
  expect(leftGutter).toBeGreaterThanOrEqual(mode === "desktop" ? 24 : 12);
  expect(rightGutter).toBeGreaterThanOrEqual(0);
  // 履歴の分の幅を取らない（左右の余白の差はスクロールバーの幅まで）。
  expect(Math.abs(leftGutter - rightGutter)).toBeLessThanOrEqual(20);
}

async function openPersistedConversation(page: Page, width: number, messages: object[]) {
  await page.setViewportSize({ width, height: width <= 375 ? 812 : 1000 });
  await mockChat(page, "ready", messages);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  const history = await openChatHistory(page);
  await history.getByRole("list", { name: "会話の履歴" }).getByRole("button").filter({ hasText: "件・" }).click();
}

/**
 * 回答モデルのカード（見出しの親）の位置と大きさを、同じスクロール位置で一度に測る。
 * カードごとに scrollIntoView してから測ると、測る間にメッセージ一覧のスクロールが動き、
 * 縦並び・横並びの比較が別々のスクロール位置の座標どうしになる（375px で縦並びを誤判定した。#833）。
 */
async function modelCardBoxes(page: Page, models: string[]) {
  const headings = models.map((model) => page.getByRole("heading", { name: model, level: 3 }));
  for (const heading of headings) await expect(heading).toBeAttached();
  await headings[0].scrollIntoViewIfNeeded();
  const cards = await Promise.all(headings.map((heading) => heading.locator("..").elementHandle()));
  return page.evaluate(
    (elements) =>
      elements.map((element) => {
        if (!element) throw new Error("回答カードを計測できません。");
        const { x, y, width, height } = element.getBoundingClientRect();
        return { x, y, width, height };
      }),
    cards
  );
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`チャットで会話を始めて根拠付き回答を表示する (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    await page.route("**/api/chat/models", (route) => route.fulfill({
      json: { data: [
        { model_id: "text-m", display_name: "Text model", kind: "text" },
        { model_id: "vision-m", display_name: "Vision model", kind: "vision" },
      ], error_messages: [], warning_messages: [] },
    }));

    await page.goto("/chat");
    await expect(page.getByRole("heading", { name: "チャット" })).toBeVisible();

    // 検索・回答プロファイルを選ぶとチャットを始められる。
    await selectSearchAnswerProfile(page, "経理アシスタント");

    await page.getByRole("button", { name: "新しい会話" }).click();

    await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
    await expectChatWorkspaceLayout(page, viewport.name as "desktop" | "mobile");

    const composer = page.getByRole("textbox");
    await composer.scrollIntoViewIfNeeded();
    await expect(composer).toBeVisible();
    await expect(page.getByRole("button", { name: "送信" })).toBeVisible();
    await expect(page.getByTestId("chat-answer-model")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "回答するモデルの説明" })).toHaveCount(0);
    const sent = page.waitForRequest((request) =>
      request.method() === "POST" && request.url().includes("/messages/stream")
    );
    await composer.fill("経費の上限は？");
    await page.getByRole("button", { name: "送信" }).click();

    expect((await sent).postDataJSON()).toMatchObject({ model_ids: [] });
    // ストリーミング → 永続化後も根拠は既定で閉じ、キーボードで展開できる。
    await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    // 会話の名前はチャットの上端に出る。履歴を開くと一覧にもある（#664）。
    await expect(page.getByTestId("chat-conversation-title")).toHaveText("経費の上限は？");
    const sessions = await openChatHistory(page);
    await expect(sessions.getByText("経費の上限は？", { exact: true })).toBeVisible();
    await expect(sessions.getByText(/^2件・\d{2}\/\d{2} \d{2}:\d{2}$/)).toBeVisible();
    if (viewport.name === "mobile") {
      await sessions.getByRole("button", { name: "会話の履歴を閉じる" }).click();
      await expect(sessions).toBeHidden();
    }
    const citationSummary = page
      .locator("summary")
      .filter({ hasText: "AI に渡した 1 件 ／ 回答で引用 1 件" })
      .first();
    const citationDetails = citationSummary.locator("..");
    await expect(citationSummary).toBeVisible();
    await expect(citationDetails).not.toHaveAttribute("open", "");
    await expect(page.getByText("経費規程.pdf")).toBeHidden();

    await citationSummary.focus();
    await page.keyboard.press("Enter");
    await expect(citationDetails).toHaveAttribute("open", "");
    await expect(page.getByText("経費規程.pdf")).toBeVisible();
    // 回答で引用した箇所には「回答で引用」を出す（#1208）。
    await expect(citationDetails.getByText("回答で引用", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /の根拠を表示$/ })).toBeVisible();
    await expect(page.getByRole("meter", { name: /取得スコア/ })).toHaveCount(0);
    await expect(page.getByRole("meter", { name: "Rerank スコア: 0.820" })).toBeVisible();

    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`chat-hidden-model-${viewport.name}.png`) });
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`チャットは安全チェック警告を表示する (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page, "ready", [
      { ...userMessage, guardrail_warnings: ["機微情報をマスクしました。"] },
      { ...assistantMessage, guardrail_warnings: ["根拠を確認してください。"] },
    ]);

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const history = await openChatHistory(page);
    await history.getByRole("button", { name: /^経費の上限は？ 2件/ }).click();

    await expect(page.getByText(/機微情報をマスクしました/)).toBeVisible();
    await expect(page.getByText(/根拠を確認してください/)).toBeVisible();
    await expectNoPageOverflow(page);
  });

  test(
    `AI に渡した件数と、回答で引用・参考の区別を出す (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      // 回答で引用した箇所の使われ方（該当を確認 / 参考）と、使用していない箇所（#2040）。
      const citations = [
        { ...citationChunk, metadata: { evidence_model_used: true, evidence_answer_use: "confirmed" } },
        {
          ...citationChunk,
          chunk_id: "ch-reference",
          file_name: "旅費規程.pdf",
          metadata: { evidence_model_used: true, evidence_answer_use: "reference" },
        },
        {
          ...citationChunk,
          chunk_id: "ch-unused",
          file_name: "出張手当.pdf",
          metadata: { evidence_model_used: false },
        },
      ];
      await mockChat(page, "ready", [userMessage, { ...assistantMessage, citations }]);

      await page.goto("/chat");
      await selectSearchAnswerProfile(page, "経理アシスタント");
      const history = await openChatHistory(page);
      await history.getByRole("button", { name: /^経費の上限は？ 2件/ }).click();

      // 件数は括弧を使わず区切りで並べ、0 件の項目は出さない。
      const summary = page
        .locator("summary")
        .filter({ hasText: "AI に渡した 3 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件 ／ 参考 1 件" })
        .first();
      await expect(summary).toBeVisible();
      await summary.click();
      const cards = summary.locator("..").getByRole("listitem");
      const card = (file: string) => cards.filter({ hasText: file }).first();
      // 該当を確認した箇所は「回答で引用」、参考の箇所は「参考」。色だけでなく文言とアイコンで区別する。
      await expect(card("経費規程.pdf").getByText("回答で引用", { exact: true })).toBeVisible();
      await expect(card("経費規程.pdf").getByText("参考", { exact: true })).toHaveCount(0);
      const reference = card("旅費規程.pdf").locator("[data-status-variant]").filter({ hasText: "参考" });
      await expect(reference).toBeVisible();
      await expect(reference.locator("svg")).toHaveCount(1);
      await expect(card("旅費規程.pdf").getByText("回答で引用", { exact: true })).toHaveCount(0);
      // 回答で引用していない箇所にはバッジを出さない。
      await expect(card("出張手当.pdf").locator("[data-status-variant]")).toHaveCount(0);
      await expectNoPageOverflow(page);
    }
  );
}

// 回答の注意書き（設定は製品ごと。#2405・#2449）。回答の本文の後・根拠の一覧の前に出し、本文には混ぜない（回答の
// カードの末尾は Agent のチャットと同じ共通の並び「注意書き → 根拠の一覧 → 評価」。#2707）。
const NOTICE = "この回答は AI が生成したものです。\n連絡先: https://example.com/help";

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`回答のカードは 本文 → 注意書き → 根拠の一覧 → 評価 の順に出す (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const streamBody = sseBody.replace(
      '"guardrail_warnings":[]}',
      `"guardrail_warnings":[],"notice":${JSON.stringify(NOTICE)}}`
    );
    expect(streamBody).toContain('"notice"');
    await mockChat(page, "ready", [], {
      streamBody,
      sentMessages: [userMessage, { ...assistantMessage, notice: NOTICE }],
    });
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill("経費の上限は？");
    await page.getByRole("button", { name: "送信" }).click();

    const notice = page.getByRole("complementary", { name: "回答の注意書き" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("この回答は AI が生成したものです。");
    await expect(notice.getByRole("link", { name: "https://example.com/help" })).toHaveAttribute(
      "href",
      "https://example.com/help"
    );
    // 本文 → 注意書き → 根拠の一覧 → 評価 の順（注意書きは本文には混ぜない）。
    const body = page.getByText("経費の上限は 10 万円です。").first();
    const citations = page.locator("summary").filter({ hasText: "AI に渡した 1 件" }).first();
    const feedback = page.getByRole("group", { name: "この回答は役に立ちましたか？" });
    await expect(feedback).toBeVisible();
    const [bodyBox, noticeBox, citationsBox, feedbackBox] = await Promise.all([
      body.boundingBox(),
      notice.boundingBox(),
      citations.boundingBox(),
      feedback.boundingBox(),
    ]);
    expect(noticeBox!.y).toBeGreaterThan(bodyBox!.y + bodyBox!.height - 1);
    expect(citationsBox!.y).toBeGreaterThan(noticeBox!.y + noticeBox!.height - 1);
    expect(feedbackBox!.y).toBeGreaterThan(citationsBox!.y + citationsBox!.height - 1);
    await expect(body).not.toContainText("AI が生成");
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-footer-${viewport.name}.png`) });
  });
}

test("保存した会話の回答に注意書きを出し、失敗した回答には出さない", async ({ page }) => {
  await mockChat(page, "ready", [
    userMessage,
    { ...assistantMessage, notice: NOTICE },
    { ...userMessage, message_id: "u2", created_at: "2026-01-01T00:00:02Z" },
    {
      ...assistantMessage,
      message_id: "a2",
      reply_to_message_id: "u2",
      status: "ERROR",
      content: "回答を作れませんでした。",
      notice: null,
      created_at: "2026-01-01T00:00:03Z",
    },
  ]);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  const history = await openChatHistory(page);
  await history.getByRole("button", { name: /^経費の上限は？ 4件/ }).click();
  await expect(page.getByRole("complementary", { name: "回答の注意書き" })).toHaveCount(1);
});

test("送信開始後に永続化された質問を重複表示しない", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page, "ready", [], { streamBody: sseStart });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("button", { name: "新しい会話" }).click();

  const detailRefreshed = page.waitForResponse((response) => {
    const request = response.request();
    return (
      request.method() === "GET" &&
      new URL(response.url()).pathname === "/api/chat/conversations/conv-1"
    );
  });
  await page.getByRole("textbox").fill(userMessage.content);
  await page.getByRole("button", { name: "送信" }).click();
  await detailRefreshed;

  const chat = page.getByRole("region", { name: "チャット" });
  // 会話名の見出し（最初の質問。#664）は数えず、メッセージだけを数える。
  await expect(
    chat.getByText(userMessage.content, { exact: true }).and(chat.locator(":not(h2)"))
  ).toHaveCount(1);
});

// 内容の言語の字形（#3335）。合成の例文だけを使う（業務の内容ではない）。同じ符号の漢字（骨・直・角）が、中国語の内容では
// Noto Sans SC、日本語の内容では Noto Sans JP で描かれることを、Chromium が実際に使った書体で確かめる。
const zhQuestion = "这个示例句子说明了什么？";
const zhAnswer = "这是一个用于测试的示例回答：骨、直、角三个字的字形因地区而不同。";
const zhExcerpt = "示例资料：同一个字在不同地区的字形可能不同，例如骨、直、角。";
const jaQuestion = "この例文は何を説明していますか？";
const jaAnswer = "これはテスト用の例文です。骨・直・角の三つの字の字形は地域で異なります。";
const langMessages = [
  { ...userMessage, message_id: "u-zh", content: zhQuestion },
  {
    ...assistantMessage,
    message_id: "a-zh",
    reply_to_message_id: "u-zh",
    content: zhAnswer,
    citations: [{ ...citationChunk, chunk_id: "ch-zh", text: zhExcerpt, file_name: "示例资料.txt" }],
  },
  { ...userMessage, message_id: "u-ja", content: jaQuestion, created_at: "2026-01-01T00:00:02Z" },
  { ...assistantMessage, message_id: "a-ja", reply_to_message_id: "u-ja", content: jaAnswer, created_at: "2026-01-01T00:00:03Z" },
];

/** `target` の子の文字を Chromium が描いた書体の名前（CDP）。 */
async function renderedFontsOf(page: Page, target: Locator, family: string, text: string): Promise<string[]> {
  await waitForCjkFallbackFaces(page);
  await target.evaluate(async (element, { family, text }) => {
    element.setAttribute("data-e2e-font-target", "");
    await document.fonts.load(`400 14px "${family}"`, text);
    await document.fonts.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, { family, text });
  const cdp = await page.context().newCDPSession(page);
  try {
    return await renderedFontsForSelector(cdp, "[data-e2e-font-target]");
  } finally {
    await cdp.detach();
    await target.evaluate((element) => element.removeAttribute("data-e2e-font-target"));
  }
}

for (const viewport of [
  { name: "desktop", width: 1280 },
  { name: "mobile", width: 375 },
]) {
  test(`中国語の質問・回答・根拠の抜粋は中国語の字形、日本語の内容は日本語の字形で描く（#3335） (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await openPersistedConversation(page, viewport.width, langMessages);

    const zhQuestionBubble = page.getByText(zhQuestion, { exact: true });
    const zhAnswerText = page.getByText(zhAnswer, { exact: true });
    const jaAnswerText = page.getByText(jaAnswer, { exact: true });
    await expect(zhAnswerText).toBeVisible();
    // 共有の部品が内容の言語を lang に付ける（画面の文言は html の ja のまま）。
    await expect(page.locator("html")).toHaveAttribute("lang", "ja");
    await expect(zhQuestionBubble).toHaveAttribute("lang", "zh-Hans");
    await expect(page.getByText(jaQuestion, { exact: true })).toHaveAttribute("lang", "ja");
    await expect(zhAnswerText.locator("xpath=ancestor-or-self::*[@lang][1]")).toHaveAttribute("lang", "zh-Hans");
    await expect(jaAnswerText.locator("xpath=ancestor-or-self::*[@lang][1]")).toHaveAttribute("lang", "ja");

    const zhFonts = await renderedFontsOf(page, zhAnswerText, "Noto Sans SC Variable", zhAnswer);
    expect(zhFonts.length).toBeGreaterThan(0);
    expect(zhFonts.every((family) => family.startsWith("Noto Sans SC")), zhFonts.join(", ")).toBe(true);
    const jaFonts = await renderedFontsOf(page, jaAnswerText, "Noto Sans JP Variable", jaAnswer);
    expect(jaFonts.length).toBeGreaterThan(0);
    expect(jaFonts.every((family) => family.startsWith("Noto Sans JP")), jaFonts.join(", ")).toBe(true);

    // 根拠の抜粋も中国語の字形（開いた一覧のカード）。
    const citationSummary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" }).first();
    await citationSummary.click();
    const excerpt = page.getByTestId("citation-text").filter({ hasText: zhExcerpt }).first();
    await expect(excerpt).toHaveAttribute("lang", "zh-Hans");
    const excerptFonts = await renderedFontsOf(page, excerpt, "Noto Sans SC Variable", zhExcerpt);
    expect(excerptFonts.every((family) => family.startsWith("Noto Sans SC")), excerptFonts.join(", ")).toBe(true);

    await expectNoPageOverflow(page);
    await zhQuestionBubble.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`chat-content-lang-${viewport.name}.png`) });
  });
}

// 中国語の回答の中の画面の文言（節の見出し・「根拠：」の行）は、回答の lang を継承せず日本語の字形で描く（#3349）。
// 回答エンジンの書式（見出し・「・」の項目・「根拠：」の行）の合成の回答。
const zhSectionTitle = "資料により確認できた事項";
const zhSectionItem = "这个示例项目说明骨、直、角三个字的字形。";
const zhStructuredAnswer = [
  "这是一个用于测试的示例回答。",
  zhSectionTitle,
  `・${zhSectionItem}`,
  "根拠：示例资料.txt",
].join("\n");
const zhStructuredMessages = [
  { ...userMessage, message_id: "u-zh-s", content: zhQuestion },
  {
    ...assistantMessage,
    message_id: "a-zh-s",
    reply_to_message_id: "u-zh-s",
    content: zhStructuredAnswer,
    citations: [{ ...citationChunk, chunk_id: "ch-zh-s", text: zhExcerpt, file_name: "示例资料.txt" }],
  },
];

for (const viewport of [
  { name: "desktop", width: 1280 },
  { name: "mobile", width: 375 },
]) {
  test(`中国語の回答の中の節の見出し・根拠の行は日本語の字形、回答の文は中国語の字形で描く（#3349） (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await openPersistedConversation(page, viewport.width, zhStructuredMessages);

    const answer = page.getByTestId("answer-text").filter({ hasText: zhSectionItem });
    await expect(answer).toBeVisible();
    const title = answer.getByRole("heading", { name: zhSectionTitle });
    const item = answer.getByText(zhSectionItem, { exact: true });
    const citation = answer.getByTestId("answer-citation-link").filter({ hasText: "根拠：示例资料.txt" });
    await expect(title).toBeVisible();
    await expect(citation).toBeVisible();
    const nearestLang = (target: Locator) =>
      target.evaluate((element) => element.closest("[lang]")?.getAttribute("lang") ?? null);
    // 回答の文は回答の言語、画面の文言は画面の言語（最も近い lang が ja）。
    expect(await nearestLang(item)).toBe("zh-Hans");
    expect(await nearestLang(title)).toBe("ja");
    expect(await nearestLang(citation)).toBe("ja");

    const titleFonts = await renderedFontsOf(page, title, "Noto Sans JP Variable", zhSectionTitle);
    expect(titleFonts.length).toBeGreaterThan(0);
    expect(titleFonts.every((family) => family.startsWith("Noto Sans JP")), titleFonts.join(", ")).toBe(true);
    const itemFonts = await renderedFontsOf(page, item, "Noto Sans SC Variable", zhSectionItem);
    expect(itemFonts.length).toBeGreaterThan(0);
    expect(itemFonts.every((family) => family.startsWith("Noto Sans SC")), itemFonts.join(", ")).toBe(true);
    // 根拠の行の「根拠」（JP にある字）は JP の字形（JP に無い字だけが SC で補われる）。
    const citationFonts = await renderedFontsOf(page, citation, "Noto Sans JP Variable", "根拠");
    expect(citationFonts[0] ?? "", citationFonts.join(", ")).toMatch(/^Noto Sans JP/);

    await expectNoPageOverflow(page);
    await answer.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`chat-content-lang-chrome-${viewport.name}.png`) });
  });
}

const longJapaneseAnswer =
  "テストデータの根拠によると、申請前に担当者と承認者を確認し、必要な資料をそろえて期限までに提出してください。";

for (const viewport of [
  { name: "desktop", width: 2048 },
  { name: "mobile", width: 375 },
]) {
  test(`長い日本語回答がカード幅を使用する (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await openPersistedConversation(page, viewport.width, [
      userMessage,
      { ...assistantMessage, content: longJapaneseAnswer },
    ]);

    const answer = page.getByText(longJapaneseAnswer, { exact: true });
    const [answerBox, cardBox] = await Promise.all([
      answer.boundingBox(),
      answer.locator("..").boundingBox(),
    ]);
    if (!answerBox || !cardBox) throw new Error("回答幅を計測できません。");
    expect(answerBox.width).toBeGreaterThan(cardBox.width - 32);
    if (viewport.name === "desktop") {
      const lineHeight = await answer.evaluate((element) =>
        Number.parseFloat(getComputedStyle(element).lineHeight)
      );
      expect(answerBox.height).toBeLessThan(lineHeight * 1.5);
    }
    await expectNoPageOverflow(page);
  });
}

test("2モデルは広い画面で空き列なく横並びになる", async ({ page }) => {
  await openPersistedConversation(page, 2048, [userMessage, ...comparisonReplies.slice(0, 2)]);

  const [first, second] = await modelCardBoxes(page, ["xai.grok-4.3", "google.gemini-2.5-pro"]);
  expect(Math.abs(first.y - second.y)).toBeLessThanOrEqual(1);
  expect(first.width).toBeGreaterThanOrEqual(560);
  expect(second.x).toBeGreaterThan(first.x + first.width);
  await expect(page.locator("details[open]")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("チャット回答の低評価理由を保存し、選択状態を維持する", async ({ page }) => {
  let feedbackPayload: Record<string, unknown> | null = null;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/feedback") {
      feedbackPayload = request.postDataJSON() as Record<string, unknown>;
    }
  });
  await openPersistedConversation(page, 1280, [userMessage, assistantMessage]);

  const notHelpful = page.getByRole("button", { name: "この回答は役に立たなかった" });
  await notHelpful.click();
  await page.getByRole("button", { name: /ナレッジ不足/ }).click();
  await page.getByLabel("修正した回答", { exact: true }).fill("  窓口へ問い合わせてください。  ");
  await page.getByRole("button", { name: "フィードバックを保存" }).click();

  await expect.poll(() => feedbackPayload).toEqual({
    trace_id: "t1",
    search_answer_profile_id: "bv-1",
    target_type: "answer",
    source_surface: "chat",
    document_id: null,
    chunk_id: null,
    message_id: "a1",
    content_snapshot: null,
    rating: "not_helpful",
    reason: "missing_knowledge",
    comment: null,
    corrected_answer: "窓口へ問い合わせてください。",
  });
  await expect(notHelpful).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("保存済み・変更できます")).toBeVisible();
});

test("3モデルはカード幅を維持して次の行へ折り返す", async ({ page }) => {
  await openPersistedConversation(page, 2048, [userMessage, ...comparisonReplies]);

  const [first, second, third] = await modelCardBoxes(page, [
    "xai.grok-4.3",
    "google.gemini-2.5-pro",
    "cohere.command-a",
  ]);
  expect(Math.abs(first.y - second.y)).toBeLessThanOrEqual(1);
  expect(third.y).toBeGreaterThan(first.y + first.height);
  expect(Math.min(first.width, second.width, third.width)).toBeGreaterThanOrEqual(560);
  expect(third.width).toBeGreaterThan(first.width * 1.8);
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1440 },
  { name: "mobile", width: 375 },
]) {
  test(`複数モデルは狭い領域で縦並びになる (${viewport.name})`, async ({ page }) => {
    await openPersistedConversation(page, viewport.width, [
      userMessage,
      ...comparisonReplies.slice(0, 2),
    ]);

    const [first, second] = await modelCardBoxes(page, ["xai.grok-4.3", "google.gemini-2.5-pro"]);
    expect(second.y).toBeGreaterThan(first.y + first.height);
    await expectNoPageOverflow(page);
  });
}

test("検索・回答プロファイル未選択ではチャットを促す空状態を出す", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page);

  await page.goto("/chat");
  await expect(
    page.getByText("検索・回答プロファイルを選択するとチャットを始められます。")
  ).toBeVisible();
  await expect(page.locator("[data-chat-toolbar] #chat-search-answer-profile")).toBeVisible();
  await page.getByRole("textbox", {name: "質問"}).fill("合成の質問");
  await expect(page.getByRole("button", {name: "送信", exact: true})).toBeDisabled();
  await expectNoPageOverflow(page);
});

test("会話一覧の読み込み中状態をカード内に表示する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const releaseConversationList = await mockChat(page, "loading");

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await expectChatWorkspaceLayout(page, "mobile");
  await openChatHistory(page);

  // 読み込み中は文言と経過時間（TimedLoadingState）と行の形の Skeleton を出す（#265）。
  const loading = page.getByTestId("chat-conversations-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText("会話を読み込んでいます");
  await expect(loading.getByRole("timer")).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "会話を読み込んでいます" })).toHaveCount(1);
  await expectNoPageOverflow(page);

  releaseConversationList();
});

test("会話一覧の読み込み失敗時に再試行可能なエラーを表示する", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page, "error");

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await openChatHistory(page);

  const error = page.getByRole("alert").filter({ hasText: "会話一覧を読み込めませんでした。" });
  await expect(error).toBeVisible({ timeout: 10_000 });
  await expect(error.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`会話名をキーボードで変更・取消できる (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockChat(page, "ready", [userMessage, assistantMessage]);
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");

    const sessions = await openChatHistory(page);
    const rename = sessions.getByRole("button", { name: "「経費の上限は？」の名前を変更" });
    if (viewport.name === "desktop") await sessions.getByRole("listitem").hover();
    await rename.click();

    const input = sessions.getByRole("textbox", { name: "会話名" });
    await input.fill("経費精算ルール");
    await input.press("Enter");
    await expect(sessions.getByText("経費精算ルール", { exact: true })).toBeVisible();

    await sessions
      .getByRole("button", { name: "「経費精算ルール」の名前を変更" })
      .click();
    await input.fill("保存しない名前");
    await input.press("Escape");
    await expect(sessions.getByText("経費精算ルール", { exact: true })).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`会話を確認ダイアログを通して削除できる (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockChat(page, "ready", [userMessage, assistantMessage]);
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");

    const sessions = await openChatHistory(page);
    const remove = sessions.getByRole("button", { name: "「経費の上限は？」を削除" });
    if (viewport.name === "desktop") await sessions.getByRole("listitem").hover();

    // 取消では消えない。
    await remove.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("この操作は取り消せません");
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(sessions.getByRole("listitem")).toHaveCount(1);

    if (viewport.name === "desktop") await sessions.getByRole("listitem").hover();
    await remove.click();
    await dialog.getByRole("button", { name: "削除" }).click();
    await expect(page.getByText("会話を削除しました。")).toBeVisible();
    await expect(sessions.getByText("まだ会話がありません。", { exact: false })).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

test("会話名変更の失敗を入力欄直下へ表示する", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page, "ready", [userMessage, assistantMessage], { renameFails: true });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");

  const sessions = await openChatHistory(page);
  await sessions.getByRole("listitem").hover();
  await sessions.getByRole("button", { name: "「経費の上限は？」の名前を変更" }).click();
  const input = sessions.getByRole("textbox", { name: "会話名" });
  await input.fill("変更後");
  await input.press("Enter");
  await expect(
    sessions.getByRole("alert").filter({ hasText: "会話名を変更できませんでした。" })
  ).toBeVisible();
  await expect(input).toBeFocused();
});

test("未送信の会話があれば新しい会話を増やさず再利用する", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  let createRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/chat/conversations"
    ) {
      createRequests += 1;
    }
  });
  await mockChat(page);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");

  const newConversation = page.getByRole("button", { name: "新しい会話", exact: true });
  await newConversation.click();
  const history = await openChatHistory(page);
  await expect(
    history.getByRole("list", { name: "会話の履歴" }).getByText("新しい会話", { exact: true })
  ).toBeVisible();
  await history.getByRole("button", { name: "会話の履歴を閉じる" }).click();
  await expect(history).toBeHidden();
  await newConversation.click();
  await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeFocused();
  expect(createRequests).toBe(1);
  await expectNoPageOverflow(page);
});

test("長い日本語の会話名でも一覧が横へはみ出さない", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockChat(page, "ready", [userMessage, assistantMessage], {
    initialTitle: "経費精算と国内外出張に関する承認ルールおよび例外申請の確認".repeat(2),
  });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await openChatHistory(page);
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(
    `回答に未反映の関連章節の各行から、その章節の原文を開ける (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const label = "「点検要領.pdf」の「第3章 設備ごとの点検」（p.5〜7）";
      const line = `${label}は、読み込み量の上限に達したため未反映です。`;
      const text = [
        "点検は月に 1 回行います。",
        "",
        "資料により確認できた事項",
        "",
        "・点検は月に 1 回行います。",
        "根拠：点検要領.pdf p.2",
        "",
        "回答に未反映の関連章節",
        "",
        `・${line}`,
      ].join("\n");
      // 読めなかった章節は回答の診断の section_expansion.unread に、本文の行と同じ文で載る（#2040）。
      const answerDiagnostics = {
        section_expansion: {
          unread: [{ document_id: "doc-9", section_id: "s3", label, reason: "token_budget", page: 5, line }],
        },
        execution_steps: [],
        evidence_tree: [],
      };
      const streamBody = [
        sseStart,
        `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text })}\n\n`,
        `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], answer_diagnostics: answerDiagnostics })}\n\n`,
        `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
      ].join("");
      await mockChat(page, "ready", [], { streamBody });

      await page.goto("/chat");
      await selectSearchAnswerProfile(page, "経理アシスタント");
      await page.getByRole("button", { name: "新しい会話" }).click();
      await page.getByRole("textbox").fill(userMessage.content);
      await page.getByRole("button", { name: "送信" }).click();

      const answer = page.getByTestId("answer-text");
      await expect(answer.getByRole("heading", { name: "回答に未反映の関連章節" })).toBeVisible();
      await expect(answer.getByText(line)).toBeVisible();
      // 読み上げで何を開くか分かる名前（見える文言「原文を開く」を含む）。キーボードで開ける。
      const open = answer.getByRole("link", { name: `${label}の原文を開く` });
      await expect(open).toHaveText("原文を開く");
      await expect(open).toHaveAttribute("href", "/documents/doc-9?page=5");
      await open.focus();
      const popupPromise = page.waitForEvent("popup");
      await page.keyboard.press("Enter");
      const popup = await popupPromise;
      await expect.poll(() => new URL(popup.url()).pathname + new URL(popup.url()).search).toBe("/documents/doc-9?page=5");
      await popup.close();
      await expectNoPageOverflow(page);
    }
  );
}

// #2361: 「資料から確認できなかった事項」は Agent の除いた内容の節と同じ規則（共通の AnswerSections）。項目が多ければ
// 先頭の 10 件だけを出し、「すべて表示（全 N 件）」で残りを同じ書式で開く（「ほか N 件」で切らない）。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`資料から確認できなかった事項が多い回答は先頭だけを出し、すべて表示で全件を開く (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const gaps = Array.from({ length: 12 }, (_, index) => `合成の条件 ${index + 1} は資料に記載がありません。`);
    const text = [
      "点検は月に 1 回行います。",
      "",
      "資料により確認できた事項",
      "",
      "・点検は月に 1 回行います。",
      "根拠：点検要領.pdf p.2",
      "",
      "資料から確認できなかった事項",
      "",
      ...gaps.map((gap) => `・${gap}`),
    ].join("\n");
    const streamBody = [
      sseStart,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text })}\n\n`,
      `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [] })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
    ].join("");
    await mockChat(page, "ready", [], { streamBody });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill(userMessage.content);
    await page.getByRole("button", { name: "送信" }).click();

    const answer = page.getByTestId("answer-text");
    const sections = answer.getByTestId("answer-section");
    await expect(sections.locator("h4")).toHaveText(["資料により確認できた事項", "資料から確認できなかった事項"]);
    // 確認できた事項の節は畳まない。
    await expect(sections.nth(0).getByTestId("answer-section-toggle")).toHaveCount(0);
    const items = sections.nth(1).getByRole("listitem");
    await expect(items).toHaveCount(10);
    const toggle = sections.nth(1).getByTestId("answer-section-toggle");
    await expect(toggle).toHaveText(`すべて表示（全 ${gaps.length} 件）`);
    await toggle.click();
    await expect(items).toHaveText(gaps);
    await expect(toggle).toHaveText("折りたたむ");
    await expectNoPageOverflow(page);
  });
}

// #2738: 根拠と矛盾した段落だけを本文から外し、確かめられない段落（未確認）と規則の検査の段落（要注意）は本文に
// 残して印を付ける（Agent と同じ共有の部品）。印を押すと理由、「確認が必要な点」から本文の段落へ移る。回答全体の注意は
// 本文の上、印の数のバッジは本文の下。内容は合成のデータ。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`矛盾した段落だけを外し、確かめられない段落に印・理由・確認が必要な点・バッジを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const unverified = "合成の手続きは翌日に自動で通知されます。";
    const text = [
      "合成の手続きは 2 段階です。",
      "",
      "資料により確認できた事項",
      "",
      "・対象の行の終了日を入力します。",
      "根拠：合成の文書 A.pdf p.2",
      "・新しい開始日の行を追加します。",
      "根拠：合成の文書 A.pdf p.3",
      "",
      unverified,
    ].join("\n");
    const answerDiagnostics = {
      execution_steps: [],
      evidence_tree: [],
      passage_marks: [
        {
          text: "・新しい開始日の行を追加します。",
          state: "warning",
          reasons: [{ code: "value_unbound", label: "根拠の表の対応と違う可能性", detail: "合成の理由 A。" }, { code: "value_unbound", label: "根拠の表の対応と違う可能性", detail: "合成の理由 C。" }],
        },
        {
          text: unverified,
          state: "unverified",
          reasons: [{ code: "unsupported", label: "根拠で確かめられない", detail: "" }],
        },
      ],
      removed_passages: [
        { text: "合成の変更は翌月から反映されます。", reasons: [{ code: "contradicted", label: "根拠と矛盾", detail: "合成の理由 B。" }] },
      ],
      answer_warnings: [{ code: "off_goal", label: "質問からずれている可能性", detail: "" }],
    };
    const streamBody = [
      sseStart,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text })}\n\n`,
      `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], answer_diagnostics: answerDiagnostics })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
    ].join("");
    await mockChat(page, "ready", [], { streamBody });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill("合成の質問です。");
    await page.getByRole("button", { name: "送信" }).click();

    await expect(page.getByText("複数のモデルの回答を並べて比べることもできる")).toHaveCount(0);
    const answer = page.getByTestId("answer-text");
    // 回答全体の注意は本文の上の warning の Banner。
    await expect(answer.getByTestId("answer-passage-warnings")).toHaveText("質問からずれている可能性");
    const marked = answer.getByTestId("answer-passage-mark");
    await expect(marked).toHaveCount(2);
    await expect(marked.nth(0)).toHaveAttribute("data-passage-state", "warning");
    await expect(marked.nth(0)).toContainText("新しい開始日の行を追加します。");
    // 印の付いた項目の根拠の行は残る。
    await expect(marked.nth(0)).toContainText("根拠：合成の文書 A.pdf p.3");
    await expect(marked.nth(1)).toHaveAttribute("data-passage-state", "unverified");
    await expect(marked.nth(1)).toContainText(unverified);
    const toggle = marked.nth(0).getByTestId("answer-passage-mark-toggle");
    await expect(toggle).toHaveText("要注意の理由");
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(marked.nth(0).getByTestId("answer-passage-mark-reasons")).toHaveText("根拠の表の対応と違う可能性合成の理由 A。根拠の表の対応と違う可能性合成の理由 C。");
    // 詳しい理由（detail）の無い印は短い理由だけを出す。
    await marked.nth(1).getByTestId("answer-passage-mark-toggle").click();
    await expect(marked.nth(1).getByTestId("answer-passage-mark-reasons")).toHaveText("根拠で確かめられない");

    await expect(answer.getByTestId("answer-removed-passages").getByTestId("answer-removed-passage")).toHaveText(
      "「合成の変更は翌月から反映されます。」根拠と矛盾：合成の理由 B。"
    );
    const points = answer.getByTestId("answer-marked-passages");
    await expect(points.locator("h4")).toHaveText("確認が必要な点");
    await expect(points.getByTestId("answer-marked-passage")).toHaveText([
      "「新しい開始日の行を追加します。」要注意：根拠の表の対応と違う可能性",
      `「${unverified}」未確認：根拠で確かめられない`,
    ]);
    await points.getByTestId("answer-marked-passage-link").nth(1).click();
    await expect(marked.nth(1)).toBeFocused();

    // 印の数のバッジは本文の下（Agent の「資料との照合」と同じ部品・文言）。
    const badges = page.getByTestId("answer-passage-badges").locator("[data-status-variant]");
    await expect(badges).toHaveText(["未確認 1 件", "要注意 1 件", "矛盾で除外 1 件"]);
    await expect(badges.nth(0)).toHaveAttribute("data-status-variant", "warning");
    await expect(badges.nth(2)).toHaveAttribute("data-status-variant", "danger");
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-marks-rag-chat-${viewport.name}.png`), fullPage: true });
  });
}

// #2096 / #3226: 回答の処理の経過は、回答の trace の span の木（工程の span は種類 answer_step と工程名・LLM の回数）を
// 1 か所に出す。名前は「処理の経過」1 つで、処理の経過 → 回答の本文 → 根拠の一覧 → 評価 の順（#2115）。
for (const theme of ["light", "dark"] as const) {
  test(`保存した回答の処理の経過は工程の一覧を閉じて出し、開くと入れ子と計測値を出す (${theme})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    const at = (seconds: number) => new Date(Date.parse("2026-01-01T00:00:01Z") + seconds * 1000).toISOString();
    let seq = 0;
    // span のイベント（step_id は span の id、親子は parent_id。実行の全体の span は行にしない。#3226）。
    const step = (
      stepId: string,
      kind: string,
      start: number,
      end: number,
      params?: Record<string, string | number>,
      details?: Record<string, unknown>,
      parent = "s0"
    ) => ({
      schema_version: 1,
      seq: ++seq,
      target_id: "a1",
      attempt: 0,
      emitted_at: at(end),
      type: "step",
      step_id: stepId,
      kind,
      status: "done",
      started_at: at(start),
      finished_at: at(end),
      ...(params ? { params } : {}),
      ...(details ? { details } : {}),
      name: kind === "answer_step" ? "rag.engine.stage" : `rag.${kind}`,
      parent_id: parent,
      trace_id: SPAN_TRACE_ID,
    });
    const progress = [
      { ...step("s0", "s0", 0, 9), name: "rag.answer", parent_id: undefined, root: true },
      // 前の会話を踏まえて質問を補うの詳細（#2341。パイプラインの工程の詳細）。
      step("s1", "history_rewrite", 0, 0.4, undefined, {
        fields: [
          { label: "参照した会話", value: "2 件の発言" },
          { label: "書き換えた質問", value: "合成の書き換えた質問" },
        ],
      }),
      step("s2", "answer_step", 0.4, 1.2, { name: "質問の理解", llm_calls: 1 }),
      step("s3", "answer_step", 1.2, 9, { name: "回答生成フロー", llm_calls: 2 }),
      // 工程の詳細（見出しと値・行。#2294 / #2341。backend が回答の記録の工程の詳細を段の詳細にする）。
      step(
        "s4",
        "answer_step",
        1.3,
        1.34,
        { name: "文書検索", note: "該当なし" },
        { fields: [{ label: "検索の候補", value: "0 件" }, { label: "結果", value: "回答に渡す根拠 0 件" }], lines: ["検索文: 2 本"] },
        "s3"
      ),
      step("s5", "answer_step", 2, 9, { name: "回答文の生成と根拠確認（1回目）", llm_calls: 2 }, undefined, "s3"),
      { schema_version: 1, seq: ++seq, target_id: "a1", attempt: 0, emitted_at: at(9), type: "terminal", status: "done" },
    ];
    await page.emulateMedia({ colorScheme: theme });
    await mockChat(page, "ready", [userMessage, { ...assistantMessage, progress }]);

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const history = await openChatHistory(page);
    await history.getByRole("button", { name: /^経費の上限は？ 2件/ }).click();
    if (await history.isVisible().catch(() => false)) {
      // lg 未満の履歴はシート。会話を選ぶと閉じる。
      await expect(page.getByRole("dialog", { name: "会話の履歴" })).toHaveCount(0);
    }

    const summary = page.getByTestId("chat-answer-progress-summary");
    await expect(summary).toHaveText("処理の経過（5 ステップ・9.0 秒）");
    const process = page.getByRole("region", { name: "処理の経過" });
    // 完了した回答は一覧を閉じて出し（#2118）、押すと開く。
    await expect(process.locator("details").first()).not.toHaveAttribute("open", "");
    await summary.click();
    await expect(process.locator("details").first()).toHaveAttribute("open", "");
    const nested = page.getByTestId("chat-answer-progress-step-s4");
    await expect(nested).toHaveAttribute("data-level", "1");
    await expect(nested).toContainText("検索文から根拠の候補を集める");
    // 計測値（工程の結果・LLM の回数）は名前の後ろの括弧、所要時間は右の列（実行の詳細と同じ RunTimeline。#2265）。
    await expect(nested).toContainText("該当なし");
    await expect(nested).toContainText("0.1 秒未満");
    const understood = page.getByTestId("chat-answer-progress-step-s2");
    await expect(understood).toContainText("LLM 1 回");
    await expect(understood).toContainText("0.8 秒");
    // 工程の行は span なので、開くと工程の詳細の後ろに技術の値（span の名前）が付く（既定で閉じる。#2294 / #3226）。
    await inspectTraceStep(page, process, "s2");
    await expect(page.locator("[data-trace-technical]")).toContainText("rag.engine.stage");
    await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
    const nestedToggle = page.getByTestId("chat-answer-progress-step-s4-toggle");
    await expect(nestedToggle).toHaveAttribute("aria-expanded", "false");
    await nestedToggle.click();
    await expect(nestedToggle).toHaveAttribute("aria-expanded", "true");
    const nestedDetails = page.getByTestId("chat-answer-progress-step-s4-details");
    // 文字の横のアイコン（状態・開閉の印）は、文字の 1 行目の中心にそろえる（#2371）。
    expect(await findIconTextMisalignment(page, { root: '[data-testid="chat-answer-progress"]' })).toEqual([]);
    await expect(nestedDetails.locator("dt").first()).toHaveText("検索の候補");
    await expect(nestedDetails).toContainText("回答に渡す根拠 0 件");
    await expect(nestedDetails).toContainText("検索文: 2 本");
    await nestedToggle.click();
    await expect(nestedDetails).toBeHidden();
    // パイプラインの工程（前の会話を踏まえて質問を補う）の詳細も同じ形で開ける（#2341）。
    await page.getByTestId("chat-answer-progress-step-s1-toggle").click();
    await expect(page.getByTestId("chat-answer-progress-step-s1-details")).toContainText("合成の書き換えた質問");
    await page.getByTestId("chat-answer-progress-step-s1-toggle").click();
    // 完了した一覧は時間の線つき（375px は線を隠し、所要時間の文字だけ）。
    const bars = process.locator("[data-run-timeline-bar]");
    await expect(bars).toHaveCount(5);
    if ((page.viewportSize()?.width ?? 1280) >= 1280) await expect(bars.first()).toBeVisible();
    else await expect(bars.first()).toBeHidden();
    await expect(page.getByTestId("chat-answer-progress-step-s1")).toContainText("前の会話を踏まえて質問を補う");
    // 状態は英語の生の値ではなくアイコンと読み上げの文言で示す。「実行記録」の表示は無い。
    await expect(process.getByText(/complete/)).toHaveCount(0);
    await expect(page.getByText(/実行記録/)).toHaveCount(0);
    // 処理の経過 → 回答の本文 → 根拠の一覧 → 評価 の順（処理の経過は回答の上。#2115）。
    const [answerBox, citationBox, processBox, feedbackBox] = await Promise.all([
      page.getByText(assistantMessage.content).first().boundingBox(),
      page.locator("summary").filter({ hasText: "AI に渡した 1 件" }).boundingBox(),
      summary.boundingBox(),
      page.getByRole("group", { name: "この回答は役に立ちましたか？" }).boundingBox(),
    ]);
    expect(processBox!.y).toBeLessThan(answerBox!.y);
    expect(answerBox!.y).toBeLessThan(citationBox!.y);
    expect(citationBox!.y).toBeLessThan(feedbackBox!.y);
    // 字下げは位置でも分かる（入れ子の行の左端が親より右）。
    const [parentLeft, childLeft] = await Promise.all([
      page.getByTestId("chat-answer-progress-step-s3").locator("svg").first().boundingBox(),
      nested.locator("svg").first().boundingBox(),
    ]);
    expect(childLeft!.x).toBeGreaterThan(parentLeft!.x);
    await expectNoPageOverflow(page);
  });
}
test("回答の根拠は Agent と同じ一覧（まとまりの表示・タブなし）で、カードの「根拠を表示」でパネルを開く（#2388）", { tag: "@responsive" }, async ({ page }, testInfo) => {
  // chunk の ID は「文書 ID:chunk set の hash:番号」の長い文字列。既定では画面に出さない。
  const longId = (n: number) => `doc-1:${"a1b2c3d4".repeat(8)}:${n}`;
  const evidenceChunk = (n: number, text: string, metadata: Record<string, unknown>) => ({
    document_id: "doc-1",
    chunk_id: longId(n),
    text,
    score: 0.5,
    rerank_score: 0.8,
    file_name: "経費規程.pdf",
    category_name: null,
    metadata,
  });
  const citations = [
    evidenceChunk(1, "1 回の申請の上限は 10 万円です。", {
      page_start: 2,
      section_title: "第 2 章 上限",
      evidence_role: "retrieved_anchor",
      evidence_model_used: true,
      evidence_answer_use: "confirmed",
    }),
    evidenceChunk(2, "上限を超える場合は事前に申請します。", {
      page_start: 2,
      evidence_role: "neighbor_context",
      evidence_model_used: false,
    }),
    evidenceChunk(3, "申請の画面で金額を入力します。", {
      page_start: 5,
      evidence_role: "operation_context",
      evidence_model_used: false,
    }),
  ];
  const answerDiagnostics = {
    confidence: "high",
    execution_steps: [],
    evidence_tree: [
      {
        parent_id: "doc-1:p1",
        source: "経費規程.pdf",
        page: 2,
        children: [
          { chunk_id: longId(1), role: "retrieved_anchor", is_model_used: true, answer_use: "confirmed" },
          { chunk_id: longId(2), role: "neighbor_context", is_model_used: false },
        ],
      },
      {
        parent_id: "doc-1:p2",
        source: "経費規程.pdf",
        page: 5,
        children: [{ chunk_id: longId(3), role: "operation_context", is_model_used: false }],
      },
    ],
  };
  const streamBody = [
    sseStart,
    `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "経費の上限は 10 万円です。" })}\n\n`,
    `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], answer_diagnostics: answerDiagnostics })}\n\n`,
    `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations })}\n\n`,
    `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  ].join("");
  await mockChat(page, "ready", [], { streamBody });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("button", { name: "新しい会話" }).click();
  await page.getByRole("textbox").fill(userMessage.content);
  await page.getByRole("button", { name: "送信" }).click();

  // 見出しは Agent と同じ書き方（まとまりの数を出さない。#2388）。
  const summary = page
    .locator("summary")
    .filter({ hasText: "AI に渡した 3 件 ／ 回答で引用 1 件 ／ うち該当確認済み 1 件" });
  await expect(summary).toBeVisible();
  await expect(page.getByText(/のまとまり/)).toHaveCount(0);
  await summary.click();
  const evidence = page.getByTestId("answer-citations");
  // 実行記録の側には根拠の構成を重ねて出さない。
  await expect(page.getByText(/^根拠の構成/)).toHaveCount(0);
  // まとまりの表示（見方の切り替え・まとまりの数・役割）は持たない。文書名と節はカードのチップにある。
  await expect(evidence.getByRole("tab")).toHaveCount(0);
  const cards = evidence.getByTestId("evidence-card");
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(0).getByTestId("evidence-chips")).toContainText("第 2 章 上限");
  await expect(evidence).not.toContainText(/neighbor_context|operation_context|retrieved_anchor|検索で見つかった箇所/);
  await expect(evidence.getByText(longId(1))).toHaveCount(0);
  await expect(cards.nth(0).getByText("回答で引用", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`chat-evidence-list-${testInfo.project.name}.png`) });
  // カードの「根拠を表示」（Agent と同じ名前）は、本文の根拠の行と同じ右のサイドパネル（#2360）を開く。
  await expect(evidence.getByRole("button", { name: "根拠を表示" })).toHaveCount(3);
  const cardOpen = evidence.getByRole("button", { name: "経費規程.pdf の根拠を表示" }).first();
  await cardOpen.click();
  const preview = page.getByRole("dialog", { name: "回答の根拠" });
  await expect(preview).toBeVisible();
  await expect(preview.getByTestId("evidence-source-summary").getByTestId("evidence-file-name")).toHaveText("経費規程.pdf");
  await expect(preview.getByTestId("evidence-source-summary")).toContainText("1 回の申請の上限は 10 万円です。");
  await expect(preview.getByTestId("evidence-source-preview")).toBeVisible();
  // 右端に付き、375px は全画面のシート。
  // 右から滑り出し切るのを待つ。
  const viewport = page.viewportSize()!;
  await expect
    .poll(async () => {
      const box = await preview.boundingBox();
      return box ? Math.round(box.x + box.width) : -1;
    })
    .toBe(viewport.width);
  if (viewport.width < 640) expect(Math.round((await preview.boundingBox())!.x)).toBe(0);
  await page.screenshot({ path: testInfo.outputPath(`chat-citation-source-sheet-${testInfo.project.name}.png`) });
  await preview.getByRole("button", { name: "閉じる" }).click();
  await expect(preview).toBeHidden();
  await expect(cardOpen).toBeFocused();
  // 一覧は一定の高さを超えたら中で縦にスクロールする（共有の bounded-scroll-area-lg）。
  await expect(evidence.locator("ul.bounded-scroll-area-lg")).toHaveCSS("overflow-y", "auto");
  await expectNoPageOverflow(page);
});

test("回答フローの回答ではチャットにも根拠パネルと会話から補った質問を表示する", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const answerDiagnostics = {
    confidence: "high",
    needs_human_review: false,
    insufficient_reason: "",
    reasoning_summary: "引用照合済みの説明 1 件、原文のみ提示 0 件、原文と一致せず除外 0 件。",
    external_data_required: true,
    external_data_items: ["申請者の役職"],
    // 回答の対応と業務ガイド（#1252）。
    outcome: "needs_environment_data",
    guide: { guide_id: "g1", title: "経費の上限を確かめる", revision: 3 },
    question_type: ["規則"],
    auto_field_filter: {
      conditions: [{ name: "金額", value_type: "number", op: "gte", value: "100000" }],
      relaxed: false,
    },
    original_question: "それの上限は？",
    rewritten_question: "経費精算の上限額は？",
    generated_queries: [],
    execution_steps: [{ name: "質問の理解", status: "complete", elapsed_seconds: 0.2, llm_calls: 0 }],
    evidence_tree: [],
    models: {
      llm: { model_id: "m1", label: "MODEL 1" },
      attached_images: 2,
      embedding: "cohere.embed-v4.0",
      rerank: "cohere.rerank-v4.0-fast",
    },
  };
  const streamBody = [
    sseStart,
    `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "経費の上限は 10 万円です。\n\n資料により確認できた事項\n\n・1 回の申請の上限は 10 万円です。\n根拠：経費規程.pdf p.2" })}\n\n`,
    `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], answer_diagnostics: answerDiagnostics })}\n\n`,
    `event: citations\ndata: ${JSON.stringify({
      model_id: "m1",
      citations: [
        {
          document_id: "doc-1",
          chunk_id: "doc-1:c2",
          text: "1 回の申請の上限は 10 万円です。",
          score: 0.5,
          rerank_score: 0.9,
          file_name: "経費規程.pdf",
          category_name: null,
          metadata: { page_start: 2 },
        },
      ],
    })}\n\n`,
    `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  ].join("");
  await mockChat(page, "ready", [], { streamBody });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("button", { name: "新しい会話" }).click();
  await page.getByRole("textbox").fill(userMessage.content);
  await page.getByRole("button", { name: "送信" }).click();

  // 処理の経過は回答の上（作成中の「N ステップ完了」の場所のまま。#2115）、回答の下は AI に渡した箇所 → 評価 の順
  // （#1417）。回答の記録の詳細は処理の経過の中の「処理の詳細」に畳む（名前は「処理の経過」1 つ。旧「回答の実行記録」。#2325）。
  const process = page.getByRole("region", { name: "処理の経過" });
  await expect(process).toBeVisible();
  await expect(page.getByText(/実行記録/)).toHaveCount(0);
  const panel = await openAnswerProcessDetails(page, "chat-answer-progress");
  await expect(panel).toBeVisible();
  const citationSummary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" });
  const feedback = page.getByRole("group", { name: "この回答は役に立ちましたか？" });
  await expect(citationSummary).toBeVisible();
  await expect(feedback).toBeVisible();
  const [citationBox, panelBox, feedbackBox] = await Promise.all([
    citationSummary.boundingBox(),
    process.boundingBox(),
    feedback.boundingBox(),
  ]);
  expect(citationBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(panelBox?.y ?? Number.POSITIVE_INFINITY);
  expect(feedbackBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(citationBox?.y ?? Number.POSITIVE_INFINITY);
  await expect(panel.getByText("会話の流れから補った質問: 経費精算の上限額は？")).toHaveCount(0);
  await expect(panel.getByText("信頼度: high")).toHaveCount(0);
  await expect(panel.getByTestId("answer-outcome-badge")).toHaveCount(0);
  await expect(panel.getByTestId("answer-guide-badge")).toHaveCount(0);
  // 1 列（既定のモデル）でも、どのモデルの回答か・使ったモデルを出す（#649）。
  // 回答モデルへ根拠の画像を添付した回答（診断の models.attached_images）は、見出しにそのことを添える（#3444）。
  await expect(page.getByRole("heading", { name: "回答モデル: MODEL 1（画像も参照）", exact: true })).toBeVisible();
  const models = panel.getByTestId("answer-models");
  await expect(models).toContainText("使用したモデル");
  await expect(models).toContainText("MODEL 1");
  await expect(models).not.toContainText("添付した画像");
  await expect(panel.locator("[data-process-identifier]")).toHaveCount(3);
  await expect(models).toContainText("cohere.embed-v4.0");
  await expect(models).toContainText("cohere.rerank-v4.0-fast");
  // rag_poc の回答 viewer と同じ情報（判断理由・外部データの確認・問い合わせ型）と本文の構成（#651）。
  await expect(panel.getByText("問い合わせ型: 規則")).toHaveCount(0);
  // 質問から読み取った条件（#652）も記録として出す。
  await expect(panel.getByTestId("auto-field-filter")).toHaveCount(0);
  await expect(panel.getByText(/^判断理由: 引用照合済みの説明 1 件/)).toHaveCount(0);
  // 資料では決まらない値の案内は「詳細」に畳まず、回答の本文の直下に出す（#2096）。
  const externalData = page.getByRole("status").filter({ hasText: "業務システムで確かめる値" });
  await expect(externalData).toContainText("申請者の役職");
  await expect(panel.getByRole("status").filter({ hasText: "業務システムで確かめる値" })).toHaveCount(0);
  const answerText = page.getByTestId("answer-text");
  await expect(answerText.getByRole("heading", { name: "資料により確認できた事項" })).toBeVisible();
  // 処理の経過と回答の本文の間に、節の見出しと同じ書式の見出し「回答」（h3）を出す（#3433）。本文の領域の名前にもなる。
  const answerBody = page.getByRole("region", { name: "回答", exact: true });
  const answerHeading = answerBody.getByRole("heading", { level: 3, name: "回答", exact: true });
  await expect(answerHeading).toBeVisible();
  await expect(answerBody.getByTestId("answer-text")).toBeVisible();
  const [processBox, answerHeadingBox, answerTextBox] = await Promise.all([
    process.boundingBox(),
    answerHeading.boundingBox(),
    answerText.boundingBox(),
  ]);
  expect(answerHeadingBox!.y).toBeGreaterThan(processBox!.y);
  expect(answerTextBox!.y).toBeGreaterThan(answerHeadingBox!.y);
  const headingStyles = await page.evaluate(() => {
    const style = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const { fontSize, fontWeight, color, lineHeight } = getComputedStyle(element);
      return { fontSize, fontWeight, color, lineHeight };
    };
    return { answer: style('[data-testid="chat-answer-heading"]'), section: style('[data-testid="answer-text"] section h4') };
  });
  expect(headingStyles.answer).not.toBeNull();
  expect(headingStyles.answer).toEqual(headingStyles.section);
  // 節の見出しの前は段落の間（0.75rem）より広い 1.25rem（17.5px）を空け、要約の段落と節を見分けられる（#2325）。
  const headingGap = await answerText.evaluate((element) => {
    const paragraph = element.querySelector(":scope > p");
    const heading = element.querySelector(":scope > section h4");
    if (!paragraph || !heading) return null;
    return heading.getBoundingClientRect().top - paragraph.getBoundingClientRect().bottom;
  });
  expect(headingGap).not.toBeNull();
  expect(headingGap!).toBeCloseTo(17.5, 0);
  await expect(answerText.getByRole("listitem")).toContainText("1 回の申請の上限は 10 万円です。");
  await expect(answerText.getByText("根拠：経費規程.pdf p.2")).toBeVisible();
  // 根拠の行から、当たる引用の原文のプレビューを右のサイドパネルで開ける（#657 / #2360）。Esc で閉じると
  // フォーカスは根拠の行へ戻る。
  const citationButton = answerText.getByRole("button", { name: "根拠：経費規程.pdf p.2 の原文を開く" });
  await citationButton.focus();
  await page.keyboard.press("Enter");
  const preview = page.getByRole("dialog", { name: "回答の根拠" });
  await expect(preview).toBeVisible();
  await expect(preview.getByTestId("evidence-source-summary").getByTestId("evidence-file-name")).toHaveText("経費規程.pdf");
  await page.keyboard.press("Escape");
  await expect(preview).toBeHidden();
  await expect(citationButton).toBeFocused();
});

test("IME の変換を確定する Enter では送信しない（#459）", async ({ page }) => {
  await mockChat(page);
  let calls = 0;
  await page.route("**/api/chat/conversations/*/messages/stream", () => {
    calls += 1;
    return new Promise<void>(() => undefined);
  });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("button", { name: "新しい会話" }).click();

  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("けいひのじょうげん");
  // 変換中の keydown（isComposing=true）は送信しない。
  await composer.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true, bubbles: true });
  await expect(composer).toHaveValue("けいひのじょうげん");
  expect(calls).toBe(0);

  // 変換を確定した後の Enter は送信する。
  await composer.press("Enter");
  await expect.poll(() => calls).toBe(1);
});

test("送信と停止は同じボタンで、生成中の Enter では停止しない（#413）", { tag: "@responsive" }, async ({ page }) => {
  await mockChat(page);
  let calls = 0;
  let aborted = 0;
  page.on("requestfailed", (request) => {
    if (request.url().includes("/messages/stream")) aborted += 1;
  });
  // 応答を返さず、生成中のままにする（mockChat より後に登録したものが優先される）。
  await page.route("**/api/chat/conversations/*/messages/stream", () => {
    calls += 1;
    return new Promise<void>(() => undefined);
  });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("button", { name: "新しい会話" }).click();

  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  const button = page.getByTestId("chat-run-stop");
  await expect(button).toHaveAccessibleName("送信");
  // 入力が空の間は送信できない（フォーカスは受ける）。
  await expect(button).toHaveAttribute("aria-disabled", "true");

  await composer.fill("経費の上限は？");
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  const idleBox = await button.boundingBox();
  await button.click();
  await expect(button).toHaveAccessibleName("停止");
  await expect(button).toBeFocused();
  const runningBox = await button.boundingBox();
  expect(runningBox?.x).toBe(idleBox?.x);
  expect(runningBox?.width).toBe(idleBox?.width);

  // 生成中も次の質問を書ける。Enter では停止も送信もしない。
  await composer.fill("次の質問");
  await composer.press("Enter");
  await page.waitForTimeout(300);
  await expect(button).toHaveAccessibleName("停止");
  await expect(composer).toBeFocused();
  expect(calls).toBe(1);
  expect(aborted).toBe(0);

  // 停止で中断する。フォーカスはボタンに残る。
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(button).toHaveAccessibleName("送信");
  await expect(button).toBeFocused();
  await expect.poll(() => aborted).toBe(1);

  // 停止の後に再び送信できる（書いておいた次の質問）。
  await expect(composer).toHaveValue("次の質問");
  await button.click();
  await expect.poll(() => calls).toBe(2);
  await expect(button).toHaveAccessibleName("停止");
  await expectNoPageOverflow(page);
});

test("会話の履歴は既定で閉じ、開くとチャットの左に並び、開閉の状態が再読込で残る（#664）", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page, "ready", [userMessage, assistantMessage]);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");

  await expectChatWorkspaceLayout(page, "desktop");
  const toggle = page.getByTestId("chat-history-toggle");
  await expect(toggle).toHaveAccessibleName("会話の履歴");
  // 新しい会話は履歴を開かなくても押せる。
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeVisible();

  const history = await openChatHistory(page);
  await expect(page.getByRole("complementary", { name: "会話の履歴" })).toBeVisible();
  const chat = page.getByRole("region", { name: "チャット" });
  const [historyBox, chatBox] = await Promise.all([history.boundingBox(), chat.boundingBox()]);
  if (!historyBox || !chatBox) throw new Error("レイアウトを計測できません。");
  expect(Math.abs(historyBox.y - chatBox.y)).toBeLessThanOrEqual(1);
  expect(chatBox.x).toBeGreaterThan(historyBox.x + historyBox.width);

  // 会話を選んでもインラインのパネルは開いたまま。今の会話の名前は上端に出る。
  await history.getByRole("button", { name: /^経費の上限は？ 2件/ }).click();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("経費の上限は？");
  await expect(history).toBeVisible();

  // 開閉の状態は作業状態として残る（workspace-state.md）。
  await page.reload();
  await expect(page.getByTestId("chat-history")).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await toggle.click();
  await expectChatWorkspaceLayout(page, "desktop");
  await page.reload();
  await expectChatWorkspaceLayout(page, "desktop");
  await expectNoPageOverflow(page);
});

test("375px では会話の履歴をシートで開き、Esc・外側・会話の選択で閉じてフォーカスを開閉ボタンへ戻す（#664）", { tag: "@responsive" }, async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockChat(page, "ready", [userMessage, assistantMessage]);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await expectChatWorkspaceLayout(page, "mobile");

  const toggle = page.getByTestId("chat-history-toggle");
  const sheet = page.getByRole("dialog", { name: "会話の履歴" });

  // キーボードで開く → 閉じるボタンへフォーカス → Tab は中で回る → Esc で閉じてボタンへ戻る。
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(sheet).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(sheet.getByRole("button", { name: "会話の履歴を閉じる" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(sheet.getByRole("button", { name: /^経費の上限は？ 2件/ })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();

  // シートの外側（scrim）を押すと閉じる。
  await toggle.click();
  await expect(sheet).toBeVisible();
  await expectNoPageOverflow(page);
  await page.getByTestId("chat-history-scrim").click({ position: { x: 360, y: 400 } });
  await expect(sheet).toBeHidden();

  // 会話を選ぶと閉じ、今の会話の名前が上端に出る。
  await toggle.click();
  await sheet.getByRole("button", { name: /^経費の上限は？ 2件/ }).click();
  await expect(sheet).toBeHidden();
  await expect(toggle).toBeFocused();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("経費の上限は？");
  await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();

  // モーダルのシートの開閉は残さない（再読込で画面を塞がない）。
  await toggle.click();
  await expect(sheet).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("chat-history-toggle")).toHaveAttribute("aria-expanded", "false");
  await expect(sheet).toBeHidden();
});

test("会話を選ばずに送信すると会話を作って回答する（#664）", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockChat(page);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");

  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  // 会話を選んでいない間は、上端に会話の名前を出さない。
  await expect(page.getByTestId("chat-conversation-title")).toHaveCount(0);
  const created = page.waitForRequest(
    (request) =>
      request.method() === "POST" && new URL(request.url()).pathname === "/api/chat/conversations"
  );
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await expect(composer).toBeEnabled();
  await composer.fill("経費の上限は？");
  await page.getByRole("button", { name: "送信" }).click();
  await created;

  await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("経費の上限は？");
});

const faqSuggestions = [1, 2, 3].map((index) => ({
  id: `faq-${index}`,
  question: `経費の上限について ${index}`,
  matched_question: `経費の上限について ${index}`,
  answer: `承認済みの回答 ${index}`,
  score: 0.8 - index / 100,
  direct: false,
}));

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`近い登録済みQ&Aがあれば、類似問か「どれでもない」を選んでから回答する（#684） (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    const suggestQueries: Record<string, unknown>[] = [];
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", async (route) => {
      suggestQueries.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        json: { data: { suggestions: faqSuggestions }, error_messages: [], warning_messages: [] },
      });
    });
    const streamBodies: Record<string, unknown>[] = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/messages/stream")) {
        streamBodies.push(request.postDataJSON() as Record<string, unknown>);
      }
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("経費の上限は？");
    await page.getByRole("button", { name: "送信" }).click();

    // 回答を作る前に、最大 3 件の類似問と「どれでもない」を出す。選ぶまで送らない（飛ばす操作は無い）。
    const choice = page.getByTestId("chat-approved-faq-choice");
    await expect(choice).toContainText("経費の上限は？");
    await expect(choice.getByRole("button", { name: "このQ&Aで回答する" })).toHaveCount(3);
    await expect(
      choice.getByRole("button", { name: "どれでもない（資料を検索する）" })
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "送信" })).toBeDisabled();
    expect(suggestQueries).toEqual([{ query: "経費の上限は？", purpose: "chat" }]);
    expect(streamBodies).toEqual([]);
    await expectNoPageOverflow(page);

    // 選ぶ前に画面を離れて戻っても（再読込を含む）、同じ質問と候補が残る（作業状態。#702）。
    await page.reload();
    await expect(choice).toContainText("経費の上限は？");
    await expect(choice.getByRole("button", { name: "このQ&Aで回答する" })).toHaveCount(3);
    expect(suggestQueries).toHaveLength(1);

    // 選んだ類似問の id を質問と一緒に送る。
    await choice.getByRole("listitem").nth(1).getByRole("button").click();
    await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    await expect(choice).toHaveCount(0);
    expect(streamBodies[0]).toMatchObject({ content: "経費の上限は？", approved_faq_id: "faq-2" });

    // 「どれでもない」は類似問を使わずに送る。
    await composer.fill("交通費は？");
    await page.getByRole("button", { name: "送信" }).click();
    await page
      .getByRole("button", { name: "どれでもない（資料を検索する）" })
      .click();
    await expect.poll(() => streamBodies.length).toBe(2);
    expect(streamBodies[1]).toMatchObject({ content: "交通費は？" });
    expect(streamBodies[1]).not.toHaveProperty("approved_faq_id");
  });
}


// #737: 登録済みQ&Aから回答したときは、根拠に FAQ の原文（回答した時点）を出典として出す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`登録済みQ&Aの回答では、FAQ の質問と承認済みの回答の原文を出典に出す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const answerDiagnostics = {
      answer_source: "approved_faq",
      approved_faq_question: "経費精算の上限はいくらですか？",
      approved_faq_answer: "1 回の申請の上限は 10 万円です。\n超える場合は部長の承認が必要です。",
      models: { llm: { model_id: "m1", label: "MODEL 1" }, attached_images: 0, embedding: "", rerank: "" },
    };
    const streamBody = [
      sseStart,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "上限は 10 万円です。\n\n（出典: 登録済みQ&A「経費精算の上限はいくらですか？」）" })}\n\n`,
      `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], answer_diagnostics: answerDiagnostics })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
    ].join("");
    await mockChat(page, "ready", [], { streamBody });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill(userMessage.content);
    await page.getByRole("button", { name: "送信" }).click();

    const panel = await openAnswerProcessDetails(page, "chat-answer-progress");
    await expect(panel.getByText("登録済みQ&Aから回答")).toHaveCount(0);
    await expect(panel.getByRole("region", { name: "回答の出典: 登録済みQ&A" })).toHaveCount(0);
    await expect(panel.locator("[data-process-identifier]")).toHaveCount(3);
    await expect(page.getByRole("region", {name: "回答", exact: true})).toContainText("出典: 登録済みQ&A");
    // 文書を検索していないので、空の「根拠の構成」は出さない。
    await expect(panel.getByText(/^根拠の構成/)).toHaveCount(0);
    await expectNoPageOverflow(page);
  });
}

// #717: 類似問の後に、検索・回答プロファイルのルールの確認の質問を出し、選んだ答えを送る。
const clarificationSuggestion = {
  rule_id: "R01",
  rule_title: "期限の確認",
  clarification: {
    question: "どの規程についてのご質問ですか？",
    multiple: true,
    allow_other: true,
    options: [
      {
        id: "travel",
        label: "出張旅費",
        description: "出張旅費規程",
        search_terms: ["出張"],
        premise: "",
        sections: [
          {
            document_id: "doc-travel",
            document_name: "出張旅費規程.pdf",
            section_id: "sec-6",
            title: "第6条 申請と精算",
            page_start: 2,
            page_end: 3,
          },
        ],
      },
      { id: "expense", label: "経費精算", description: "", search_terms: [], premise: "", sections: [] },
    ],
  },
};

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`回答前の確認に答えてから回答し、確認せずに回答もできる（「確認をやめる」は無い。#717 / #1839 / #2311） (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
      route.fulfill({ json: { data: { suggestions: [] }, error_messages: [], warning_messages: [] } })
    );
    const clarifyQueries: Record<string, unknown>[] = [];
    await page.route("**/api/search-answer-profiles/*/clarifications/suggest", async (route) => {
      clarifyQueries.push(route.request().postDataJSON() as Record<string, unknown>);
      // 古い保存の allow_other: false でも「その他（自由入力）」を出す（#1839）。
      const suggestion = {
        ...clarificationSuggestion,
        clarification: { ...clarificationSuggestion.clarification, allow_other: false },
      };
      await route.fulfill({
        json: { data: { suggestion, question_context_token: "synthetic-context-token" }, error_messages: [], warning_messages: [] },
      });
    });
    const streamBodies: Record<string, unknown>[] = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/messages/stream")) {
        streamBodies.push(request.postDataJSON() as Record<string, unknown>);
      }
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("申請の期限は？");
    await page.getByRole("button", { name: "送信" }).click();

    // 3 製品共通の回答前の確認のカード（#1839）。
    const choice = page.getByTestId("chat-clarification-choice");
    await expect(choice.getByRole("heading", { name: "回答の前に確認させてください" })).toBeVisible();
    const prompt = choice.getByRole("heading", { name: "どの規程についてのご質問ですか？" });
    await expect(prompt).toBeVisible();
    // 送信の直後に出るので、確認事項の文へフォーカスを移す。
    await expect(prompt).toBeFocused();
    await expect(prompt).toHaveCSS("outline-style", "none");
    await expect(choice).toContainText("対象: 「出張旅費規程.pdf」第6条 申請と精算 p.2–3");
    const confirm = choice.getByRole("button", { name: "この内容で回答" });
    await expect(confirm).toBeDisabled();
    await expect(confirm).toHaveAccessibleDescription(
      "選択肢を選ぶか、「その他（自由入力）」に書くと押せます。"
    );
    // 確認を終えるのは「確認せずに回答」だけ（質問を取り消すのは入力欄の「停止」。#2311）。
    await expect(choice.getByRole("button")).toHaveText(["この内容で回答", "確認せずに回答"]);
    await expect(choice.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
    await expect(choice.getByRole("textbox", { name: "その他（自由入力）" })).toBeVisible();
    await expect(page.getByRole("button", { name: "送信" })).toBeDisabled();
    expect(clarifyQueries).toEqual([{ query: "申請の期限は？", conversation_id: "conv-1" }]);
    expect(streamBodies).toEqual([]);
    await expectNoPageOverflow(page);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
      await page.evaluate((dark) => document.documentElement.classList.toggle("dark", dark), colorScheme === "dark");
      await choice.screenshot({
        path: test.info().outputPath(`chat-confirm-before-answer-${viewport.name}-${colorScheme}.png`),
      });
    }
    await page.emulateMedia({ colorScheme: "light" });
    await page.evaluate(() => document.documentElement.classList.remove("dark"));

    // 選ぶ前に再読込しても残る（作業状態）。
    await page.reload();
    await expect(prompt).toBeVisible();

    await choice.getByRole("checkbox", { name: /出張旅費/ }).check();
    await choice.getByRole("textbox", { name: "その他（自由入力）" }).fill("海外出張");
    await confirm.click();
    await expect.poll(() => streamBodies.length).toBe(1);
    expect(streamBodies[0]).toMatchObject({
      content: "申請の期限は？",
      question_context_token: "synthetic-context-token",
      clarification: { rule_id: "R01", option_ids: ["travel"], other_text: "海外出張" },
    });
    await expect(choice).toHaveCount(0);

    // 「確認せずに回答」は確認を使わずに送り、飛ばしたことだけを会話に残す（前提を示して回答する）。
    await composer.fill("交通費の期限は？");
    await page.getByRole("button", { name: "送信" }).click();
    await expect(choice.getByRole("button", { name: "確認せずに回答" })).toHaveAccessibleDescription(
      /前提を示して回答します。/
    );
    await choice.getByRole("button", { name: "確認せずに回答" }).click();
    await expect.poll(() => streamBodies.length).toBe(2);
    // 質問はそのまま送り（入力欄には戻さない）、取り消さない。
    expect(streamBodies[1]).toMatchObject({ content: "交通費の期限は？", clarification_skipped_rule_id: "R01", question_context_token: "synthetic-context-token" });
    expect(clarifyQueries[1]).toMatchObject({ conversation_id: "conv-1" });
    expect(streamBodies[1]).not.toHaveProperty("clarification");
    await expect(choice).toHaveCount(0);
    await expect(composer).toHaveValue("");
  });

  test(`回答前に確認した内容を、会話の利用者の側に残す（#1839） (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    const answered = {
      ...userMessage,
      clarification: {
        outcome: "answered",
        entries: [
          { prompt: "どの規程についてのご質問ですか？", selected: ["出張旅費"], other_text: "海外出張" },
          // 選択肢の無い確認事項（自由入力だけで答える）の答えは「その他: 」を付けない（#3410）。
          { prompt: "対象の名前を教えてください。", selected: [], other_text: "名前 A", free_text_only: true },
        ],
      },
    };
    await page.route(
      (url) => url.pathname === "/api/chat/conversations/conv-1",
      (route) =>
        route.fulfill({
          json: {
            data: conversationDetail([answered, assistantMessage]),
            error_messages: [],
            warning_messages: [],
          },
        })
    );
    await page.goto("/chat?search_answer_profile_id=bv-1&conversation_id=conv-1");
    const record = page.getByTestId("chat-clarification-record");
    await expect(record.getByRole("group", { name: "回答前に確認した内容" })).toBeVisible();
    await expect(record).toContainText("どの規程についてのご質問ですか？");
    await expect(record).toContainText("出張旅費、その他: 海外出張");
    await expect(record).toContainText("対象の名前を教えてください。");
    await expect(record).toContainText("名前 A");
    await expect(record).not.toContainText("その他: 名前 A");
    await expectNoPageOverflow(page);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
      await page.evaluate((dark) => document.documentElement.classList.toggle("dark", dark), colorScheme === "dark");
      await record.screenshot({
        path: test.info().outputPath(`chat-clarification-record-${viewport.name}-${colorScheme}.png`),
      });
    }
  });
}

test("範囲を絞った回答の下から、範囲を指定せずに同じ質問を送り直せる（#721）", async ({ page }) => {
  await mockChat(page);
  await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
    route.fulfill({ json: { data: { suggestions: [] }, error_messages: [], warning_messages: [] } })
  );
  const clarifyQueries: unknown[] = [];
  await page.route("**/api/search-answer-profiles/*/clarifications/suggest", async (route) => {
    clarifyQueries.push(route.request().postDataJSON());
    await route.fulfill({ json: { data: { suggestion: null }, error_messages: [], warning_messages: [] } });
  });
  // 保存された回答は、確認で絞った範囲を末尾に持つ。
  const scoped = { ...assistantMessage, content: "期限は 1 か月です。（対象: 「出張旅費規程.pdf」の「第6条 申請と精算」）" };
  await page.route(
    (url) => url.pathname === "/api/chat/conversations/conv-1",
    (route) =>
      route.fulfill({
        json: { data: conversationDetail([userMessage, scoped]), error_messages: [], warning_messages: [] },
      })
  );
  const streamBodies: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/messages/stream")) {
      streamBodies.push(request.postDataJSON() as Record<string, unknown>);
    }
  });

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("textbox", { name: "質問", exact: true }).fill("経費の上限は？");
  await page.getByRole("button", { name: "送信" }).click();
  await expect.poll(() => streamBodies.length).toBe(1);

  const askAgain = page.getByRole("button", { name: "範囲を指定せずに質問し直す" });
  await expect(askAgain).toBeVisible();
  const suggestCalls = clarifyQueries.length;
  await askAgain.click();
  await expect.poll(() => streamBodies.length).toBe(2);
  // 類似問・確認の質問を出さずに、同じ質問をそのまま送る。
  expect(streamBodies[1]).toMatchObject({ content: "経費の上限は？" });
  expect(streamBodies[1]).not.toHaveProperty("clarification");
  expect(streamBodies[1]).not.toHaveProperty("approved_faq_id");
  expect(clarifyQueries).toHaveLength(suggestCalls);
});

// #907: 送った質問は、会話の作成・回答（start）を待たずにすぐ会話の欄の末尾へ出す（楽観的な表示）。
// 失敗・停止のときも質問を残し、失敗は「送信できませんでした」と「再送信」を出す。

/** テストの中で応答を止めておき、好きなときに返す。 */
function gate() {
  let open: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { open, opened };
}

/** 回答の作成中の表示だけが回る（動くスピナーは 1 つ。messaging.md §3.7）。 */
async function expectSingleChatSpinner(page: Page) {
  await expect(page.getByTestId("chat-messages").locator("svg.animate-spin:visible")).toHaveCount(1);
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
}

async function expectChatScrolledToEnd(page: Page) {
  await expect
    .poll(() =>
      page
        .getByTestId("chat-messages")
        .evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)
    )
    .toBeLessThanOrEqual(2);
}

const chatComposer = (page: Page) => page.getByRole("textbox", { name: "質問", exact: true });

async function screenshotBothThemes(page: Page, target: Locator, path: (theme: string) => string) {
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value;
    }, theme);
    await target.screenshot({ path: path(theme) });
  }
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`送った質問は会話の作成と回答を待たずにすぐ会話の欄へ出る（#907） (${viewport.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    const create = gate();
    const stream = gate();
    await page.route("**/api/chat/conversations", async (route) => {
      if (route.request().method() === "POST") await create.opened;
      await route.fallback();
    });
    await page.route("**/api/chat/conversations/*/messages/stream", async (route) => {
      await stream.opened;
      await route.fallback();
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const empty = page.getByText("質問を入力して会話を始めます");
    await expect(empty).toBeVisible();

    const composer = chatComposer(page);
    await composer.fill("経費の上限は？");
    await composer.press("Enter");

    // 会話の作成の応答の前に、質問と回答の作成中の表示が出る。空の状態はすぐ消える。
    const live = page.getByTestId("chat-live-turn");
    await expect(live.locator('[data-status="sending"]')).toHaveText("経費の上限は？");
    await expect(live.getByTestId("chat-answer-progress")).toBeVisible();
    // 処理の経過だけの間（回答の本文が届く前）は見出し「回答」を出さない（#3433）。
    await expect(live.getByTestId("chat-answer-heading")).toHaveCount(0);
    await expect(empty).toHaveCount(0);
    await expect(page.getByRole("log", { name: "会話" })).toContainText("経費の上限は？");
    await expect(composer).toHaveValue("");
    await expect(composer).toBeFocused();
    await expect(page.getByTestId("chat-run-stop")).toHaveAccessibleName("停止");
    await expectSingleChatSpinner(page);
    await expectChatScrolledToEnd(page);
    await expectNoPageOverflow(page);
    await screenshotBothThemes(page, page.getByRole("region", { name: "チャット" }), (theme) =>
      testInfo.outputPath(`chat-sending-${viewport.name}-${theme}.png`)
    );

    // 会話ができても回答（start）の前は、同じ質問を出し続ける。
    create.open();
    await expect(page.getByTestId("chat-conversation-title")).toBeVisible();
    await expect(live.locator('[data-status="sending"]')).toHaveText("経費の上限は？");

    // 回答が届いたら同じ場所に流し込み、取り直した会話でも質問を二重に出さない。
    stream.open();
    await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    // 回答の本文が届いたら、本文の前に見出し「回答」を出す（#3433）。
    await expect(page.getByTestId("chat-answer-heading")).toHaveText("回答");
    await expect(page.getByTestId("chat-live-turn")).toHaveCount(0);
    const messages = page.getByTestId("chat-messages");
    await expect(messages.getByText("経費の上限は？", { exact: true })).toHaveCount(1);
    await expect(page.getByTestId("chat-run-stop")).toHaveAccessibleName("送信");
  });

  test(`続きの会話でも、送った質問は回答を待たずに末尾へ出る（#907） (${viewport.name})`, async ({ page }) => {
    await openPersistedConversation(page, viewport.width, [userMessage, assistantMessage]);
    if (viewport.width < 1024) await page.keyboard.press("Escape");
    await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    await page.route("**/api/chat/conversations/*/messages/stream", () => new Promise<void>(() => undefined));

    const composer = chatComposer(page);
    await composer.fill("交通費も含まれますか？");
    await composer.press("Enter");

    const live = page.getByTestId("chat-live-turn");
    await expect(live.locator('[data-status="sending"]')).toHaveText("交通費も含まれますか？");
    await expect(live.getByTestId("chat-answer-progress")).toBeVisible();
    // 前の質問と回答の後（末尾）に出る。
    const text = await page.getByTestId("chat-messages").innerText();
    expect(text.indexOf("経費の上限は 10 万円です。")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("経費の上限は 10 万円です。")).toBeLessThan(text.indexOf("交通費も含まれますか？"));
    await expect(composer).toHaveValue("");
    await expectSingleChatSpinner(page);
    await expectChatScrolledToEnd(page);
    await expectNoPageOverflow(page);
  });

  test(`送信できなかった質問は残し、再送信できる（#907） (${viewport.name})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    let streamCalls = 0;
    await page.route("**/api/chat/conversations/*/messages/stream", async (route) => {
      streamCalls += 1;
      if (streamCalls === 1) {
        await route.fulfill({
          status: 503,
          json: {
            data: null,
            error_messages: ["回答を作成できませんでした。時間をおいて再送信してください。"],
            warning_messages: [],
          },
        });
        return;
      }
      await route.fallback();
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = chatComposer(page);
    await composer.fill("経費の上限は？");
    await composer.press("Enter");

    const live = page.getByTestId("chat-live-turn");
    const failed = live.locator('[data-status="failed"]');
    await expect(failed).toContainText("経費の上限は？");
    await expect(failed).toContainText("送信できませんでした");
    const failure = page.getByTestId("chat-send-failure");
    await expect(failure.getByRole("alert")).toContainText(
      "回答を作成できませんでした。時間をおいて再送信してください。"
    );
    // 入力欄には戻さない（質問は会話の欄に残っている）。処理中の表示は消える。
    await expect(composer).toHaveValue("");
    await expect(live.getByTestId("chat-answer-progress")).toHaveCount(0);
    await expect(page.getByTestId("chat-run-stop")).toHaveAccessibleName("送信");
    await expectNoPageOverflow(page);
    await screenshotBothThemes(page, live, (theme) =>
      testInfo.outputPath(`chat-failed-${viewport.name}-${theme}.png`)
    );

    await failure.getByRole("button", { name: "再送信" }).click();
    await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    await expect(page.getByTestId("chat-send-failure")).toHaveCount(0);
    await expect(page.getByTestId("chat-messages").getByText("経費の上限は？", { exact: true })).toHaveCount(1);
    expect(streamCalls).toBe(2);
  });

  test(`停止しても送った質問は会話の欄に残る（#907） (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page);
    await page.route("**/api/chat/conversations/*/messages/stream", () => new Promise<void>(() => undefined));

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = chatComposer(page);
    await composer.fill("経費の上限は？");
    await composer.press("Enter");
    const live = page.getByTestId("chat-live-turn");
    await expect(live.getByTestId("chat-answer-progress")).toBeVisible();

    const button = page.getByTestId("chat-run-stop");
    await button.click();
    await expect(button).toHaveAccessibleName("送信");
    await expect(live.locator('[data-status="stopped"]')).toHaveText("経費の上限は？");
    await expect(live.getByTestId("chat-stopped")).toHaveText(
      "回答の作成を停止しました。もう一度送ると、新しく回答を作成します。"
    );
    await expect(live.getByTestId("chat-answer-progress")).toHaveCount(0);
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(0);
    await expect(composer).toHaveValue("");
    await expectNoPageOverflow(page);

    // 止めた質問は新しい会話に持ち越さない。
    await page.getByRole("button", { name: "新しい会話" }).click();
    await expect(page.getByTestId("chat-live-turn")).toHaveCount(0);
    await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  });
}

// 別の会話・検索・回答プロファイルへ移ったら前の送信を打ち切り、「送信」に戻す（移った先で送れなくしない）。
test("類似問の照会中に別の会話を選んでも、送信中のまま残らず送信できる", async ({ page }) => {
  await mockChat(page, "ready", [userMessage, assistantMessage]);
  // 類似問の照会は中止できないので、応答しないまま別の会話へ移る。
  await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", () => new Promise<void>(() => undefined));

  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  const composer = chatComposer(page);
  await composer.fill("交通費の上限は？");
  await composer.press("Enter");
  const button = page.getByTestId("chat-run-stop");
  await expect(button).toHaveAccessibleName("停止");

  const history = await openChatHistory(page);
  await history.getByRole("list", { name: "会話の履歴" }).getByRole("button").filter({ hasText: "件・" }).click();
  await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
  await expect(page.getByTestId("chat-live-turn")).toHaveCount(0);
  await expect(button).toHaveAccessibleName("送信");
  await composer.fill("日当は？");
  await expect(button).toBeEnabled();
});

test("生成中に新しい会話を作ると、前の会話の生成を止めて新しい会話で送信できる", async ({ page }) => {
  await openPersistedConversation(page, 1280, [userMessage, assistantMessage]);
  await expect(page.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
  let streamAborted = false;
  page.on("requestfailed", (request) => {
    if (request.url().endsWith("/messages/stream")) streamAborted = true;
  });
  await page.route("**/api/chat/conversations/*/messages/stream", () => new Promise<void>(() => undefined));
  const created = { ...conversationDetail([], null), id: "conv-2" };
  await page.route("**/api/chat/conversations", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({ json: { data: created, error_messages: [], warning_messages: [] } });
  });
  await page.route("**/api/chat/conversations/conv-2", (route) =>
    route.fulfill({ json: { data: created, error_messages: [], warning_messages: [] } })
  );

  const composer = chatComposer(page);
  await composer.fill("交通費の上限は？");
  await composer.press("Enter");
  await expect(page.getByTestId("chat-live-turn").getByTestId("chat-answer-progress")).toBeVisible();

  await page.getByRole("button", { name: "新しい会話" }).click();
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await expect(page.getByTestId("chat-live-turn")).toHaveCount(0);
  await expect(page.getByTestId("chat-run-stop")).toHaveAccessibleName("送信");
  await expect.poll(() => streamAborted).toBe(true);
});


// 失敗の表示（3 製品共通の失敗のカード。#2342）。処理の経過は表へ書いた span（回答の trace の API が返す。#3226）。
function progressEvent(seq: number, extra: Record<string, unknown>) {
  return traceSse([
    {
      schema_version: 1,
      seq,
      target_id: "a1",
      attempt: 0,
      emitted_at: `2026-01-01T00:00:0${seq}Z`,
      ...extra,
    } as unknown as ChatProgressEventJson,
  ]);
}

// 処理の経過は span のイベント（#3226）。工程の span（文書検索）が失敗し、実行の全体の span も失敗で終わる。
const failedSpan = { name: "rag.engine.stage", kind: "answer_step", params: { name: "文書検索" }, parent_id: "r0", trace_id: SPAN_TRACE_ID };
const failedRoot = { step_id: "r0", kind: "r0", name: "rag.answer", root: true, trace_id: SPAN_TRACE_ID };
const failedStreamBody = [
  sseStart,
  progressEvent(1, { type: "step", ...failedRoot, status: "running" }),
  progressEvent(2, { type: "step", step_id: "s1", ...failedSpan, status: "running" }),
  progressEvent(3, { type: "step", step_id: "s1", ...failedSpan, status: "failed" }),
  progressEvent(4, { type: "step", ...failedRoot, status: "failed" }),
  progressEvent(5, { type: "terminal", status: "failed" }),
  `event: error\ndata: ${JSON.stringify({
    model_id: "m1",
    message_id: "a1",
    trace_id: "trace-failed",
    message: "検索処理中にエラーが発生しました。",
    error_type: "DatabaseError",
    error_code: "ORA-03146",
    stage: "文書検索",
    occurred_at: "2026-01-01T00:00:04Z",
  })}\n\n`,
  `event: all_done\ndata: ${JSON.stringify({ conversation_id: "conv-1" })}\n\n`,
].join("");

const unsavedStreamBody = [
  sseStart,
  `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "経費の上限は 10 万円です。" })}\n\n`,
  `event: metadata\ndata: ${JSON.stringify({
    model_id: "m1",
    message_id: "a1",
    trace_id: "t1",
    elapsed_ms: 5,
    guardrail_warnings: [],
    storage_failures: [
      { step: "answer_record_save", error_type: "DatabaseError", error_code: "ORA-03146" },
      { step: "chat_message_save", error_type: "DatabaseError", error_code: "ORA-40441" },
    ],
  })}\n\n`,
  `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations: [citationChunk] })}\n\n`,
  `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  `event: all_done\ndata: ${JSON.stringify({ conversation_id: "conv-1" })}\n\n`,
].join("");

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`回答の失敗は、止まった段・原因・次の操作・「詳細」を共通の失敗のカードで出す（#2342） (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page, "ready", [], {
      streamBody: failedStreamBody,
      sentMessages: [
        userMessage,
        { ...assistantMessage, content: "検索処理中にエラーが発生しました。", citations: [], trace_id: "trace-failed", status: "ERROR" },
      ],
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = chatComposer(page);
    await composer.fill("経費の上限は？");
    await composer.press("Enter");

    const failure = page.getByTestId("chat-answer-failure");
    await expect(failure.getByRole("alert")).toContainText("「検索文から根拠の候補を集める」で処理が止まりました。");
    await expect(failure).toContainText("検索処理中にエラーが発生しました。");
    await expect(failure).toContainText("もう一度送信してください。");
    // 技術的な詳細は「詳細」（失敗では開いて出す）。取り直した会話の回答にも引き継ぐ。
    const details = failure.locator("details");
    await expect(details).toHaveAttribute("open", "");
    for (const value of ["止まった段", "DatabaseError", "ORA-03146", "trace-failed"]) {
      await expect(details).toContainText(value);
    }
    await expect(failure.getByRole("button", { name: "もう一度送信" })).toBeVisible();
    // 失敗だけの枠には見出し「回答」を出さない（#3433）。
    await expect(page.getByTestId("chat-answer-heading")).toHaveCount(0);
    // 処理の経過は失敗した段を ✗ で残す。
    await expect(page.getByTestId("chat-answer-progress").locator('[data-status="failed"]').first()).toBeVisible();
    await expectNoPageOverflow(page);
  });

  test(`作った回答を保存できなかったときは、回答を出したまま warning で理由を出す（#2342） (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChat(page, "ready", [], {
      streamBody: unsavedStreamBody,
      // 会話の回答は保存できず、読み込み直すと保存できなかった理由の文になる。
      sentMessages: [
        userMessage,
        { ...assistantMessage, content: "回答は作成しましたが、会話に保存できなかったため表示できません。", citations: [], status: "ERROR" },
      ],
    });

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const composer = chatComposer(page);
    await composer.fill("経費の上限は？");
    await composer.press("Enter");

    const live = page.getByTestId("chat-live-turn");
    await expect(live.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    const storage = live.getByTestId("chat-answer-storage-failure");
    await expect(storage).toContainText("回答は表示していますが、一部を保存できませんでした。");
    await expect(storage).toContainText("会話に保存できなかったため");
    await expect(storage.getByRole("alert")).toHaveCount(0);
    await storage.getByText("詳細", { exact: true }).click();
    await expect(storage).toContainText("ORA-40441");
    // 取り直した後も作った回答を出したまま（保存できなかった理由の文に置き換えない）。
    await expect(page.getByTestId("chat-run-stop")).toHaveAccessibleName("送信");
    await expect(live.getByText("経費の上限は 10 万円です。").first()).toBeVisible();
    await expect(page.getByTestId("chat-answer-failure")).toHaveCount(0);
    await expectNoPageOverflow(page);
  });
}

// 自由入力の条件は選択肢なしでも確認し、送信した値を会話へ引き継ぐ。
test("業務ガイドの自由入力条件だけを確認して回答へ進める", { tag: "@responsive" }, async ({ page }) => {
  await mockChat(page);
  await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
    route.fulfill({ json: { data: { suggestions: [] }, error_messages: [], warning_messages: [] } })
  );
  await page.route("**/api/search-answer-profiles/*/clarifications/suggest", (route) =>
    route.fulfill({
      json: {
        data: {
          suggestion: {
            rule_id: "guide:g-text:target",
            rule_title: "資料の項目を確認する",
            clarification: {
              question: "対象の資料と項目を教えてください。",
              multiple: false,
              allow_other: true,
              options: [],
            },
          },
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/messages/stream")) bodies.push(request.postDataJSON() as Record<string, unknown>);
  });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("textbox", { name: "質問", exact: true }).fill("資料の項目を確認したい");
  await page.getByRole("button", { name: "送信" }).click();
  const card = page.getByTestId("chat-clarification-choice");
  await expect(card.getByRole("heading", { name: "対象の資料と項目を教えてください。" })).toBeFocused();
  await expect(card.getByRole("radio")).toHaveCount(0);
  const confirm = card.getByRole("button", { name: "この内容で回答" });
  await expect(confirm).toBeDisabled();
  // 選択肢が無いので「その他」「選択肢に無いとき」と言わず、自由入力だけの言い方にする（#3366）。
  const answer = card.getByRole("textbox", { name: "答え", exact: true });
  await expect(answer).toHaveAttribute("placeholder", "答えを書いてください");
  await expect(card.getByText("答えを書くと押せます。")).toBeVisible();
  await expect(card).not.toContainText("その他");
  await expect(card).not.toContainText("選択肢");
  await answer.fill("資料 A の項目 B");
  await expect(confirm).toBeEnabled();
  await expectNoPageOverflow(page);
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
    await page.evaluate((dark) => document.documentElement.classList.toggle("dark", dark), colorScheme === "dark");
    await card.screenshot({ path: test.info().outputPath(`guide-text-${colorScheme}.png`) });
  }
  await confirm.click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({
    clarification: { rule_id: "guide:g-text:target", option_ids: [], other_text: "資料 A の項目 B" },
  });
  await expect(card).toHaveCount(0);
});


function longProcessReply(index: number) {
  const id = `long-answer-${index}`;
  const at = (seconds: number) => new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString();
  return {
    ...assistantMessage,
    message_id: id,
    model: `synthetic-model-${index}`,
    trace_id: `long-trace-${index}`,
    content: "合成の回答です。\n\n".repeat(8),
    citations: Array.from({ length: 118 }, (_, i) => ({
      ...citationChunk,
      document_id: "synthetic-document",
      chunk_id: `${id}-chunk-${i}`,
      text: `合成の根拠 ${i + 1}`,
      file_name: "合成の文書.pdf",
      metadata: { evidence_model_used: i === 0, evidence_answer_use: i === 0 ? "confirmed" : "unused" },
    })),
    // 処理の経過は span のイベント（実行の全体の span と 16 の工程の span。#3226）。
    progress: [
      {
        schema_version: 1, seq: 1, target_id: id, attempt: 0, emitted_at: at(16),
        type: "step", step_id: "r0", kind: "r0", status: "done", started_at: at(0), finished_at: at(16),
        name: "rag.answer", root: true, trace_id: SPAN_TRACE_ID,
      } as Record<string, unknown>,
      ...Array.from({ length: 16 }, (_, i): Record<string, unknown> => ({
        schema_version: 1, seq: i + 2, target_id: id, attempt: 0, emitted_at: at(i + 1),
        type: "step", step_id: `s${i}`, kind: "answer_step", status: "done",
        started_at: at(i), finished_at: at(i + 1),
        params: { name: `合成の工程 ${i + 1}`, llm_calls: 1 },
        name: "rag.engine.stage", parent_id: "r0", trace_id: SPAN_TRACE_ID,
      })),
    ],
  };
}

for (const compare of [false, true]) {
  test(`低い画面で処理の経過と詳細を開いても根拠と評価まで読める (${compare ? "比較" : "単一"})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    // desktop の条件は 1920×600 をブラウザで 150% に拡大したときの CSS viewport。
    // 実ブラウザの zoom と DPR は別途確認し、CI は headless shell で同じレイアウト条件を検査する。
    if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 1280, height: 400 });
    const replies = [longProcessReply(1), ...(compare ? [longProcessReply(2)] : [])];
    await mockChat(page, "ready", [userMessage, ...replies]);
    await page.route("**/api/search/answers?**", route => route.fulfill({ json: pageEnvelope(replies.map(reply => ({ trace_id: reply.trace_id }))) }));
    await page.route("**/api/search/answers/*", route => route.fulfill({ json: {
      data: { trace_id: "synthetic-trace", created_at: assistantMessage.created_at, question: "合成の質問", answer: "合成の回答", citations: [], surface: "chat", evaluation_available: false,
        answer_diagnostics: { outcome: "answered", confidence: "high", reasoning_summary: "合成の診断です。".repeat(100) } },
      error_messages: [], warning_messages: [],
    } }));
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    const history = await openChatHistory(page);
    await history.getByRole("list", { name: "会話の履歴" }).getByRole("button").filter({ hasText: "件・" }).click();
    const log = page.getByRole("log", { name: "会話" });
    for (const reply of replies) {
      const answer = page.locator(`[id="message-${reply.message_id}"]`);
      await answer.getByTestId("chat-answer-progress-summary").click();
      await expect(answer.getByTestId("chat-answer-progress-summary")).toContainText("16 ステップ");
    }
    const last = page.locator(`[id="message-${replies.at(-1)!.message_id}"]`);
    const evidence = last.locator("summary").filter({ hasText: "AI に渡した 118 件 ／ 回答で引用 1 件 ／ うち該当確認済み 1 件" });
    const feedback = last.getByRole("group", { name: "この回答は役に立ちましたか？" });
    // scrollTop の代入だけで済ませず、会話の欄の上でホイールを動かして末尾へ進む。
    await log.hover();
    await page.mouse.wheel(0, 20_000);
    await expect(evidence).toBeInViewport({ ratio: 1 });
    await expect(feedback).toBeInViewport({ ratio: 1 });
    for (const reply of replies) {
      const answer = page.locator(`[id="message-${reply.message_id}"]`);
      await answer.getByTestId("chat-answer-progress-details").click();
      await expect(answer.getByRole("region", { name: "処理の詳細" }).getByText(/合成の診断です/)).toHaveCount(0);
    }
    await log.hover();
    await page.mouse.wheel(0, 20_000);
    await expect(evidence).toBeInViewport({ ratio: 1 });
    await expect(feedback).toBeInViewport({ ratio: 1 });
    await evidence.click();
    const cards = last.getByTestId("answer-citations").getByTestId("evidence-card");
    await expect(cards).toHaveCount(118);
    await cards.last().scrollIntoViewIfNeeded();
    await expect(cards.last()).toBeInViewport();
    await log.hover();
    // ポインタの下が根拠の一覧（一定の高さの中のスクロール）なら、一覧の末尾の後で会話の欄へ続けて動く（スクロールの連鎖）。
    await page.mouse.wheel(0, 20_000);
    await page.mouse.wheel(0, 20_000);
    await expect(feedback).toBeInViewport({ ratio: 1 });
    // 外側のページでも入力欄まで移動できる。低い画面で会話を潰して入力欄だけを固定しない。
    await page.getByRole("textbox", { name: "質問" }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("textbox", { name: "質問" })).toBeInViewport();
    await evidence.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`expanded-process-${compare ? "compare" : "single"}.png`), fullPage: true });
  });
}

/** Trace ID とコピーのボタンが同じ行にあるか（375px でも。#3219）。 */
async function traceIdRowsOnOneLine(page: Page): Promise<boolean> {
  return page.evaluate(() => [...document.querySelectorAll("[data-trace-id-row]")].every((row) => {
    const [text, button] = [row.querySelector("span"), row.querySelector("button")].map((el) => el?.getBoundingClientRect());
    return text != null && button != null && button.top < text.bottom && text.top < button.bottom;
  }));
}

test("処理の経過は回答の trace の span の木で、2 つ目の「トレースの詳細」は出さない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  // RAG の段・回答エンジンの工程・モデルの呼び出しの span（#3226）。
  const progress = savedSpanProgress(
    "a1",
    [
      ...answerSpans(["回答生成フロー"]),
      { key: "chat", spec: ragSpan.technical("gen_ai.chat", "step-0", { "gen_ai.request.model": "synthetic-model" }) },
    ],
    { terminal: "done" }
  );
  const streamBody = [
    sseStart,
    // 処理の経過は表へ書いた span（回答の trace の API が返す。#3226）。
    traceSse(progress),
    `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "合成の回答です。" })}\n\n`,
    `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "synthetic-a", elapsed_ms: 2000, guardrail_warnings: [], answer_diagnostics: { outcome: "answered", execution_steps: [{ name: "合成の処理", status: "完了", elapsed_seconds: 2 }] } })}\n\n`,
    `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  ].join("");
  await mockChat(page, "ready", [], { streamBody });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, searchAnswerProfile.name);
  await page.getByRole("textbox").fill("合成データを確認");
  await page.getByRole("button", { name: "送信" }).click();
  const progressRegion = page.getByTestId("chat-answer-progress");
  await expect(page.getByTestId("chat-answer-progress-summary")).toContainText("処理の経過（3 ステップ・");
  await page.getByTestId("chat-answer-progress-summary").click();
  // 段は処理の経過と同じ名前、技術の span は日本語の名前とモデル名。入れ子は span の親子（root は行にしない）。
  const labels = progressRegion.locator("[data-run-timeline-label]");
  await expect(labels).toHaveText(["質問の条件を整理して回答を作る", "根拠を検索して回答を作る", "モデルを呼び出す（用途の記録なし）"]);
  await expect(progressRegion.locator("[data-run-timeline-step]").nth(2)).toHaveAttribute("data-level", "2");
  await expect(labels.filter({ hasText: /rag\.|gen_ai\./ })).toHaveCount(0);
  // 「処理の詳細」には Trace ID とコピーのボタンを出し、「トレースの詳細」の 2 つ目の表示は無い。
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${SPAN_TRACE_ID}`);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  await expect(page.getByTestId("trace-details")).toHaveCount(0);
  expect(await traceIdRowsOnOneLine(page)).toBe(true);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("rag-chat-span-tree.png"), fullPage: true });
});

test("保存した回答を開き直しても処理の経過は span の木 1 つで、回答の記録の詳細に trace を重ねない", { tag: "@responsive" }, async ({ page }) => {
  const reply = longProcessReply(1);
  await mockChat(page, "ready", [userMessage, reply]);
  await page.route("**/api/search/answers?**", route => route.fulfill({ json: pageEnvelope([{ trace_id: reply.trace_id }]) }));
  await page.route("**/api/search/answers/*", route => route.fulfill({ json: {
    data: { trace_id: reply.trace_id, created_at: assistantMessage.created_at, question: "合成の質問", answer: "合成の回答", citations: [], surface: "chat", evaluation_available: false,
      answer_diagnostics: { outcome: "answered", confidence: "high", trace_summary: { schema_version: 1, spans: [] } } },
    error_messages: [], warning_messages: [],
  } }));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  const history = await openChatHistory(page);
  await history.getByRole("list", { name: "会話の履歴" }).getByRole("button").filter({ hasText: "件・" }).click();
  const answer = page.locator(`[id="message-${reply.message_id}"]`);
  await expect(answer.getByTestId("chat-answer-progress-summary")).toContainText("16 ステップ");
  await answer.getByTestId("chat-answer-progress-summary").click();
  await answer.getByTestId("chat-answer-progress-details").click();
  await expect(answer.getByRole("region", { name: "処理の詳細" }).getByText("信頼度: high")).toHaveCount(0);
  await expect(answer.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${SPAN_TRACE_ID}`);
  await expect(answer.getByTestId("trace-details-toggle")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("span の無い古い形式の処理の経過は、空白にせず古い形式の案内を出す", { tag: "@responsive" }, async ({ page }) => {
  const reply = {
    ...longProcessReply(1),
    // 古い形式の記録（#3226 の前の段のイベント）は表を読めないので、trace の API は終端だけを返す。
    progress: [
      { schema_version: 1, seq: 1, target_id: "long-answer-1", attempt: 0, emitted_at: "2026-01-01T00:00:01Z", type: "terminal", status: "done" },
    ],
  };
  await mockChat(page, "ready", [userMessage, reply]);
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  const history = await openChatHistory(page);
  await history.getByRole("list", { name: "会話の履歴" }).getByRole("button").filter({ hasText: "件・" }).click();
  const answer = page.locator(`[id="message-${reply.message_id}"]`);
  await expect(answer.getByTestId("chat-answer-progress-legacy")).toHaveText(
    "この実行は古い形式の記録のため、処理の経過を表示できません。"
  );
  await expect(answer.getByTestId("chat-answer-progress-summary")).toHaveCount(0);
  await expect(answer.getByText(/検索文から根拠の候補を集める/)).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("チャットと履歴の工程の内容・階層・状態・時間が一致する", {tag: "@responsive"}, async ({page}, testInfo) => {
  const reply = longProcessReply(1);
  // 工程の span を親子に 3 段つなぐ（入れ子は span の親子。#3226）。
  reply.progress = [reply.progress[0], ...reply.progress.slice(1, 4).map((event, index) => ({
    ...event, parent_id: index === 0 ? "r0" : `s${index - 1}`, params: {...(event.params as Record<string, unknown>), note: "根拠不足で終了"},
  }))];
  reply.citations = [];
  await openPersistedConversation(page, page.viewportSize()!.width, [{...userMessage, content: "合成の質問"}, reply]);
  const progress = page.getByTestId("chat-answer-progress");
  await progress.getByTestId("chat-answer-progress-summary").click();
  const live = await timelineSnapshot(progress);
  const liveTypography = await timelineTypography(progress);
  expect(liveTypography.steps.length).toBeGreaterThan(0);
  expect(liveTypography.steps.every(style => style?.fontSize === "12px" && style.fontFamily.includes("Noto Sans JP"))).toBe(true);
  expect(liveTypography.summary?.fontSize).toBe("12px");
  expect(live.map((row) => row.level)).toEqual([0,1,2]);
  await page.screenshot({path: testInfo.outputPath("chat-process.png")});
  await page.route("**/api/history/runs/synthetic-parity", (route) => route.fulfill({json: {data: {
    citation_count: 0, answer_outcome: "insufficient_evidence", search_answer_profile_id: "bv-1", search_answer_profile_name: "プロファイル A",
    run_id: "synthetic-parity", surface: "chat", question: "合成の質問", status: "success", answer_generated: true,
    created_at: "2026-01-01T00:00:03Z", elapsed_ms: 3000, executed_by_me: true, executor_display_name: "利用者 A",
    conversation_id: "conv-1", message_id: reply.message_id,
    feedback: [], citations: [], answer: reply.content, answer_available: true,
    answer_diagnostics: null, conversation_owner_display_name: "利用者 A", owned_by_me: true,
  }}}));
  // 実行の trace の API は会話の回答と同じ span を返す（#3226）。
  await mockTraceApi(page, {
    "synthetic-parity": (reply.progress as unknown as ChatProgressEventJson[]).map((event) => ({
      ...event,
      target_id: "synthetic-parity",
    })),
  });
  await page.goto("/history/synthetic-parity");
  await expect(page.getByTestId("run-history-summary")).toBeVisible();
  await expect(page.getByTestId("run-history-summary").locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
  const history = page.getByTestId("run-history-timeline");
  await page.getByTestId("run-history-timeline-summary").click();
  expect(await timelineSnapshot(history)).toEqual(live);
  expect(await timelineTypography(history)).toEqual(liveTypography);
  await expectNoPageOverflow(page);
  // 「処理の詳細」には Trace ID とコピーのボタンを出し、「トレースの詳細」の 2 つ目の表示は無い（#3226）。
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${SPAN_TRACE_ID}`);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  expect(await traceIdRowsOnOneLine(page)).toBe(true);
  await page.getByTestId("run-history-timeline").screenshot({path: testInfo.outputPath("history-process.png")});
});

// #3444: 回答は列のモデルだけで作る。そのモデルへ根拠の画像を添付した回答は、見出しに「画像も参照」を添える。作成した直後
// （配信の診断の models.attached_images）と、会話を開き直したとき（会話のメッセージの answer_attached_images）で同じ見出しにする。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`画像を添付して答えた回答の見出しに画像も参照と出し、開き直しても同じ (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const imageReply = { ...assistantMessage, answer_attached_images: 2 };
    const textReply = { ...assistantMessage, message_id: "a2", trace_id: "t2", reply_to_message_id: "u2", answer_attached_images: 0 };
    const llm = { model_id: "m1", label: "MODEL 1" };
    const streamBody = [
      sseStart,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: assistantMessage.content })}\n\n`,
      `event: metadata\ndata: ${JSON.stringify({
        model_id: "m1",
        message_id: "a1",
        trace_id: "t1",
        elapsed_ms: 5,
        guardrail_warnings: [],
        answer_diagnostics: { models: { llm, attached_images: 1, embedding: "", rerank: "" } },
      })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
    ].join("");
    await mockChat(page, "ready", [userMessage, imageReply, { ...userMessage, message_id: "u2" }, textReply], {
      streamBody,
      sentMessages: [userMessage, imageReply],
    });
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");

    // 会話を開き直したとき: 画像を添付した回答は「画像も参照」、添付しなかった回答は列のモデルだけ。
    const history = await openChatHistory(page);
    await history.getByRole("button", { name: /^経費の上限は？ 4件/ }).click();
    const headings = page.getByTestId("chat-answer-model-heading");
    // 保存した回答の列のモデルは model_id（モデルの一覧の mock は空）。
    await expect(headings).toHaveText(["回答モデル: m1（画像も参照）", "回答モデル: m1"]);
    await expect(headings.first()).toHaveAttribute("title", "回答モデル: m1（画像も参照）");
    await headings.first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/chat-answer-images-heading-saved-${viewport.name}.png`, fullPage: false });
    await expectNoPageOverflow(page);

    // 作成した直後: 配信の診断の添付の件数で見出しに出す（Vision モデルの名前は出さない）。
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill(userMessage.content);
    await page.getByRole("button", { name: "送信" }).click();
    const live = page.getByTestId("chat-answer-model-heading");
    await expect(live).toHaveText(["回答モデル: MODEL 1（画像も参照）"]);
    expect(await live.first().evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    await expectNoPageOverflow(page);
  });
}

test("同頁の説明ごとに出典を開き、複数の確定箇所と保存後の対応を保つ", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { itemCitationText, itemCitationChunks, expectItemCitationSources } = await import("./_item-citations");
  const question = { ...userMessage, content: "合成の説明を確認してください。" };
  const reply = { ...assistantMessage, content: itemCitationText, citations: itemCitationChunks };
  const streamBody = [
    `event: start\ndata: ${JSON.stringify({ conversation_id: "conv-1", user_message: question, columns: [{ model_id: "m1", label: "MODEL 1" }] })}\n\n`,
    `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: itemCitationText })}\n\n`,
    `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations: itemCitationChunks })}\n\n`,
    `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  ].join("");
  await mockChat(page, "ready", [], { streamBody, sentMessages: [question, reply] });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, searchAnswerProfile.name);
  await page.getByRole("button", { name: "新しい会話" }).click();
  await page.getByRole("textbox", { name: "質問", exact: true }).fill(question.content);
  await page.getByRole("button", { name: "送信", exact: true }).click();
  const answer = page.getByTestId("answer-text");
  await expectItemCitationSources(page, answer);
  await page.screenshot({ path: testInfo.outputPath("item-citations-chat.png"), fullPage: true });
  await page.reload();
  await expectItemCitationSources(page, answer);
});

// #3721: 確認用トークンを会話未選択のまま作らない。
for (const withConfirmation of [false, true]) {
  test(`初回送信は会話を確定してから質問の文脈を準備する（確認${withConfirmation ? "あり" : "なし"}）`, async ({ page }) => {
    await mockChat(page);
    const order: string[] = [];
    const preparations: Record<string, unknown>[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/chat/conversations" && request.method() === "POST") {
        order.push("create");
      }
    });
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
      route.fulfill({ json: { data: { suggestions: [] }, error_messages: [], warning_messages: [] } })
    );
    await page.route("**/api/search-answer-profiles/*/clarifications/suggest", async (route) => {
      order.push("prepare");
      const body = route.request().postDataJSON() as Record<string, unknown>;
      preparations.push(body);
      await route.fulfill({ json: {
        data: {
          suggestion: withConfirmation ? clarificationSuggestion : null,
          question_context_token: `context-for-${String(body.conversation_id)}`,
        }, error_messages: [], warning_messages: [],
      } });
    });
    await page.route("**/api/chat/conversations/*/messages/stream", async (route) => {
      order.push("send");
      if (route.request().postDataJSON().question_context_token !== "context-for-conv-1") {
        await route.fulfill({ status: 409, json: {
          data: null, error_messages: ["会話または設定が変わりました。"], warning_messages: [],
        } });
        return;
      }
      await route.fallback();
    });
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await page.getByRole("textbox", { name: "質問", exact: true }).fill("利用条件を教えてください。");
    await page.getByRole("button", { name: "送信", exact: true }).click();
    if (withConfirmation) {
      const choice = page.getByTestId("chat-clarification-choice");
      await choice.getByRole("checkbox", { name: /出張旅費/ }).check();
      await choice.getByRole("button", { name: "この内容で回答", exact: true }).click();
    }
    await expect(page.getByTestId("chat-answer-body").getByText(assistantMessage.content, { exact: true })).toBeVisible();
    expect(order).toEqual(["create", "prepare", "send"]);
    expect(preparations).toEqual([{ query: "利用条件を教えてください。", conversation_id: "conv-1" }]);
    await expect(page.getByTestId("chat-send-failure")).toHaveCount(0);
  });
}

test("初回の会話作成に失敗しても質問を残し、再送信で会話を作れる", async ({ page }) => {
  await mockChat(page);
  let creates = 0;
  await page.route("**/api/chat/conversations", async (route) => {
    if (route.request().method() === "POST" && ++creates === 1) {
      await route.fulfill({ status: 503, json: {
        data: null, error_messages: ["会話を作成できませんでした。"], warning_messages: [],
      } });
      return;
    }
    await route.fallback();
  });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, "経理アシスタント");
  await page.getByRole("textbox", { name: "質問", exact: true }).fill("利用条件を教えてください。");
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await expect(page.getByTestId("chat-send-failure")).toContainText("会話を作成できませんでした。");
  await expect(page.getByTestId("chat-live-turn")).toContainText("利用条件を教えてください。");
  await page.getByRole("button", { name: "再送信", exact: true }).click();
  await expect(page.getByTestId("chat-answer-body").getByText(assistantMessage.content, { exact: true })).toBeVisible();
  expect(creates).toBe(2);
});
