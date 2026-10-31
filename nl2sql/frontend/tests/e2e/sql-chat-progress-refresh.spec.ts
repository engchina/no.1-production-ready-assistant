import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { confirmChatRequirement, mockChatRequirement } from "./_helpers/chat-requirement";
import { withJobProgress } from "./_helpers/job-progress";

// #1160: チャットの回答の作成中に、処理の経過と回答が自動で更新されなくなる不具合の回帰テスト。
// 2 回目の質問のジョブを、段階が進む・取り直しが失敗する・DB の状態の確認が応答しない、の各条件で追い、
// 完了・失敗の終端が読み込み直さずに画面へ出ることを確かめる。

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

type Phase = "prepare" | "generate" | "done" | "error";

interface State {
  phase: Phase;
  /** 会話の取得を 5xx にする回数。 */
  failChats: number;
  /** DB の状態の確認（`GET /api/ready/database`）に応答しない。 */
  hangReadiness: boolean;
  /** 会話の取得に応答しない回数（応答を止める）。 */
  hangChats: number;
  chatRequests: number;
}

const iso = (offsetSeconds: number) => new Date(Date.now() + offsetSeconds * 1000).toISOString();

function firstTurn(createdAt: string) {
  return {
    job_id: "chat-1",
    question: "カテゴリ別売上",
    engine: "enterprise_ai_direct",
    status: "done",
    created_at: createdAt,
    started_at: createdAt,
    finished_at: createdAt,
    steps: [],
    result: {
      generated_sql: "SELECT CATEGORY, SUM(AMOUNT) FROM APP.SALES GROUP BY CATEGORY",
      original_question: "カテゴリ別売上",
      explanation: "カテゴリごとの売上合計です。",
      safety: { is_safe: true, referenced_tables: ["APP.SALES"] },
    },
  };
}

function secondTurn(phase: Phase, createdAt: string) {
  const step = (stage: string, status: string) => ({
    stage,
    status,
    started_at: status === "pending" ? null : createdAt,
    finished_at: status === "done" || status === "error" ? createdAt : null,
  });
  const base = {
    job_id: "chat-2",
    question: "多い順にして",
    engine: "enterprise_ai_direct",
    previous_job_id: "chat-1",
    created_at: createdAt,
    started_at: createdAt,
  };
  if (phase === "prepare")
    return { ...base, status: "running", result: null, steps: [step("prepare_context", "running")] };
  if (phase === "generate")
    return {
      ...base,
      status: "running",
      result: null,
      steps: [step("prepare_context", "done"), step("generate_sql", "running")],
    };
  if (phase === "error")
    return {
      ...base,
      status: "error",
      result: null,
      finished_at: createdAt,
      error_code: "SQL_GENERATION_FAILED",
      error_message: "SQL を生成できませんでした。",
      steps: [step("prepare_context", "done"), step("generate_sql", "error")],
    };
  return {
    ...base,
    status: "done",
    finished_at: createdAt,
    steps: [step("prepare_context", "done"), step("generate_sql", "done"), step("safety_check", "done")],
    result: {
      generated_sql: "SELECT CATEGORY, SUM(AMOUNT) FROM APP.SALES GROUP BY CATEGORY ORDER BY 2 DESC",
      original_question: "多い順にして",
      explanation: "売上の多い順です。",
      safety: { is_safe: true, referenced_tables: ["APP.SALES"] },
    },
  };
}

async function setup(page: Page): Promise<State> {
  await mockDatabaseGateReady(page);
  // 送信の前の要件の検査（#1442）は「確認が要らない」で、聞かずに SQL を生成する。
  await mockChatRequirement(page);
  const state: State = {
    phase: "prepare",
    failChats: 0,
    hangReadiness: false,
    hangChats: 0,
    chatRequests: 0,
  };
  const createdAt = iso(-5);
  let submitted = false;
  await page.route("**/api/ready/database", (route) => {
    // 応答しない（backend が詰まっている間の確認）。route を終えないと要求は返らない。
    if (state.hangReadiness) return;
    return route.fulfill({ json: { data: { status: "ok", check: "ok", detail: null } } });
  });
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) => {
    const conversation = { id: "chat-1", title: "カテゴリ別売上", profile_id: "sales", created_at: createdAt };
    if (new URL(route.request().url()).pathname === "/api/nl2sql/chats")
      return route.fulfill({
        json: { data: { items: [conversation], next_cursor: null, total: 1, limit: 25 } },
      });
    state.chatRequests += 1;
    if (state.hangChats > 0) {
      // 応答しない（route を終えない）。画面が取り直しで打ち切るまで返らない。
      state.hangChats -= 1;
      return;
    }
    if (state.failChats > 0) {
      state.failChats -= 1;
      return route.fulfill({ status: 503, json: { error: "一時的に応答できません。" } });
    }
    const turns = submitted ? [firstTurn(createdAt), secondTurn(state.phase, createdAt)] : [firstTurn(createdAt)];
    return route.fulfill({ json: { data: { conversation, turns: turns.map(withJobProgress) } } });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    submitted = true;
    return route.fulfill({
      json: {
        data: {
          job_id: "chat-2",
          status: "pending",
          created_at: createdAt,
          steps: [],
        },
      },
    });
  });
  return state;
}

