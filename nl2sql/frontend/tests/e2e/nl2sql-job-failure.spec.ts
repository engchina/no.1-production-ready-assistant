import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { withJobProgress } from "./_helpers/job-progress";

/**
 * NL2SQL のジョブの失敗の文（#1072）。1 文目は利用者の言葉（何が起きたか・次の操作）にし、
 * 例外・Oracle のエラーの元の文（error_detail）とエラーコードは開いた「詳細」に分けて出す
 * （UX 契約 messaging.md §10.3）。
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
const now = "2026-10-02T22:00:00Z";
const failureMessage =
  "SQL の生成に失敗しました。時間をおいてもう一度実行してください。繰り返し失敗するときは、「詳細」の内容を管理者に伝えてください。";
const failureDetail =
  "OCI Enterprise AI の呼び出しに失敗しました: ORA-04027: self-deadlock during automatic validation";

// 公開の後に業務プロファイル・Schema が変わった業務定義（#1827）。時間をおいても直らないので
// 「もう一度」と案内せず、管理者が再検証・公開する所を 1 文目に出す。
const revalidationMessage =
  "この業務プロファイルの業務定義（公開版のオントロジー）は、公開の後に業務プロファイルか DB の構造（スキーマ）が変わったため、SQL の生成に使えません。時間をおいても直りません。管理者に、「オントロジー構築」でこの業務プロファイルの Markdown を確認し直して公開するよう依頼してください。";
const revalidationDetail =
  "公開の後に業務プロファイルか DB の構造（スキーマ）が変わったため、公開版の業務定義を使えません。「オントロジー構築」で Markdown を確認し直して公開してください。（BUSINESS_SCOPE_CHANGED）";

async function mockChatWithFailedTurn(
  page: Page,
  failure: { message: string; code: string; detail: string } = {
    message: failureMessage,
    code: "ORA-04027",
    detail: failureDetail,
  },
) {
  await mockDatabaseGateReady(page);
  const conversation = { id: "chat-1", title: "カテゴリ別売上", profile_id: "sales", created_at: now };
  const turn = {
    job_id: "chat-1",
    question: "カテゴリ別売上",
    status: "error",
    generation_only: true,
    created_at: now,
    steps: [],
    result: null,
    error_message: failure.message,
    error_code: failure.code,
    error_detail: failure.detail,
  };
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) =>
    route.fulfill({
      json: {
        data:
          new URL(route.request().url()).pathname === "/api/nl2sql/chats"
            ? { items: [conversation], next_cursor: null, total: 1, limit: 25 }
            : { conversation, turns: [turn].map(withJobProgress) },
      },
    }),
  );
}

for (const width of [1280, 375]) {
  test(`チャットのジョブの失敗は、利用者向けの文と開いた「詳細」の元の文に分けて出す (${width}px)`, async ({
    page,
  }) => {
    await mockChatWithFailedTurn(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/chat");
    await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
    await page.getByTestId("sql-chat-history").getByText("カテゴリ別売上", { exact: true }).click();
    if (width >= 1024) await page.getByRole("button", { name: "会話の履歴", exact: true }).click();

    // 3 製品共通の失敗のカード（#2342）。backend の利用者向けの文は原因の段落に 1 回だけ出す。
    const failure = page.getByTestId("sql-chat-turn-failure");
    await expect(failure).toBeVisible();
    // 本文には例外・ORA の文を出さない。
    const summary = failure.locator("[data-process-failure-cause]");
    await expect(summary).toHaveText(failureMessage);
    await expect(summary).not.toContainText("ORA-");
    // 「詳細」は失敗なので開いて出し、エラーコードと元のメッセージを並べる。
    const details = failure.locator("details");
    await expect(details).toHaveAttribute("open", "");
    await expect(details).toContainText("エラーコード");
    await expect(details).toContainText("ORA-04027");
    await expect(details).toContainText("元のメッセージ");
    await expect(details).toContainText(failureDetail);
    // 長い元の文でも横にはみ出さない。
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

for (const width of [1280, 375]) {
  test(`業務定義の再検証が要る失敗は、再試行を案内せず管理者の対処を 1 文目に出す (${width}px)`, async ({
    page,
  }) => {
    await mockChatWithFailedTurn(page, {
      message: revalidationMessage,
      code: "NL2SQL_PROFILE_REVALIDATION_REQUIRED",
      detail: revalidationDetail,
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/chat");
    await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
    await page.getByTestId("sql-chat-history").getByText("カテゴリ別売上", { exact: true }).click();
    if (width >= 1024) await page.getByRole("button", { name: "会話の履歴", exact: true }).click();

    const failure = page.getByTestId("sql-chat-turn-failure");
    await expect(failure).toBeVisible();
    const summary = failure.locator("[data-process-failure-cause]");
    await expect(summary).toHaveText(revalidationMessage);
    await expect(summary).not.toContainText("時間をおいてもう一度");
    // 再試行を案内しない（次の操作は管理者への連絡だけ）。
    await expect(failure).not.toContainText("もう一度送信");
    await expect(summary).not.toContainText("BUSINESS_SCOPE_CHANGED");
    const details = failure.locator("details");
    await expect(details).toHaveAttribute("open", "");
    await expect(details).toContainText("NL2SQL_PROFILE_REVALIDATION_REQUIRED");
    await expect(details).toContainText(revalidationDetail);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
