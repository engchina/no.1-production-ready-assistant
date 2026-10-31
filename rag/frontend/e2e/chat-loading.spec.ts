import { expect, type Page, test } from "./fixtures/test";

import { mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";

/**
 * チャットの前提（検索・回答プロファイルの一覧・開いている会話の内容）の読み込み中・失敗の表示（#1153）。
 * 3 製品で同じ規則（UX 契約 messaging.md §11.7）: 前提が揃うまで会話の欄に空の状態を出さず、
 * 会話の形の Skeleton を出し、送信を止める。入力欄・新しい会話・履歴の開閉を無効にするのは対象の一覧の
 * 読み込み中だけで、会話の内容の読み込み中は入力欄に書ける（#1188）。
 * desktop と mobile（375px）の 2 project で実行する。
 */

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
const profilesPage = {
  json: envelope({ items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false }),
};
const conversation = {
  id: "conv-1",
  search_answer_profile_id: "bv-1",
  title: "経費の上限",
  status: "ACTIVE",
  message_count: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:02Z",
};

const EMPTY_TEXT = "質問を入力して会話を始めます";
const COMPOSER = "質問";
/** 取得の失敗は TanStack Query の既定の再試行（3 回・1 + 2 + 4 秒）の後に出る。 */
const RETRY_TIMEOUT = 20_000;

async function setup(
  page: Page,
  options: { conversationFails?: boolean; noConversations?: boolean } = {}
) {
  await mockLocalAuth(page);
  await page.route("**/api/chat/models", (route) => route.fulfill({ json: envelope([]) }));
  // noConversations: 会話が無い状態から始め、「新しい会話」で作る（作成の応答は内容が空の会話）。
  let created = !options.noConversations;
  await page.route("**/api/chat/conversations**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/chat/conversations" && request.method() === "POST") {
      created = true;
      await route.fulfill({ json: envelope({ ...conversation, title: null, messages: [] }) });
      return;
    }
    if (path === "/api/chat/conversations") {
      const items = created ? [conversation] : [];
      await route.fulfill({
        json: envelope({ items, total: items.length, limit: 25, offset: 0, has_next: false }),
      });
      return;
    }
    if (options.conversationFails) {
      await route.fulfill({
        status: 500,
        json: { data: null, error_messages: ["会話を取得できません。"], warning_messages: [] },
      });
      return;
    }
    await route.fulfill({ json: envelope({ ...conversation, messages: [] }) });
  });
}

/** 会話（conv-1）の内容の取得を、戻り値の関数を呼ぶまで止める。 */
async function holdConversation(page: Page) {
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/chat/conversations/conv-1", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await released;
    await route.fulfill({ json: envelope({ ...conversation, messages: [] }) });
  });
  return () => release();
}

/** 検索・回答プロファイルの一覧の応答を、戻り値の関数を呼ぶまで止める。 */
async function holdProfiles(page: Page) {
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/search-answer-profiles**", async (route) => {
    await released;
    await route.fulfill(profilesPage);
  });
  return () => release();
}

test(`検索・回答プロファイルの読み込み中は空の状態を出さず、Skeleton と無効の入力を出す`, async ({
  page,
}, testInfo) => {
  await setup(page);
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill(profilesPage));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, /経理アシスタント/);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toBeVisible();
  const composer = page.getByRole("textbox", { name: COMPOSER, exact: true });
  await composer.fill("書きかけの質問");

  // 再読込: 選んだ検索・回答プロファイルと書きかけの質問は作業状態から戻る。一覧の応答を止める。
  await page.unroute("**/api/search-answer-profiles**");
  const release = await holdProfiles(page);
  await page.reload();

  // 上のカードは文言と経過時間（同じ取得の経過時間はここだけ）。
  await expect(page.getByTestId("chat-search-answer-profiles-loading")).toContainText(
    "検索・回答プロファイルを読み込んでいます"
  );
  // 会話の領域は最初から描き、空の状態の代わりに会話の形の Skeleton を出す。経過時間は重ねない。
  await expect(page.getByTestId("chat-messages-skeleton")).toBeVisible();
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("timer")).toHaveCount(1);
  // 入力欄（書いた文字は残す）・送信・新しい会話・履歴の開閉は無効。
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveValue("書きかけの質問");
  await expect(page.getByTestId("chat-run-stop")).toBeDisabled();
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeDisabled();
  await expect(page.getByTestId("chat-history-toggle")).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath(`chat-loading.png`) });

  // 読み込み後: 空の状態と、使える入力欄。
  release();
  await expect(page.getByTestId("chat-search-answer-profiles-loading")).toHaveCount(0);

  const toolbarTarget = page.locator("[data-chat-toolbar] #chat-search-answer-profile");
  await expect(toolbarTarget).toBeVisible();
  expect(await toolbarTarget.evaluate(node => node.closest("[data-chat-target-selector]") !== null)).toBe(true);

  await expect(page.getByTestId("chat-messages-skeleton")).toHaveCount(0);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue("書きかけの質問");
  await expect(page.getByTestId("chat-run-stop")).toBeEnabled();
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeEnabled();
  await expect(page.getByTestId("chat-history-toggle")).toBeEnabled();
});

