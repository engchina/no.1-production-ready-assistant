import { confirmTraceExplorer, inspectTraceStep } from "../../../../platform/packages/ui/e2e/trace-explorer";
import { syntheticCallDetails, syntheticStageDetails } from "../../../../platform/packages/ui/e2e/call-fixture";
// 実行履歴（#1955。UX 契約 run-history.md §7）: 共通の一覧（RunHistoryToolbar → RunHistoryTable → OffsetPagination）と
// `?id=<run_id>` の全幅の詳細。状態は `[data-run-status]` の値で確かめる（文言・色に頼らない）。
import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { chooseSelectFieldOption } from "./_helpers/select-field";
import { jobProgressEvents, withRecordedTrace } from "./_helpers/job-progress";
import {
  findClippedPlaceholders,
  findIconTextMisalignment,
  findToolbarMisalignment,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

async function fulfillJson(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(status === 200 ? { data } : { detail: data }),
  });
}

type HistoryRecord = Record<string, unknown> & { id: string };

const historyItems: HistoryRecord[] = [
  {
    id: "history-new",
    question: "未入金の顧客を確認",
    engine: "enterprise_ai_direct",
    generated_sql: "SELECT CUSTOMER_NAME FROM INVOICES WHERE PAID_AT IS NULL",
    executable_sql: "SELECT CUSTOMER_NAME FROM INVOICES WHERE PAID_AT IS NULL FETCH FIRST 100 ROWS ONLY",
    created_at: "2026-06-22T10:00:00.000Z",
    elapsed_ms: 250,
    generation_elapsed_ms: 180,
    engine_timings: [
      { engine: "enterprise_ai_direct", elapsed_ms: 80, status: "failed", error: "team execution failed" },
      { engine: "enterprise_ai_direct", elapsed_ms: 100, status: "success", error: "" },
    ],
    // 処理の経過は実行の span の木（#3226）。生成の中のモデルの呼び出しの 1 回目は失敗。
    progress_events: jobProgressEvents("history-new", {
      at: "2026-06-22T10:00:00.250Z",
      terminal: "done",
      steps: {
        queue: { status: "done", startedAt: "2026-06-22T10:00:00.000Z", finishedAt: "2026-06-22T10:00:00.010Z" },
        prepare_context: {
          status: "done",
          startedAt: "2026-06-22T10:00:00.010Z",
          finishedAt: "2026-06-22T10:00:00.030Z",
          details: { fields: [{ label: "生成に使った質問", value: "未入金顧客" }] },
        },
        generate_sql: {
          status: "done",
          startedAt: "2026-06-22T10:00:00.030Z",
          finishedAt: "2026-06-22T10:00:00.210Z",
          params: { engine: "enterprise_ai_direct" },
          children: [{ name: "gen_ai.chat", status: "failed", attributes: { "gen_ai.request.model": "test-text-model", "error.type": "TimeoutError" } }],
        },
        safety_check: { status: "done", startedAt: "2026-06-22T10:00:00.210Z", finishedAt: "2026-06-22T10:00:00.225Z", params: { tables: "APP.INVOICES", table_count: 1 } },
        execute_sql: {
          status: "done",
          startedAt: "2026-06-22T10:00:00.225Z",
          finishedAt: "2026-06-22T10:00:00.245Z",
          params: { rows: 2 },
          details: { fields: [{ label: "取得した行数", value: "2 行" }, { label: "列", value: "CUSTOMER_NAME", mono: true }] },
        },
        format_results: { status: "done", startedAt: "2026-06-22T10:00:00.245Z", finishedAt: "2026-06-22T10:00:00.250Z" },
      },
    }),
    feedback_rating: "good",
    profile_id: "finance",
    profile_name: "経理プロファイル",
    rewritten_question: "未入金顧客",
    safety_is_safe: true,
    result_row_count: 2,
    result_columns: ["CUSTOMER_NAME"],
    feedback_comment: "期待通りです",
    actor_user_uuid: "me",
    executed_by_me: true,
    executor_display_name: "システム管理者",
    conversation_owner_display_name: "システム管理者",
    owned_by_me: true,
    kind: "chat",
    session_id: "conversation-finance-1",
    // 計測できない呼び出しと Enterprise AI Direct の呼び出しが混ざる実行（#3543）。
    usage: {
      models: ["test-text-model"],
      requests: 2,
      input_tokens: 12000,
      output_tokens: 345,
      total_tokens: 12345,
      cached_input_tokens: null,
      reasoning_output_tokens: null,
      measurement: "partial",
      measured_requests: 1,
      unmeasured_sources: ["external_model"],
    },
  },
  {
    id: "history-middle",
    question: "請求金額を確認",
    engine: "enterprise_ai_direct",
    generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    created_at: "2026-06-21T10:00:00.000Z",
    elapsed_ms: 120,
    feedback_rating: "bad",
    profile_id: "default",
    profile_name: "既定プロファイル",
    rewritten_question: "請求金額一覧",
    safety_is_safe: true,
    result_row_count: 4,
    result_columns: ["TOTAL_AMOUNT"],
    feedback_comment: "集計条件が違います",
    // 計測できない呼び出しだけの実行は数を作らず未計測（#3543）。
    usage: {
      models: [],
      requests: 0,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      measurement: "unmeasured",
      measured_requests: 0,
      unmeasured_sources: ["external_model"],
    },
    actor_user_uuid: "me",
    executed_by_me: true,
    executor_display_name: "システム管理者",
    conversation_owner_display_name: null,
    owned_by_me: null,
    kind: "query",
  },
  {
    id: "history-old",
    question: "監査ログを削除",
    engine: "enterprise_ai_direct",
    generated_sql: "DELETE FROM AUDIT_LOG",
    executable_sql: "",
    created_at: "2026-06-20T10:00:00.000Z",
    elapsed_ms: 30,
    progress_events: jobProgressEvents("history-old", {
      at: "2026-06-20T10:00:00.030Z",
      terminal: "failed",
      steps: { queue: "done", prepare_context: "done", generate_sql: "done", safety_check: "failed", execute_sql: "skipped", format_results: "done" },
    }),
    feedback_rating: null,
    profile_id: "audit",
    profile_name: "監査プロファイル",
    rewritten_question: "",
    safety_is_safe: false,
    result_row_count: 0,
    result_columns: [],
    feedback_comment: "",
    actor_user_uuid: "me",
    executed_by_me: true,
    executor_display_name: "システム管理者",
    conversation_owner_display_name: null,
    owned_by_me: null,
    kind: "query",
  },
];