async function openSecondQuestion(page: Page) {
  await page.goto("/chat");
  await page.getByTestId("sql-chat-history-toggle").click();
  await page.getByTestId("sql-chat-history").getByText("カテゴリ別売上", { exact: true }).click();
  if ((page.viewportSize()?.width ?? 1280) >= 1024) await page.getByTestId("sql-chat-history-toggle").click();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("多い順にして");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const turn = page.getByTestId("sql-chat-turn").nth(1);
  await expect(turn.getByTestId("sql-chat-progress-current")).toContainText("質問と参照できる表を整理しています");
  return turn;
}

for (const colorScheme of ["light", "dark"] as const) {
  test.describe(`処理の経過の自動の更新（${colorScheme}）`, () => {
    test.beforeEach(async ({ page }) => {
      await page.emulateMedia({ colorScheme });
    });

    test("2 回目の質問で段階が進み、完了したら読み込み直さずに結果に置き換わる", async ({ page }) => {
      const state = await setup(page);
      const turn = await openSecondQuestion(page);
      state.phase = "generate";
      await expect(turn.getByTestId("sql-chat-progress-current")).toContainText("SQL を生成しています");
      await expect(turn.getByTestId("sql-chat-progress-running")).toContainText("処理中（2 ステップ完了）");
      state.phase = "done";
      await expect(turn.locator("pre")).toContainText("ORDER BY 2 DESC");
      await expect(turn.getByTestId("sql-chat-progress-summary")).toContainText("処理の経過");
      // 実行中は開いていた一覧を、完了したら閉じる（3 製品で同じ。#2265）。
      await expect(turn.getByTestId("sql-chat-progress").locator("details").first()).not.toHaveAttribute("open", "");
      // 処理の詳細に trace id を出す（「トレースの詳細」の 2 つ目の表示は無い。#3226）。
      await expect(turn.getByTestId("sql-chat-progress").locator("[data-trace-id-row]")).toHaveCount(1);
      await expect(turn.getByText("トレースの詳細")).toHaveCount(0);
      await expect(page.getByTestId("sql-chat-send")).toHaveAccessibleName("送信");
    });

    test("取得が 5xx を返し DB の状態の確認が応答しなくても、取り直しを続けて完了が出る", async ({ page }) => {
      test.setTimeout(90_000);
      const state = await setup(page);
      const turn = await openSecondQuestion(page);
      // backend が詰まった（#1155）: 会話の取得が 5xx を返し、DB の状態の確認は応答しない。
      state.hangReadiness = true;
      state.failChats = 1;
      await expect.poll(() => state.failChats).toBe(0);
      // その間にジョブは完了する。読み込み直さずに結果が出ること（修正前は確認の応答を待ったまま止まった）。
      state.phase = "done";
      await expect(turn.locator("pre")).toContainText("ORDER BY 2 DESC", { timeout: 30_000 });
      await expect(turn.getByTestId("sql-chat-progress-summary")).toContainText("処理の経過");
      await expect(turn.getByTestId("sql-chat-progress-reconnecting")).toHaveCount(0);
    });

    test("取得が応答しなくなったら「接続を確認しています」を出して取り直し、完了が出る", async ({ page }) => {
      test.setTimeout(90_000);
      const state = await setup(page);
      const turn = await openSecondQuestion(page);
      // 応答を止める（2 回）。取り直しが応答しない取得を打ち切り、新しく取り直す。
      state.hangChats = 2;
      const reconnecting = turn.getByTestId("sql-chat-progress-reconnecting");
      await expect(reconnecting).toHaveText("接続を確認しています。", { timeout: 20_000 });
      // 案内は見出し「処理中（N ステップ完了）」の直下（左の見出しの側の 2 行目）に出す。今の段階の行の下・右の経過時間の
      // 列には出さない（#2426）。
      const timerBox = await turn.getByTestId("sql-chat-progress-timer").boundingBox();
      const noticeBox = await reconnecting.boundingBox();
      expect(timerBox && noticeBox && noticeBox.y >= timerBox.y + timerBox.height - 1).toBeTruthy();
      expect(timerBox && noticeBox && noticeBox.x < timerBox.x).toBeTruthy();
      await expect(turn.getByTestId("sql-chat-progress-current").getByTestId("sql-chat-progress-reconnecting")).toHaveCount(0);
      await expect(turn.getByTestId("sql-chat-progress-running").getByTestId("sql-chat-progress-reconnecting")).toHaveCount(1);
      // 今の段階の行は残したまま、遅延の案内の代わりに出す（行を動かさない）。
      await expect(turn.getByTestId("sql-chat-progress-current")).toContainText("質問と参照できる表を整理しています");
      await expect(turn.getByTestId("sql-chat-progress-current")).toHaveAttribute("data-reconnecting", "true");
      state.phase = "done";
      await expect(turn.locator("pre")).toContainText("ORDER BY 2 DESC", { timeout: 40_000 });
      await expect(reconnecting).toHaveCount(0);
      await expect(turn.getByTestId("sql-chat-progress-summary")).toContainText("処理の経過");
    });

    test("取得の失敗が続いても backoff して再開し、失敗の終端が出る", async ({ page }) => {
      test.setTimeout(90_000);
      const state = await setup(page);
      const turn = await openSecondQuestion(page);
      state.failChats = 8;
      await expect.poll(() => state.failChats, { timeout: 60_000 }).toBe(0);
      state.phase = "error";
      await expect(turn.getByTestId("sql-chat-progress-summary")).toContainText("処理の経過", { timeout: 30_000 });
      await expect(turn.getByText("SQL を生成できませんでした。")).toBeVisible();
      await expect(turn.getByTestId("sql-chat-progress-reconnecting")).toHaveCount(0);
    });
  });
}
