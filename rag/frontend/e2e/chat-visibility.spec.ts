import { expect, type Page, test } from "./fixtures/test";

import { mockLocalAuth, openChatHistory, selectSearchAnswerProfile } from "./_helpers";

/**
 * 「AI 活用」のチャットの会話の見え方（#1889・#3244）。system_admin は全員の会話を見られ、他の利用者の
 * 会話には履歴に利用者の表示名を添える。他の利用者の会話は閲覧と停止・削除だけで、入力欄の代わりに閲覧だけの
 * 案内を出し、続きの操作（送信・再実行・評価）は出さない（続きの質問・評価・名前の変更は持ち主だけ。backend は
 * 403）。#3244 より前に持ち主以外が送った質問には送った人を添える。持ち主での絞り込み（一般の利用者は自分の
 * 会話だけ・他人は 404・system_admin も他人の会話には 403）は backend のテスト（tests/test_chat_visibility.py）で
 * 確かめる。
 */

// 3 製品共通の ChatReadOnlyNotice の文（backend の OTHERS_CONTENT_FORBIDDEN_MESSAGE と同じ）。
const READ_ONLY_TITLE = "他の利用者の会話です（閲覧のみ）";
const READ_ONLY_NOTICE =
  "他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。閲覧・停止・削除はできます。調べ直すときは自分の会話で質問してください。";

const searchAnswerProfile = {
  id: "bv-1",
  name: "経理アシスタント",
  description: "経費の相談",
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });

const conversations = [
  {
    id: "conv-own",
    search_answer_profile_id: "bv-1",
    title: "自分の質問",
    status: "ACTIVE",
    message_count: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:04Z",
    owned: true,
    actor_display_name: "ローカル利用者",
  },
  {
    id: "conv-alice",
    search_answer_profile_id: "bv-1",
    title: "アリスの質問",
    status: "ACTIVE",
    message_count: 4,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:03Z",
    owned: false,
    actor_display_name: "アリス",
  },
  {
    id: "conv-running",
    search_answer_profile_id: "bv-1",
    title: "作成中の質問",
    status: "ACTIVE",
    message_count: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:02Z",
    owned: false,
    actor_display_name: "アリス",
  },
  {
    id: "conv-gone",
    search_answer_profile_id: "bv-1",
    title: "削除済みの人の質問",
    status: "ACTIVE",
    message_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
    owned: false,
    actor_display_name: "",
  },
];

const message = (overrides: Record<string, unknown>) => ({
  conversation_id: "conv-alice",
  model: null,
  citations: [],
  guardrail_warnings: [],
  trace_id: null,
  status: "COMPLETE",
  reply_to_message_id: null,
  created_at: "2026-01-01T00:00:00Z",
  sent_by_owner: true,
  sender_display_name: "",
  ...overrides,
});

// アリスの会話: アリスの質問と、#3244 より前に system_admin（ローカル利用者）が送った続きの質問。
const aliceMessages = [
  message({ message_id: "u1", role: "USER", content: "質問 A" }),
  message({ message_id: "a1", role: "ASSISTANT", content: "回答 A です。", reply_to_message_id: "u1", trace_id: "t1" }),
  message({
    message_id: "u2",
    role: "USER",
    content: "質問 B",
    sent_by_owner: false,
    sender_display_name: "ローカル利用者",
  }),
  message({ message_id: "a2", role: "ASSISTANT", content: "回答 B です。", reply_to_message_id: "u2", trace_id: "t2" }),
];

const ownMessages = [
  message({ conversation_id: "conv-own", message_id: "o1", role: "USER", content: "自分の質問 A" }),
  message({
    conversation_id: "conv-own",
    message_id: "o2",
    role: "ASSISTANT",
    content: "自分の回答です。",
    reply_to_message_id: "o1",
    trace_id: "t-own",
  }),
];

// アリスの会話で作成中の回答（停止できる）。
const runningMessages = [
  message({ conversation_id: "conv-running", message_id: "r1", role: "USER", content: "質問 C" }),
  message({
    conversation_id: "conv-running",
    message_id: "r2",
    role: "ASSISTANT",
    content: "",
    status: "STREAMING",
    reply_to_message_id: "r1",
  }),
];

async function setup(page: Page) {
  // 作成中の回答は停止で変わるので、テストごとに写す（並列・繰り返しでも前のテストの状態を持ち込まない）。
  const running: Record<string, unknown>[] = runningMessages.map((item) => ({ ...item }));
  const state = { sent: [] as string[], cancelled: [] as string[], deleted: [] as string[] };
  await mockLocalAuth(page);
  await page.route("**/api/feedback/current**", (route) => route.fulfill({ json: envelope([]) }));
  await page.route("**/api/chat/models", (route) => route.fulfill({ json: envelope([]) }));
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: envelope({ items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false }),
    })
  );
  await page.route("**/api/chat/conversations**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/messages/stream")) {
      state.sent.push(path);
      await route.fulfill({ status: 403, json: { data: null, error_messages: [READ_ONLY_NOTICE], warning_messages: [] } });
      return;
    }
    if (path.endsWith("/cancel")) {
      state.cancelled.push(path);
      running[1] = { ...running[1], status: "CANCELLED", content: "回答の作成を停止しました。" };
      await route.fulfill({ json: envelope({ cancelled: true }) });
      return;
    }
    if (path === "/api/chat/conversations") {
      const visible = conversations.filter((item) => !state.deleted.includes(item.id));
      await route.fulfill({
        json: envelope({ items: visible, total: visible.length, limit: 25, offset: 0, has_next: false }),
      });
      return;
    }
    const id = decodeURIComponent(path.split("/").at(-1) ?? "");
    const conversation = conversations.find((item) => item.id === id && !state.deleted.includes(item.id));
    if (!conversation) {
      await route.fulfill({
        status: 404,
        json: { data: null, error_messages: ["会話が見つかりません。"], warning_messages: [] },
      });
      return;
    }
    if (request.method() === "DELETE") {
      state.deleted.push(id);
      await route.fulfill({ json: envelope(null) });
      return;
    }
    const messages =
      id === "conv-alice" ? aliceMessages : id === "conv-own" ? ownMessages : id === "conv-running" ? running : [];
    await route.fulfill({ json: envelope({ ...conversation, messages }) });
  });
  return state;
}