/**
 * モックの backend の今の時刻（#3386）。画面は端末の今の時刻から期間の開始（since）を求めるので、モックは
 * 「今から何 ms 前か」を受け取り、この時刻から同じ幅だけ前を期間の開始として比べる（記録の日時は固定のまま）。
 */
const MOCK_HISTORY_NOW = Date.parse("2026-06-25T00:00:00.000Z");
const DAY_MS = 24 * 60 * 60 * 1000;

function mockSince(since: string | null): string | null {
  if (!since) return null;
  return new Date(MOCK_HISTORY_NOW - (Date.now() - Date.parse(since))).toISOString();
}

function manyItems(count: number): HistoryRecord[] {
  return Array.from({ length: count }, (_, index) => ({
    ...historyItems[1],
    id: `history-${String(index + 1).padStart(3, "0")}`,
    question: `ページ確認の質問 ${index + 1}`,
    // 期間の既定（過去 7 日）に入る日時（MOCK_HISTORY_NOW の前日）。
    created_at: new Date(Date.UTC(2026, 5, 24, 0, 0, count - index)).toISOString(),
  }));
}

interface MockOptions {
  items?: readonly HistoryRecord[];
  othersVisible?: boolean;
  requests?: URL[];
  /** 一覧の応答を待たせる（読み込み中を確かめる）。 */
  gate?: Promise<void>;
  /** 一覧の取得を失敗させる。 */
  fail?: () => boolean;
}

/** 種類の絞り込み（backend の `history_kind_filters`。`not_evaluation` は品質評価を除く。#1986）。 */
function kindMatches(item: HistoryRecord, kind: string) {
  if (kind === "all") return true;
  if (kind === "not_evaluation") return item.kind !== "evaluation";
  return item.kind === kind;
}

/** backend と同じく、絞り込み（q・status・kind・since・executor）→ 新しい順 → offset / limit で返す。 */
function historyPage(url: URL, items: readonly HistoryRecord[], othersVisible: boolean) {
  const q = (url.searchParams.get("q") ?? "").trim().toLocaleLowerCase("ja-JP");
  const status = url.searchParams.get("status") ?? "all";
  const kind = url.searchParams.get("kind") ?? "all";
  const since = mockSince(url.searchParams.get("since"));
  const executor = url.searchParams.get("executor") ?? "all";
  const filtered = items.filter((item) => {
    // 停止の行（#3348）は安全性の判定を持たず、完了・ブロックに数えない。
    const cancelled = item.run_status === "cancelled";
    if (status === "cancelled" && !cancelled) return false;
    if (status === "succeeded" && (cancelled || item.safety_is_safe !== true)) return false;
    if (status === "blocked" && (cancelled || item.safety_is_safe !== false)) return false;
    if (!kindMatches(item, kind)) return false;
    if (since && String(item.created_at) < since) return false;
    if (executor === "mine" && item.executed_by_me === false) return false;
    if (executor === "others" && item.executed_by_me !== false) return false;
    const text = [item.question, item.generated_sql, item.feedback_comment].join("\n").toLocaleLowerCase("ja-JP");
    return !q || text.includes(q);
  });
  const sorted = [...filtered].sort((a, b) => (String(a.created_at) < String(b.created_at) ? 1 : -1));
  const limit = Number(url.searchParams.get("limit") ?? "25");
  const offset = Number(url.searchParams.get("offset") ?? "0");
  // 一覧の行は backend と同じく合計の token と計測の状態を持つ（記録の無い行はどちらも null。#3543）。
  const pageItems = sorted.slice(offset, offset + limit).map((item) => {
    const usage = item.usage as { total_tokens: number; measurement: string } | undefined;
    return {
      ...item,
      total_tokens: usage && usage.measurement !== "unmeasured" ? usage.total_tokens : null,
      usage_measurement: usage ? usage.measurement : null,
    };
  });
  // 「全 M 件」は種類の範囲で数える（品質評価を含まない種類なら品質評価を除く。#1986）。
  const evaluations = items.filter((item) => item.kind === "evaluation").length;
  const unfilteredTotal = kind === "all" || kind === "evaluation" ? items.length : items.length - evaluations;
  return {
    items: pageItems,
    total: sorted.length,
    limit,
    offset,
    has_next: offset + pageItems.length < sorted.length,
    unfiltered_total: unfilteredTotal,
    others_visible: othersVisible,
    hidden_evaluation_total: kind === "not_evaluation" && unfilteredTotal === 0 ? evaluations : 0,
  };
}

async function mockHistory(page: Page, options: MockOptions = {}) {
  const items = options.items ?? historyItems;
  await page.route("**/api/nl2sql/history**", async (route) => {
    const url = new URL(route.request().url());
    const detail = /\/api\/nl2sql\/history\/([^/?]+)$/.exec(url.pathname);
    if (detail) {
      const item = items.find((entry) => entry.id === decodeURIComponent(detail[1]));
      // 処理の経過（span）は mock の span の表に書き、行の処理の経過の API が返す（#3226 の案 A）。
      return item ? fulfillJson(route, withRecordedTrace(item.id, item)) : fulfillJson(route, "実行履歴が見つかりません。", 404);
    }
    options.requests?.push(url);
    if (options.gate) await options.gate;
    if (options.fail?.()) return fulfillJson(route, "履歴更新テストエラー", 503);
    return fulfillJson(route, historyPage(url, items, options.othersVisible ?? false));
  });
}

function table(page: Page) {
  return page.getByTestId("history-table");
}

function row(page: Page, id: string) {
  return page.getByTestId(`history-table-row-${id}`);
}

function titleLink(page: Page, question: string) {
  return page.getByRole("link", { name: `${question}（詳細を開く）` });
}

/** 375px は絞り込みをシートにまとめる（3 つ以上）。広い画面はツールバーの中の選択欄。 */
async function chooseFilter(page: Page, name: "status" | "kind" | "period" | "executor", value: string) {
  const toolbar = page.getByTestId("history-toolbar");
  const sheetMode = (await toolbar.getAttribute("data-list-toolbar-filters-layout")) === "sheet";
  if (sheetMode) await toolbar.getByRole("button", { name: /^絞り込み/ }).click();
  await chooseSelectFieldOption(page.getByTestId(`history-toolbar-${name}`), value);
  if (sheetMode) await page.keyboard.press("Escape");
}

