import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { mockChatRequirement } from "./_helpers/chat-requirement";

/**
 * チャットの前提（業務プロファイルの一覧・開いている会話の内容）の読み込み中・失敗の表示（#1153）。
 * 3 製品で同じ規則（UX 契約 messaging.md §11.7）: 前提が揃うまで会話の欄に空の状態を出さず、
 * 会話の形の Skeleton を出し、送信を止める。入力欄・新しい会話・履歴の開閉を無効にするのは
 * 業務プロファイルの一覧の読み込み中だけで、会話の内容の読み込み中は書ける（#1188。sql-chat.spec.ts）。
 * desktop と mobile-375 の 2 project で実行する。
 */

const profile = {
  id: "sales",
  name: "売上分析",
  description: "売上の集計",
  archived: false,
  allowed_tables: ["APP.SALES"],
  allowed_views: [],
  allowed_table_count: 1,
  allowed_view_count: 0,
  version: 1,
};
const profilePage = {
  json: { data: { items: [profile], total: 1, next_cursor: null } },
};

async function setup(page: Page) {
  await mockDatabaseGateReady(page);
  // 送信の前の要件の検査（#1442）は「確認が要らない」で、聞かずに SQL を生成する。
  await mockChatRequirement(page);
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) =>
    route.fulfill({ json: { data: { items: [], next_cursor: null, total: 0, limit: 25 } } }),
  );
}

/** 業務プロファイルの一覧の応答を、release() まで止める。 */
async function holdProfiles(page: Page) {
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/nl2sql/profiles/search**", async (route) => {
    await released;
    await route.fulfill(profilePage);
  });
  return () => release();
}

test(`業務プロファイルの読み込み中は会話の欄に空の状態を出さず、Skeleton と無効の入力を出す`, async ({
  page,
}, testInfo) => {
  await setup(page);
  const release = await holdProfiles(page);
  await page.goto("/chat");

  // ツールバーの対象の欄に文言と経過時間を出す（同じ取得の経過時間はここだけ）。
  const profilesLoading = page.getByTestId("sql-chat-profiles-loading");
  await expect(profilesLoading).toContainText(
    "業務プロファイルを読み込んでいます",
  );
  // 会話の欄は最初から描き、空の状態の代わりに会話の形の Skeleton を出す。経過時間は重ねない。
  const panel = page.getByTestId("sql-chat-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("sql-chat-conversation-skeleton")).toBeVisible();
  await expect(
    page.getByText("質問を入力して会話を始めます", { exact: true }),
  ).toHaveCount(0);
  await expect(panel.getByRole("log").getByRole("timer")).toHaveCount(0);
  await expect(page.getByRole("timer")).toHaveCount(1);
  // 入力欄・送信・新しい会話・履歴の開閉は無効。生成方法は 1 つだけなので選ぶ欄を出さない（#7）。
  await expect(
    page.getByRole("textbox", { name: "質問", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("#sql-chat-engine")).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-send")).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "新しい会話", exact: true }),
  ).toBeDisabled();
  await expect(page.getByTestId("sql-chat-history-toggle")).toBeDisabled();
  await page.screenshot({
    path: testInfo.outputPath(`chat-loading-light.png`),
  });

  // 読み込み後: 空の状態と、使える入力欄。
  release();
  await expect(page.getByTestId("sql-chat-profiles-loading")).toHaveCount(0);
  await expect(page.locator("[data-chat-toolbar] #sql-chat-profile")).toBeVisible();
  await expect(page.getByTestId("sql-chat-conversation-skeleton")).toHaveCount(0);
  await expect(
    page.getByText("質問を入力して会話を始めます", { exact: true }),
  ).toBeVisible();
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await expect(composer).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "新しい会話", exact: true }),
  ).toBeEnabled();
  await expect(page.getByTestId("sql-chat-history-toggle")).toBeEnabled();
  await composer.fill("カテゴリ別売上");
  await expect(page.getByTestId("sql-chat-send")).toBeEnabled();
});

test("読み込み中も入力欄に書いた文字は残す（作業状態）", async ({ page }) => {
  await setup(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill(profilePage),
  );
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("書きかけのクエリ");

  await page.unroute("**/api/nl2sql/profiles/search**");
  const release = await holdProfiles(page);
  await page.reload();
  await expect(page.getByTestId("sql-chat-conversation-skeleton")).toBeVisible();
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveValue("書きかけのクエリ");
  release();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue("書きかけのクエリ");
});

test("業務プロファイルの一覧を読めなかったときは、空の状態ではなく失敗と再試行を出す", async ({
  page,
}) => {
  await setup(page);
  let fail = true;
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    fail
      ? route.fulfill({
          status: 500,
          json: { error: "データベースに接続できません。" },
        })
      : route.fulfill(profilePage),
  );
  await page.goto("/chat");
  const error = page.getByTestId("sql-chat-profiles-error");
  // 取得は TanStack Query の既定の再試行（3 回・1 + 2 + 4 秒）の後に失敗になる。
  await expect(error).toContainText("データベースに接続できません。", {
    timeout: 20_000,
  });
  // 会話の欄（空の状態・入力欄）は出さない。
  await expect(page.getByTestId("sql-chat-panel")).toHaveCount(0);
  await expect(
    page.getByText("質問を入力して会話を始めます", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("利用できる業務プロファイルがありません", { exact: true }),
  ).toHaveCount(0);

  fail = false;
  await error.getByRole("button", { name: "再読み込み" }).click();
  await expect(page.locator("#sql-chat-profile")).toContainText("売上分析");
  await expect(
    page.getByText("質問を入力して会話を始めます", { exact: true }),
  ).toBeVisible();
});
