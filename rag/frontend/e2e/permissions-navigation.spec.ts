import { expect, test, type Page } from "./fixtures/test";

import {
  apiEnvelope,
  dbUser,
  expandSidebarSections,
  expectNoPageOverflow,
  mockAuthUser,
  mockDatabaseReady,
  openSidebarNav,
  selectSearchAnswerProfile,
} from "./_helpers";

// 権限によるナビ・ルート・ページ内操作の出し分け（#214）。
// backend は implies を展開済みの permissions を返す。frontend は一覧に含まれるかだけを見る。

const modifier = process.platform === "darwin" ? "Meta" : "Control";

const KNOWLEDGE_BASE = {
  id: "kb-1",
  name: "社内規程",
  description: "経費・人事",
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 1,
  indexed_document_count: 1,
  error_document_count: 0,
  searchable_chunk_count: 4,
  created_at: "2026-06-15T00:00:00Z",
  updated_at: "2026-06-15T00:00:00Z",
  archived_at: null,
};

const SEARCH_ANSWER_PROFILE = {
  id: "bv-1",
  name: "人事 FAQ",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-06-30T00:00:00Z",
  updated_at: "2026-06-30T00:00:00Z",
  archived_at: null,
};

function page1<T>(items: T[]) {
  return apiEnvelope({ items, total: items.length, limit: 50, offset: 0, has_next: false });
}

async function mockApi(page: Page) {
  // 画面の描画に必要な一覧だけを返し、他は空の 200 にする（ナビとルートの判定を確かめる）。
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } })
  );
  await mockDatabaseReady(page);
  await page.route("**/api/knowledge-bases**", (route) => route.fulfill({ json: page1([KNOWLEDGE_BASE]) }));
  await page.route("**/api/search-answer-profiles**", (route) => route.fulfill({ json: page1([SEARCH_ANSWER_PROFILE]) }));
}


test("権限のある画面だけをナビに出し、空のセクションは隠す", async ({ page }) => {
  await mockApi(page);
  await mockAuthUser(page, {
    permissions: ["menu.search", "menu.chat", "menu.upload", "menu.security_users"],
  });

  // `/` はナビの並びで最初に開ける画面（チャット。#399 でナビの先頭をチャットにした）へ。
  await page.goto("/");
  await expect(page).toHaveURL(/\/chat$/);
  const sidebar = await openSidebarNav(page);
  // 既定では「AI 活用」だけが開いている（#1563）。権限のあるセクションをすべて開いて項目を数える。
  await expandSidebarSections(sidebar);
  for (const name of ["RAG 検索", "チャット", "文書アップロード", "ユーザー管理"]) {
    await expect(sidebar.getByRole("link", { name })).toBeVisible();
  }
  // ナビの項目だけを数える（下部のアカウントの欄もアカウントの画面へのリンク。#1873）。
  await expect(sidebar.getByRole("navigation").getByRole("link")).toHaveCount(4);
  for (const section of ["取込・検索・回答設定", "セキュリティ設定", "運用設定", "システム設定"]) {
    await expect(sidebar.getByText(section, { exact: true })).toHaveCount(0);
  }
  await expectNoPageOverflow(page);
});

test("権限のない URL を直接開くと権限なしの画面を出し、利用可能な画面へ戻れる", async ({ page }) => {
  await mockApi(page);
  await mockAuthUser(page, { permissions: ["menu.chat", "menu.feedback"] });

  // 実行履歴（一覧・詳細。#1890）も menu.history が無ければ開けない。
  for (const path of ["/settings/upload-storage", "/settings/security/permissions", "/knowledge-bases/kb-1", "/history", "/history/run-1"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/forbidden$/);
    await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
  }
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);
  await expectNoPageOverflow(page);

  await page.getByRole("button", { name: "利用可能な画面へ戻る" }).click();
  await expect(page).toHaveURL(/\/chat$/);

  // 文書の詳細はワークスペース専用の API を使うため、チャットの権限だけでは開けない（#303）。
  await page.goto("/documents/doc-1");
  await expect(page).toHaveURL(/\/forbidden$/);
});