async function expectAlignedHeader(page: Page) {
  const title = await page.getByRole("heading", { level: 1 }).boundingBox();
  const card = await page.getByTestId("history-list-card").boundingBox();
  expect(title).not.toBeNull();
  expect(card).not.toBeNull();
  expect(Math.abs(title!.x - card!.x)).toBeLessThanOrEqual(1);
}

test("実行履歴は共通の表で質問・状態・種類・開始日時・所要時間を並べ、状態を data-run-status で示す", { tag: "@responsive" }, async ({ page, isMobile }, testInfo) => {
  await mockHistory(page);
  await page.goto("/history");
  await expect(page.getByRole("heading", { level: 1, name: "実行履歴" })).toBeVisible();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  // 期間の既定は過去 7 日（#3386）。期間で絞っているので件数は「N 件（全 M 件）」。
  await expect(page.getByTestId("history-list-count")).toHaveText("3 件（全 3 件）");

  const newRow = row(page, "history-new");
  await expect(newRow.locator("[data-run-status]:visible")).toHaveAttribute("data-run-status", "succeeded");
  await expect(row(page, "history-old").locator("[data-run-status]:visible")).toHaveAttribute("data-run-status", "blocked");
  await expect(newRow).toContainText("経理プロファイル");
  // 実行者の列は、他の人の実行が見えない利用者には出さない（#1954。この一覧は自分の実行だけ）。
  await expect(table(page).getByRole("columnheader", { name: "実行者" })).toHaveCount(0);
  const compact = newRow.locator("[data-run-history-compact-meta]");
  if (isMobile) {
    // 375px は題名と操作の 2 列で、状態・日時・所要時間・種類を題名の下の 1 行にまとめる。
    await expect(compact).toBeVisible();
    await expect(compact).toContainText("2026/06/22");
    await expect(compact).toContainText("チャット");
    await expect(table(page).getByRole("columnheader", { name: "状態" })).toBeHidden();
  } else {
    await expect(compact).toBeHidden();
    for (const header of ["質問", "状態", "種類", "開始日時", "所要時間", "評価", "行数"]) {
      await expect(table(page).getByRole("columnheader", { name: header, exact: true })).toBeVisible();
    }
    await expect(newRow).toContainText("チャット");
    await expect(row(page, "history-middle")).toContainText("SQL 生成");
    await expect(row(page, "history-old")).toContainText("未実行");
  }
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  expect(await findToolbarMisalignment(page)).toEqual([]);
  expect(await findClippedPlaceholders(page)).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("history-list.png"), fullPage: true });
});

test("管理者と、自分の会話で他の人が実行した行を持つ利用者には実行者の列と絞り込みを出す（#1954）", { tag: "@responsive" }, async ({ page, isMobile }) => {
  await page.route("**/api/auth/me", (route) => fulfillJson(route, {
    ...systemAdminMe, is_system_admin: false, role_codes: ["ANALYST"], permissions: ["menu.history"],
  }));
  const requests: URL[] = [];
  const [own, byAdmin, standalone] = historyItems;
  await mockHistory(page, {
    othersVisible: true,
    requests,
    items: [
      { ...own, executor_display_name: "アリス", conversation_owner_display_name: "アリス" },
      {
        ...byAdmin, actor_user_uuid: "admin", executed_by_me: false, executor_display_name: "管理者 太郎",
        conversation_owner_display_name: "アリス", owned_by_me: true, kind: "chat",
      },
      { ...standalone, executor_display_name: "アリス" },
    ],
  });
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  if (!isMobile) {
    await expect(table(page).getByRole("columnheader", { name: "実行者", exact: true })).toBeVisible();
    await expect(row(page, "history-middle")).toContainText("管理者 太郎");
    // 自分の実行は「自分」（UUID・ログイン ID を出さない）。
    await expect(row(page, "history-new")).toContainText("自分");
  } else {
    await expect(row(page, "history-middle").locator("[data-run-history-compact-meta]")).toContainText("管理者 太郎");
  }
  await expect(page.getByText("admin", { exact: true })).toHaveCount(0);

  // 実行者の絞り込み（既定 すべて）。サーバーへ executor を渡し、件数は「N 件（全 M 件）」。
  expect(requests.at(-1)?.searchParams.get("executor")).toBeNull();
  await chooseFilter(page, "executor", "others");
  await expect.poll(() => requests.at(-1)?.searchParams.get("executor")).toBe("others");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(1);
  await expect(page.getByTestId("history-list-count")).toHaveText("1 件（全 3 件）");
  await chooseFilter(page, "executor", "self");
  await expect.poll(() => requests.at(-1)?.searchParams.get("executor")).toBe("mine");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(2);
  // 絞り込みは作業状態に残り、再読み込みの後も同じ条件で取り直す。
  await page.reload();
  await expect.poll(() => requests.at(-1)?.searchParams.get("executor")).toBe("mine");

  // 詳細は実行者と、実行者と違うときだけ会話の持ち主を出す。
  await chooseFilter(page, "executor", "all");
  await titleLink(page, "請求金額を確認").click();
  const summary = page.getByTestId("history-detail-summary");
  await expect(summary.locator('[data-run-fact="executor"] dd')).toHaveText("管理者 太郎");
  await expect(summary.locator('[data-run-fact="owner"] dd')).toHaveText("自分");
  await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
  await titleLink(page, "監査ログを削除").click();
  await expect(page.getByTestId("history-detail-summary").locator('[data-run-fact="owner"]')).toHaveCount(0);
});

