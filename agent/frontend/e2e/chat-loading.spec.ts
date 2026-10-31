import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

/**
 * チャットの前提（業務 Agent の一覧・開いている会話の内容）の読み込み中・失敗の表示（#1153）。
 * 3 製品で同じ規則（UX 契約 messaging.md §11.7）: 前提が揃うまで会話の欄に空の状態を出さず、
 * 会話の形の Skeleton を出し、送信を止める。入力欄・新しい会話・履歴の開閉を無効にするのは対象の一覧の
 * 読み込み中だけで、会話の内容の読み込み中は入力欄に書ける（#1188）。
 * desktop と mobile-375 の 2 project で実行する。
 */

const THREAD_ID = `thread_${"b".repeat(32)}`;
const AGENTS_URL = /\/api\/agents(\?.*)?$/;

/** 業務 Agent の一覧の応答を、戻り値の関数を呼ぶまで止める。 */
async function holdAgents(page: Page) {
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route(AGENTS_URL, async (route) => {
    await released;
    await route.fallback();
  });
  return () => release();
}

function seedThread(mockApi: MockApi) {
  mockApi.state.runs.push({
    id: "run-chat-loading",
    goal: "契約の更新条件は？",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [{ id: "answer-1", kind: "answer", name: "回答", content: { text: "1 年ごとに自動更新です。" } }],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

test(`業務 Agent の読み込み中は会話の欄に空の状態を出さず、Skeleton と無効の入力を出す`, async ({
  page,
}, testInfo) => {
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問" });
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await composer.fill("書きかけの質問");

  // 再読込: 書きかけの質問は作業状態から戻る。業務 Agent の一覧の応答を止める。
  const release = await holdAgents(page);
  await page.reload();

  // 上のカードは文言と経過時間（同じ取得の経過時間はここだけ）。
  await expect(page.getByTestId("chat-agents-loading")).toContainText("業務 Agent を読み込んでいます");
  // 会話の欄は最初から描き、空の状態の代わりに会話の形の Skeleton を出す。経過時間は重ねない。
  await expect(page.getByTestId("chat-conversation-skeleton")).toBeVisible();
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);
  await expect(page.getByRole("timer")).toHaveCount(1);
  // 入力欄（書いた文字は残す）・送信・新しい会話・履歴の開閉は無効。
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveValue("書きかけの質問");
  await expect(page.getByTestId("chat-send")).toBeDisabled();
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeDisabled();
  await expect(page.getByTestId("chat-history-toggle")).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath(`chat-loading.png`) });

  // 読み込み後: 空の状態と、使える入力欄。
  release();
  await expect(page.getByTestId("chat-agents-loading")).toHaveCount(0);

  const toolbarTarget = page.locator("[data-chat-toolbar] #chat-agent");
  await expect(toolbarTarget).toBeVisible();
  expect(await toolbarTarget.evaluate(node => node.closest("[data-chat-target-selector]") !== null)).toBe(true);

  await expect(page.getByTestId("chat-conversation-skeleton")).toHaveCount(0);
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue("書きかけの質問");
  await expect(page.getByTestId("chat-send")).toBeEnabled();
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeEnabled();
  await expect(page.getByTestId("chat-history-toggle")).toBeEnabled();
});

test("業務 Agent の一覧を読めなかったときは、空の状態ではなく失敗と再試行を出す", async ({ page }) => {
  let fail = true;
  await page.route(AGENTS_URL, async (route) => {
    if (!fail) return route.fallback();
    await route.fulfill({ status: 500, json: { detail: "データベースに接続できません。" } });
  });
  await page.goto("/chat");
  // 取得は既定の再試行の後に失敗になる。
  const error = page.getByTestId("chat-agents-error");
  await expect(error).toContainText("データベースに接続できません。", { timeout: 20_000 });
  // 会話の欄（空の状態・入力欄）は出さない。
  await expect(page.getByRole("textbox", { name: "質問" })).toHaveCount(0);
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);

  fail = false;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(page.locator("#chat-agent")).toContainText("資料・データ横断アシスタント");
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
});

test("会話の内容の読み込み中は会話の形の Skeleton と経過時間を出し、読めなかったときは送信しない", async ({
  page,
  mockApi,
}) => {
  seedThread(mockApi);
  await page.goto("/chat");
  await page.getByTestId("chat-history-toggle").click();
  await page.getByTestId("chat-history").getByText("契約の更新条件は？").click();
  await expect(page.getByText("1 年ごとに自動更新です。")).toBeVisible();

  // 再読込で開いている会話を読み込む間（応答を止め、その後は一時的な失敗を返す）。
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/threads/${THREAD_ID}`, async (route) => {
    await released;
    await route.fulfill({ status: 500, json: { detail: "会話を取得できません。" } });
  });
  await page.reload();
  const loading = page.getByTestId("chat-thread-loading");
  await expect(loading).toContainText("会話を読み込んでいます");
  await expect(loading.locator('[data-skeleton="chat"]')).toBeVisible();
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);
  // 会話の内容の読み込み中も入力欄には書ける（書いている途中で無効にしない。#1188）。送信だけを止める。
  const composer = page.getByRole("textbox", { name: "質問" });
  await expect(composer).toBeEnabled();
  await composer.fill("続きの質問");
  await expect(page.getByTestId("chat-send")).toBeDisabled();
  await composer.press("Enter");
  await expect(page.getByTestId("chat-pending-turn")).toHaveCount(0);
  await expect(composer).toHaveValue("続きの質問");

  release();
  await expect(page.getByTestId("chat-thread-error")).toContainText("会話を取得できません。");
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);
  await expect(composer).toHaveValue("続きの質問");
  await expect(page.getByTestId("chat-send")).toBeDisabled();
  await composer.press("Enter");
  await expect(page.getByTestId("chat-pending-turn")).toHaveCount(0);
  await expect(composer).toHaveValue("続きの質問");
  // 新しい会話には移れる（失敗の後も操作できる）。
  await page.getByRole("button", { name: "新しい会話", exact: true }).click();
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await expect(page.getByTestId("chat-send")).toBeEnabled();
});