test("廃止したダッシュボードの旧 URL と未知の URL は既定の入口へ置き換えて移す（#261）", async ({ page }) => {
  await mockApi(page);
  const setUser = await mockAuthUser(page, { permissions: ["menu.search", "menu.chat"] });

  // チャットを開ける利用者はチャットへ（#432）。履歴を置き換えるので、戻ると旧 URL ではなく直前の画面。
  await page.goto("/search");
  await expect(page).toHaveURL(/\/search$/);
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/search$/);
  await page.goto("/no-such-page");
  await expect(page).toHaveURL(/\/chat$/);

  // チャットを開けない利用者は `/` 経由でナビの並びで最初に開ける画面へ（権限なしの画面にしない）。
  setUser(dbUser({ permissions: ["menu.upload", "menu.file_list"] }));
  for (const path of ["/dashboard", "/", "/no-such-page"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/upload$/);
  }
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "ダッシュボード" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("API の 403 は権限なしの画面へ移し、調査用の request ID を示す", async ({ page }) => {
  await mockApi(page);
  await mockAuthUser(page, { permissions: ["menu.settings_huggingface"] });
  await page.route("**/api/settings/huggingface**", (route) =>
    route.fulfill({
      status: 403,
      headers: { "X-Request-ID": "req-forbidden-1" },
      json: { data: null, error_messages: ["この機能を利用する権限がありません。"], warning_messages: [] },
    })
  );

  await page.goto("/settings/huggingface");
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByText("req-forbidden-1")).toBeVisible();
});

test("管理権限が無い利用者には、検索・回答プロファイル / KB の作成・アーカイブを出さない", async ({ page }) => {
  await mockApi(page);
  const setUser = await mockAuthUser(page, {
    permissions: ["menu.search_answer_profiles", "menu.knowledge_bases"],
    allowed_search_answer_profile_ids: ["bv-1"],
    allowed_knowledge_base_ids: ["kb-1"],
  });

  await page.goto("/knowledge-bases");
  const main = page.getByRole("main");
  await expect(main.getByRole("heading", { name: "ナレッジベース", level: 1 })).toBeVisible();
  await expect(main.getByText("社内規程")).toBeVisible();
  await expect(main.getByRole("button", { name: "新規作成" })).toHaveCount(0);
  // 行の操作はアーカイブだけなので、操作メニューごと出さない。
  await expect(page.getByTestId("knowledge-base-row-actions-kb-1")).toHaveCount(0);
  // 作成画面の URL を直接開いても一覧を出す（検索・回答プロファイルと同じ。#555）。
  await page.goto("/knowledge-bases?id=new");
  await expect(main.getByText("社内規程")).toBeVisible();
  await expect(main.getByRole("textbox", { name: "名前", exact: true })).toHaveCount(0);

  await page.goto("/search-answer-profiles");
  await expect(main.getByText("人事 FAQ")).toBeVisible();
  await expect(main.getByRole("button", { name: "新規作成" })).toHaveCount(0);
  // 作成画面の URL を直接開いても一覧を出す。
  await page.goto("/search-answer-profiles?id=new");
  await expect(main.getByText("人事 FAQ")).toBeVisible();
  await expect(main.getByRole("button", { name: "新規作成" })).toHaveCount(0);

  // 管理権限を持つ利用者（implies 展開済み）は作成できる。
  setUser(
    dbUser({
      permissions: [
        "menu.search_answer_profiles",
        "menu.knowledge_bases",
        "rag.search_answer_profiles.manage",
        "rag.knowledge_bases.manage",
      ],
      allowed_search_answer_profile_ids: null,
      allowed_knowledge_base_ids: null,
    })
  );
  await page.goto("/knowledge-bases");
  await expect(main.getByRole("button", { name: "新規作成" })).toBeVisible();
  await expect(page.getByTestId("knowledge-base-row-actions-kb-1")).toBeVisible();
  await page.goto("/search-answer-profiles");
  await expect(main.getByRole("button", { name: "新規作成" })).toBeVisible();
});

test("検索・回答プロファイルの KB を利用できない検索の 403 は、画面を移さず理由を検索結果の位置に出す", async ({ page }) => {
  await mockApi(page);
  await mockAuthUser(page, {
    permissions: ["menu.search"],
    allowed_search_answer_profile_ids: ["bv-1"],
    allowed_knowledge_base_ids: [],
  });
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 403,
      json: {
        data: null,
        error_messages: ["この検索・回答プロファイルのナレッジベースを利用する権限がありません。管理者に権限を依頼してください。"],
        warning_messages: [],
        // 範囲外は経路の権限拒否と区別できる error_code で返る（#224）。
        error_code: "RAG_SCOPE_FORBIDDEN",
      },
    })
  );

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /人事 FAQ/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("育休の申請期限");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect(
    page.getByRole("main").getByText("この検索・回答プロファイルのナレッジベースを利用する権限がありません。管理者に権限を依頼してください。")
  ).toBeVisible();
  await expect(page).toHaveURL(/\/search$/);
});
