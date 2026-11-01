import { expect, type Page, test } from "./fixtures/test";

import { expectNoPageOverflow, mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";

// #2517: チャットの添付（会話の期間だけ使う作業ファイル）。運用設定で有効なら入力欄から添付して送り、
// 送った質問の下に添付の名前、回答の根拠に添付の箇所（原文のプレビューは開かない）を出す。

const LIMITS = {
  max_bytes: 20 * 1024 * 1024,
  max_files: 10,
  ttl_hours: 24,
  accept: [".pdf", ".csv", ".xlsx", ".png"],
  image_enabled: true,
};

const attachmentRef = { attachment_id: "att-1", file_name: "売上表.csv", kind: "csv", size_bytes: 48 };

const userMessage = {
  message_id: "u1",
  conversation_id: "conv-1",
  role: "USER",
  content: "添付の表で金額が最も大きい品目は？",
  model: null,
  citations: [],
  trace_id: null,
  status: "COMPLETE",
  reply_to_message_id: null,
  created_at: "2026-01-01T00:00:00Z",
  attachments: [attachmentRef],
};

const attachmentCitation = {
  document_id: "attachment:att-1",
  chunk_id: "attachment:att-1:t1.1",
  text: "シート「売上表」\n行 1: コード: A-1 / 品目: 品目甲 / 金額: 300",
  score: 1,
  rerank_score: null,
  file_name: "売上表.csv",
  category_name: null,
  metadata: { evidence_model_used: true, chat_attachment_id: "att-1", sheet_name: "売上表" },
};

const sseBody = [
  `event: start\ndata: ${JSON.stringify({
    conversation_id: "conv-1",
    user_message: userMessage,
    columns: [{ model_id: "m1", label: "MODEL 1" }],
  })}\n\n`,
  `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "金額が最も大きい品目は品目甲です。" })}\n\n`,
  `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5 })}\n\n`,
  `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations: [attachmentCitation] })}\n\n`,
  `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
  `event: all_done\ndata: ${JSON.stringify({ conversation_id: "conv-1" })}\n\n`,
].join("");

function envelope<T>(data: T) {
  return { data, error_messages: [], warning_messages: [] };
}

async function mockChatWithAttachments(page: Page, { enabled }: { enabled: boolean }) {
  const streamBodies: unknown[] = [];
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: envelope({
        items: [
          {
            id: "bv-1",
            name: "経理アシスタント",
            description: "",
            status: "ACTIVE",
            knowledge_base_count: 1,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_next: false,
      }),
    })
  );
  await page.route("**/api/chat/models", (route) => route.fulfill({ json: envelope([]) }));
  await page.route("**/api/chat-attachments**", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      await route.fulfill({ json: envelope({ items: [], limits: { ...LIMITS, enabled } }) });
      return;
    }
    if (method === "POST") {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({
        json: envelope({
          ...attachmentRef,
          media_type: "text/csv",
          conversation_id: null,
          section_count: 0,
          sheets: [{ sheet: "売上表", columns: ["コード", "品目", "金額"], row_count: 2 }],
          warnings: [],
          created_at: "2026-01-01T00:00:00Z",
          expires_at: "2026-01-02T00:00:00Z",
          uploaded_by_me: true,
        }),
      });
      return;
    }
    await route.fulfill({ json: envelope({ deleted: true }) });
  });
  let sent = false;
  await page.route("**/api/chat/conversations**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const conversation = {
      id: "conv-1",
      search_answer_profile_id: "bv-1",
      title: sent ? userMessage.content : null,
      status: "ACTIVE",
      message_count: sent ? 2 : 0,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    if (path.endsWith("/messages/stream")) {
      sent = true;
      streamBodies.push(request.postDataJSON());
      await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: sseBody });
      return;
    }
    if (path === "/api/chat/conversations" && request.method() === "POST") {
      await route.fulfill({ json: envelope({ ...conversation, messages: [] }) });
      return;
    }
    if (path === "/api/chat/conversations") {
      await route.fulfill({
        json: envelope({ items: [], total: 0, limit: 50, offset: 0, has_next: false, history_days: 90 }),
      });
      return;
    }
    const messages = sent
      ? [
          userMessage,
          {
            ...userMessage,
            message_id: "a1",
            role: "ASSISTANT",
            content: "金額が最も大きい品目は品目甲です。",
            model: "m1",
            citations: [attachmentCitation],
            trace_id: "t1",
            reply_to_message_id: "u1",
            attachments: [],
          },
        ]
      : [];
    await route.fulfill({ json: envelope({ ...conversation, messages }) });
  });
  return streamBodies;
}

const csv = { name: "売上表.csv", mimeType: "text/csv", buffer: Buffer.from("コード,品目,金額\nA-1,品目甲,300\n") };

test.describe("チャットの添付", { tag: "@responsive" }, () => {
  test("添付して送ると、質問の下に添付を、根拠に添付の箇所を出す", async ({ page }, testInfo) => {
    const streamBodies = await mockChatWithAttachments(page, { enabled: true });
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await expect(page.getByTestId("chat-attachments-add")).toHaveText("ファイルを添付");

    await page.getByTestId("chat-attachments-input").setInputFiles(csv);
    const chip = page.locator("[data-chat-attachment]").first();
    await expect(chip).toHaveAttribute("data-status", "ready");
    await page.getByRole("textbox", { name: "質問" }).fill(userMessage.content);
    await page.getByTestId("chat-run-stop").click();

    await expect.poll(() => (streamBodies[0] as { attachment_ids?: string[] } | undefined)?.attachment_ids).toEqual([
      "att-1",
    ]);
    await expect(page.getByRole("list", { name: "添付ファイル" }).first()).toContainText("売上表.csv");
    await expect(page.getByText("金額が最も大きい品目は品目甲です。").first()).toBeVisible();
    // 根拠を開くと、添付の箇所に「添付ファイル」の印があり、原文のプレビューの操作は無い。
    const citationSummary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" }).first();
    await citationSummary.click();
    await expect(page.getByText("添付ファイル", { exact: true }).last()).toBeVisible();
    await expect(page.getByRole("button", { name: /の根拠を表示$/ })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("chat-attachment-answer.png"), fullPage: true });
    await expectNoPageOverflow(page);
  });

  test("運用設定で無効なら添付の操作を出さない", async ({ page }) => {
    await mockChatWithAttachments(page, { enabled: false });
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理アシスタント");
    await expect(page.getByRole("textbox", { name: "質問" })).toBeVisible();
    await expect(page.getByTestId("chat-attachments-add")).toHaveCount(0);
  });
});

test("運用設定でチャットの添付を有効にする", { tag: "@responsive" }, async ({ page }) => {
  await mockLocalAuth(page);
  let enabled = false;
  await page.route("**/api/settings/chat-attachments", async (route) => {
    if (route.request().method() === "PUT") enabled = Boolean((route.request().postDataJSON() as { enabled: boolean }).enabled);
    await route.fulfill({
      json: envelope({ enabled, updated_at: null, max_bytes: LIMITS.max_bytes, max_files: 10, ttl_hours: 24 }),
    });
  });
  await page.goto("/settings/chat-attachments");
  await expect(page.getByRole("heading", { name: "チャットの添付", level: 1 })).toBeVisible();
  const toggle = page.getByTestId("chat-attachments-enabled");
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await toggle.click();
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => enabled).toBe(true);
  await expectNoPageOverflow(page);
});
