// DB 管理チャット（#2264）。質問から Oracle の SQL を作り、生成した SQL を必ず見せる。生成した SQL はサーバーが
// 文に分け、文ごとに分類・実行する（#2333）: SELECT は「実行」で、データや構造を変える文は実行確認語を入力した
// ときだけ実行し、止める文はその文だけを実行できなくする。「すべて実行」は上から順に実行し、最初のエラーで止める。
// 画面の骨格・会話の履歴・入力欄・送信中の往復・SQL の表示・結果の表・処理の経過は「チャット」と同じ共通の部品で
// 描く。会話はサーバーが保存し、チャットと同じ会話の履歴のパネル（開き直し・名前の変更・削除・新しい会話）を持つ
// （#2319）。
// LLM・Oracle・会話の保存は page.route の決定論の mock に置き換える（表・列の名前は合成のもの）。
// 各 test は playwright.config の desktop（1280px）と mobile-375 の両方で動く。
import { expect, test, type Page, type Request, type Route } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { dbAdminChatProgressEvents, withRecordedTrace } from "./_helpers/job-progress";

const timing = { created_at: "2026-10-18T00:00:00Z", finished_at: "2026-10-18T00:00:01Z", elapsed_ms: 120 };

type Json = Record<string, unknown>;

interface StatementPlan extends Json {
  index: number;
  statement_type: string;
  sql: string;
  select_only: boolean;
  implicit_commit: boolean;
  blocked_reason: string;
  requires_confirmation: boolean;
  confirmation_target: string;
  unknown_columns: string[];
  warnings: string[];
}

/** サーバーと同じ規則の、文ごとの分類（SELECT は確認語なし、それ以外は ADMIN_EXECUTE、止める文は実行できない）。 */
function statementPlan(index: number, sql: string, blockedReason = "", unknownColumns: string[] = []): StatementPlan {
  const select = /^\s*(select|with)\b/i.test(sql);
  const changes = !select && !blockedReason;
  return {
    index,
    statement_type: select ? "SELECT" : sql.trim().split(/\s+/)[0]!.toUpperCase(),
    sql,
    select_only: select,
    implicit_commit: false,
    blocked_reason: blockedReason,
    requires_confirmation: changes,
    confirmation_target: changes ? "ADMIN_EXECUTE" : "",
    unknown_columns: unknownColumns,
    warnings: [],
  };
}

/** 1 文（文字列）か、文の並び（文字列か [SQL, 止める理由, 存在しない列]）の分類。 */
function plan(sqls: string | (string | [string, string] | [string, string, string[]])[], overrides: Record<string, unknown> = {}) {
  const items = (Array.isArray(sqls) ? sqls : [sqls]).map((item, index) =>
    Array.isArray(item) ? statementPlan(index + 1, item[0], item[1], item[2]) : statementPlan(index + 1, item),
  );
  const changing = items.filter((item) => !item.select_only);
  const blockedReasons = items.map((item) => item.blocked_reason).filter(Boolean);
  const requiresConfirmation = changing.length > 0 && blockedReasons.length === 0;
  return {
    sql: Array.isArray(sqls) ? items.map((item) => `${item.sql};`).join("\n") : sqls,
    statements: items,
    read_only: items.length > 0 && changing.length === 0,
    requires_confirmation: requiresConfirmation,
    confirmation_target: requiresConfirmation ? "ADMIN_EXECUTE" : "",
    implicit_commit: false,
    blocked: blockedReasons.length > 0,
    blocked_reasons: blockedReasons,
    ...overrides,
  };
}

type Plan = ReturnType<typeof plan>;

interface Generated {
  plan: Plan;
  explanation?: string;
  /** 回答の注意書き（#2405。backend がターンに付ける文）。 */
  notice?: string | null;
}

function generated(planData: Plan, explanation = "説明の文です。"): Generated {
  return { plan: planData, explanation };
}

function selectResultFor(index: number, column = "BANNER_FULL", value = "Oracle AI Database Release 1.0") {
  return {
    executed: true,
    runtime: "oracle",
    execution_context: "admin_control_plane",
    select_result: {
      columns: [column],
      rows: [{ [column]: value }],
      total: 1,
      returned_count: 1,
      has_more: false,
      truncated: false,
      execution_context: "admin_control_plane",
    },
    statements: [
      { index, statement_type: "SELECT", status: "executed", sql: "SELECT 1", row_count: 1, message: "1 rows", elapsed_ms: 5 },
    ],
    committed: false,
    rolled_back: false,
    warnings: [],
    timing,
  };
}

const selectResult = selectResultFor(1);

function mutationResult(sql: string, ok = true, index = 1) {
  return {
    executed: ok,
    runtime: "oracle",
    execution_context: "admin_control_plane",
    statements: [
      ok
        ? { index, statement_type: "DELETE", status: "success", sql, row_count: 3, message: "RowsAffected=3", elapsed_ms: 5 }
        : {
            index,
            statement_type: "DELETE",
            status: "error",
            sql,
            error_message: "ORA-00942: table or view does not exist",
            error_code: "ORA-00942",
            message: "",
            elapsed_ms: 5,
          },
    ],
    committed: ok,
    rolled_back: !ok,
    // backend は DB のエラーの文を警告にも入れる。画面は失敗のカードに重ねて出さない（#2342）。
    warnings: ok ? [] : ["ORA-00942: table or view does not exist Help: https://docs.oracle.com/error-help/db/ora-00942/"],
    timing,
  };
}

interface StoredTurn extends Json {
  turn_id: string;
  question: string;
  plan: Plan;
  revision: number;
  corrected: boolean;
  statement_executions: Json[];
  last_run_all: Json | null;
}

interface StoredConversation {
  id: string;
  title: string;
  created_at: string;
  owned: boolean;
  actor_display_name: string;
  turns: StoredTurn[];
}

interface ChatMock {
  generateBodies: Json[];
  executeBodies: Json[];
  analyzeBodies: Json[];
  planBodies: Json[];
  conversations: StoredConversation[];
}

function storedTurn(turnId: string, question: string, next: Generated): StoredTurn {
  // 生成したときの span は mock の span の表に書き、ターンの処理の経過の API が返す（#3226 の案 A）。
  return withRecordedTrace(turnId, {
    turn_id: turnId,
    question,
    created_at: "2026-10-18T00:00:00Z",
    generated_at: "2026-10-18T00:00:02Z",
    explanation: next.explanation ?? "",
    warnings: [],
    context_tables: [],
    model: "test-text-model",
    plan: next.plan,
    revision: 0,
    corrected: false,
    statement_executions: [],
    last_run_all: null,
    notice: next.notice ?? null,
    // 生成したときの処理の経過（生成と SQL の確認の span の木。#3226）。
    progress_events: dbAdminChatProgressEvents(turnId, next.plan, {
      createdAt: "2026-10-18T00:00:00Z",
      generatedAt: "2026-10-18T00:00:02Z",
    }),
  });
}

/** 保存した 1 文の最後の実行の要約（サーバーと同じく、結果の行は残さない）。 */
function savedExecution(index: number, status: string, result: Json | null, mode = "statement"): Json {
  const select = result?.select_result as Json | undefined;
  return {
    index,
    executed_at: "2026-10-17T09:31:00Z",
    mode,
    batch_id: `batch-${index}`,
    confirmation_given: false,
    status,
    row_count: select ? select.returned_count : null,
    column_count: select ? (select.columns as unknown[]).length : null,
    has_more: false,
    outcome: status,
    result: result ? { ...result, select_result: null } : null,
  };
}