test("実行の詳細はチャットの往復だけ「会話を開く」を出し、再実行は出さない (#2343)", { tag: "@responsive" }, async ({ page }) => {
  let permissions = ["menu.history"];
  await page.route("**/api/auth/me", (route) => fulfillJson(route, {
    ...systemAdminMe, is_system_admin: false, role_codes: ["ANALYST"], permissions,
  }));
  await mockHistory(page);
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  // 一覧の行にも再実行を置かない（RAG の実行履歴と同じ）。
  await expect(page.getByTestId("history-table-row-actions-history-new")).toHaveCount(0);
  await titleLink(page, "未入金の顧客を確認").click();
  await expect(page.getByTestId("history-detail-summary")).toBeVisible();
  await expect(page.getByRole("button", { name: "この質問で再実行" })).toHaveCount(0);
  // チャットを開けない利用者には「会話を開く」も出さない。
  await expect(page.getByTestId("history-detail-open-chat")).toHaveCount(0);

  permissions = ["menu.history", "menu.chat", "menu.query"];
  await page.reload();
  const openChat = page.getByTestId("history-detail-open-chat");
  await expect(openChat).toHaveText("会話を開く");
  await expect(openChat).toHaveAttribute("href", /\/chat\?profile_id=finance&conversation_id=conversation-finance-1$/);
  await expect(page.getByRole("button", { name: "この質問で再実行" })).toHaveCount(0);

  // チャットの実行に「SQL 生成で開く」は出さない（どちらか 1 つ。#3309）。
  await expect(page.getByTestId("history-detail-open-query")).toHaveCount(0);

  // SQL 生成の画面の実行は、開く元の会話が無いので「会話を開く」は出さず、本人の実行は「SQL 生成で開く」（#3309）。
  await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
  await titleLink(page, "請求金額を確認").click();
  await expect(page.getByTestId("history-detail-summary")).toBeVisible();
  await expect(page.getByTestId("history-detail-open-chat")).toHaveCount(0);
  await expect(page.getByTestId("history-detail-open-query")).toHaveAttribute(
    "href",
    /\/query\?question=%E8%AB%8B%E6%B1%82%E9%87%91%E9%A1%8D%E3%82%92%E7%A2%BA%E8%AA%8D&profile_id=default$/
  );
  // SQL 生成の画面の権限が無ければ出さない。
  permissions = ["menu.history", "menu.chat"];
  await page.reload();
  await expect(page.getByTestId("history-detail-summary")).toBeVisible();
  await expect(page.getByTestId("history-detail-open-query")).toHaveCount(0);
});

test("検索・状態・種類・期間の絞り込みはサーバーへ渡し、0 件なら条件のクリアを案内する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const requests: URL[] = [];
  await mockHistory(page, { requests });
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  // 期間の既定は 3 製品で共通の過去 7 日（#3386）。既定のままなら「条件をクリア」を出さない。
  const since = requests.at(-1)?.searchParams.get("since");
  expect(Date.now() - Date.parse(String(since))).toBeGreaterThan(7 * DAY_MS - 60 * 60 * 1000);
  expect(Date.now() - Date.parse(String(since))).toBeLessThan(7 * DAY_MS + 60 * 60 * 1000);
  await expect(page.getByRole("button", { name: "条件をクリア", exact: true })).toHaveCount(0);

  await chooseFilter(page, "status", "blocked");
  await expect.poll(() => requests.at(-1)?.searchParams.get("status")).toBe("blocked");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(1);
  await expect(page.getByTestId("history-list-count")).toHaveText("1 件（全 3 件）");

  await chooseFilter(page, "status", "all");
  await chooseFilter(page, "kind", "chat");
  await expect.poll(() => requests.at(-1)?.searchParams.get("kind")).toBe("chat");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(1);

  // 期間は今の時刻からの ISO 8601（since）。モックの記録は過去 24 時間より古いので 0 件になる。
  await chooseFilter(page, "period", "24h");
  await expect
    .poll(() => Date.now() - Date.parse(String(requests.at(-1)?.searchParams.get("since"))))
    .toBeLessThan(DAY_MS + 60 * 60 * 1000);
  await expect(page.getByText("条件に一致する実行がありません")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("history-no-results.png"), fullPage: true });
  // 0 件の取り直しの間も、絞り込みの欄を残す（スケルトンに置き換えない。#739）。
  await expect(page.getByTestId("history-toolbar")).toBeVisible();
  await page.getByRole("button", { name: "条件をクリア" }).first().click();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  // 「条件をクリア」は期間を既定（過去 7 日）、種類を既定（品質評価以外。#1986）へ戻す。
  expect(Date.now() - Date.parse(String(requests.at(-1)?.searchParams.get("since")))).toBeGreaterThan(7 * DAY_MS - 60 * 60 * 1000);
  expect(requests.at(-1)?.searchParams.get("kind")).toBe("not_evaluation");

  // 「すべて」は既定からの変更として数え、since を送らない。
  await chooseFilter(page, "period", "all");
  await expect.poll(() => requests.at(-1)?.searchParams.has("since")).toBe(false);
  await expect(page.getByTestId("history-list-count")).toHaveText("全 3 件");
  await expect(page.getByRole("button", { name: "条件をクリア", exact: true }).first()).toBeVisible();

  // 検索欄（debounce・IME は SearchField）。
  await page.getByRole("searchbox", { name: "実行履歴を検索" }).fill("AUDIT_LOG");
  await expect.poll(() => requests.at(-1)?.searchParams.get("q")).toBe("AUDIT_LOG");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(1);
});

test("過去 7 日に実行が無く、それより前にはあるときは、期間を「すべて」にする操作を出す（#3386）", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const requests: URL[] = [];
  const old = historyItems.map((item, index) => ({
    ...item,
    created_at: new Date(MOCK_HISTORY_NOW - (20 + index) * DAY_MS).toISOString(),
  }));
  await mockHistory(page, { items: old, requests });
  await page.goto("/history");
  const empty = page.getByTestId("history-period-empty");
  await expect(empty).toContainText("過去 7 日の実行はありません");
  await expect(empty).toContainText("期間を「すべて」にすると表示します");
  await expect(page.getByTestId("history-empty")).toHaveCount(0);
  await expect(page.getByTestId("history-toolbar")).toBeVisible();
  await expect(page.getByTestId("history-list-count")).toHaveText("0 件（全 3 件）");
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("history-period-empty.png"), fullPage: true });
  await empty.getByRole("button", { name: "すべての期間を表示" }).click();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  expect(requests.at(-1)?.searchParams.has("since")).toBe(false);
  // 選んだ「すべて」は再読込の後も残す。
  await page.reload();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  expect(requests.at(-1)?.searchParams.has("since")).toBe(false);
});

