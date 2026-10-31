// チャットで生成した SQL の実行（#1154 / #1176）。SQL 生成の画面と同じく送信のジョブが実行まで行い、画面は
// 結果の行を 1 回だけ受け取って、同じ吹き出しの SQL の下に、要約・先頭の行のプレビュー（表の中で縦横
// スクロール）・「すべての行を見る」・「CSV をダウンロード」で出す。「もう一度実行」は明示の操作。
// 各 test は playwright.config の desktop（1280px）と mobile-375 の両方で動く。
import { readFile } from "node:fs/promises";

import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { confirmChatRequirement, mockChatRequirement } from "./_helpers/chat-requirement";
import { expectSingleSpinner } from "./_helpers/single-spinner";
import { withJobProgress } from "./_helpers/job-progress";

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
const now = "2026-10-05T05:00:00Z";
const SQL = "SELECT * FROM APP.SALES";
const COLUMNS = [
  "SALE_ID",
  "CATEGORY",
  "AMOUNT",
  "NOTE",
  "CUSTOMER_NAME",
  "REGION",
  "CREATED_AT",
  "DESCRIPTION",
];

function rows(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    SALE_ID: index + 1,
    CATEGORY: `カテゴリ${(index % 4) + 1}`,
    AMOUNT: (index + 1) * 1000,
    NOTE: index === 0 ? null : `備考${index}`,
    CUSTOMER_NAME: `顧客 ${index + 1}`,
    REGION: ["東日本", "西日本"][index % 2],
    CREATED_AT: "2026-10-01T09:00:00",
    DESCRIPTION: `とても長い説明の文字列です。${"説明".repeat(40)}`,
  }));
}

function execution(rowCount: number, overrides: Record<string, unknown> = {}) {
  return {
    job_id: "chat-1",
    status: "done",
    executed_at: now,
    elapsed_ms: 800,
    executable_sql: SQL,
    results: {
      columns: COLUMNS,
      rows: rows(rowCount),
      total: rowCount,
      returned_count: rowCount,
      has_more: false,
      truncated: false,
      execution_context: "oracle_data_plane",
      vpd_context_enforced: false,
    },
    row_limit: 1000,
    max_cell_chars: 2000,
    cells_truncated: false,
    history_id: "history-1",
    ...overrides,
  };
}

interface Setup {
  turns: Record<string, unknown>[];
  /** 送信の要求（#1176）。 */
  jobRequests: Record<string, unknown>[];
  executeRequests: string[];
  executeResponse: () => { status?: number; json: unknown };
  gate: Promise<void> | null;
  /** 送信のジョブの中で実行した結果（#1176）。backend は 1 回だけ受け取らせる。 */
  jobExecution: () => ReturnType<typeof execution>;
  receiveRequests: string[];
  receiveGate: Promise<void> | null;
  /** 受け取り済みのジョブ（2 回目からは 404）。 */
  received: Set<string>;
}

const FUTURE = "2099-01-01T00:00:00Z";
const DONE_STEPS = ["prepare_context", "generate_sql", "safety_check", "execute_sql", "format_results"].map(
  (stage) => ({ stage, status: "done" }),
);

function summaryOf(data: ReturnType<typeof execution>, resultExpiresAt: string | null) {
  return {
    status: data.status,
    executed_at: data.executed_at,
    elapsed_ms: data.elapsed_ms,
    row_count: data.results.total,
    column_count: data.results.columns.length,
    has_more: Boolean(data.results.has_more),
    error_code: (data as { error_code?: string }).error_code ?? null,
    history_id: data.history_id,
    result_expires_at: resultExpiresAt,
  };
}