/** 保存した会話（開き直しの確かめに使う）。 */
function savedConversation(
  id: string,
  title: string,
  turns: StoredTurn[],
  overrides: Partial<StoredConversation> = {},
): StoredConversation {
  return { id, title, created_at: "2026-10-17T09:30:00Z", owned: true, actor_display_name: "", turns, ...overrides };
}

function outcomeStatus(result: Json | null): string {
  if (!result) return "not_run";
  const statements = (result.statements as Json[]) ?? [];
  return statements.every((item) => item.status === "success" || item.status === "executed") ? "succeeded" : "failed";
}

/** 会話の保存・生成・実行・分析を、サーバーと同じ契約の mock にする。 */
async function setup(
  page: Page,
  {
    responses = [],
    execute,
    executeAll,
    analyze,
    conversations = [],
  }: {
    responses?: Generated[];
    /** 1 文の実行の結果（その文だけの管理 SQL の実行の結果）。 */
    execute?: (body: Json, turn: StoredTurn, statement: StatementPlan) => Json;
    /** 「すべて実行」の文ごとの結果（null は実行しなかった文）。既定はすべて成功。 */
    executeAll?: (body: Json, turn: StoredTurn) => (Json | null)[];
    analyze?: unknown;
    conversations?: StoredConversation[];
  } = {},
): Promise<ChatMock> {
  await mockDatabaseGateReady(page);
  const mock: ChatMock = {
    generateBodies: [],
    executeBodies: [],
    analyzeBodies: [],
    planBodies: [],
    conversations: [...conversations],
  };
  const body = (request: Request) => JSON.parse(request.postData() ?? "{}") as Json;
  const summary = (item: StoredConversation) => ({
    id: item.id,
    title: item.title,
    created_at: item.created_at,
    owned: item.owned,
    actor_display_name: item.actor_display_name,
  });
  const find = (id: string) => mock.conversations.find((item) => item.id === id);
  await page.route("**/api/nl2sql/db-admin-chat/conversations**", (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const parts = url.pathname.split("/conversations")[1]!.split("/").filter(Boolean).map(decodeURIComponent);
    if (parts.length === 0) {
      const offset = Number(url.searchParams.get("offset") ?? "0");
      const limit = Number(url.searchParams.get("limit") ?? "25");
      const items = mock.conversations.slice(offset, offset + limit).map(summary);
      return route.fulfill({
        json: {
          data: { items, total: mock.conversations.length, limit, offset, has_next: offset + limit < mock.conversations.length },
        },
      });
    }
    const conversation = find(parts[0]!);
    if (!conversation) return route.fulfill({ status: 404, json: { detail: "会話が見つかりません。" } });
    if (parts[1] === "turns" && parts[3] === "plan") {
      const update = body(request);
      mock.planBodies.push(update);
      const turn = conversation.turns.find((item) => item.turn_id === parts[2])!;
      // statement_index があればその文だけを置き換え、サーバーが分類し直す。
      const index = typeof update.statement_index === "number" ? update.statement_index : null;
      const sqls = index
        ? turn.plan.statements.map((item) => (item.index === index ? String(update.sql) : item.sql))
        : [String(update.sql)];
      Object.assign(turn, {
        plan: plan(sqls.length === 1 ? sqls[0]! : sqls),
        revision: turn.revision + 1,
        corrected: true,
        statement_executions: [],
        last_run_all: null,
      });
      return route.fulfill({ json: { data: turn } });
    }
    if (request.method() === "PATCH") {
      conversation.title = String(body(request).title);
      return route.fulfill({ json: { data: summary(conversation) } });
    }
    if (request.method() === "DELETE") {
      mock.conversations = mock.conversations.filter((item) => item.id !== conversation.id);
      return route.fulfill({ json: { data: null } });
    }
    return route.fulfill({ json: { data: { conversation: summary(conversation), turns: conversation.turns } } });
  });
  await page.route("**/api/nl2sql/db-admin-chat/generate", (route) => {
    const request = body(route.request());
    mock.generateBodies.push(request);
    const next = responses.shift();
    if (!next) return route.fulfill({ status: 502, json: { detail: "SQL の生成に失敗しました。" } });
    const turnId = String(request.client_turn_id);
    const id = String(request.conversation_id || turnId);
    let conversation = find(id);
    if (!conversation) {
      conversation = savedConversation(id, String(request.question), [], { created_at: "2026-10-18T00:00:00Z" });
      mock.conversations.unshift(conversation);
    }
    const turn = storedTurn(turnId, String(request.question), next);
    conversation.turns.push(turn);
    return route.fulfill({ json: { data: { conversation: summary(conversation), turn } } });
  });
  await page.route("**/api/nl2sql/db-admin-chat/execute", (route) => {
    const request = body(route.request());
    mock.executeBodies.push(request);
    const conversation = find(String(request.conversation_id))!;
    const turn = conversation.turns.find((item) => item.turn_id === request.turn_id)!;
    const statements = turn.plan.statements;
    const results =
      request.mode === "all"
        ? (executeAll?.(request, turn) ?? statements.map((item) => selectResultFor(item.index)))
        : statements.map((item) =>
            item.index === request.statement_index
              ? (execute ? execute(request, turn, item) : selectResultFor(item.index))
              : undefined,
          );
    const outcomes = statements
      .map((item, position) => ({ item, result: results[position] }))
      .filter(({ result }) => result !== undefined)
      .map(({ item, result }) => ({ index: item.index, status: outcomeStatus(result ?? null), result: result ?? null }));
    const stopped = outcomes.find((outcome) => outcome.status === "failed");
    const committed = outcomes.some((outcome) => Boolean(outcome.result?.committed));
    const rolledBack = outcomes.some((outcome) => Boolean(outcome.result?.rolled_back));
    for (const outcome of outcomes) {
      turn.statement_executions = [
        ...turn.statement_executions.filter((saved) => saved.index !== outcome.index),
        savedExecution(outcome.index, outcome.status, outcome.result, String(request.mode)),
      ].sort((a, b) => Number(a.index) - Number(b.index));
    }
    if (request.mode === "all") {
      turn.last_run_all = {
        executed_at: "2026-10-17T09:31:00Z",
        batch_id: "batch-all",
        confirmation_given: Boolean(request.confirmation),
        succeeded: !stopped && outcomes.every((outcome) => outcome.status === "succeeded"),
        statement_count: outcomes.length,
        succeeded_count: outcomes.filter((outcome) => outcome.status === "succeeded").length,
        stopped_at: stopped?.index ?? null,
        committed,
        rolled_back: rolledBack,
        warnings: [],
      };
    }
    return route.fulfill({
      json: {
        data: {
          mode: request.mode,
          batch_id: "batch-all",
          executed: outcomes.some((outcome) => outcome.status === "succeeded"),
          committed,
          rolled_back: rolledBack,
          stopped_at: request.mode === "all" ? (stopped?.index ?? null) : null,
          statements: outcomes,
          warnings: [],
          timing,
        },
      },
    });
  });
  await page.route("**/api/nl2sql/db-admin-chat/conversations/*/turns/*/analyze-error", (route) => {
    mock.analyzeBodies.push(body(route.request()));
    return route.fulfill({ json: { data: analyze } });
  });
  return mock;
}

async function ask(page: Page, question: string) {
  const composer = page.locator("#db-admin-chat-composer");
  await composer.fill(question);
  // Enter で送信（Shift+Enter は改行）。チャットと同じ共通の入力欄。
  await composer.press("Enter");
}