async function openConversation(page: Page, title: string) {
  const history = await openChatHistory(page);
  await history.locator(`[title="${title}"]`).click();
  return history;
}

test.describe("チャットの会話の見え方（#1889・#3244）", { tag: "@responsive" }, () => {
  test("system_admin は他の利用者の会話を閲覧だけでき、続きの操作を出さない", async ({ page }) => {
    const state = await setup(page);
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, /経理アシスタント/);
    // 新しい会話（会話を選んでいない）では入力欄を出し、閲覧だけの案内は出さない。
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeEditable();
    await expect(page.getByTestId("chat-read-only-notice")).toHaveCount(0);
    const history = await openChatHistory(page);
    // 自分の会話には利用者を添えず、他の利用者の会話にだけ添える（引けない利用者は削除済み）。
    await expect(history.getByText(/・アリス$/).first()).toBeVisible();
    await expect(history.getByText(/・（削除済みのユーザー）$/)).toBeVisible();
    await expect(history.getByText(/・ローカル利用者$/)).toHaveCount(0);
    // 名前の変更は持ち主だけ。他の利用者の会話の行は削除だけを出す（行の操作は sm 以上でホバーの間に出る）。
    await history.locator('[title="アリスの質問"]').hover();
    await expect(history.getByRole("button", { name: "「アリスの質問」の名前を変更" })).toHaveCount(0);
    await expect(history.getByRole("button", { name: "「アリスの質問」を削除" })).toBeVisible();
    await history.locator('[title="自分の質問"]').hover();
    await expect(history.getByRole("button", { name: "「自分の質問」の名前を変更" })).toBeVisible();

    await history.locator('[title="アリスの質問"]').click();
    // 閲覧はできる。#3244 より前に持ち主以外が送った質問にだけ送った人を添える。
    await expect(page.getByText("回答 B です。")).toBeVisible();
    const senders = page.locator("[data-chat-sender]");
    await expect(senders).toHaveCount(1);
    await expect(senders).toHaveText("ローカル利用者 が送信");

    // 入力欄の代わりに閲覧だけの案内（停止する回答が無いので停止のボタンも無い）。
    const notice = page.getByTestId("chat-read-only-notice");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(READ_ONLY_TITLE);
    await expect(notice).toContainText(READ_ONLY_NOTICE);
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("chat-run-stop")).toHaveCount(0);
    await expect(page.getByTestId("chat-read-only-stop")).toHaveCount(0);
    // 回答と根拠の評価は持ち主だけ。
    await expect(page.getByRole("button", { name: "この回答は役に立った" })).toHaveCount(0);
    expect(state.sent).toEqual([]);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("system_admin は他の利用者の会話の作成中の回答を停止し、会話を削除できる", async ({ page }) => {
    const state = await setup(page);
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, /経理アシスタント/);
    await openConversation(page, "作成中の質問");

    const stop = page.getByTestId("chat-read-only-stop");
    await expect(stop).toBeVisible();
    await stop.click();
    await expect.poll(() => state.cancelled).toEqual(["/api/chat/conversations/conv-running/messages/r1/cancel"]);
    await expect(page.getByText("回答の作成を停止しました。")).toBeVisible();
    await expect(stop).toHaveCount(0);

    // 削除は運用の操作なので、他の利用者の会話でも確認を通して削除できる（開いている会話の行は操作を常に出す）。
    // 375px では会話を選ぶと履歴のシートが閉じるので開き直す。
    const sessions = await openChatHistory(page);
    await sessions.getByRole("button", { name: "「作成中の質問」を削除" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();
    await expect(page.getByText("会話を削除しました。")).toBeVisible();
    expect(state.deleted).toEqual(["conv-running"]);
    expect(state.sent).toEqual([]);
  });

  test("system_admin でも自分の会話では入力欄と評価を出す", async ({ page }) => {
    await setup(page);
    await page.goto("/chat");
    await selectSearchAnswerProfile(page, /経理アシスタント/);
    await openConversation(page, "自分の質問");
    await expect(page.getByText("自分の回答です。")).toBeVisible();
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeEditable();
    await expect(page.getByTestId("chat-read-only-notice")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "この回答は役に立った" })).toBeVisible();
  });
});