test("検索・回答プロファイルの一覧を読めなかったときは、0 件の案内ではなく失敗と再試行を出す", async ({
  page,
}) => {
  await setup(page);
  let fail = true;
  await page.route("**/api/search-answer-profiles**", (route) =>
    fail
      ? route.fulfill({
          status: 500,
          json: { data: null, error_messages: ["データベースに接続できません。"], warning_messages: [] },
        })
      : route.fulfill(profilesPage)
  );
  await page.goto("/chat");
  // 取得は既定の再試行（3 回・backoff）の後に失敗になる。
  await expect(page.getByText("データベースに接続できません。")).toBeVisible({ timeout: RETRY_TIMEOUT });
  await expect(page.getByText(/公開済みの検索・回答プロファイルがありません/)).toHaveCount(0);
  // 会話の領域（空の状態・入力欄）は出さない。
  await expect(page.getByRole("textbox", { name: COMPOSER, exact: true })).toHaveCount(0);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toHaveCount(0);

  fail = false;
  await page.getByRole("button", { name: "再試行" }).click();
  await selectSearchAnswerProfile(page, /経理アシスタント/);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toBeVisible();
});

test("開いている会話の内容を読めなかったときは、失敗を出して送信しない", async ({ page }) => {
  await setup(page, { conversationFails: true });
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill(profilesPage));
  await page.goto("/chat?search_answer_profile_id=bv-1&conversation_id=conv-1");
  await expect(page.getByText("会話を読み込めませんでした。")).toBeVisible({ timeout: RETRY_TIMEOUT });
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toHaveCount(0);
  const composer = page.getByRole("textbox", { name: COMPOSER, exact: true });
  await composer.fill("続きの質問");
  await expect(page.getByTestId("chat-run-stop")).toBeDisabled();
  await composer.press("Enter");
  // 送らない（会話の欄に質問を出さず、入力欄の文字も残す）。
  await expect(page.getByRole("log", { name: "会話" }).getByText("続きの質問")).toHaveCount(0);
  await expect(composer).toHaveValue("続きの質問");
  // 新しい会話には移れる（失敗の後も操作できる）。
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeEnabled();
});

// 会話の内容の読み込み中も入力欄は書ける（書いている途中で無効にしてフォーカスと入力を失わせない）。
// 送信・Enter だけを読み込み後まで止める（messaging.md §11.7、#1188）。
test("開いている会話の内容の読み込み中も入力欄に書け、送信は読み込み後にできる", async ({ page }) => {
  await setup(page);
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill(profilesPage));
  const release = await holdConversation(page);
  await page.goto("/chat?search_answer_profile_id=bv-1&conversation_id=conv-1");
  await expect(page.getByTestId("chat-messages-loading")).toContainText("会話の内容を読み込んでいます");
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toHaveCount(0);
  const composer = page.getByRole("textbox", { name: COMPOSER, exact: true });
  await expect(composer).toBeEnabled();
  await composer.fill("続きの質問");
  await expect(composer).toHaveValue("続きの質問");
  await expect(page.getByTestId("chat-run-stop")).toBeDisabled();
  await composer.press("Enter");
  await expect(composer).toHaveValue("続きの質問");
  // 「新しい会話」と履歴の開閉は会話の内容の読み込み中も使える。
  await expect(page.getByRole("button", { name: "新しい会話", exact: true })).toBeEnabled();

  release();
  await expect(page.getByTestId("chat-messages-loading")).toHaveCount(0);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("続きの質問");
  await expect(page.getByTestId("chat-run-stop")).toBeEnabled();
});