async function setup(
  page: Page,
  options: { safe?: boolean; me?: Record<string, unknown>; canExecute?: boolean } = {},
) {
  await mockDatabaseGateReady(page);
  // 送信の前の要件の検査（#1442）は「確認が要らない」で、聞かずに SQL を生成する。
  await mockChatRequirement(page);
  if (options.me)
    await page.route("**/api/auth/me", (route) =>
      route.fulfill({ json: { data: { ...systemAdminMe, ...options.me } } }),
    );
  const state: Setup = {
    turns: [],
    jobRequests: [],
    executeRequests: [],
    executeResponse: () => ({ json: { data: execution(60) } }),
    gate: null,
    jobExecution: () => execution(60),
    receiveRequests: [],
    receiveGate: null,
    received: new Set(),
  };
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) => {
    const conversation = {
      id: "chat-1",
      title: state.turns[0]?.question,
      profile_id: "sales",
      created_at: now,
    };
    return route.fulfill({
      json: {
        data:
          new URL(route.request().url()).pathname === "/api/nl2sql/chats"
            ? {
                items: state.turns.length ? [conversation] : [],
                next_cursor: null,
                total: state.turns.length ? 1 : 0,
                limit: 25,
              }
            : { conversation, turns: state.turns.map(withJobProgress) },
      },
    });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    const body = route.request().postDataJSON();
    state.jobRequests.push(body);
    const safe = options.safe ?? true;
    const id = `chat-${state.turns.length + 1}`;
    // backend と同じく、実行の権限があれば送信のジョブが実行まで行い、無ければ生成だけ（#1176）。
    const executes = safe && (options.canExecute ?? true);
    const data = { ...state.jobExecution(), job_id: id };
    state.turns.push({
      job_id: id,
      question: body.question,
      engine: body.engine,
      conversation_id: "chat-1",
      chat: true,
      generation_only: !executes,
      last_execution: executes ? summaryOf(data, FUTURE) : null,
      status: safe ? "done" : "error",
      error_code: safe ? undefined : "SQL_BLOCKED",
      error_message: safe ? undefined : "SELECT 以外の SQL は実行できません。",
      created_at: now,
      steps: executes ? DONE_STEPS : [],
      result: {
        generated_sql: safe ? SQL : "DELETE FROM APP.SALES",
        original_question: body.question,
        explanation: "売上の明細です。",
        safety: { is_safe: safe },
      },
    });
    return route.fulfill({
      json: {
        data: {
          job_id: id,
          status: safe ? "done" : "error",
          created_at: now,
          steps: [],
        },
      },
    });
  });
  await page.route("**/api/nl2sql/jobs/*/execute", async (route) => {
    state.executeRequests.push(new URL(route.request().url()).pathname);
    if (state.gate) await state.gate;
    const response = state.executeResponse();
    return route.fulfill({ status: response.status ?? 200, json: response.json });
  });
  await page.route("**/api/nl2sql/jobs/*/execution-result", async (route) => {
    const path = new URL(route.request().url()).pathname;
    state.receiveRequests.push(path);
    if (state.receiveGate) await state.receiveGate;
    const id = path.split("/").at(-2)!;
    // 行は 1 回だけ受け取れる（backend は受け取ったら消す）。
    if (state.received.has(id))
      return route.fulfill({
        status: 404,
        json: { detail: "実行の結果はもう受け取れません。もう一度実行してください。" },
      });
    state.received.add(id);
    return route.fulfill({ json: { data: { ...state.jobExecution(), job_id: id } } });
  });
  return state;
}

async function sendQuestion(page: Page, question = "売上の明細", badge = "安全検査済み・実行済み") {
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill(question);
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText(badge)).toBeVisible();
  return page.getByTestId("sql-chat-turn").last();
}

async function expectNoPageOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
}

