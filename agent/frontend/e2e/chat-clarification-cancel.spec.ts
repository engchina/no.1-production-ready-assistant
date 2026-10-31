import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1441・#1445: Agent のチャットで、利用者の答えを待っている確認（NL2SQL の SQL の生成前の確認・RAG の確認）の下に
// 「データベースの検索は不要」「資料の検索は不要」を同じ部品で置く。押すとその文を送り、取り消した往復には
// 「データベースを使わずに回答を続けます」などの補足を出す。NL2SQL の確認を始めるボタンは置かない（会話の中で自動で聞く）。
// desktop と mobile-375 の 2 project で実行する。

const THREAD_ID = `thread_${"c".repeat(32)}`;
const GOAL = "今月の売上を部門別に教えて";

function supportTask(runId: string, pending: Record<string, unknown>[]) {
  return {
    id: `${runId}-support`,
    kind: "support_task",
    name: "支援タスクの状態",
    created_at: MOCK_NOW,
    content: {
      schema_version: 1,
      thread_id: THREAD_ID,
      owner_user_uuid: "local",
      run_id: runId,
      goal: GOAL,
      known_conditions: {},
      pending_clarifications: pending,
      guide: null,
      outcome: "needs_clarification",
      gaps: [],
      evidence: [],
      clarification_cancellations: [],
      budget: {
        run: { tool_calls: 1, rag_calls: 0, tool_seconds: 1, rag_seconds: 0 },
        task: { runs: 1, tool_calls: 1, rag_calls: 0, tool_seconds: 1, rag_seconds: 0 },
        limits: { rag_calls_per_run: 4, tool_calls_per_task: 60 },
      },
      updated_at: MOCK_NOW,
    },
  };
}

const NL2SQL_PENDING = {
  kind: "nl2sql",
  connection: "nl2sql",
  condition_id: "item-period",
  label: "期間の日付の列",
  question: "SALES のどの日付の列で期間を絞りますか？",
  options: ["SALES.ORDER_DATE（受注日）", "SALES.SHIP_DATE（出荷日）"],
  requirement_session_id: "qs-1",
};
const RAG_PENDING = {
  kind: "rag",
  condition_id: "screen",
  label: "画面",
  question: "どの画面の操作ですか？",
  options: ["受注入力", "請求"],
};

function seedRun(mockApi: MockApi, id: string, text: string, pending: Record<string, unknown>[], extra: Record<string, unknown> = {}) {
  mockApi.state.runs.push({
    id,
    goal: GOAL,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [
      { id: `${id}-answer`, kind: "answer", name: "回答", created_at: MOCK_NOW, content: { text } },
      supportTask(id, pending),
    ],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...extra,
  });
}

async function openSeedThread(page: Page, title = GOAL) {
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${title}`) }).click();
    return;
  }
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText(title).click();
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("NL2SQL の確認の下に「データベースの検索は不要」を出し、押すと取り消して補足を出す", async ({ page, mockApi }, testInfo) => {
  seedRun(mockApi, "run-nl2sql-1", "SALES のどの日付の列で期間を絞りますか？（受注日 / 出荷日）", [NL2SQL_PENDING]);
  await page.goto("/chat");
  await openSeedThread(page);

  const turn = page.getByTestId("chat-turn-run-nl2sql-1");
  await expect(turn.getByText("SALES のどの日付の列で期間を絞りますか？（受注日 / 出荷日）")).toBeVisible();
  // 回答の本文が利用者への問い返しの文でも、回答の本文なので見出し「回答」を出す（#3433）。
  await expect(turn.getByRole("heading", { level: 3, name: "回答", exact: true })).toBeVisible();
  // NL2SQL の確認（回答前の確認。旧名「AI要件確認」）を始めるボタンは置かない。取り消しの操作だけを確認の質問のそばに出す。
  await expect(page.getByRole("button", { name: /回答前の確認|AI要件確認/ })).toHaveCount(0);
  const actions = turn.getByRole("group", { name: "確認の取り消し" });
  const button = actions.getByRole("button", { name: "データベースの検索は不要" });
  await expect(button).toBeVisible();
  await expect(actions.getByRole("button", { name: "資料の検索は不要" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("nl2sql-cancel-before.png"), fullPage: true });

  await button.click();
  // 送信の request は click の後に非同期に出るので、記録されるまで待つ（#1519）。
  await expect.poll(() => mockApi.lastRequest("POST", "/api/runs")?.body).toMatchObject({
    goal: "データベースの検索は不要",
    thread_id: THREAD_ID,
    clarification_cancel: "nl2sql",
  });
  const created = page.getByTestId("chat-turn-run-chat-2");
  await expect(created.getByText("データベースの検索は不要").first()).toBeVisible();
  await expect(created.getByTestId("chat-clarification-cancelled-run-chat-2")).toHaveText(
    "データベースを使わずに回答を続けます"
  );
  // 取り消した後は、前の回答の操作を出さない（最後の往復だけに出す）。
  await expect(page.getByRole("group", { name: "確認の取り消し" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("nl2sql-cancel-after.png"), fullPage: true });
});

test("RAG の確認には同じ部品で「資料の検索は不要」を出し、会話の文で取り消した往復にも補足を出す", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-rag-0", "受注入力の画面の手順です。", [], {
    goal: "資料は関係ないです",
    metadata: { clarification_cancel: [{ kind: "rag", trigger: "message", questions: ["どの画面？"] }] },
    created_at: "2026-09-01T00:00:00Z",
  });
  seedRun(mockApi, "run-rag-1", "どの画面の操作ですか？（受注入力 / 請求）", [RAG_PENDING]);
  await page.goto("/chat");
  // 会話の名前は最初の発言。
  await openSeedThread(page, "資料は関係ないです");

  // 会話の文で取り消した往復の補足（Run の記録から）。
  await expect(page.getByTestId("chat-clarification-cancelled-run-rag-0")).toHaveText("資料を使わずに回答を続けます");

  const turn = page.getByTestId("chat-turn-run-rag-1");
  const actions = turn.getByRole("group", { name: "確認の取り消し" });
  await expect(actions.getByRole("button")).toHaveText(["資料の検索は不要"]);
  // キーボードで押せる（フォーカスして Enter）。
  const button = actions.getByRole("button", { name: "資料の検索は不要" });
  await button.focus();
  await expect(button).toBeFocused();
  await page.keyboard.press("Enter");
  // Enter の後、送信の request は非同期に出る。押した直後に読むと、遅い runner ではまだ記録が無い（#1519）。
  await expect.poll(() => mockApi.lastRequest("POST", "/api/runs")?.body).toMatchObject({
    goal: "資料の検索は不要",
    clarification_cancel: "rag",
  });
  await expect(page.getByTestId("chat-clarification-cancelled-run-chat-3")).toHaveText("資料を使わずに回答を続けます");
  await expectNoHorizontalOverflow(page);
});

test("確認を待っていない回答には取り消しの操作を出さない", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-done-1", "今月の部門別の売上は、営業が 1,200 です。", []);
  await page.goto("/chat");
  await openSeedThread(page);
  await expect(page.getByTestId("chat-turn-run-done-1").getByText("今月の部門別の売上は、営業が 1,200 です。")).toBeVisible();
  await expect(page.getByRole("group", { name: "確認の取り消し" })).toHaveCount(0);
});