test("「新しい会話」で空の会話を開いた直後から入力でき、内容の取得を待たずに送れる", async ({ page }) => {
  await setup(page);
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill(profilesPage));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, /経理アシスタント/);
  // 履歴に空の会話（conv-1）があるので「新しい会話」はそれを開く。その内容の取得を止める。
  const release = await holdConversation(page);
  await page.getByRole("button", { name: "新しい会話", exact: true }).click();
  const composer = page.getByRole("textbox", { name: COMPOSER, exact: true });
  await expect(composer).toBeFocused();
  await page.keyboard.type("新しい質問");
  await expect(composer).toHaveValue("新しい質問");
  await expect(composer).toBeEnabled();
  // 空と分かっている会話なので、読み込み中の表示を出さず、すぐ送れる。
  await expect(page.getByTestId("chat-messages-loading")).toHaveCount(0);
  await expect(page.getByText(EMPTY_TEXT, { exact: true })).toBeVisible();
  await expect(page.getByTestId("chat-run-stop")).toBeEnabled();
  release();
});

test("「新しい会話」で会話を作った直後から入力でき、内容の取得を待たずに送れる", async ({ page }) => {
  await setup(page, { noConversations: true });
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill(profilesPage));
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, /経理アシスタント/);
  const release = await holdConversation(page);
  const createdResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/chat/conversations"
  );
  await page.getByRole("button", { name: "新しい会話", exact: true }).click();
  const composer = page.getByRole("textbox", { name: COMPOSER, exact: true });
  await composer.fill("新しい質問");
  await createdResponse;
  await expect(composer).toBeFocused();
  await page.keyboard.type("の続き");
  await expect(composer).toHaveValue("新しい質問の続き");
  await expect(composer).toBeEnabled();
  await expect(page.getByTestId("chat-messages-loading")).toHaveCount(0);
  await expect(page.getByTestId("chat-run-stop")).toBeEnabled();
  release();
});

// #3739: 先頭ページより後の対象も名前で選択でき、復元時も内部IDを表示しない。
test("51件目以降の検索・回答プロファイルを選び、再読込後も名前を表示する", async ({ page }, testInfo) => {
  await setup(page, { noConversations: true });
  const first = Array.from({ length: 50 }, (_, i) => ({
    ...searchAnswerProfile, id: `profile-${i}`, name: `検証用_プロファイル${i + 1}`,
  }));
  const target = { ...searchAnswerProfile, name: "検証用_後続プロファイル" };
  const offsets: number[] = [];
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get("offset") ?? 0);
    offsets.push(offset);
    await route.fulfill({ json: envelope({
      items: offset === 0 ? first : [target], total: 51, limit: 50, offset, has_next: offset === 0,
    }) });
  });
  await page.goto("/chat");
  await selectSearchAnswerProfile(page, target.name);
  const selected = page.locator("#chat-search-answer-profile");
  await expect(selected).toHaveText(target.name);
  expect(offsets).toContain(50);
  await page.reload();
  await expect(selected).toHaveText(target.name);
  await page.screenshot({ path: testInfo.outputPath("chat-profile-after-first-page.png") });
});

test("プロファイルの後続ページに失敗したら、不完全な候補で送信させない", async ({ page }) => {
  await setup(page, { noConversations: true });
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? 0);
    await route.fulfill(offset === 0 ? { json: envelope({
      items: [searchAnswerProfile], total: 51, limit: 50, offset: 0, has_next: true,
    }) } : { status: 503, json: {
      data: null, error_messages: ["後続の候補を取得できませんでした。"], warning_messages: [],
    } });
  });
  await page.goto("/chat?search_answer_profile_id=bv-1");
  await expect(page.getByText("後続の候補を取得できませんでした。", { exact: true })).toBeVisible({ timeout: RETRY_TIMEOUT });
  await expect(page.getByRole("button", { name: "送信", exact: true })).toHaveCount(0);
});
