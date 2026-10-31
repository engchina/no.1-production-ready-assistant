import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { confirmChatRequirement, mockChatRequirement } from "./_helpers/chat-requirement";
import { withJobProgress } from "./_helpers/job-progress";

/**
 * 「AI 活用」の下の機能のデータの見え方（#1889・#3244）。system_admin は全員の会話を見られ、履歴に利用者の
 * 表示名を出す。他の利用者の会話は閲覧と運用の操作（停止・削除）だけで、入力欄・「実行」・名前の変更を出さず、
 * 入力欄の代わりに閲覧だけの案内を出す（続きの質問・実行・名前の変更は持ち主だけ。backend も 403）。
 * すでに入っている持ち主以外（#3244 より前の system_admin）が送った質問には、送った人の表示名を添える。
 * 一般の利用者の履歴は本人の会話だけで、利用者を出さない（絞り込みは backend。ここでは画面の出し方を確かめる）。
 */

// 3 製品共通の ChatReadOnlyNotice の文（backend の OTHERS_CONTENT_FORBIDDEN_MESSAGE と同じ）。
const READ_ONLY_TITLE = "他の利用者の会話です（閲覧のみ）";
const READ_ONLY_NOTICE =
  "他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。閲覧・停止・削除はできます。調べ直すときは自分の会話で質問してください。";

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
const now = "2026-10-07T09:59:00Z";
const SQL = "SELECT * FROM APP.SALES";

type Conversation = {
  id: string;
  title: string;
  profile_id: string;
  created_at: string;
  owned: boolean;
  actor_display_name: string;
};

function turn(jobId: string, question: string, sender: string | null, status = "done") {
  return {
    job_id: jobId,
    question,
    conversation_id: "chat-alice",
    chat: true,
    generation_only: true,
    status,
    created_at: now,
    steps: [],
    last_execution: null,
    sender_display_name: sender,
    result: {
      generated_sql: SQL,
      original_question: question,
      explanation: "売上の明細です。",
      safety: { is_safe: true },
    },
  };
}

async function setup(
  page: Page,
  conversations: Conversation[],
  turns: Record<string, ReturnType<typeof turn>[]> = {},
) {
  await mockDatabaseGateReady(page);
  await mockChatRequirement(page);
  const jobRequests: Record<string, unknown>[] = [];
  const cancelled: string[] = [];
  const deleted: string[] = [];
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/nl2sql/chats") {
      return route.fulfill({
        json: {
          data: { items: conversations, next_cursor: null, total: conversations.length, limit: 25 },
        },
      });
    }
    const id = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    const target = conversations.find((item) => item.id === id);
    if (!target) {
      return route.fulfill({ status: 404, json: { error_messages: ["会話が見つかりません。"] } });
    }
    if (route.request().method() === "DELETE") {
      // 削除は system_admin も他の利用者の会話にできる（運用の操作。#3244）。
      deleted.push(id);
      conversations.splice(conversations.indexOf(target), 1);
      return route.fulfill({ json: { data: null } });
    }
    return route.fulfill({
      json: { data: { conversation: target, turns: (turns[id] ?? []).map(withJobProgress) } },
    });
  });
  // 自分の会話への続き（他の利用者の会話には画面から送れない。#3244）。
  await page.route("**/api/nl2sql/jobs", (route) => {
    const body = route.request().postDataJSON();
    jobRequests.push(body);
    const list = (turns["chat-mine"] ??= []);
    const sent = turn(`chat-${list.length + 1}`, body.question, null);
    list.push(sent);
    return route.fulfill({
      json: {
        data: withJobProgress({
          job_id: sent.job_id,
          status: "done",
          created_at: now,
          steps: [],
        }),
      },
    });
  });
  // 停止は system_admin も他の利用者の会話でできる（運用の操作。#3244）。
  await page.route("**/api/nl2sql/jobs/*/cancel", (route) => {
    const jobId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-2) ?? "");
    cancelled.push(jobId);
    for (const list of Object.values(turns)) {
      const target = list.find((item) => item.job_id === jobId);
      if (target) {
        Object.assign(target, {
          status: "error",
          error_code: "JOB_CANCELLED",
          error_message: "停止しました。",
        });
      }
    }
    return route.fulfill({
      json: { data: { job_id: jobId, status: "error", created_at: now, steps: [] } },
    });
  });
  return { jobRequests, cancelled, deleted };
}