// #1986: Agent の品質評価の Run が MCP で呼んだ実行は既定で隠し、「種類: 品質評価」「すべて」で出す。
// MCP（外部のクライアント・Agent の Run）は種類に呼び出し元を添える。件数と「条件をクリア」は既定が基準。
const originItems: HistoryRecord[] = [
  ...historyItems,
  {
    ...historyItems[1],
    id: "history-mcp",
    question: "MCP で売上を確認",
    created_at: "2026-06-23T10:00:00.000Z",
    run_source: "mcp",
    api_key_name: "Claude Code",
    kind: "mcp",
  },
  {
    ...historyItems[1],
    id: "history-eval-1",
    question: "品質評価のケース 1",
    created_at: "2026-06-24T10:00:00.000Z",
    run_source: "evaluation",
    agent_run_id: "run-eval-1",
    kind: "evaluation",
  },
  {
    ...historyItems[1],
    id: "history-eval-2",
    question: "品質評価のケース 2",
    created_at: "2026-06-24T11:00:00.000Z",
    run_source: "evaluation",
    agent_run_id: "run-eval-2",
    kind: "evaluation",
  },
];

test("品質評価の実行は既定で隠し、種類で品質評価・すべてを選ぶと出る（#1986）", { tag: "@responsive" }, async ({ page, isMobile }) => {
  const requests: URL[] = [];
  await mockHistory(page, { items: originItems, requests });
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(4);
  await expect(table(page).locator('[data-run-id^="history-eval-"]')).toHaveCount(0);
  expect(requests.at(-1)?.searchParams.get("kind")).toBe("not_evaluation");
  await expect(page.getByTestId("history-list-count")).toHaveText("4 件（全 4 件）");
  await expect(page.getByRole("button", { name: "条件をクリア", exact: true })).toHaveCount(0);
  const mcpKind = isMobile ? row(page, "history-mcp").locator("[data-run-history-compact-meta]") : row(page, "history-mcp");
  await expect(mcpKind).toContainText("MCP（Claude Code）");

  await chooseFilter(page, "kind", "evaluation");
  await expect.poll(() => requests.at(-1)?.searchParams.get("kind")).toBe("evaluation");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(2);
  await expect(page.getByTestId("history-list-count")).toHaveText("2 件（全 6 件）");
  await chooseFilter(page, "kind", "all");
  await expect.poll(() => requests.at(-1)?.searchParams.has("kind")).toBe(false);
  await expect(table(page).locator("[data-run-id]")).toHaveCount(6);

  // 品質評価の実行の詳細は種類「品質評価」と、処理の経過の中の「処理の詳細」に Agent の実行 ID（#2325）。
  await titleLink(page, "品質評価のケース 1").click();
  const summary = page.getByTestId("history-detail-summary");
  await expect(summary.locator('[data-run-fact="kind"] dd')).toHaveText("品質評価");
  // span の記録の無い（古い形式の）実行は開閉の見出しを出さず、古い形式の案内と処理の詳細を節の中に直接置く
  // （#2473・#3226。空白にしない）。
  await expect(page.getByTestId("history-detail-timeline-summary")).toHaveCount(0);
  await expect(page.getByTestId("history-detail-timeline-legacy")).toHaveText(
    "この実行は古い形式の記録のため、処理の経過を表示できません。",
  );
  await page.getByTestId("history-detail-process-details").click();
  await expect(page.getByTestId("history-detail-caller")).toHaveCount(0);
  await expect(page.getByTestId("history-detail-technical").locator("[data-process-identifier]")).toHaveCount(3);
  await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();

  await page.getByRole("button", { name: "条件をクリア", exact: true }).first().click();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(4);
  expect(requests.at(-1)?.searchParams.get("kind")).toBe("not_evaluation");
});

test("品質評価の実行だけがあるときは、絞り込みを残して既定で隠していることと出し方を出す（#1986）", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockHistory(page, { items: originItems.filter((item) => item.kind === "evaluation") });
  await page.goto("/history");
  await expect(page.getByText("品質評価の実行（2 件）は既定で表示しません。")).toBeVisible();
  await expect(page.getByTestId("history-empty")).toHaveCount(0);
  await expect(page.getByTestId("history-toolbar")).toBeVisible();
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("history-evaluation-hidden.png"), fullPage: true });
  await chooseFilter(page, "kind", "evaluation");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(2);
});

test("実行履歴は初回の読み込み中を経過時間とスケルトンで示し、実行が無いときは空の状態を出す", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockHistory(page, { items: [], gate });
  await page.goto("/history");
  await expect(page.getByTestId("history-list-loading")).toContainText("実行履歴を読み込んでいます");
  await expect(page.getByTestId("history-list-skeleton")).toBeVisible();
  release();
  await expect(page.getByTestId("history-empty")).toContainText("まだ実行がありません");
  await expect(page.getByTestId("history-toolbar")).toHaveCount(0);
});

test("実行履歴は 25 件ずつのサーバーのページを表の直下で移り、ページ番号の入力・表示件数を残す (#1891)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const requests: URL[] = [];
  await mockHistory(page, { items: manyItems(60), requests });
  await page.goto("/history");
  const pagination = page.getByTestId("history-pagination");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(25);
  await expect(pagination).toContainText("1–25 / 60 件");
  await jumpToPage(pagination, 3);
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("50");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(10);
  await expect(pagination).toContainText("51–60 / 60 件");
  await page.screenshot({ path: testInfo.outputPath("history-page-3.png"), fullPage: true });
  // 再読み込みの後も同じページ。
  await page.reload();
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("50");
  // 表示件数を変えると 1 ページ目へ戻る。
  await chooseSelectFieldOption(pagination.getByRole("combobox"), "10");
  await expect.poll(() => requests.at(-1)?.searchParams.get("limit")).toBe("10");
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("0");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(10);
});