test("送信のジョブが実行まで行い、結果の行を 1 回だけ受け取って吹き出しの中の表で縦横にスクロールする", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const state = await setup(page);
  let releaseReceive!: () => void;
  state.receiveGate = new Promise<void>((resolve) => {
    releaseReceive = resolve;
  });
  // 空の状態の案内（#1176: SQL 生成の画面と同じく、送信で実行する）。
  await page.goto("/chat");
  await expect(
    page.getByText("生成した SQL は安全性を確認してから実行し、結果を表で出します。", { exact: false }),
  ).toBeVisible();
  const turn = await sendQuestion(page);
  expect(state.jobRequests[0]).toMatchObject({ chat: true, profile_id: "sales" });
  expect(state.jobRequests[0]).not.toHaveProperty("generation_only");
  // 受け取っている間は、結果の位置に文言・経過時間と表の形の Skeleton（ボタンを押さずに受け取る）。
  const receiving = turn.getByTestId("sql-chat-execution-receiving");
  await expect(receiving).toContainText("実行結果を読み込んでいます");
  await expect(receiving.locator(".animate-pulse").first()).toBeVisible();
  releaseReceive();

  const region = turn.getByTestId("sql-chat-execution");
  await expect(region.getByTestId("sql-chat-result-summary")).toHaveText("60 行・8 列・0.8 秒");
  await expect(receiving).toHaveCount(0);
  expect(state.receiveRequests).toEqual(["/api/nl2sql/jobs/chat-1/execution-result"]);
  expect(state.executeRequests).toEqual([]);
  await expect(turn.getByText("安全検査済み・実行済み")).toBeVisible();
  const run = turn.getByTestId("sql-chat-execute");
  await expect(run).toHaveAccessibleName("もう一度実行");
  // 処理の経過は実行・結果の整形まで（6 ステップ）。
  await expect(turn.getByTestId("sql-chat-progress-summary")).toHaveText(/^処理の経過（6 ステップ・/);

  // プレビューは先頭の 50 行。表の中で縦にスクロールし、吹き出し・ページを伸ばさない。
  const table = region.getByTestId("sql-chat-result-table");
  await expect(table.locator("tbody tr")).toHaveCount(50);
  await expect(region.getByTestId("sql-chat-result-preview-note")).toContainText("先頭の 50 行");
  const scroll = region.getByTestId("sql-chat-result-scroll");
  const box = await scroll.evaluate((element) => ({
    vertical: element.scrollHeight - element.clientHeight,
    horizontal: element.scrollWidth - element.clientWidth,
    height: element.getBoundingClientRect().height,
  }));
  expect(box.vertical).toBeGreaterThan(0);
  expect(box.horizontal).toBeGreaterThan(0);
  // 表頭は固定（表の中を縦にスクロールしても列名がスクロール領域の上端に残る）。
  await scroll.scrollIntoViewIfNeeded();
  await scroll.evaluate((element) => element.scrollTo({ top: 400 }));
  await expect
    .poll(() =>
      scroll.evaluate((element) => {
        const header = element.querySelector("thead th")!.getBoundingClientRect().top;
        return Math.abs(header - element.getBoundingClientRect().top);
      }),
    )
    .toBeLessThanOrEqual(2);
  // NULL は空と区別して「NULL」と出し、数値の列は右寄せ。
  await scroll.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect(table.locator("tbody tr").first().getByText("NULL", { exact: true })).toBeVisible();
  await expect(table.locator("tbody tr").first().locator("td").nth(2)).toHaveClass(/text-right/);
  // 長い値は 1 行で省略し、title で全文を出す。
  const longCell = table.locator("tbody tr").first().locator("td").nth(7).locator("span");
  await expect(longCell).toHaveAttribute("title", /とても長い説明の文字列です。/);
  await expectNoPageOverflow(page);

  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme });
    await page.evaluate((dark) => document.documentElement.classList.toggle("dark", dark), colorScheme === "dark");
    await turn.screenshot({ path: testInfo.outputPath(`chat-execution-${colorScheme}.png`) });
  }

  // 「もう一度実行」は明示の操作。実行中はラベルを変えず、アイコンがスピナーになる（スピナーは 1 つ）。
  // 処理の経過（送信のジョブの段）は変えない（段の数・合計時間。実行は SQL の下の実行結果に出す）。
  const progress = turn.getByTestId("sql-chat-progress-summary");
  const progressBefore = (await progress.textContent()) ?? "";
  let release!: () => void;
  state.gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await run.click();
  await expect(turn.getByTestId("sql-chat-execution-running")).toBeVisible();
  await expect(run).toHaveAccessibleName("もう一度実行");
  await expect(run).toHaveAttribute("aria-disabled", "true");
  await expectSingleSpinner(page, run);
  await expect(progress).toHaveText(progressBefore);
  release();
  await expect(turn.getByTestId("sql-chat-execution-running")).toHaveCount(0);
  expect(state.executeRequests).toEqual(["/api/nl2sql/jobs/chat-1/execute"]);
  expect(state.receiveRequests).toHaveLength(1);
  await expect(progress).toHaveText(progressBefore);
});