async function openHistory(page: Page) {
  await page.goto("/chat");
  const toggle = page.getByTestId("sql-chat-history-toggle");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  return page.getByTestId("sql-chat-history");
}

test.describe("会話のデータの見え方（#1889・#3244）", { tag: "@responsive" }, () => {
  test("system_admin は全員の会話を利用者つきで見て、他の利用者の会話は閲覧と削除だけにする", async ({ page }) => {
    const { jobRequests, deleted } = await setup(
      page,
      [
        {
          id: "chat-alice",
          title: "アリスの売上",
          profile_id: "sales",
          created_at: now,
          owned: false,
          actor_display_name: "アリス",
        },
        {
          id: "chat-gone",
          title: "削除済みの人の会話",
          profile_id: "sales",
          created_at: "2026-10-07T09:58:00Z",
          owned: false,
          actor_display_name: "",
        },
      ],
      {
        "chat-alice": [
          turn("chat-alice", "売上の明細", null),
          turn("chat-2", "カテゴリ別にして", "別の管理者"),
          turn("chat-3", "地域別にして", ""),
        ],
      },
    );
    const history = await openHistory(page);
    await expect(history.getByText(/・アリス$/)).toBeVisible();
    await expect(history.getByText(/・（削除済みのユーザー）$/)).toBeVisible();
    // 新しい会話では閲覧だけの案内を出さない。
    await expect(page.getByTestId("sql-chat-read-only-notice")).toHaveCount(0);

    await history.locator('[title="アリスの売上"]').click();
    const turns = page.getByTestId("sql-chat-turn");
    await expect(turns).toHaveCount(3);
    // 持ち主の質問には送り手を出さず、持ち主以外が送った質問には送った人を添える（引けない人は削除済み）。
    await expect(turns.nth(0).locator("[data-chat-sender]")).toHaveCount(0);
    await expect(turns.nth(1).locator("[data-chat-sender]")).toHaveText("別の管理者 が送信");
    await expect(turns.nth(2).locator("[data-chat-sender]")).toHaveText(
      "（削除済みのユーザー） が送信",
    );
    // 続きの質問・実行は持ち主だけ: 入力欄・送信・「実行」を出さず、入力欄の代わりに閲覧だけの案内を出す。
    const readOnly = page.getByTestId("sql-chat-read-only-notice");
    await expect(readOnly).toContainText(READ_ONLY_TITLE);
    await expect(readOnly).toContainText(READ_ONLY_NOTICE);
    await expect(readOnly).toBeInViewport();
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-send")).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-execute")).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-read-only-stop")).toHaveCount(0);
    // SQL は見られる。
    await expect(turns.nth(0).getByText(SQL)).toBeVisible();

    // 履歴の行は名前の変更を出さず、削除だけを出す（削除は運用の操作）。
    const toggle = page.getByTestId("sql-chat-history-toggle");
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await expect(history.getByRole("button", { name: "「アリスの売上」の名前を変更" })).toHaveCount(0);
    await history.getByRole("button", { name: "「アリスの売上」を削除" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "削除", exact: true }).click();
    await expect(history.locator('[title="アリスの売上"]')).toHaveCount(0);
    expect(deleted).toEqual(["chat-alice"]);
    // 開いていた会話を消したら、新しい会話（入力欄あり）に戻る。
    if ((page.viewportSize()?.width ?? 0) < 1024) await page.keyboard.press("Escape");
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeEditable();
    await expect(page.getByTestId("sql-chat-read-only-notice")).toHaveCount(0);
    expect(jobRequests).toEqual([]);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("system_admin は他の利用者の会話で処理中の SQL の生成を止められる", async ({ page }) => {
    const { cancelled } = await setup(
      page,
      [
        {
          id: "chat-alice",
          title: "アリスの売上",
          profile_id: "sales",
          created_at: now,
          owned: false,
          actor_display_name: "アリス",
        },
      ],
      { "chat-alice": [turn("chat-alice", "売上の明細", null, "running")] },
    );
    const history = await openHistory(page);
    await history.locator('[title="アリスの売上"]').click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    await expect(page.getByTestId("sql-chat-read-only-notice")).toContainText(READ_ONLY_NOTICE);
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toHaveCount(0);
    const stop = page.getByTestId("sql-chat-read-only-stop");
    await expect(stop).toHaveText("停止");
    await stop.click();
    await expect.poll(() => cancelled).toEqual(["chat-alice"]);
    await expect(page.getByTestId("sql-chat-read-only-stop")).toHaveCount(0);
    await expect(page.getByText("SQL の生成を停止しました", { exact: false })).toBeVisible();
  });

  test("system_admin が自分の会話を開いたときは、入力欄と「実行」を出し、閲覧だけの案内は出さない", async ({ page }) => {
    const { jobRequests } = await setup(
      page,
      [
        {
          id: "chat-mine",
          title: "管理者の売上",
          profile_id: "sales",
          created_at: now,
          owned: true,
          actor_display_name: "システム管理者",
        },
      ],
      { "chat-mine": [turn("chat-mine", "管理者の売上", null)] },
    );
    const history = await openHistory(page);
    await history.locator('[title="管理者の売上"]').click();
    const turns = page.getByTestId("sql-chat-turn");
    await expect(turns).toHaveCount(1);
    await expect(page.getByTestId("sql-chat-read-only-notice")).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-execute")).toBeVisible();
    const toggle = page.getByTestId("sql-chat-history-toggle");
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await expect(history.getByRole("button", { name: "「管理者の売上」の名前を変更" })).toHaveCount(1);
    if ((page.viewportSize()?.width ?? 0) < 1024) await page.keyboard.press("Escape");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("上位 5 件だけ");
    await composer.press("Enter");
    await confirmChatRequirement(page);
    await expect(turns).toHaveCount(2);
    expect(jobRequests[0]).toMatchObject({
      question: "上位 5 件だけ",
      chat: true,
      previous_job_id: "chat-mine",
    });
    await expect(page.locator("[data-chat-sender]")).toHaveCount(0);
  });

  test("一般の利用者の履歴は本人の会話だけで、利用者を出さない", async ({ page }) => {
    await setup(
      page,
      [
        {
          id: "chat-own",
          title: "自分の売上",
          profile_id: "sales",
          created_at: now,
          owned: true,
          actor_display_name: "",
        },
      ],
      { "chat-own": [turn("chat-own", "自分の売上", null)] },
    );
    // setup の後に登録した auth/me が優先されるよう、一般の利用者を登録し直す。
    await page.route("**/api/auth/me", (route) =>
      route.fulfill({
        json: {
          data: {
            ...systemAdminMe,
            is_system_admin: false,
            role_codes: ["ANALYST"],
            permissions: ["menu.chat", "nl2sql.query.generate", "nl2sql.sql.execute"],
            allowed_profile_ids: ["sales"],
          },
        },
      }),
    );
    const history = await openHistory(page);
    await expect(history.locator('[title="自分の売上"]')).toBeVisible();
    await expect(history.getByText(/・/)).toHaveCount(0);
    await history.locator('[title="自分の売上"]').click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    await expect(page.locator("[data-chat-sender]")).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-read-only-notice")).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeEditable();
  });
});