test("詳細は題名のリンクで ?id= の全幅の画面に開き、一覧へ戻ると条件・ページ・開いた行が戻る", { tag: "@responsive" }, async ({ page }) => {
  const requests: URL[] = [];
  const items = manyItems(40);
  await mockHistory(page, { items, requests });
  await page.goto("/history");
  await jumpToPage(page.getByTestId("history-pagination"), 2);
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("25");
  await chooseFilter(page, "status", "succeeded");
  await jumpToPage(page.getByTestId("history-pagination"), 2);
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("25");

  const target = items[30];
  const link = titleLink(page, String(target.question));
  // 題名は href 付きのリンク（新しいタブで開ける）。
  await expect(link).toHaveAttribute("href", `/history?id=${target.id}`);
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/history\\?id=${target.id}$`));
  await expect(page.getByRole("heading", { level: 1, name: "実行の詳細" })).toBeVisible();
  await expect(page.getByTestId("history-list-view")).toBeHidden();
  const requestCount = requests.length;

  await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(link).toBeVisible();
  // 一覧は作り直さず、同じ条件・同じページのまま（取り直さない）。
  expect(requests.length).toBe(requestCount);
  await expect(page.getByTestId("history-pagination")).toContainText("26–40 / 40 件");
  await expect(page.getByTestId("history-toolbar-status").or(page.getByTestId("history-toolbar").getByRole("button", { name: /^絞り込み（1）/ })).first()).toBeVisible();
  await expect(link).toHaveAttribute("aria-current", "true");

  // ブラウザの戻る・進むでも同じ。
  await page.goForward();
  await expect(page.getByRole("heading", { level: 1, name: "実行の詳細" })).toBeVisible();
  await page.goBack();
  await expect(link).toBeVisible();

  // URL を直接開ける（再読み込みの後も詳細）。
  await page.goto(`/history?id=${target.id}`);
  await expect(page.getByTestId("history-detail-summary")).toContainText(String(target.question));
  await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("25");
  expect(requests.at(-1)?.searchParams.get("status")).toBe("succeeded");
});

test("詳細は概要 → 処理の経過（処理の詳細を含む）→ SQL と結果 → フィードバックを 1 枚に並べ、段は Enter で開閉できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockHistory(page);
  await page.goto("/history?id=history-new");
  const detail = page.locator("[data-run-detail]");
  await expect(detail).toBeVisible();
  const order = await detail.evaluate((node) =>
    Array.from(node.children).map((child) => child.getAttribute("data-testid"))
  );
  expect(order).toEqual([
    "history-detail-summary",
    "history-detail-timeline",
    "history-detail-sql",
    "history-detail-feedback",
  ]);
  const summary = page.getByTestId("history-detail-summary");
  await expect(summary).toBeVisible();
  await expect(summary.locator("[data-run-status]")).toHaveCount(0);
  await expect(summary.locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
  await expect(summary.locator('[data-run-fact="runId"] dd')).toHaveText("history-new");
  await expect(summary.locator('[data-run-fact="target"] dd')).toHaveText("経理プロファイル");
  await expect(summary.locator('[data-run-fact="startedAt"] time')).toHaveAttribute("datetime", "2026-06-22T10:00:00.000Z");

  // 処理の経過: 既定は「N ステップ・合計 M 秒」の 1 行に閉じる（回答の根拠・チャットの処理の経過と同じ開閉。#2473）。
  // 開くとチャットの処理の経過と同じ span の木（#3226）。実行の全体は行にせず、生成の中のモデルの呼び出しは入れ子。
  const timeline = page.getByTestId("history-detail-timeline");
  const timelineSummary = page.getByTestId("history-detail-timeline-summary");
  await expect(timelineSummary).toContainText("7 ステップ・合計");
  await expect(timeline.locator("details[data-run-timeline-section]")).not.toHaveAttribute("open", "");
  await expect(timeline.locator("[data-run-timeline-step]").first()).toBeHidden();
  await timelineSummary.click();
  const rows = timeline.locator("[data-run-timeline-step]");
  await expect(rows).toHaveCount(7);
  await expect(rows.nth(0)).toContainText("処理を開始しました");
  await expect(rows.nth(2)).toContainText("SQL を生成しました");
  // 「トレースの詳細」の 2 つ目の表示は無い。Trace ID は処理の詳細に出す。
  await expect(timeline.getByText("トレースの詳細")).toHaveCount(0);
  await expect(timeline.locator("[data-trace-id-row]")).toHaveCount(1);
  const failed = rows.nth(3);
  await expect(failed).toHaveAttribute("data-status", "failed");
  await expect(failed).toContainText("モデルを呼び出す（用途の記録なし）");
  await inspectTraceStep(page, timeline, (await failed.getAttribute("data-run-timeline-step"))!);
  await expect(page.locator("[data-trace-technical]")).toContainText("Span name");
  await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
  const toggle = rows.nth(1).locator("[data-run-timeline-toggle]");
  const expanded = await toggle.getAttribute("aria-expanded");
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", expanded === "true" ? "false" : "true");
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", expanded ?? "false");
  // 段の詳細（#2294）: 記録の値（書き換えた質問・行数と列）と span の名前。既定で閉じる。
  const prepare = rows.nth(1);
  const prepareToggle = prepare.locator("[data-run-timeline-toggle]");
  await expect(prepareToggle).toHaveAttribute("aria-expanded", "false");
  // 開閉の印は行の文字の大きさ（実行の詳細は md の 14px）にそろえる（#2338。lucide の既定の 24px にしない）。
  const chevron = await prepareToggle.evaluate((button) => {
    const svg = button.querySelector("svg");
    return { size: svg ? Number.parseFloat(getComputedStyle(svg).height) : 0, font: Number.parseFloat(getComputedStyle(button).fontSize) };
  });
  expect(chevron.size).toBeCloseTo(chevron.font, 1);
  await prepareToggle.click();
  await expect(prepare.locator("[data-run-timeline-details]")).toContainText("生成に使った質問");
  // 文字の横のアイコン（状態・開閉の印・処理の詳細）は、文字の 1 行目の中心にそろえる（#2371）。
  expect(await findIconTextMisalignment(page, { root: "[data-run-detail]" })).toEqual([]);
  const execute = rows.nth(5);
  await execute.locator("[data-run-timeline-toggle]").click();
  await expect(execute.locator("[data-run-timeline-details]")).toContainText("2 行");
  await expect(execute.locator("[data-run-timeline-details]")).toContainText("CUSTOMER_NAME");

  // SQL と結果: 実行した SQL とコピー、結果の要約（行数・列）。
  const sql = page.getByTestId("history-detail-sql");
  await expect(sql.getByTestId("history-detail-sql-code")).toContainText("FETCH FIRST 100 ROWS ONLY");
  await expect(sql.getByRole("button", { name: "SQL をコピー" })).toBeVisible();
  await expect(sql.locator('[data-run-fact="columns"]')).toContainText("CUSTOMER_NAME");
  await expect(page.getByTestId("history-detail-feedback")).toContainText("期待通りです");
  // 処理の詳細（呼び出し元・生成した SQL・エンジンの試行）は、処理の経過の節の中のタイムラインの後ろに既定で閉じて置く
  // （チャットの処理の経過と同じ名前・部品・位置。ページの末尾に別の節として出さない。#2325）。
  const technical = timeline.getByTestId("history-detail-technical");
  const processDetails = page.getByTestId("history-detail-process-details");
  await expect(processDetails).toHaveText("処理の詳細");
  await expect(page.getByText("技術的な詳細", { exact: true })).toHaveCount(0);
  const lastStepBox = await timeline.locator("[data-run-timeline-step]").last().boundingBox();
  const detailsBox = await processDetails.boundingBox();
  expect(lastStepBox && detailsBox && detailsBox.y > lastStepBox.y).toBeTruthy();
  await expect(technical.getByTestId("history-detail-generated-sql")).toBeHidden();
  await processDetails.click();
  await expect(technical.getByTestId("history-detail-generated-sql")).toHaveCount(0);
  await expect(technical.locator("[data-process-identifier]")).toHaveCount(3);
  await expect(technical.locator("[data-run-used-models]")).toBeVisible();
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value: string) => { document.documentElement.dataset.copiedId = value; } } }));
  const messageRow = technical.locator('[data-process-identifier="回答メッセージ ID"]');
  const messageId = await messageRow.locator("span.font-mono").textContent();
  await messageRow.getByRole("button", { name: "回答メッセージ ID をコピー", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.copiedId)).toBe(messageId);
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("history-detail.png"), fullPage: true });
});

test("一覧の token の列と詳細の概要は、記録した利用量・一部計測・未計測（計測できない呼び出し・記録なし）を RAG と同じ形で出す（#3543）", { tag: "@responsive" }, async ({ page, isMobile }, testInfo) => {
  await mockHistory(page);
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  const usageHeader = table(page).getByRole("columnheader", { name: "token", exact: true });
  if (isMobile) {
    // 狭い画面では token の列を出さない（共通の一覧と同じ。詳細の概要で読む）。
    await expect(usageHeader).toBeHidden();
  } else {
    await expect(usageHeader).toBeVisible();
    await expect(row(page, "history-new")).toContainText("12,345 token");
    await expect(row(page, "history-new").locator("[data-run-usage-note]")).toHaveText("一部計測");
    // 計測できない呼び出しだけの実行と、記録の無い行は数を作らず未計測（0 token と出さない）。
    for (const id of ["history-middle", "history-old"]) {
      await expect(row(page, id).locator("[data-run-usage-note]")).toHaveText("未計測");
      await expect(row(page, id)).not.toContainText("0 token");
    }
  }

  await titleLink(page, "未入金の顧客を確認").click();
  const summary = page.getByTestId("history-detail-summary");
  const fact = (id: string) => summary.locator(`[data-run-fact="${id}"] dd`);
  await expect(fact("model")).toHaveText("test-text-model");
  await expect(fact("tokens")).toHaveText("12,345 token（一部計測）");
  await expect(fact("modelCalls")).toHaveText("2 回（うち 1 回を計測。入力 12,000 / 出力 345 token）");
  await expect(fact("unmeasuredCalls")).toHaveText("external_model");
  // 並びは RAG の概要と同じ（モデル → token → モデルの呼び出し）。
  const order = await summary.locator("[data-run-fact]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-run-fact")));
  expect(order.slice(order.indexOf("model"), order.indexOf("model") + 4)).toEqual(["model", "tokens", "modelCalls", "unmeasuredCalls"]);
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  await summary.screenshot({ path: testInfo.outputPath("history-detail-usage-partial.png") });

  await page.goto("/history?id=history-middle");
  await expect(fact("tokens")).toHaveText("未計測");
  await expect(fact("modelCalls")).toHaveText("未計測");
  await expect(fact("unmeasuredCalls")).toHaveText("external_model");
  await expect(summary.locator('[data-run-fact="model"]')).toHaveCount(0);
  await summary.screenshot({ path: testInfo.outputPath("history-detail-usage-unmeasured.png") });

  await page.goto("/history?id=history-old");
  await expect(fact("tokens")).toHaveText("未計測");
  await expect(fact("modelCalls")).toHaveText("未計測");
  await expect(summary.locator('[data-run-fact="unmeasuredCalls"]')).toHaveCount(0);
});

test("ブロックした実行は概要に理由を出し、無い実行・見えない実行は「この実行は見つかりません」", async ({ page }) => {
  await mockHistory(page);
  await page.goto("/history?id=history-old");
  const summary = page.getByTestId("history-detail-summary");
  await expect(summary).toBeVisible();
  await expect(summary.locator("[data-run-status]")).toHaveCount(0);
  await expect(summary.locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
  await expect(page.getByTestId("history-detail-blocked")).toContainText("安全性の判定で SQL の実行を止めました");
  await page.getByTestId("history-detail-timeline-summary").click();
  await expect(
    page.getByTestId("history-detail-timeline").locator("[data-run-timeline-step]").filter({ hasText: "SQL の種類と参照範囲を検査できませんでした" })
  ).toHaveAttribute("data-status", "failed");
  await expect(summary.locator('[data-run-fact="rows"] dd')).toHaveText("未実行");

  await page.goto("/history?id=missing-run");
  await expect(page.getByTestId("history-detail-not-found")).toContainText("この実行は見つかりません");
  await page.getByTestId("history-detail-not-found").getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
});

test("一覧の読み込みに失敗したら一覧の位置に再試行付きの失敗を出し、前の一覧があれば残す", async ({ page }) => {
  let failing = true;
  await mockHistory(page, { fail: () => failing });
  await page.goto("/history");
  const error = page.getByTestId("history-list-error");
  await expect(error).toBeVisible();
  // 取得できていない条件の結果を「0 件」と取り違えさせない。
  await expect(page.getByText("条件に一致する実行がありません")).toHaveCount(0);
  failing = false;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  await expect(error).toHaveCount(0);

  failing = true;
  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await expect(error).toBeVisible();
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
});

test(`実行履歴の一覧と詳細は 1280 / 1920 / 375px で横にあふれず、見出しと本文の左端がそろう`, { tag: "@responsive" }, async ({ page, isMobile }, testInfo) => {
  const long = {
    ...historyItems[0],
    id: "history-long",
    question: "対象テーブル：VERY_LONG_UNBROKEN_IDENTIFIER_WITHOUT_SPACES_0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ の売上を地域別・月別に集計して前年同月比も出す",
    profile_name: "プロファイル_WITH_A_VERY_LONG_UNBROKEN_IDENTIFIER_0123456789",
    executable_sql: `SELECT "VERY_LONG_UNBROKEN_FILTER_IDENTIFIER_0123456789" FROM "V_EMP_DEPT"`,
  };
  await mockHistory(page, { items: [long, ...historyItems.slice(1)], othersVisible: true });
  await page.goto("/history");
  await expect(page.locator("html")).toHaveCSS("color-scheme", "light");
  for (const width of isMobile ? [375] : [1280, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await expectAlignedHeader(page);
    await page.screenshot({ path: testInfo.outputPath(`history-list-light-${width}.png`), fullPage: true });
    await titleLink(page, String(long.question)).click();
    await expect(page.getByTestId("history-detail-summary")).toBeVisible();
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`history-detail-light-${width}.png`), fullPage: true });
    await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
  }
});

test("最初の Tab で本文へスキップでき、一覧の題名のリンクへキーボードで届く", async ({ page }) => {
  await mockHistory(page);
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveText(/本文へスキップ/);
  const link: Locator = titleLink(page, "未入金の顧客を確認");
  for (let index = 0; index < 30; index += 1) {
    if (await link.evaluate((node) => node === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(link).toBeFocused();
});

/** 停止で終わった実行の行（#3348。SQL・実行結果・評価を持たない）。 */
function cancelledItem(id: string, cancellation: Record<string, unknown>): HistoryRecord {
  return {
    ...historyItems[1],
    id,
    question: `停止した質問 ${id}`,
    generated_sql: "",
    executable_sql: "",
    rewritten_question: "",
    feedback_rating: null,
    feedback_comment: "",
    result_row_count: 0,
    result_columns: [],
    created_at: "2026-06-23T10:00:00.000Z",
    kind: "mcp",
    run_source: "mcp",
    run_status: "cancelled",
    cancellation: { actor_recorded: true, by_me: false, api_key_name: "", ...cancellation },
  };
}

const CANCELLED_ITEMS = [
  cancelledItem("history-cancel-mcp", { via: "mcp", actor_display_name: "管理 花子", api_key_name: "合成のキー A" }),
  cancelledItem("history-cancel-agent", { via: "agent_run", actor_display_name: "システム管理者", by_me: true }),
  cancelledItem("history-cancel-screen", { via: "screen", actor_display_name: "" }),
];

test("停止した実行は一覧で「停止」になり、状態の絞り込みで選べ、評価と行数を持たない (#3348)", async ({ page }) => {
  const requests: URL[] = [];
  await mockHistory(page, { items: [...CANCELLED_ITEMS, ...historyItems], requests });
  await page.goto("/history");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(6);
  const cancelledRow = row(page, "history-cancel-mcp");
  await expect(cancelledRow.locator("[data-run-status]:visible")).toHaveAttribute("data-run-status", "cancelled");
  // 評価の列は「—」（評価できない）、行数は「未実行」。
  await expect(cancelledRow).toContainText("—");
  await expect(cancelledRow).toContainText("未実行");
  await expect(cancelledRow).not.toContainText("未評価");

  await chooseFilter(page, "status", "cancelled");
  await expect.poll(() => requests.at(-1)?.searchParams.get("status")).toBe("cancelled");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(3);
  await expect(page.getByTestId("history-list-count")).toHaveText("3 件（全 6 件）");
  await chooseFilter(page, "status", "succeeded");
  await expect(table(page).locator("[data-run-id]")).toHaveCount(2);
  await expect(row(page, "history-cancel-mcp")).toHaveCount(0);
});

for (const [id, text] of [
  ["history-cancel-mcp", "管理 花子 · MCP（合成のキー A）"],
  ["history-cancel-agent", "自分 · Agent の実行から"],
  ["history-cancel-screen", "削除済みのユーザー · 画面"],
] as const) {
  test(`停止した実行の詳細は概要に取り消した人と入口を出し、SQL と結果の節を出さない (${id})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await mockHistory(page, { items: [...CANCELLED_ITEMS, ...historyItems] });
    await page.goto(`/history?id=${id}`);
    const summary = page.getByTestId("history-detail-summary");
    const fact = summary.locator('[data-run-fact="cancellation"]');
    await expect(fact.locator("dt")).toHaveText("取消");
    await expect(fact.locator("dd")).toHaveText(text);
    const order = await summary.locator("[data-run-fact]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-run-fact")));
    expect(order.indexOf("cancellation")).toBeGreaterThan(order.indexOf("executor"));
    expect(order.indexOf("cancellation")).toBeLessThan(order.indexOf("target"));
    await expect(summary.locator('[data-run-fact="rows"] dd')).toHaveText("未実行");
    await expect(page.getByTestId("history-detail-sql")).toHaveCount(0);
    await expect(page.getByTestId("history-detail-feedback")).toHaveCount(0);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    if (id === "history-cancel-mcp") {
      await summary.screenshot({ path: testInfo.outputPath(`history-cancel-${testInfo.project.name}.png`) });
      if (process.env.E2E_SCREENSHOT_DIR) {
        await summary.screenshot({ path: `${process.env.E2E_SCREENSHOT_DIR}/nl2sql-history-cancellation-${testInfo.project.name}.png` });
      }
    }
  });
}

test("呼び出しの詳細で入力・結果・思考・Schema・計時を確認できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const id = "synthetic-call";
  const row = { ...historyItems[0], id, progress_events: jobProgressEvents(id, { at: "2026-01-01T00:00:01Z", terminal: "done", steps: {
    prepare_context: { status: "done", startedAt: "2026-01-01T00:00:02Z", finishedAt: "2026-01-01T00:00:03Z", details: syntheticStageDetails, children: [{name: "nl2sql.attachment_evidence", status: "skipped"}] },
    generate_sql: { status: "done", startedAt: "2026-01-01T00:00:00Z", finishedAt: "2026-01-01T00:00:01Z", details: { call: syntheticCallDetails } },
  } }) };
  await mockHistory(page, { items: [row] });
  await page.goto(`/history?id=${id}`);
  await page.getByTestId("history-detail-timeline-summary").click();
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Span name");
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Parent span ID");
  await confirmTraceExplorer(page, testInfo, expect);
});