test("「すべての行を見る」は広いシートでページを送り、CSV は取得した行を書き出す", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await setup(page);
  const turn = await sendQuestion(page);
  const region = turn.getByTestId("sql-chat-execution");
  await expect(region.getByTestId("sql-chat-result-summary")).toHaveText("60 行・8 列・0.8 秒");

  const viewAll = region.getByRole("button", { name: "すべての行を見る" });
  await viewAll.click();
  const sheet = page.getByRole("dialog", { name: "実行結果（60 行）" });
  await expect(sheet).toBeVisible();
  const viewport = page.viewportSize()!;
  const sheetBox = (await sheet.boundingBox())!;
  if (viewport.width < 640) expect(sheetBox.width).toBeGreaterThanOrEqual(viewport.width - 1);
  else expect(sheetBox.width).toBeGreaterThan(viewport.width * 0.6);
  const pagination = sheet.getByTestId("sql-chat-result-all-pagination");
  await expect(pagination).toContainText("1–25 / 60 件");
  expect((await pagination.boundingBox())!.y).toBeGreaterThan((await sheet.getByTestId("sql-chat-result-all-scroll").boundingBox())!.y);
  await expect(pagination.getByRole("button", { name: "次へ" })).toBeInViewport();
  await expect(sheet.getByTestId("sql-chat-result-all-table").locator("tbody tr")).toHaveCount(25);
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("26–50 / 60 件");
  // 1 ページの行数を選べる（ページングで描く行を絞る。仮想化はしない）。
  // 表示件数は表の下の共通ページ送りに置く。狭い画面でも操作できる。
  await expect(pagination.getByRole("combobox", { name: "表示件数" })).toBeInViewport();
  if (viewport.width >= 640) {
    expect((await pagination.getByRole("combobox", { name: "表示件数" }).boundingBox())!.x)
      .toBeLessThan((await pagination.getByRole("button", { name: "次へ" }).boundingBox())!.x);
  }
  await sheet.getByRole("combobox", { name: "表示件数" }).click();
  await page.getByRole("option", { name: "50 件" }).click();
  await expect(pagination).toContainText("1–50 / 60 件");
  // 全行の表はシートの本文でスクロールする（プレビューの高さを引き継がない）。
  const allScroll = sheet.getByTestId("sql-chat-result-all-scroll");
  expect(await allScroll.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(0);
  const header = allScroll.locator("thead");
  const headerTop = (await header.boundingBox())!.y;
  await allScroll.evaluate((element) => { element.scrollTop = 320; });
  await expect.poll(async () => (await header.boundingBox())!.y).toBeCloseTo(headerTop, 0);
  await allScroll.evaluate((element) => { element.scrollTop = 0; });
  await sheet.screenshot({ path: testInfo.outputPath("chat-execution-sheet.png") });

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    sheet.getByRole("button", { name: "CSV をダウンロード" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^nl2sql-chat-result-\d{8}-\d{6}\.csv$/);
  const csv = await readFile((await download.path())!, "utf8");
  const lines = csv.replace(/^\uFEFF/, "").trimEnd().split("\r\n");
  expect(lines[0]).toBe(COLUMNS.join(","));
  expect(lines).toHaveLength(61);
  expect(lines[1]).toContain("1,カテゴリ1,1000,,顧客 1");

  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(viewAll).toBeFocused();
});

test("上限で打ち切った結果は明示し、すべての行は「SELECT SQL を実行」で SQL を入れて開く", async ({
  page,
}) => {
  const state = await setup(page);
  state.jobExecution = () =>
    execution(1000, {
      elapsed_ms: 1200,
      results: {
        ...execution(1000).results,
        has_more: true,
        truncated: true,
      },
    });
  const turn = await sendQuestion(page);
  const region = turn.getByTestId("sql-chat-execution");
  await expect(region.getByTestId("sql-chat-result-summary")).toHaveText(
    "先頭の 1,000 行を取得しました（さらに行があります）・8 列・1.2 秒",
  );
  const truncated = region.getByTestId("sql-chat-result-truncated");
  await expect(truncated).toContainText("1 回に取得するのは先頭の 1,000 行までです。");
  await expect(truncated).toContainText("表示と CSV は取得した行だけです");
  await expect(truncated).toContainText("「SELECT SQL を実行」で取得件数上限を指定して実行してください");
  await expect(region.getByTestId("sql-chat-result-table").locator("tbody tr")).toHaveCount(50);
  await truncated.getByRole("link", { name: "SELECT SQL を実行で開く" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  await expect(page.locator("#direct-sql-input")).toHaveValue(SQL);
});

test("0 行は「該当する行はありません」、実行の失敗は吹き出しの中の danger の Banner で詳細を畳む", async ({
  page,
}) => {
  const state = await setup(page);
  state.jobExecution = () => execution(0);
  const turn = await sendQuestion(page);
  const region = turn.getByTestId("sql-chat-execution");
  await expect(region.getByTestId("sql-chat-result-summary")).toHaveText(
    "該当する行はありません・8 列・0.8 秒",
  );
  await expect(region.getByTestId("sql-chat-result-table")).toHaveCount(0);
  await expect(region.getByRole("button", { name: "CSV をダウンロード" })).toHaveCount(0);

  state.executeResponse = () => ({
    json: {
      data: execution(0, {
        status: "error",
        results: { columns: [], rows: [], total: 0 },
        error_message:
          "生成した SQL の実行に失敗しました。生成した SQL と「詳細」の Oracle のエラーを確認し、質問を言い換えて実行し直してください。",
        error_code: "ORA-00942",
        error_detail: "SELECT の実行に失敗しました: ORA-00942: table or view does not exist",
      }),
    },
  });
  await turn.getByTestId("sql-chat-execute").click();
  const failure = region.getByTestId("sql-chat-execution-error");
  await expect(failure.getByRole("alert")).toContainText("生成した SQL の実行に失敗しました。");
  // 技術的な詳細（ORA のコードと元の文）は「詳細」に畳み、失敗なので開いて出す。
  await expect(failure.getByText("ORA-00942", { exact: true })).toBeVisible();
  // 1 文目と本文に ORA の文を混ぜない（実行した後の失敗は結果の位置に、共通の失敗のカードで出す。#2342）。
  await expect(failure.locator("[data-process-failure-cause]")).not.toContainText("ORA-");
  await expect(failure.getByRole("alert")).toContainText("SQL を実行できませんでした。");

  // 実行の要求の失敗（生成の後に安全検査を通らなくなった等の 400）は API の失敗の Banner。
  state.executeResponse = () => ({
    status: 400,
    json: { detail: "安全検査を通っていない SQL は実行できません。" },
  });
  await turn.getByTestId("sql-chat-execute").click();
  await expect(region.getByTestId("sql-chat-execution-request-error")).toContainText(
    "安全検査を通っていない SQL は実行できません。",
  );
});

test("送信のジョブの中の実行が失敗しても、生成した SQL と失敗の理由・「もう一度実行」を出す", async ({
  page,
}) => {
  const state = await setup(page);
  state.jobExecution = () =>
    execution(0, {
      status: "error",
      results: { columns: [], rows: [], total: 0 },
      error_message:
        "生成した SQL の実行に失敗しました。生成した SQL と「詳細」の Oracle のエラーを確認し、質問を言い換えて実行し直してください。",
      error_code: "ORA-00942",
      error_detail: "SELECT の実行に失敗しました: ORA-00942: table or view does not exist",
    });
  const turn = await sendQuestion(page);
  await expect(turn.locator("pre")).toContainText(SQL);
  const failure = turn.getByTestId("sql-chat-execution-error");
  await expect(failure.getByRole("alert")).toContainText("生成した SQL の実行に失敗しました。");
  await expect(failure.getByText("ORA-00942", { exact: true })).toBeVisible();
  await expect(turn.getByTestId("sql-chat-execute")).toHaveAccessibleName("もう一度実行");
});

test("安全検査で遮断した SQL（DML）は実行しない（SQL 生成の画面と同じ）", async ({
  page,
}) => {
  const state = await setup(page, { safe: false });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("売上を消して");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const turn = page.getByTestId("sql-chat-turn").last();
  await expect(turn.getByText("安全検査でブロック")).toBeVisible();
  await expect(turn.getByTestId("sql-chat-execute")).toHaveCount(0);
  await expect(turn.getByTestId("sql-chat-execution")).toHaveCount(0);
  expect(state.receiveRequests).toEqual([]);
  expect(state.executeRequests).toEqual([]);
});

test("実行の権限が無い利用者は生成だけになり、理由を吹き出しの中に出す", async ({ page }) => {
  const state = await setup(page, {
    canExecute: false,
    me: {
      is_system_admin: false,
      role_codes: ["CHAT"],
      permissions: ["menu.chat"],
      allowed_profile_ids: ["sales"],
    },
  });
  const turn = await sendQuestion(page, "売上の明細", "安全検査済み・未実行");
  await expect(turn.getByTestId("sql-chat-execute")).toHaveCount(0);
  expect(state.receiveRequests).toEqual([]);
  await expect(turn.getByText("SQL を実行するには「SELECT SQL 実行」の権限が必要です。", { exact: false })).toBeVisible();
});

test("会話を開き直したときは前回の実行の要約と「もう一度実行」を出す（行は保存しない）", async ({
  page,
}) => {
  const state = await setup(page);
  state.turns.push({
    job_id: "chat-1",
    question: "売上の明細",
    conversation_id: "chat-1",
    generation_only: true,
    status: "done",
    created_at: now,
    steps: [],
    result: {
      generated_sql: SQL,
      original_question: "売上の明細",
      explanation: "売上の明細です。",
      safety: { is_safe: true },
    },
    last_execution: {
      status: "done",
      executed_at: "2026-10-05T05:03:00Z",
      elapsed_ms: 800,
      row_count: 12,
      column_count: 8,
      has_more: false,
      history_id: "history-1",
      // 送信のジョブの中で実行した行は、別の画面（タブ）が受け取り済み（#1176。2 回目は 404）。
      result_expires_at: FUTURE,
    },
  });
  state.received.add("chat-1");
  await page.goto("/chat");
  const history = page.getByRole("button", { name: "会話の履歴", exact: true });
  await history.click();
  await page.getByTestId("sql-chat-history").getByText("売上の明細", { exact: true }).click();
  const turn = page.getByTestId("sql-chat-turn");
  // 日時は既定の表示の時間帯（UTC。#3274・#3291）で書く。
  await expect(turn.getByTestId("sql-chat-last-execution")).toHaveText(
    "前回の実行（10/5 05:03）: 12 行・8 列。結果の行は保存していないため、見るにはもう一度実行してください。",
  );
  await expect(turn.getByText("安全検査済み・実行済み")).toBeVisible();
  // 受け取りを 1 回試み、受け取れなければ要約のまま（失敗の表示を出さない）。
  await expect.poll(() => state.receiveRequests).toEqual(["/api/nl2sql/jobs/chat-1/execution-result"]);
  await expect(turn.getByTestId("sql-chat-execution-receiving")).toHaveCount(0);
  await expect(turn.getByTestId("sql-chat-last-execution")).toBeVisible();
  const run = turn.getByTestId("sql-chat-execute");
  await expect(run).toHaveAccessibleName("もう一度実行");
  // 開き直した会話でも、「もう一度実行」は処理の経過を変えない（前回の実行の要約は経過と別に出す）。
  const progressCount = await turn.getByTestId("sql-chat-progress-summary").count();
  const progressBefore = progressCount ? await turn.getByTestId("sql-chat-progress-summary").textContent() : null;
  await run.click();
  await expect(turn.getByTestId("sql-chat-result-summary")).toHaveText("60 行・8 列・0.8 秒");
  await expect(turn.getByTestId("sql-chat-last-execution")).toHaveCount(0);
  await expect(turn.getByTestId("sql-chat-progress-summary")).toHaveCount(progressCount);
  if (progressBefore !== null) await expect(turn.getByTestId("sql-chat-progress-summary")).toHaveText(progressBefore);
  expect(state.executeRequests).toEqual(["/api/nl2sql/jobs/chat-1/execute"]);
});