function historyToggle(page: Page) {
  return page.getByRole("button", { name: "会話の履歴", exact: true });
}

/** 会話の履歴を開いて会話を選ぶ（desktop は左のパネル、狭い画面はシート。チャットと同じ）。 */
async function openFromHistory(page: Page, title: string) {
  const toggle = historyToggle(page);
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await page.getByTestId("db-admin-chat-history").getByText(title, { exact: true }).click();
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

test("DB 管理チャットは「データ準備」の「管理 SQL を実行」の上にあり、チャットと同じ会話の履歴の開閉ボタンを持つ", { tag: "@responsive" }, async ({ page }) => {
  await setup(page);
  await page.goto("/db-admin-chat");
  await expect(page.getByRole("heading", { level: 1, name: "DB 管理チャット" })).toBeVisible();
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  // 会話の履歴の開閉はアイコンだけのボタンで、会話の欄の上端の行の左端にある（チャットと同じ。#889）。
  const toggle = historyToggle(page);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toHaveAttribute("aria-controls", "db-admin-chat-history");
  await expect(toggle).toHaveText("");
  const toggleBox = (await toggle.boundingBox())!;
  const newButtonBox = (await page.getByRole("button", { name: "新しい会話", exact: true }).boundingBox())!;
  const panelBox = (await page.getByTestId("db-admin-chat-panel").boundingBox())!;
  expect(toggleBox.x - panelBox.x).toBeLessThan(24);
  expect(toggleBox.x).toBeLessThan(newButtonBox.x);
  await toggle.click();
  await expect(page.getByTestId("db-admin-chat-history").getByText("会話はまだありません")).toBeVisible();
  // 狭い画面の履歴はシート（Esc で閉じて開閉ボタンへ戻る）、desktop は左のパネル（もう一度押して閉じる）。
  if ((page.viewportSize()?.width ?? 0) < 1024) {
    await page.keyboard.press("Escape");
    await expect(toggle).toBeFocused();
  } else {
    await toggle.click();
  }
  await expect(toggle).toHaveAttribute("aria-expanded", "false");

  const sidebar = await openSidebarNav(page);
  const expand = sidebar.getByRole("button", { name: "データ準備 を展開" });
  if (await expand.isVisible()) await expand.click();
  const chatLink = sidebar.getByRole("link", { name: "DB 管理チャット" });
  const adminSqlLink = sidebar.getByRole("link", { name: "管理 SQL を実行" });
  await expect(chatLink).toBeVisible();
  const chatBox = await chatLink.boundingBox();
  const adminBox = await adminSqlLink.boundingBox();
  expect(chatBox && adminBox && chatBox.y < adminBox.y).toBeTruthy();
  await expectNoHorizontalOverflow(page);
});

test("SELECT は「実行」を押すと実行し、結果を表で出す。続きの質問は同じ会話に送り、前の往復は送らない", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page, {
    responses: [
      generated(plan("SELECT BANNER_FULL FROM V$VERSION")),
      generated(plan("SELECT TABLESPACE_NAME, USED_PERCENT FROM DBA_TABLESPACE_USAGE_METRICS")),
    ],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "データベースの版");

  const turn = page.getByTestId("db-admin-chat-turn").first();
  await expect(turn.getByTestId("db-admin-chat-sql")).toHaveText("SELECT BANNER_FULL FROM V$VERSION");
  await expect(turn.getByText("読み取り専用", { exact: true })).toBeVisible();
  // 1 文だけのターンには「すべて実行」を出さない。
  await expect(turn.getByTestId("db-admin-chat-run-all")).toHaveCount(0);
  // 生成しただけでは実行しない。
  expect(mock.executeBodies).toHaveLength(0);
  // SELECT は確認語の欄を出さない。
  await expect(turn.getByRole("textbox", { name: "実行確認語" })).toHaveCount(0);
  // 処理の経過は生成と SQL の確認まで。実行は経過に足さず（段の数・合計時間を変えない）、SQL の区画に出す。
  const progress = turn.getByTestId("db-admin-chat-progress-summary");
  await expect(progress).toHaveText(/^処理の経過（2 ステップ・/);
  const progressBefore = (await progress.textContent()) ?? "";
  await turn.getByTestId("db-admin-chat-execute").click();
  await expect(turn.getByTestId("db-admin-chat-result")).toBeVisible();
  await expect(turn.getByText("Oracle AI Database Release 1.0")).toBeVisible();
  await expect(turn.getByTestId("db-admin-chat-execute")).toHaveText("もう一度実行");
  await expect(progress).toHaveText(progressBefore);
  await turn.getByTestId("db-admin-chat-execute").click();
  await expect.poll(() => mock.executeBodies.length).toBe(2);
  await expect(turn.getByTestId("db-admin-chat-result")).toBeVisible();
  await expect(progress).toHaveText(progressBefore);
  // SQL は送らず、ターンに保存した SQL を実行する（#2319）。
  const conversationId = String(mock.generateBodies[0]!.client_turn_id);
  expect(mock.generateBodies[0]).toMatchObject({ question: "データベースの版", conversation_id: "" });
  expect(mock.executeBodies[0]).toEqual({
    conversation_id: conversationId,
    turn_id: conversationId,
    mode: "statement",
    statement_index: 1,
    confirmation: "",
    row_limit: expect.any(Number),
  });

  await ask(page, "表領域の使用状況");
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(2);
  // 続きの質問は同じ会話に送り、前の往復はサーバーが保存した会話から復元する。
  expect(mock.generateBodies[1]).toMatchObject({ question: "表領域の使用状況", conversation_id: conversationId });
  expect(mock.generateBodies[1]).not.toHaveProperty("history");
  await expect(page.getByTestId("db-admin-chat-panel").getByText("データベースの版", { exact: true }).first()).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("会話は履歴から開き直せ、SQL・分類・前回の実行の要約が戻る。「新しい会話」は空から始める", { tag: "@responsive" }, async ({ page }) => {
  const selectTurn = storedTurn("turn-a1", "データベースの版", generated(plan("SELECT BANNER_FULL FROM V$VERSION")));
  selectTurn.statement_executions = [savedExecution(1, "succeeded", selectResult)];
  const deleteSql = "DELETE FROM APP.TEMP_ITEMS WHERE QTY = 0";
  const mutationTurn = storedTurn("turn-a2", "在庫が 0 の行を消す", generated(plan(deleteSql)));
  mutationTurn.statement_executions = [savedExecution(1, "succeeded", mutationResult(deleteSql))];
  const mock = await setup(page, {
    responses: [generated(plan("SELECT 1 FROM DUAL"))],
    conversations: [
      savedConversation("conversation-a", "会話 A", [selectTurn, mutationTurn]),
      savedConversation("conversation-b", "会話 B", []),
    ],
  });
  await page.goto("/db-admin-chat");
  await openFromHistory(page, "会話 A");
  const turns = page.getByTestId("db-admin-chat-turn");
  await expect(turns).toHaveCount(2);
  // 結果の行は保存しないので、SELECT は前回の実行の要約と「もう一度実行」（チャットと同じ）。
  await expect(turns.nth(0).getByTestId("db-admin-chat-last-execution")).toContainText("1 行・1 列");
  await expect(turns.nth(0).getByText("読み取り専用", { exact: true })).toBeVisible();
  await expect(turns.nth(0).getByTestId("db-admin-chat-execute")).toHaveText("もう一度実行");
  // 更新系は文ごとの状態・影響行数・commit。確認語は保存しないので空からやり直す。
  await expect(turns.nth(1).getByText("変更あり", { exact: true })).toBeVisible();
  await expect(turns.nth(1).getByText("影響件数 3 件", { exact: true })).toBeVisible();
  await expect(turns.nth(1).getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  // 開いている会話の名前を会話の欄の上端に出す。
  if ((page.viewportSize()?.width ?? 0) >= 1024) await historyToggle(page).click();
  else await expect(page.getByRole("dialog", { name: "会話の履歴" })).toHaveCount(0);
  await expect(page.getByTestId("db-admin-chat-panel").getByText("会話 A", { exact: true }).first()).toBeVisible();

  // 再読み込みしても開いていた会話に戻る（会話の ID は作業状態に残す）。
  await page.reload();
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(2);

  // 「もう一度実行」も保存した SQL を実行する。
  await page.getByTestId("db-admin-chat-turn").nth(0).getByTestId("db-admin-chat-execute").click();
  await expect(page.getByTestId("db-admin-chat-result")).toBeVisible();
  expect(mock.executeBodies.at(-1)).toMatchObject({ conversation_id: "conversation-a", turn_id: "turn-a1" });

  // 開いた会話への続きの質問は、その会話に送る。
  await ask(page, "続きの質問");
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(3);
  expect(mock.generateBodies.at(-1)).toMatchObject({ conversation_id: "conversation-a" });

  await page.getByRole("button", { name: "新しい会話", exact: true }).click();
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(0);
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

/** 会話の欄の末尾からの距離（px）。 */
function distanceFromBottom(page: Page) {
  return page
    .getByTestId("db-admin-chat-conversation")
    .evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
}

// 往復の多い会話でも、チャットと同じく本文は画面の高さに収め、会話の欄の中だけをスクロールする（#2332）。
// 入力欄は会話のカードの下端、履歴のパネルは左に固定。375px はチャットと同じく会話のカードの高さを決めて
// その中をスクロールし、ページは縦に流れる。長い SQL と結果の表は会話の欄の中に収める。
for (const width of [1280, 1920, 375]) {
  test(`往復の多い会話は会話の欄の中でスクロールし、入力欄を画面内に保つ (#2332, ${width}px)`, async ({ page }, testInfo) => {
    const longSql = `SELECT ${Array.from({ length: 40 }, (_, index) => `COLUMN_${index + 1}`).join(", ")} FROM APP.SAMPLE_ITEMS`;
    const turns = Array.from({ length: 12 }, (_, index) =>
      storedTurn(
        `turn-many-${index + 1}`,
        `質問 ${index + 1}`,
        generated(plan(index % 3 === 0 ? longSql : `SELECT ${index + 1} FROM DUAL`)),
      ),
    );
    const wideColumns = Array.from({ length: 16 }, (_, index) => `COLUMN_${index + 1}`);
    await setup(page, {
      responses: [generated(plan(longSql))],
      conversations: [savedConversation("conversation-many", "会話 M", turns)],
      execute: () => ({
        ...selectResult,
        select_result: {
          ...selectResult.select_result,
          columns: wideColumns,
          rows: [Object.fromEntries(wideColumns.map((column) => [column, `${column}_VALUE_LONG_TEXT`]))],
        },
      }),
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/db-admin-chat");
    await openFromHistory(page, "会話 M");
    const log = page.getByTestId("db-admin-chat-conversation");
    await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(12);
    // 会話を開き直したら会話の欄の末尾を出す（チャットと同じ）。
    await expect.poll(() => distanceFromBottom(page)).toBeLessThanOrEqual(2);
    // 会話の欄の中にスクロールがある。
    expect(await log.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeGreaterThan(0);

    const composer = page.locator("#db-admin-chat-composer");
    if (width >= 1024) {
      // ページも本文（#pr-main）も縦にスクロールしない。
      expect(
        await page.evaluate(() => document.scrollingElement!.scrollHeight - window.innerHeight),
      ).toBeLessThanOrEqual(0);
      expect(await page.locator("#pr-main").evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1);
      // 入力欄は会話のカードの下端にあり、スクロールせずに画面内に見える。
      await expect(composer).toBeInViewport();
      const panel = (await page.getByTestId("db-admin-chat-panel").boundingBox())!;
      const composerRegion = (await page.getByTestId("db-admin-chat-composer-region").boundingBox())!;
      expect(Math.abs(panel.y + panel.height - (composerRegion.y + composerRegion.height))).toBeLessThanOrEqual(2);
      expect(panel.y + panel.height).toBeLessThanOrEqual(900);
      // 履歴のパネルは左に開いたまま、会話の欄を上へスクロールしても動かない。
      const history = page.getByTestId("db-admin-chat-history");
      await expect(history).toBeVisible();
      const historyBefore = (await history.boundingBox())!;
      await log.evaluate((el) => el.scrollTo({ top: 0, behavior: "auto" }));
      await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBe(0);
      const historyAfter = (await history.boundingBox())!;
      expect(historyAfter.y).toBe(historyBefore.y);
      await expect(composer).toBeInViewport();
      await page.screenshot({ path: testInfo.outputPath(`db-admin-chat-many-${width}.png`) });
      // 新しい往復を送ると、上を読んでいても末尾へ移る（送った本人の操作なので末尾へ追う）。
      await ask(page, "続きの質問");
      await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(13);
      await expect.poll(() => distanceFromBottom(page)).toBeLessThanOrEqual(2);
      await expect(composer).toBeInViewport();
    } else {
      // 375px はチャットと同じ形: 履歴はシートで閉じ、会話のカードは画面の高さ以内で、その中をスクロールする。
      await expect(page.getByRole("dialog", { name: "会話の履歴" })).toHaveCount(0);
      const panel = (await page.getByTestId("db-admin-chat-panel").boundingBox())!;
      expect(panel.height).toBeLessThanOrEqual(900);
      await composer.focus();
      await expect(composer).toBeInViewport();
      await ask(page, "続きの質問");
      await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(13);
      await expect.poll(() => distanceFromBottom(page)).toBeLessThanOrEqual(2);
    }

    // 長い SQL と横に広い結果の表は会話の欄の中で横にスクロールし、ページを横に広げない。
    const last = page.getByTestId("db-admin-chat-turn").last();
    await last.getByTestId("db-admin-chat-execute").click();
    await expect(last.getByTestId("db-admin-chat-result")).toBeVisible();
    const logBox = (await log.boundingBox())!;
    for (const box of [
      (await last.getByTestId("db-admin-chat-sql").boundingBox())!,
      (await last.getByTestId("db-admin-chat-result").boundingBox())!,
    ]) {
      expect(box.x).toBeGreaterThanOrEqual(logBox.x - 1);
      expect(box.x + box.width).toBeLessThanOrEqual(logBox.x + logBox.width + 1);
    }
    await expectNoHorizontalOverflow(page);
    if (width >= 1024) {
      expect(
        await page.evaluate(() => document.scrollingElement!.scrollHeight - window.innerHeight),
      ).toBeLessThanOrEqual(0);
      await expect(composer).toBeInViewport();
    }
  });
}

test("会話の名前の変更と削除はチャットと同じ操作でできる", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page, {
    conversations: [
      savedConversation("conversation-a", "会話 A", [
        storedTurn("turn-a1", "データベースの版", generated(plan("SELECT 1 FROM DUAL"))),
      ]),
    ],
  });
  await page.goto("/db-admin-chat");
  await openFromHistory(page, "会話 A");
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(1);
  const toggle = historyToggle(page);
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const history = page.getByTestId("db-admin-chat-history");
  await history.getByRole("button", { name: "「会話 A」の名前を変更" }).click();
  const title = history.getByRole("textbox", { name: "会話名" });
  await title.fill("会話 A2");
  await title.press("Enter");
  await expect(history.getByText("会話 A2", { exact: true })).toBeVisible();
  expect(mock.conversations[0]!.title).toBe("会話 A2");

  await history.getByRole("button", { name: "「会話 A2」を削除" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除", exact: true }).click();
  await expect(history.getByText("会話はまだありません")).toBeVisible();
  expect(mock.conversations).toHaveLength(0);
  // 開いていた会話を消したら、新しい会話に戻る。
  if ((page.viewportSize()?.width ?? 0) < 1024) await page.keyboard.press("Escape");
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(0);
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
});

test("system_admin が見る他の利用者の会話は、日時に利用者の名前を添え、閲覧と削除だけにする（#3244）", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page, {
    conversations: [
      savedConversation(
        "conversation-x",
        "会話 X",
        [storedTurn("turn-x1", "データベースの版", generated(plan("SELECT 1 FROM DUAL")))],
        { owned: false, actor_display_name: "利用者 B" },
      ),
    ],
  });
  await page.goto("/db-admin-chat");
  await historyToggle(page).click();
  await expect(page.getByTestId("db-admin-chat-history").getByText("利用者 B", { exact: false })).toBeVisible();
  await page.getByTestId("db-admin-chat-history").getByText("会話 X", { exact: true }).click();
  await expect(page.getByTestId("db-admin-chat-turn")).toHaveCount(1);
  // 続きの質問・実行・名前の変更は持ち主だけ: 入力欄と「実行」を出さず、閲覧だけの案内を出す。
  const readOnly = page.getByTestId("db-admin-chat-read-only-notice");
  await expect(readOnly).toContainText("他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。");
  await expect(readOnly).toBeInViewport();
  await expect(page.getByRole("textbox", { name: "質問", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("db-admin-chat-send")).toHaveCount(0);
  await expect(page.getByTestId("db-admin-chat-execute")).toHaveCount(0);
  await expect(page.getByTestId("db-admin-chat-sql")).toBeVisible();
  // 履歴の行は名前の変更を出さず、削除だけを出す。
  const toggle = historyToggle(page);
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const history = page.getByTestId("db-admin-chat-history");
  await expect(history.getByRole("button", { name: "「会話 X」の名前を変更" })).toHaveCount(0);
  await history.getByRole("button", { name: "「会話 X」を削除" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除", exact: true }).click();
  await expect(history.getByText("会話はまだありません")).toBeVisible();
  expect(mock.conversations).toHaveLength(0);
  await expectNoHorizontalOverflow(page);
});

test("SELECT 以外は実行確認語を入れたときだけ実行し、失敗は AI の分析で直した SQL を確認からやり直す", { tag: "@responsive" }, async ({ page }) => {
  const sql = "DELETE FROM APP.TEMP_ITEMS WHERE QTY = 0";
  const corrected = "DELETE FROM APP.TEMP_ITEMS_V2 WHERE QTY = 0";
  let calls = 0;
  const mock = await setup(page, {
    responses: [generated(plan(sql, { implicit_commit: false }))],
    execute: (_request, turn) => {
      calls += 1;
      return mutationResult(turn.plan.sql, calls > 1);
    },
    analyze: {
      cause: "対象の表がありません。", fix: "列の定義を確認します。", conclusion: "修正後に再実行します。",
      corrected_plan: plan(corrected),
      source: "oci_enterprise_ai",
      model: "test-text-model",
      warnings: [],
    },
  });
  await page.goto("/db-admin-chat");
  await ask(page, "在庫が 0 の行を消す");
  const turn = page.getByTestId("db-admin-chat-turn").first();
  await expect(turn.getByText("変更あり", { exact: true })).toBeVisible();
  await expect(turn.getByText("この文はデータベースを変更します。", { exact: false })).toBeVisible();
  const confirmation = turn.getByRole("textbox", { name: "実行確認語" });
  const execute = turn.getByTestId("db-admin-chat-execute");
  await expect(execute).toBeDisabled();
  await confirmation.fill("admin_execute");
  await expect(execute).toBeDisabled();
  await confirmation.fill("ADMIN_EXECUTE");
  await expect(execute).toBeEnabled();
  await execute.click();
  expect(mock.executeBodies[0]).toMatchObject({ confirmation: "ADMIN_EXECUTE" });
  expect(mock.executeBodies[0]).not.toHaveProperty("sql");
  await expect(turn.getByText("ORA-00942: table or view does not exist").first()).toBeVisible();
  // 実行した後の失敗は結果の位置に 1 つの失敗のカード（何が起きたか・DB の文 1 回・具体的な原因・「詳細」。#2342）。
  const failure = turn.getByTestId("db-admin-statement-failure");
  await expect(failure).toHaveCount(1);
  await expect(failure.getByRole("alert")).toContainText("この文の実行に失敗しました。");
  await expect(failure).toContainText("参照先のテーブルまたはビューが存在しない");
  await expect(failure.locator("details")).toContainText("ORA-00942");
  await expect(turn.getByText("ORA-00942: table or view does not exist", { exact: true })).toHaveCount(1);
  await expect(turn.getByText("エラー概要", { exact: true })).toHaveCount(0);
  // 実行の確認は 1 回ごと。
  await expect(confirmation).toHaveValue("");

  await turn.getByTestId("db-admin-chat-analyze").click();
  await expect(turn.getByTestId("db-admin-chat-corrected-sql")).toHaveText(corrected);
  expect(mock.analyzeBodies[0]).toEqual({ statement_index: 1 });
  await turn.getByTestId("db-admin-chat-use-corrected").click();
  // 直した SQL はサーバーがその文を置き換えて分類し直す。
  await expect.poll(() => mock.planBodies).toEqual([{ sql: corrected, statement_index: 1 }]);

  const updated = page.getByTestId("db-admin-chat-turn").first();
  await expect(updated.getByTestId("db-admin-chat-sql")).toHaveText(corrected);
  await expect(updated.getByText("AI のエラー分析で修正した SQL です。")).toBeVisible();
  // 直した SQL も確認語からやり直す。
  await expect(updated.getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  await expect(updated.getByTestId("db-admin-chat-execute")).toBeDisabled();
  await updated.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await updated.getByTestId("db-admin-chat-execute").click();
  expect(mock.executeBodies[1]).toMatchObject({ confirmation: "ADMIN_EXECUTE" });
  await expect(updated.getByText("影響件数 3 件", { exact: true })).toBeVisible();
  await expect(updated.getByTestId("db-admin-chat-execute")).toHaveText("もう一度実行");
  await expectNoHorizontalOverflow(page);
});

test("実行できない SQL は理由を出し、実行の操作を出さない", { tag: "@responsive" }, async ({ page }) => {
  const sql = "ALTER SYSTEM SET OPEN_CURSORS = 300";
  await setup(page, {
    responses: [
      generated(plan([[sql, "ALTER SYSTEM（インスタンスの設定・セッションの強制終了など）は実行できません。"]])),
    ],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "カーソルの上限を変える");
  const turn = page.getByTestId("db-admin-chat-turn").first();
  await expect(turn.getByText("この文は実行できません")).toBeVisible();
  await expect(turn.getByText("実行できない", { exact: true })).toBeVisible();
  await expect(turn.getByText("ALTER SYSTEM（インスタンスの設定・セッションの強制終了など）は実行できません。")).toBeVisible();
  await expect(turn.getByTestId("db-admin-chat-execute")).toHaveCount(0);
  await expect(turn.getByRole("textbox", { name: "実行確認語" })).toHaveCount(0);
});

test("参照するオブジェクトに無い列の文は実際の列を添えた理由を出し、AI のエラー分析で直せる", { tag: "@responsive" }, async ({ page }) => {
  const sql = "SELECT SID, LOCK_TYPE FROM V$LOCK";
  const reason = "列が存在しません: LOCK_TYPE（V$LOCK の列: ADDR, SID, TYPE, LMODE, REQUEST）";
  const corrected = "SELECT SID, TYPE FROM V$LOCK";
  const mock = await setup(page, {
    responses: [generated(plan([[sql, reason, ["LOCK_TYPE"]]]))],
    analyze: {
      cause: "V$LOCK に LOCK_TYPE はありません。", fix: "列の定義を確認します。", conclusion: "修正後に再実行します。",
      corrected_plan: plan(corrected),
      source: "oci_enterprise_ai",
      model: "test-text-model",
      warnings: [],
    },
  });
  await page.goto("/db-admin-chat");
  await ask(page, "ロックの状況を見たい");
  const turn = page.getByTestId("db-admin-chat-turn").first();
  await expect(turn.getByText("この文は実行できません")).toBeVisible();
  await expect(turn.getByText(reason)).toBeVisible();
  await expect(turn.getByText("「AI でエラーを分析」で、参照するオブジェクトの実際の列を使った SQL に直せます。")).toBeVisible();
  await expect(turn.getByTestId("db-admin-chat-execute")).toHaveCount(0);
  // 実行の前に止めた文も、止めた理由を渡して AI のエラー分析で直せる。
  await turn.getByTestId("db-admin-chat-analyze").click();
  await expect(turn.getByTestId("db-admin-chat-corrected-sql")).toHaveText(corrected);
  expect(mock.analyzeBodies[0]).toEqual({ statement_index: 1 });
  await turn.getByTestId("db-admin-chat-use-corrected").click();
  await expect.poll(() => mock.planBodies).toEqual([{ sql: corrected, statement_index: 1 }]);
  const updated = page.getByTestId("db-admin-chat-turn").first();
  await expect(updated.getByTestId("db-admin-chat-sql")).toHaveText(corrected);
  await expect(updated.getByTestId("db-admin-chat-execute")).toBeEnabled();
  await expectNoHorizontalOverflow(page);
});

test("生成に失敗した質問は残して、同じターンの ID で「再送信」する", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page);
  await page.goto("/db-admin-chat");
  await ask(page, "データベースの版");
  const pending = page.getByTestId("db-admin-chat-pending-turn");
  await expect(pending.getByText("データベースの版")).toBeVisible();
  await expect(page.getByTestId("db-admin-chat-send-error")).toBeVisible();
  await page.getByRole("button", { name: "再送信" }).click();
  await expect.poll(() => mock.generateBodies.length).toBe(2);
  // 応答が届かなかっただけならサーバーは保存したターンを返す（二重に生成しない）。
  expect(mock.generateBodies[1]!.client_turn_id).toBe(mock.generateBodies[0]!.client_turn_id);
});

test("DB 管理チャットの権限の無い利用者には出ず、画面も開けない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: {
        data: {
          ...systemAdminMe,
          is_system_admin: false,
          role_codes: ["ADMIN_SQL_OPERATOR"],
          permissions: ["menu.admin_sql"],
        },
      },
    }),
  );
  await page.goto("/admin-sql");
  const sidebar = await openSidebarNav(page);
  const expand = sidebar.getByRole("button", { name: "データ準備 を展開" });
  if (await expand.isVisible()) await expand.click();
  await expect(sidebar.getByRole("link", { name: "管理 SQL を実行" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "DB 管理チャット" })).toHaveCount(0);
  await page.goto("/db-admin-chat");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

// ---- 複数の文（#2333）: 文ごとの実行・すべて実行・止める文の分離・開き直し ----

function statements(page: Page) {
  return page.getByTestId("db-admin-chat-turn").first().getByTestId("db-admin-chat-statement");
}

test("複数の SELECT は文ごとに確認語なしで実行でき、結果はその文の直下に出る。「すべて実行」も確認語なし", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page, {
    responses: [
      generated(
        plan(["SELECT BANNER_FULL FROM V$VERSION", "SELECT TABLESPACE_NAME, USED_PERCENT FROM DBA_TABLESPACE_USAGE_METRICS"]),
        "**版**と**表領域の使用率**を、文ごとに読みます。",
      ),
    ],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "版と表領域");
  const blocks = statements(page);
  await expect(blocks).toHaveCount(2);
  // 説明は共通の Markdown の表示で描き、記号を生で出さない。
  const answer = page.getByTestId("db-admin-chat-turn").first();
  await expect(answer.locator("strong", { hasText: "表領域の使用率" })).toBeVisible();
  await expect(answer).not.toContainText("**");
  await expect(blocks.nth(0).getByText("文 1", { exact: true })).toBeVisible();
  await expect(blocks.nth(1).getByText("文 2", { exact: true })).toBeVisible();
  await expect(blocks.nth(1).getByTestId("db-admin-chat-sql")).toHaveText(
    "SELECT TABLESPACE_NAME, USED_PERCENT FROM DBA_TABLESPACE_USAGE_METRICS",
  );
  // 複数の SELECT も止めない。確認語の欄は 1 つも出さない。
  await expect(page.getByText("実行できない", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "実行確認語" })).toHaveCount(0);

  await blocks.nth(1).getByTestId("db-admin-chat-execute").click();
  await expect(blocks.nth(1).getByTestId("db-admin-chat-result")).toBeVisible();
  // ほかの文には結果を出さない。
  await expect(blocks.nth(0).getByTestId("db-admin-chat-result")).toHaveCount(0);
  expect(mock.executeBodies[0]).toMatchObject({ mode: "statement", statement_index: 2, confirmation: "" });

  const progress = page.getByTestId("db-admin-chat-progress-summary");
  const progressBefore = (await progress.textContent()) ?? "";
  await page.getByTestId("db-admin-chat-run-all").click();
  await expect(blocks.nth(0).getByTestId("db-admin-chat-result")).toBeVisible();
  // 「すべて実行」も処理の経過を変えない。
  await expect(progress).toHaveText(progressBefore);
  await expect(blocks.nth(1).getByTestId("db-admin-chat-result")).toBeVisible();
  expect(mock.executeBodies[1]).toMatchObject({ mode: "all", confirmation: "" });
  expect(mock.executeBodies[1]).not.toHaveProperty("statement_index");
  await expect(page.getByTestId("db-admin-chat-run-all-summary")).toContainText("2 文すべてが成功しました");
  await expectNoHorizontalOverflow(page);
});

test("変更する文を含む「すべて実行」は確認語を 1 回入れて実行し、最初のエラーで止めて失敗の文と実行しなかった文を示す", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const deleteSql = "DELETE FROM APP.TEMP_ITEMS WHERE QTY = 0";
  const mock = await setup(page, {
    responses: [generated(plan(["SELECT COUNT(*) FROM APP.TEMP_ITEMS", deleteSql, "SELECT COUNT(*) FROM APP.TEMP_ITEMS"]))],
    executeAll: () => [selectResultFor(1, "N", "3"), { ...mutationResult(deleteSql, false, 2), rolled_back: true }, null],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "件数を見てから消す");
  const region = page.getByTestId("db-admin-chat-run-all-region");
  // トランザクションの扱い（上から順に・最初のエラーで止める・全部成功したときだけ commit）を示す。
  await expect(region.getByText("1 つのトランザクションで上から順に実行します", { exact: false })).toBeVisible();
  const runAll = region.getByTestId("db-admin-chat-run-all");
  await expect(runAll).toBeDisabled();
  await region.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await expect(runAll).toBeEnabled();
  await runAll.click();
  // 確認語は「すべて実行」に 1 回だけ。文ごとの確認語の欄は使わない。
  await expect.poll(() => mock.executeBodies.length).toBe(1);
  expect(mock.executeBodies[0]).toMatchObject({ mode: "all", confirmation: "ADMIN_EXECUTE" });

  const summary = page.getByTestId("db-admin-chat-run-all-summary");
  await expect(summary).toContainText("文 2 で失敗して止めました（成功 1 文・実行しなかった文 1 文）");
  await expect(summary.getByText("ロールバック済み", { exact: true })).toBeVisible();
  const blocks = statements(page);
  await expect(blocks.nth(0).getByTestId("db-admin-chat-result")).toBeVisible();
  await expect(blocks.nth(1).getByText("ORA-00942: table or view does not exist").first()).toBeVisible();
  await expect(blocks.nth(1).getByTestId("db-admin-chat-analyze")).toBeVisible();
  await expect(blocks.nth(2).getByTestId("db-admin-chat-not-run")).toContainText("実行しなかった");
  // 実行の確認は 1 回ごと。
  await expect(region.getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  await expectNoHorizontalOverflow(page);
  // 要約のバッジは会話の欄の幅に収める（375px でも切れない）。
  const logBox = (await page.getByTestId("db-admin-chat-conversation").boundingBox())!;
  for (const badge of await summary.locator("span").all()) {
    const box = await badge.boundingBox();
    if (box) expect(box.x + box.width).toBeLessThanOrEqual(logBox.x + logBox.width + 1);
  }
  await summary.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("db-admin-chat-run-all.png") });
});

test("止める文はその文だけを実行できなくし、ほかの文は実行できる。「すべて実行」は押せず理由を出す", { tag: "@responsive" }, async ({ page }) => {
  const mock = await setup(page, {
    responses: [
      generated(
        plan([
          "SELECT NAME, VALUE FROM V$PARAMETER WHERE NAME = 'open_cursors'",
          ["ALTER SYSTEM SET OPEN_CURSORS = 300", "ALTER SYSTEM（インスタンスの設定・セッションの強制終了など）は実行できません。"],
        ]),
      ),
    ],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "カーソルの上限を確かめて変える");
  await expect(page.getByTestId("db-admin-chat-run-all")).toBeDisabled();
  await expect(page.getByTestId("db-admin-chat-run-all-blocked")).toContainText("実行できない文（#2）を含むため");
  const blocks = statements(page);
  await expect(blocks.nth(1).getByText("この文は実行できません")).toBeVisible();
  await expect(blocks.nth(1).getByTestId("db-admin-chat-execute")).toHaveCount(0);
  await expect(blocks.nth(1).getByRole("textbox", { name: "実行確認語" })).toHaveCount(0);
  await blocks.nth(0).getByTestId("db-admin-chat-execute").click();
  await expect(blocks.nth(0).getByTestId("db-admin-chat-result")).toBeVisible();
  expect(mock.executeBodies).toEqual([expect.objectContaining({ mode: "statement", statement_index: 1 })]);
});

test("複数の文の会話を開き直すと、文ごとの前回の実行の要約・「すべて実行」の要約と「実行」が戻る", { tag: "@responsive" }, async ({ page }) => {
  const deleteSql = "DELETE FROM APP.TEMP_ITEMS WHERE QTY = 0";
  const turn = storedTurn(
    "turn-m1",
    "件数を見てから消す",
    generated(plan(["SELECT COUNT(*) FROM APP.TEMP_ITEMS", deleteSql, "SELECT 1 FROM DUAL"])),
  );
  turn.statement_executions = [
    savedExecution(1, "succeeded", selectResultFor(1, "N", "3"), "all"),
    savedExecution(2, "failed", mutationResult(deleteSql, false, 2), "all"),
    savedExecution(3, "not_run", null, "all"),
  ];
  turn.last_run_all = {
    executed_at: "2026-10-17T09:31:00Z",
    batch_id: "batch-all",
    confirmation_given: true,
    succeeded: false,
    statement_count: 3,
    succeeded_count: 1,
    stopped_at: 2,
    committed: false,
    rolled_back: true,
    warnings: [],
  };
  const mock = await setup(page, { conversations: [savedConversation("conversation-m", "会話 M", [turn])] });
  await page.goto("/db-admin-chat");
  await openFromHistory(page, "会話 M");
  const blocks = statements(page);
  await expect(blocks).toHaveCount(3);
  await expect(blocks.nth(0).getByTestId("db-admin-chat-last-execution")).toContainText("1 行・1 列");
  await expect(blocks.nth(0).getByTestId("db-admin-chat-execute")).toHaveText("もう一度実行");
  await expect(blocks.nth(1).getByText("ORA-00942: table or view does not exist").first()).toBeVisible();
  await expect(blocks.nth(2).getByTestId("db-admin-chat-not-run")).toBeVisible();
  await expect(blocks.nth(2).getByTestId("db-admin-chat-execute")).toHaveText("実行");
  const summary = page.getByTestId("db-admin-chat-run-all-summary");
  await expect(summary).toContainText("前回のすべて実行");
  await expect(summary).toContainText("文 2 で失敗して止めました");
  // 開き直した文も、保存した SQL のその文を実行する。
  if ((page.viewportSize()?.width ?? 0) >= 1024) await historyToggle(page).click();
  await blocks.nth(2).getByTestId("db-admin-chat-execute").click();
  await expect(blocks.nth(2).getByTestId("db-admin-chat-result")).toBeVisible();
  expect(mock.executeBodies.at(-1)).toMatchObject({ turn_id: "turn-m1", mode: "statement", statement_index: 3 });
  await expectNoHorizontalOverflow(page);
});

test("回答の注意書きを SQL の後に出す", { tag: "@responsive" }, async ({ page }) => {
  await setup(page, {
    responses: [
      {
        ...generated(plan("SELECT BANNER_FULL FROM V$VERSION")),
        notice: "この回答は AI が生成したものです。\n実行の前に SQL を確かめてください。",
      },
    ],
  });
  await page.goto("/db-admin-chat");
  await ask(page, "データベースの版");
  const turn = page.getByTestId("db-admin-chat-turn").first();
  const notice = turn.getByRole("complementary", { name: "回答の注意書き" });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("実行の前に SQL を確かめてください。");
  const [sqlBox, noticeBox] = await Promise.all([
    turn.getByTestId("db-admin-chat-sql").boundingBox(),
    notice.boundingBox(),
  ]);
  expect(noticeBox!.y).toBeGreaterThan(sqlBox!.y);
  await expectNoHorizontalOverflow(page);
});


test("修正後の再分析は履歴を表示し、同じ SQL の警告では適用を出さない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const analysis: Json = {
    cause: "修正案です。", fix: "列の定義を確認します。", conclusion: "修正後に再実行します。", corrected_plan: plan("DELETE FROM T2"),
    source: "oci_enterprise_ai", model: "test-text-model", warnings: [], previous_attempts: [],
  };
  const mock = await setup(page, {
    responses: [generated(plan("DELETE FROM T1"))],
    execute: (_request, turn) => mutationResult(turn.plan.sql, false),
    analyze: analysis,
  });
  await page.goto("/db-admin-chat");
  await ask(page, "合成の行を削除する");
  let turn = page.getByTestId("db-admin-chat-turn").first();
  await turn.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await turn.getByTestId("db-admin-chat-execute").click();
  await turn.getByTestId("db-admin-chat-analyze").click();
  await expect(turn.getByTestId("db-admin-chat-use-corrected")).toBeVisible();
  await turn.getByTestId("db-admin-chat-use-corrected").click();
  turn = page.getByTestId("db-admin-chat-turn").first();
  await expect(turn.getByTestId("db-admin-chat-sql")).toHaveText("DELETE FROM T2");
  analysis.corrected_plan = null;
  analysis.warnings = ["前に試して失敗した SQL と同じ修正しか得られませんでした。"];
  analysis.previous_attempts = [{ statement_index: 1, revision: 0, sql: "DELETE FROM T1", error_code: "ORA-00942", error_summary: "ORA-00942: synthetic failure", executed_at: "2026-10-22" }];
  await turn.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await turn.getByTestId("db-admin-chat-execute").click();
  await turn.getByTestId("db-admin-chat-analyze").click();
  await expect(turn.getByTestId("db-admin-chat-analysis")).toContainText("同じ修正しか得られませんでした");
  await expect(turn.getByTestId("db-admin-chat-use-corrected")).toHaveCount(0);
  await turn.getByText("これまでの修正（1 回）", { exact: true }).click();
  await expect(turn.getByText("版 0", { exact: true })).toBeVisible();
  await expect(turn.getByText("ORA-00942: synthetic failure", { exact: true })).toBeVisible();
  await expect(turn.getByTestId("db-admin-chat-analysis")).toContainText("DELETE FROM T1");
  const previousSql = turn.getByTestId("db-admin-chat-analysis").getByText("DELETE FROM T1", { exact: true });
  await previousSql.scrollIntoViewIfNeeded();
  await expect(previousSql).toBeVisible();
  expect(mock.analyzeBodies).toEqual([{ statement_index: 1 }, { statement_index: 1 }]);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("correction-history.png"), fullPage: true });
});


for (const code of ["ORA-01489", "ORA-01722"]) {
  test(`失敗の案内はチャットの操作を示し、分析の三項目を分ける ${code}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await setup(page, {
      responses: [generated(plan("DELETE FROM T1"))],
      execute: (_request, turn) => {
        const result = mutationResult(turn.plan.sql, false);
        const statements = result.statements as Json[];
        statements[0]!.error_message = `${code}: synthetic failure`;
        statements[0]!.error_code = code;
        return result;
      },
      analyze: { cause: "失敗の原因です。", fix: "列の定義を確認します。", conclusion: "修正後に再実行します。", corrected_plan: null, source: "oci_enterprise_ai", model: "test-model", warnings: [], previous_attempts: [] },
    });
    await page.goto("/db-admin-chat");
    await ask(page, "合成の行を削除する");
    const turn = page.getByTestId("db-admin-chat-turn").first();
    await turn.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
    await turn.getByTestId("db-admin-chat-execute").click();
    const failure = turn.getByTestId("db-admin-statement-failure");
    await expect(failure).toContainText("「AI でエラーを分析」");
    for (const obsolete of ["最新情報を取得", "SQL プレビュー", "取込データ"]) await expect(failure).not.toContainText(obsolete);
    await turn.getByTestId("db-admin-chat-analyze").click();
    for (const [name, body] of [["原因", "失敗の原因です。"], ["直し方", "列の定義を確認します。"], ["結論", "修正後に再実行します。"]]) {
      const section = turn.getByRole("region", { name, exact: true });
      await section.scrollIntoViewIfNeeded();
      await expect(section.getByRole("heading", { name, exact: true })).toBeVisible();
      await expect(section).toContainText(body!);
    }
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`analysis-${code}.png`), fullPage: true });
  });
}


for (const targetIndex of [1, 2, 3]) {
  test(`複数 SQL の文 ${targetIndex} だけを修正し文数と他の SQL を保つ`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const original = ["SELECT 1 FROM DUAL", "SELECT 2 FROM DUAL", "SELECT 3 FROM DUAL"];
    const corrected = "SELECT 9 FROM DUAL";
    const mock = await setup(page, {
      responses: [generated(plan(original))],
      execute: (_request, _turn, statement) => mutationResult(statement.sql, false, statement.index),
      analyze: {
        cause: "合成のエラーです。", fix: "対象の式を直します。", conclusion: "修正後に確認します。",
        corrected_plan: plan(corrected), source: "oci_enterprise_ai", model: "test-model", warnings: [], previous_attempts: [],
      },
    });
    await page.goto("/db-admin-chat");
    await ask(page, "3 つの SQL を作成してください。");
    let blocks = statements(page);
    await expect(blocks).toHaveCount(3);
    const target = blocks.nth(targetIndex - 1);
    await target.getByTestId("db-admin-chat-execute").click();
    await target.getByTestId("db-admin-chat-analyze").click();
    await expect(target.getByTestId("db-admin-chat-corrected-sql")).toHaveText(corrected);
    expect(mock.analyzeBodies).toEqual([{ statement_index: targetIndex }]);
    await target.getByTestId("db-admin-chat-use-corrected").click();
    await expect.poll(() => mock.planBodies).toEqual([{ sql: corrected, statement_index: targetIndex }]);
    blocks = statements(page);
    await expect(blocks).toHaveCount(3);
    for (let index = 0; index < original.length; index++) {
      await expect(blocks.nth(index).getByTestId("db-admin-chat-sql")).toHaveText(index === targetIndex - 1 ? corrected : original[index]!);
    }
    expect(mock.generateBodies).toHaveLength(1);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`single-correction-${targetIndex}.png`), fullPage: true });
  });
}

for (const count of [14, 26]) {
  test(`DB 管理の結果 ${count} 行のシートを開くとページ送りと範囲が見える`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await setup(page, {
      responses: [generated(plan("SELECT 1 FROM DUAL"))],
      execute: () => {
        const result = selectResultFor(1, "VALUE", "1");
        const values = Array.from({ length: count }, (_, index) => ({ VALUE: String(index + 1) }));
        return { ...result, select_result: { ...result.select_result, rows: values, total: count, returned_count: count } };
      },
    });
    await page.goto("/db-admin-chat");
    await ask(page, "合成の結果を確認する");
    const turn = page.getByTestId("db-admin-chat-turn").first();
    await turn.getByTestId("db-admin-chat-execute").click();
    await turn.getByRole("button", { name: "すべての行を見る" }).click();
    const sheet = page.getByRole("dialog", { name: `実行結果（${count} 行）` });
    const pagination = sheet.locator("nav[data-pagination]");
    await expect(pagination).toContainText(`1–${Math.min(count, 25)} / ${count} 件`);
    await expect(pagination.getByRole("button", { name: "次へ" })).toBeInViewport();
    await expect(sheet.locator("tbody tr")).toHaveCount(Math.min(count, 25));
    if (count > 25) {
      await pagination.getByRole("button", { name: "次へ" }).click();
      await expect(pagination).toContainText("26–26 / 26 件");
      await expect(sheet.locator("tbody tr")).toHaveCount(1);
    } else {
      await expect(pagination.getByRole("button", { name: "次へ" })).toBeDisabled();
      await expect(pagination.getByRole("textbox", { name: "ページ番号" })).toHaveValue("1");
    }
    await sheet.screenshot({ path: testInfo.outputPath(`result-pages-${count}.png`) });
  });
}
