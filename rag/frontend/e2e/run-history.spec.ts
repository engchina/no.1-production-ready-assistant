import { confirmTraceExplorer, inspectTraceStep } from "../../../platform/packages/ui/e2e/trace-explorer";
import { syntheticCallDetails, syntheticStageDetails } from "../../../platform/packages/ui/e2e/call-fixture";
import { expectGrowingList } from "./_list-viewport";
import { expect, type Locator, type Page, test } from "./fixtures/test";
import { mockTraceApi, ragSpan, savedSpanProgress, SPAN_TRACE_ID, traceSnapshotJson } from "./_chat-progress";
import {
  apiEnvelope,
  expectedControlHeight,
  expectNoPageOverflow,
  mockAuthUser,
  mockDatabaseReady,
  mockLocalAuth,
  openSidebarNav,
} from "./_helpers";
import {
  findIconTextMisalignment,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../platform/packages/ui/e2e/layout-checks";

// Issue 1890・1955（段 2）: RAG の「AI 活用」の「実行履歴」を 3 製品の共通の型（UX 契約 run-history.md）で作る。
// - 一覧: RunHistoryToolbar（検索 → 状態 → 種類 → 期間 → 実行者。FilterSelect）→ RunHistoryTable → OffsetPagination
//   （既定 25 件）。読み込み中は経過時間と表の Skeleton。条件・ページ・開いた行は作業状態に残る。
// - 状態は共通の 7 つに写し、`[data-run-status]` で確かめる（success → succeeded・blocked・error → failed）。
// - 375px は表が 2 列で、状態・日時・所要時間が題名の下の 1 行（`[data-run-history-compact-meta]`）。
// - 見え方（Issue 1889・1954）: system_admin は全員の実行、それ以外は自分が実行した実行と、自分の会話で
//   他の人（system_admin）が実行した実行。「実行者」の列と絞り込みは他の人の実行が見える利用者だけ。
// - 詳細: RunDetailLayout（概要 → 処理の経過〔タイムラインと処理の詳細〕→ 回答 → 根拠 → フィードバック）をタブ無しの 1 枚で。

const PAGE_SIZE = 25;
const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
/** 一覧の行の既定の開始の基準（期間の既定の過去 7 日に入る、直近の時刻。#3386）。 */
const RECENT_BASE_MS = Date.now() - 2 * HOUR_MS;

/** 期間の開始（since）が今から何 ms 前か。 */
function sinceAgeMs(since: string | null | undefined) {
  return Date.now() - Date.parse(since as string);
}

interface RunRow {
  run_id: string;
  surface: "search" | "chat";
  search_answer_profile_id: string | null;
  search_answer_profile_name: string | null;
  question: string | null;
  answer_generated: boolean;
  status: "success" | "blocked" | "error";
  answer_outcome: string | null;
  verification: "verified" | "partial" | "unverified" | null;
  validation_execution?: string | null;
  confidence: string | null;
  needs_human_review: boolean | null;
  citation_count: number;
  elapsed_ms: number | null;
  error_stage: string | null;
  model_id: string | null;
  conversation_id: string | null;
  message_id: string | null;
  created_at: string;
  executed_by_me: boolean;
  executor_display_name: string;
  /** 詳細だけの項目（会話の持ち主。既定は会話のある実行なら自分）。 */
  conversation_owner_display_name?: string | null;
  owned_by_me?: boolean | null;
  /** 詳細だけの項目（失敗の種類・コードと保存できなかった記録。#2342）。 */
  error_type?: string | null;
  error_code?: string | null;
  storage_failures?: { step: string; error_type: string; error_code: string | null }[];
  /** 詳細だけの項目（見えている根拠と、今の権限で見られない根拠の数。#3252）。 */
  citations?: (typeof CITATION)[];
  withheld_evidence_count?: number;
  /** 詳細だけの項目（RAG 検索の件数と絞り込み。「RAG 検索で開く」が画面に戻す。#3309）。 */
  search_conditions?: { top_k: number | null; filters: Record<string, string> } | null;
  /** 詳細だけの項目（回答の本文。既定は 1 文）。 */
  answer?: string | null;
  /** 詳細だけの項目（モデルの呼び出しの回数と利用量。記録の無い実行は null。#3419）。 */
  usage?: {
    models: string[];
    requests: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cached_input_tokens: number | null;
    reasoning_output_tokens: number | null;
    measurement: "complete" | "partial";
    measured_requests: number;
  } | null;
}

function run(index: number, overrides: Partial<RunRow> = {}): RunRow {
  const chat = index % 2 === 1;
  return {
    run_id: `run-${index}`,
    surface: chat ? "chat" : "search",
    search_answer_profile_id: "bv-1",
    search_answer_profile_name: "受注業務",
    question: `受注の登録方法 ${index}`,
    answer_generated: chat,
    status: "success",
    answer_outcome: chat ? "answered" : null,
    verification: chat ? "verified" : null,
    confidence: chat ? "high" : null,
    needs_human_review: chat ? false : null,
    citation_count: 2,
    elapsed_ms: 1200 + index,
    error_stage: null,
    model_id: null,
    conversation_id: chat ? `conv-${index}` : null,
    message_id: chat ? `msg-${index}` : null,
    // 期間の既定（過去 7 日）に入る直近の時刻（行の番号が大きいほど 1 分ずつ古い）。
    created_at: new Date(RECENT_BASE_MS - index * 60 * 1000).toISOString(),
    executed_by_me: true,
    executor_display_name: "ローカル利用者",
    ...overrides,
  };
}

const CITATION = {
  document_id: "doc-1",
  chunk_id: "doc-1:c1",
  file_name: "受注マニュアル.pdf",
  text: "受注番号を入力し、登録ボタンを押します。",
  score: 0.91,
  metadata: {},
};

function detail(row: RunRow) {
  return {
    ...row,
    conversation_owner_display_name:
      row.conversation_owner_display_name !== undefined
        ? row.conversation_owner_display_name
        : row.conversation_id
          ? "ローカル利用者"
          : null,
    owned_by_me: row.owned_by_me !== undefined ? row.owned_by_me : row.conversation_id ? true : null,
    error_type: row.error_type !== undefined ? row.error_type : row.status === "error" ? "TimeoutError" : null,
    answer: row.answer !== undefined ? row.answer : row.answer_generated ? "受注番号を入力して登録します。" : null,
    answer_available: row.answer_generated,
    citations: row.citations ?? [CITATION],
    withheld_evidence_count: row.withheld_evidence_count ?? 0,
    answer_diagnostics: row.answer_generated ? { outcome: "answered", confidence: "high", execution_steps: [] } : null,
    // 処理の経過は実行の trace の API が返す span の段（共通の span の表。#3226。実行の詳細の応答には入れない）。
    // 回答を作った実行は回答の段の中に工程の span、回答を作らない検索は検索の段。
    // 対象は実行（trace の API は実行の id を対象にする）。
    progress_events: savedSpanProgress(
      row.run_id,
      row.answer_generated
        ? [
            { key: "root", spec: ragSpan.root(), at: [0, 1.2] },
            { key: "answer", spec: ragSpan.stage("answer"), at: [0, 1.1] },
            {
              key: "understand",
              spec: ragSpan.step("質問の理解"),
              at: [0, 0.3],
              // 送り直しで API への送信が呼び出しより多い工程（#2966）。
              params: { llm_calls: 1, llm_sends: 2 },
            },
            {
              key: "search",
              spec: ragSpan.step("文書検索"),
              at: [0.3, 0.8],
              details: {
                fields: [
                  { label: "検索の候補", value: "12 件（3 文書）" },
                  { label: "候補の経路", value: "ベクトルと全文の両方 4・ベクトルだけ 6・全文だけ 2" },
                  { label: "結果", value: "回答に渡す根拠 4 件（2 文書）" },
                ],
                lines: ["回答に渡す根拠:", "1. 文書 A › 第 1 章（p.3）"],
              },
            },
            // 段の詳細（見出しと値・行。#2341）。
            {
              key: "guardrail",
              spec: ragSpan.stage("answer_guardrail"),
              at: [1.1, 1.14],
              details: { fields: [{ label: "結果", value: "問題なし" }] },
            },
          ]
        : [
            { key: "root", spec: ragSpan.root(false), at: [0, 1.1], status: row.status === "error" ? "failed" : "done" },
            {
              key: "retrieval",
              spec: ragSpan.stage("retrieval"),
              at: [0, 1.1],
              status: row.status === "error" ? "failed" : "done",
            },
          ],
      { base: "2026-10-15T00:00:00Z" }
    ),
    feedback: row.answer_generated
      ? [
          {
            feedback_id: "fb-1",
            target_type: "answer",
            rating: "helpful",
            reason: null,
            document_id: null,
            created_at: "2026-10-15T01:00:00Z",
          },
        ]
      : [],
  };
}

async function mockRuns(page: Page, rows: RunRow[], { gated = false } = {}) {
  let release: () => void = () => undefined;
  const gate = gated ? new Promise<void>((resolve) => (release = resolve)) : Promise.resolve();
  const requests: URLSearchParams[] = [];
  await page.route("**/api/history/runs**", async (route) => {
    const url = new URL(route.request().url());
    const traceMatch = url.pathname.match(/^\/api\/history\/runs\/([^/]+)\/trace$/);
    if (traceMatch) {
      // 実行の trace の API（共通の span の表の snapshot。#3226）。
      const found = rows.find((item) => item.run_id === decodeURIComponent(traceMatch[1]));
      await route.fulfill(
        found
          ? { json: traceSnapshotJson(detail(found).progress_events) }
          : { status: 404, json: { data: null, error_messages: ["実行が見つかりません。"], warning_messages: [] } }
      );
      return;
    }
    const detailMatch = url.pathname.match(/^\/api\/history\/runs\/(.+)$/);
    if (detailMatch) {
      const found = rows.find((item) => item.run_id === decodeURIComponent(detailMatch[1]));
      const { progress_events: _trace, ...body } = found ? detail(found) : { progress_events: null };
      await route.fulfill(
        found
          ? { json: apiEnvelope(body) }
          : {
              status: 404,
              json: { data: null, error_messages: ["実行が見つかりません。"], warning_messages: [] },
            }
      );
      return;
    }
    requests.push(url.searchParams);
    await gate;
    const q = url.searchParams.get("q") ?? "";
    const surface = url.searchParams.get("surface");
    const status = url.searchParams.get("status");
    const executor = url.searchParams.get("executor");
    const since = url.searchParams.get("since");
    const matched = rows.filter(
      (item) =>
        (!q || (item.question ?? "").includes(q) || item.run_id === q) &&
        (!surface || item.surface === surface) &&
        (!status || item.status === status) &&
        (!executor || (executor === "mine" ? item.executed_by_me : !item.executed_by_me)) &&
        (!since || item.created_at >= since)
    );
    const limit = Number(url.searchParams.get("limit") ?? PAGE_SIZE);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    await route.fulfill({
      json: apiEnvelope({
        // 一覧の行は利用量の全体ではなく、合計の token と計測の状態だけ（backend の JSON_VALUE と同じ。#3466）。
        items: matched.slice(offset, offset + limit).map(({ usage, ...item }) => ({
          ...item,
          total_tokens: usage ? usage.total_tokens : null,
          usage_measurement: usage ? usage.measurement : null,
        })),
        total: matched.length,
        limit,
        offset,
        has_next: offset + limit < matched.length,
      }),
    });
  });
  // 件数の「全 M 件」・他の人の実行の有無を数える 1 件の取得（limit=1）を除いた、一覧の取得。
  const listRequests = () => requests.filter((params) => params.get("limit") !== "1");
  return { release, requests, listRequests };
}

function isNarrow(page: Page) {
  return (page.viewportSize()?.width ?? 1440) < 768;
}

/** ツールバーの絞り込みを選ぶ（375px は「絞り込み」のシートを開いてから選び、閉じる）。 */
async function chooseFilter(page: Page, name: string, option: string) {
  const toolbar = page.getByTestId("run-history-toolbar");
  const sheet = (await toolbar.getAttribute("data-list-toolbar-filters-layout")) === "sheet";
  if (sheet) await toolbar.getByRole("button", { name: /^絞り込み/ }).click();
  await page.getByRole("combobox", { name }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
  if (sheet) await page.getByRole("button", { name: "絞り込みを閉じる" }).click();
}

/** 表頭は固定され、表示行数（md 未満 5 行・md 以上 8 行）を超えた行は表の中で縦スクロールする。 */
async function expectScrollsInsideTable(page: Page, region: Locator) {
  await expect(region).toBeVisible();
  const expectedRows = isNarrow(page) ? 5 : 8;
  const metrics = await region.evaluate((element) => {
    const header = element.querySelector("thead")!;
    const rows = Array.from(element.querySelectorAll('tbody tr[data-row-kind="data"]'));
    const visible = rows.filter((row) => row.getBoundingClientRect().bottom <= element.getBoundingClientRect().bottom + 1);
    return {
      sticky: getComputedStyle(header).position,
      scrollable: element.scrollHeight > element.clientHeight + 1,
      visibleRows: visible.length,
    };
  });
  expect(metrics.sticky).toBe("sticky");
  expect(metrics.scrollable).toBe(true);
  if (isNarrow(page)) expect(metrics.visibleRows).toBe(expectedRows);
  else expect(metrics.visibleRows).toBeGreaterThanOrEqual(expectedRows);
}

test.describe("実行履歴", { tag: "@responsive" }, () => {
  test("system_admin: 読み込み中は経過時間と表の Skeleton、全員の実行と実行者の列、ページ番号で移りページは再読込でも残る", async ({
    page,
  }) => {
    await mockLocalAuth(page);
    const rows = Array.from({ length: 55 }, (_, index) =>
      run(index + 1, {
        executed_by_me: index === 0,
        executor_display_name: index === 0 ? "ローカル利用者" : index % 3 === 2 ? "" : `利用者 ${index + 1}`,
        status: index === 4 ? "blocked" : index === 6 ? "error" : "success",
      })
    );
    const { release, listRequests } = await mockRuns(page, rows, { gated: true });

    await page.goto("/history");
    const loading = page.getByTestId("run-history-loading");
    await expect(loading).toBeVisible();
    await expect(loading).toContainText("実行履歴を読み込んでいます");
    await expect(loading.getByRole("timer")).toBeVisible();
    await expect(loading.locator('[data-skeleton="table"]')).toBeVisible();
    await expectNoPageOverflow(page);
    release();

    await expect(loading).toHaveCount(0);
    await expect(page.getByTestId("run-history-scope")).toContainText("すべての利用者の実行");
    const table = page.getByRole("table", { name: "実行履歴" });
    // 状態は共通の値（色・文言に頼らない）。ブロックの文言は RAG の言い方。
    await expect(page.getByTestId("run-history-row-run-1").locator('[data-run-status="succeeded"]:visible')).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-5").locator('[data-run-status="blocked"]:visible')).toContainText(
      "安全チェックで停止"
    );
    await expect(page.getByTestId("run-history-row-run-7").locator('[data-run-status="failed"]:visible')).toBeVisible();
    // 題名は詳細へのリンク（新しいタブでも開ける）。下に検索・回答プロファイル。
    const title = page.getByTestId("run-history-row-run-1").locator('[data-run-id="run-1"]');
    await expect(title).toHaveAttribute("href", /\/history\/run-1$/);
    await expect(page.getByTestId("run-history-row-run-1")).toContainText("受注業務");

    if (isNarrow(page)) {
      // 375px: 表は題名だけの列で、状態・日時・所要時間・種類・実行者を題名の下の 1 行にまとめる。
      const meta = page.getByTestId("run-history-row-run-2").locator("[data-run-history-compact-meta]");
      await expect(meta).toBeVisible();
      await expect(meta).toContainText("利用者 2");
      await expect(meta).toContainText("RAG 検索");
      await expect(table.getByRole("columnheader", { name: "実行者" })).toBeHidden();
    } else {
      await expect(page.getByTestId("run-history-row-run-1").locator("[data-run-history-compact-meta]")).toBeHidden();
      await expect(table.getByRole("columnheader", { name: "実行者" })).toBeVisible();
      await expect(table.getByRole("columnheader", { name: "開始日時" })).toBeVisible();
      await expect(table.getByRole("columnheader", { name: "回答の対応" })).toBeVisible();
      // 自分の行は「自分」、引けない利用者は UUID を出さずに「削除済みのユーザー」。
      await expect(page.getByTestId("run-history-row-run-1")).toContainText("自分");
      await expect(page.getByTestId("run-history-row-run-2")).toContainText("利用者 2");
      await expect(page.getByTestId("run-history-row-run-3")).toContainText("（削除済みのユーザー）");
      await expect(page.getByTestId("run-history-row-run-1")).toContainText("照合済み");
      await expect(page.getByTestId("run-history-row-run-2")).toContainText("回答なし（検索のみ）");
    }
    await expectScrollsInsideTable(page, page.getByRole("region", { name: "実行履歴" }));
    expect(listRequests().at(-1)?.get("limit")).toBe(String(PAGE_SIZE));
    // 期間の既定は 3 製品で共通の過去 7 日（#3386）。
    const since = listRequests().at(-1)?.get("since");
    expect(since).toBeTruthy();
    expect(sinceAgeMs(since)).toBeGreaterThan(7 * DAY_MS - HOUR_MS);
    expect(sinceAgeMs(since)).toBeLessThan(7 * DAY_MS + HOUR_MS);

    const pagination = page.getByTestId("run-history-pagination");
    await expect(pagination).toContainText("1–25 / 55 件");
    await jumpToPage(pagination, 2);
    await expect(pagination).toContainText("26–50 / 55 件");
    await expect(page.getByTestId("run-history-row-run-26")).toBeVisible();
    expect(listRequests().at(-1)?.get("offset")).toBe("25");
    await expectNoPageOverflow(page);

    await page.reload();
    await expect(page.getByTestId("run-history-pagination")).toContainText("26–50 / 55 件");
  });

  test("一般の利用者: 他の人が実行した行があるときだけ実行者の列と絞り込みを出し、実行者で絞れる。ナビの「AI 活用」に History のアイコンで並ぶ", async ({
    page,
  }) => {
    await mockDatabaseReady(page);
    await mockAuthUser(page, { permissions: ["menu.chat", "menu.search", "menu.history"] });
    const adminRun = run(3, {
      question: "管理者が続けた質問",
      executed_by_me: false,
      executor_display_name: "システム管理者",
    });
    const { listRequests } = await mockRuns(page, [run(1), run(2), adminRun]);

    await page.goto("/history");
    await expect(page.getByTestId("run-history-scope")).toContainText("自分の会話で他の人が実行した実行");
    await expect(page.getByTestId("run-history-row-run-3")).toContainText("システム管理者");
    // 1 ページしかないときはページングを出さない。
    await expect(page.getByTestId("run-history-pagination")).toHaveCount(0);
    await expectNoPageOverflow(page);

    // 実行者の絞り込み（既定はすべて）。選んだ値は API の executor と作業状態に残す。
    expect(listRequests().at(-1)?.get("executor")).toBeNull();
    await chooseFilter(page, "実行者", "他の人が実行");
    await expect(page.getByTestId("run-history-row-run-3")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-1")).toHaveCount(0);
    expect(listRequests().at(-1)?.get("executor")).toBe("others");
    await expect(page.getByTestId("run-history-toolbar")).toContainText("1 件（全 3 件）");
    await page.reload();
    await expect(page.getByTestId("run-history-row-run-1")).toHaveCount(0);
    await chooseFilter(page, "実行者", "自分が実行");
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-3")).toHaveCount(0);
    expect(listRequests().at(-1)?.get("executor")).toBe("mine");

    const sidebar = await openSidebarNav(page);
    const link = sidebar.getByRole("link", { name: "実行履歴" });
    await expect(link).toHaveAttribute("href", /\/history$/);
    await expect(link.locator("svg").first()).toHaveClass(/lucide-history/);
    const useItems = sidebar.getByRole("link").filter({ hasText: /^(チャット|RAG 検索|実行履歴)$/ });
    await expect(useItems).toHaveText(["チャット", "RAG 検索", "実行履歴"]);
  });

  test("一般の利用者: 自分の実行だけなら実行者の列と絞り込みを出さない", async ({ page }) => {
    await mockDatabaseReady(page);
    await mockAuthUser(page, { permissions: ["menu.chat", "menu.search", "menu.history"] });
    await mockRuns(page, [run(1), run(2)]);

    await page.goto("/history");
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await expect(page.getByRole("table", { name: "実行履歴" }).getByRole("columnheader", { name: "実行者" })).toHaveCount(0);
    await expect(page.locator("#run-history-executor")).toHaveCount(0);
  });

  test("絞り込み: 期間の既定は過去 7 日。状態・種類・期間・検索はサーバーで絞り、条件で 0 件なら「絞り込みをクリア」、ツールバーの「条件をクリア」で既定へ戻す", async ({
    page,
  }) => {
    await mockLocalAuth(page);
    const recent = new Date(Date.now() - HOUR_MS).toISOString();
    const { listRequests } = await mockRuns(page, [
      run(1, { created_at: recent }),
      run(2, { status: "error", question: "失敗した質問", created_at: recent }),
      run(3, { created_at: new Date(Date.now() - 20 * DAY_MS).toISOString() }),
    ]);

    await page.goto("/history");
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    // 既定の過去 7 日より前の実行は出さず、件数で全件があることを伝える。既定のままなら「条件をクリア」を出さない（#3386）。
    await expect(page.getByTestId("run-history-row-run-3")).toHaveCount(0);
    expect(sinceAgeMs(listRequests().at(-1)?.get("since"))).toBeGreaterThan(7 * DAY_MS - HOUR_MS);
    const toolbar = page.getByTestId("run-history-toolbar");
    await expect(toolbar).toContainText("2 件（全 3 件）");
    await expect(toolbar.getByRole("button", { name: "条件をクリア" })).toHaveCount(0);
    if (!isNarrow(page)) await expect(page.getByRole("combobox", { name: "期間" })).toContainText("過去 7 日");

    await chooseFilter(page, "状態", "失敗");
    await expect(page.getByTestId("run-history-row-run-2")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-1")).toHaveCount(0);
    expect(listRequests().at(-1)?.get("status")).toBe("error");
    await expect(toolbar).toContainText("1 件（全 3 件）");

    await chooseFilter(page, "状態", "すべて");
    await chooseFilter(page, "種類", "チャット");
    expect(listRequests().at(-1)?.get("surface")).toBe("chat");
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-2")).toHaveCount(0);

    // 期間は開始の時刻（ISO 8601）を since で送る。
    await chooseFilter(page, "期間", "過去 24 時間");
    const since = listRequests().at(-1)?.get("since");
    expect(sinceAgeMs(since)).toBeGreaterThan(23 * HOUR_MS);
    expect(sinceAgeMs(since)).toBeLessThan(25 * HOUR_MS);
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-3")).toHaveCount(0);

    // 条件をすべて既定へ戻す（ツールバーの「条件をクリア」）。期間は過去 7 日へ戻る。
    await toolbar.getByRole("button", { name: "条件をクリア" }).click();
    await expect(page.getByTestId("run-history-row-run-2")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-3")).toHaveCount(0);
    expect(listRequests().at(-1)?.get("surface")).toBeNull();
    expect(sinceAgeMs(listRequests().at(-1)?.get("since"))).toBeGreaterThan(7 * DAY_MS - HOUR_MS);

    // 「すべて」は既定からの変更として「条件をクリア」を出し、再読込の後も残す。
    await chooseFilter(page, "期間", "すべて");
    await expect(page.getByTestId("run-history-row-run-3")).toBeVisible();
    expect(listRequests().at(-1)?.get("since")).toBeNull();
    await expect(toolbar).toContainText("全 3 件");
    await expect(toolbar.getByRole("button", { name: "条件をクリア" })).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("run-history-row-run-3")).toBeVisible();
    expect(listRequests().at(-1)?.get("since")).toBeNull();

    // 実行 ID でも探せる。
    const search = page.getByRole("searchbox", { name: "質問・実行 ID で絞り込み" });
    await search.fill("run-2");
    await expect(page.getByTestId("run-history-row-run-2")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-1")).toHaveCount(0);

    await search.fill("存在しない");
    await expect(page.getByText("条件に一致する実行がありません")).toBeVisible();
    expect(listRequests().at(-1)?.get("q")).toBe("存在しない");
    await page.getByRole("button", { name: "絞り込みをクリア" }).click();
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await expect(page.getByTestId("run-history-row-run-3")).toHaveCount(0);
    await expectNoPageOverflow(page);
  });

  test("過去 7 日に実行が無く、それより前にはあるときは、期間を「すべて」にする操作を出す（#3386）", async ({ page }) => {
    await mockLocalAuth(page);
    const { listRequests } = await mockRuns(page, [run(1, { created_at: new Date(Date.now() - 20 * DAY_MS).toISOString() })]);
    await page.goto("/history");
    const empty = page.getByTestId("run-history-period-empty");
    await expect(empty).toContainText("過去 7 日の実行はありません");
    await expect(empty).toContainText("期間を「すべて」にすると表示します");
    await expect(page.getByText("実行はまだありません")).toHaveCount(0);
    await expectNoPageOverflow(page);
    await empty.getByRole("button", { name: "すべての期間を表示" }).click();
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    expect(listRequests().at(-1)?.get("since")).toBeNull();
  });

  test("実行がまだ無いときの空の状態", async ({ page }) => {
    await mockLocalAuth(page);
    await mockRuns(page, []);
    await page.goto("/history");
    await expect(page.getByText("実行はまだありません")).toBeVisible();
    await expectNoPageOverflow(page);
  });

  test("詳細: 長いファイル名の根拠の行は名前を省略して頁は残し、回答のカードが横にはみ出さない（#3353）", async ({ page }, testInfo) => {
    await mockLocalAuth(page);
    const longName = "合成の文書_長いファイル名の例_第03版_項目の一覧と確認の方法_2026年度_改訂.pdf";
    const answer = [
      "合成の手続きは 2 段階です。",
      "",
      "資料により確認できた事項",
      "",
      "・対象の行の終了日を入力します。",
      `根拠：${longName} p.12`,
      "・新しい開始日の行を追加します。",
      `根拠：${longName} p.13`,
    ].join("\n");
    const citation = { ...CITATION, file_name: longName, metadata: { page: 12 } };
    await mockRuns(page, [run(1, { answer, citations: [citation] })]);

    await page.goto("/history/run-1");
    const card = page.getByTestId("run-history-answer");
    const link = card.getByTestId("answer-citation-link").first();
    await expect(link).toBeVisible();
    // 全文は読み上げの名前に残す（省略しても何の根拠か分かる。省略したときは全文の Tooltip も出す）。
    await expect(link).toHaveAccessibleName(new RegExp(`${longName} p\\.12`));
    // 根拠の行はカードの幅に収め、長い名前は末尾を省略する（本文は折り返す）。
    const metrics = await card.evaluate((element) => {
      const cardBox = element.getBoundingClientRect();
      const button = element.querySelector<HTMLElement>('[data-testid="answer-citation-link"]')!;
      const label = button.querySelector<HTMLElement>("span") ?? button;
      const location = button.querySelector<HTMLElement>(":scope > span:last-child") ?? button;
      const locationBox = location.getBoundingClientRect();
      return {
        locationVisible: locationBox.width > 0 && locationBox.right <= cardBox.right + 1 && location.scrollWidth <= location.clientWidth + 1,
        cardOverflow: element.scrollWidth - element.clientWidth,
        buttonRight: button.getBoundingClientRect().right - cardBox.right,
        truncated: label.scrollWidth > label.clientWidth,
      };
    });
    expect(metrics.cardOverflow).toBeLessThanOrEqual(1);
    expect(metrics.buttonRight).toBeLessThanOrEqual(1);
    expect(metrics.locationVisible).toBe(true);
    // 頁は省略しない（名前だけを省略する）。
    await expect(link.locator(":scope > span").last()).toHaveText("p.12");
    if (isNarrow(page)) {
      expect(metrics.truncated).toBe(true);
      await expect(link).toHaveAttribute("data-truncated", "true");
    }
    await card.screenshot({ path: testInfo.outputPath("answer-long-citations.png") });
    await expectNoPageOverflow(page);
  });

  test("詳細: 概要 → 処理の経過（処理の詳細を含む）→ 回答 → 根拠 → フィードバックの 1 枚。一覧へ戻ると条件・ページと開いた行が戻る", async ({
    page,
  }, testInfo) => {
    await mockLocalAuth(page);
    const rows = Array.from({ length: 30 }, (_, index) => run(index + 1));
    await mockRuns(page, rows);

    await page.goto("/history");
    await chooseFilter(page, "種類", "チャット");
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    await page.getByTestId("run-history-pagination").getByRole("combobox", { name: "表示件数" }).click();
    await page.getByRole("option", { name: "10 件" }).click();
    await jumpToPage(page.getByTestId("run-history-pagination"), 2);
    await expect(page.getByTestId("run-history-pagination")).toContainText("11–15 / 15 件");
    await page.getByTestId("run-history-row-run-21").locator('[data-run-id="run-21"]').click();
    await expect(page).toHaveURL(/\/history\/run-21$/);

    const detailRoot = page.getByTestId("run-history-detail");
    const sections = await detailRoot.evaluate((element) =>
      Array.from(element.children).map((child) => child.getAttribute("data-testid"))
    );
    expect(sections).toEqual([
      "run-history-summary",
      "run-history-timeline",
      "run-history-answer",
      "run-history-evidence",
      "run-history-feedback",
    ]);
    // タブは無い（根拠・処理の経過をページ内検索・読み上げで届くように）。
    await expect(page.getByRole("tablist")).toHaveCount(0);

    const summary = page.getByTestId("run-history-summary");
    await expect(summary.locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
    await expect(summary.locator("[data-run-status]")).toHaveCount(0);
    await summary.screenshot({path: testInfo.outputPath("summary-without-badges.png")});
    await expect(summary).toContainText("受注の登録方法 21");
    await expect(summary.locator("[data-run-summary-heading]")).not.toContainText("照合済み");
    await expect(summary.locator('[data-run-fact="target"]')).toContainText("受注業務");
    await expect(summary.locator('[data-run-fact="executor"] dd')).toHaveText("自分");
    // 自分が実行した自分の会話は「会話の持ち主」を出さない。
    await expect(summary.locator('[data-run-fact="owner"]')).toHaveCount(0);
    await expect(page.getByTestId("run-history-open-chat")).toHaveAttribute(
      "href",
      /\/chat\?search_answer_profile_id=bv-1&conversation_id=conv-21$/
    );
    await expect(page.getByTestId("run-history-answer")).toContainText("受注番号を入力して登録します。");
    // 根拠はチャットと同じ部品（「AI に渡した N 件 …」の折りたたみ。既定は閉じる。#2328）。
    const evidence = page.getByTestId("run-history-evidence").getByTestId("answer-citations");
    await expect(evidence.locator("summary")).toContainText("AI に渡した");
    await expect(evidence).not.toHaveAttribute("open", "");
    await evidence.locator("summary").click();
    await expect(page.getByTestId("run-history-citations")).toContainText("受注マニュアル.pdf");
    await expect(page.getByTestId("run-history-citations")).toHaveClass(/bounded-scroll-area-lg/);

    // 処理の経過: 既定は「N ステップ・合計 M 秒」の 1 行に閉じる（回答の根拠・チャットの処理の経過と同じ開閉。#2473）。
    // 段の数は入れ子の工程も数える。開くと段と、回答フローの工程（字下げ）。所要時間は文字でも出す。
    const timeline = page.getByTestId("run-history-timeline");
    const timelineSummary = page.getByTestId("run-history-timeline-summary");
    await expect(timelineSummary).toHaveText("4 ステップ・合計 1.2 秒");
    await expect(timeline.locator("details[data-run-timeline-section]")).not.toHaveAttribute("open", "");
    await expect(timeline.locator("[data-run-timeline-step]").first()).toBeHidden();
    await timelineSummary.click();
    // 行は span の木（#3226）: 包む段（質問の条件を整理して回答を作る）と、その子の工程（字下げ）。
    await expect(timeline.locator("[data-run-timeline-step]")).toHaveCount(4);
    await expect(timeline.locator("[data-run-timeline-step]").nth(0)).toContainText("質問の条件を整理して回答を作る");
    await expect(timeline.locator("[data-run-timeline-step]").nth(1)).toHaveAttribute("data-level", "1");
    await expect(timeline.locator("[data-run-timeline-step]").nth(1)).toContainText("0.3 秒");
    await expect(timeline.locator("[data-run-timeline-step]").nth(1).locator("[data-run-result-summary]")).toContainText("LLM 1 回・送信 2 回");
    await expect(timeline.locator("[data-run-timeline-step]").nth(3)).toContainText("回答に安全上の問題がないか検査する");
    if (!isNarrow(page)) await expect(timeline.locator("[data-run-timeline-bar]").first()).toBeVisible();
    // 段の詳細（#2341）: 行を開くと工程の詳細と技術の値（span の名前）を出す（既定で閉じる）。
    const steps = timeline.locator("[data-run-timeline-step]");
    await inspectTraceStep(page, timeline, (await steps.nth(1).getAttribute("data-run-timeline-step"))!);
    await expect(page.locator("[data-trace-technical]")).toContainText("rag.engine.stage");
    await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
    const searchToggle = steps.nth(2).locator("[data-run-timeline-toggle]");
    await expect(searchToggle).toHaveAttribute("aria-expanded", "false");
    await searchToggle.click();
    await expect(searchToggle).toHaveAttribute("aria-expanded", "true");
    const searchDetails = steps.nth(2).locator("[data-run-timeline-details]");
    await expect(searchDetails.locator("dt").first()).toHaveText("検索の候補");
    await expect(searchDetails).toContainText("12 件（3 文書）");
    await expect(searchDetails).toContainText("1. 文書 A › 第 1 章（p.3）");
    // 文字の横のアイコン（状態・開閉の印）は、文字の 1 行目の中心にそろえる（#2371）。
    expect(await findIconTextMisalignment(page, { root: '[data-testid="run-history-timeline"]' })).toEqual([]);
    await expectNoPageOverflow(page);
    await searchToggle.click();
    await expect(searchDetails).toBeHidden();
    const guardrailToggle = steps.nth(3).locator("[data-run-timeline-toggle]");
    await guardrailToggle.click();
    await expect(steps.nth(3).locator("[data-run-timeline-details]")).toContainText("問題なし");

    await expect(page.getByTestId("run-history-feedback-open-fb-1")).toHaveAttribute(
      "href",
      /\/feedback\?feedback=fb-1&period=all$/
    );
    // 処理の詳細（回答の記録の詳細と識別子。チャットの処理の経過と同じ名前・部品）は、処理の経過の節の中のタイムラインの
    // 後ろに既定で閉じて置く。根拠の節の後ろやページの末尾に別の節として出さない（#2325）。
    const technical = timeline.getByTestId("run-history-technical");
    const processDetailsSummary = page.getByTestId("run-history-process-details");
    await expect(processDetailsSummary).toHaveText("処理の詳細");
    const lastStepBox = await timeline.locator("[data-run-timeline-step]").last().boundingBox();
    const detailsSummaryBox = await processDetailsSummary.boundingBox();
    expect(lastStepBox && detailsSummaryBox && detailsSummaryBox.y > lastStepBox.y).toBeTruthy();
    await expect(technical.getByText("conv-21")).toBeHidden();
    await processDetailsSummary.click();
    await expect(technical.getByText("conv-21")).toBeVisible();
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value: string) => { document.documentElement.dataset.copiedId = value; } } }));
    for (const [label, value] of [["Trace ID", SPAN_TRACE_ID], ["会話 ID", "conv-21"], ["回答メッセージ ID", "msg-21"]]) {
      await technical.getByRole("button", { name: `${label} をコピー`, exact: true }).click();
      await expect.poll(() => page.evaluate(() => document.documentElement.dataset.copiedId)).toBe(value);
    }
    // 「処理の詳細」の先頭に Trace ID（「トレースの詳細」の 2 つ目の表示は無い。#3226）。
    await expect(technical.locator("[data-trace-id-row]")).toContainText(SPAN_TRACE_ID);
    await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
    await expect(technical.getByText("信頼度: high")).toHaveCount(0);
    await expect(technical.locator("[data-process-identifier]")).toHaveCount(3);
    await expect(technical.getByRole("button", { name: "会話 ID をコピー", exact: true })).toBeVisible();
    await expect(technical.getByRole("button", { name: "回答メッセージ ID をコピー", exact: true })).toBeVisible();
    await expect(technical.locator("[data-run-used-models]")).toBeVisible();
    // 信頼度は回答の記録の詳細のバッジだけに出し、識別子の一覧に重ねない。旧「回答の詳細」「技術的な詳細」は出さない。
    await expect(technical.getByText("回答の確からしさ", { exact: true })).toHaveCount(0);
    await expect(page.getByText("回答の詳細", { exact: true })).toHaveCount(0);
    await expect(page.getByText("技術的な詳細", { exact: true })).toHaveCount(0);
    await expect(page.getByTestId("run-history-evidence").getByText("信頼度: high")).toHaveCount(0);
    await expectNoPageOverflow(page);

    await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
    await expect(page).toHaveURL(/\/history$/);
    await expect(page.getByTestId("run-history-pagination")).toContainText("11–15 / 15 件");
    await expect(page.getByTestId("run-history-row-run-21").locator('[data-run-id="run-21"]')).toHaveAttribute(
      "aria-current",
      "true"
    );
    await expect(page.getByTestId("run-history-row-run-1")).toHaveCount(0);
  });

  test("詳細: 回答を作らない RAG 検索は根拠だけ、失敗は理由の Banner、見えない実行は理由を出す", async ({ page }) => {
    await mockLocalAuth(page);
    await mockRuns(page, [
      run(2),
      run(4, { status: "error", error_stage: "retrieval", error_type: "DatabaseError", error_code: "ORA-03146" }),
      // 回答は作ったが会話に保存できなかった実行（#2342）。
      run(6, {
        storage_failures: [{ step: "chat_message_save", error_type: "DatabaseError", error_code: "ORA-40441" }],
      }),
    ]);

    await page.goto("/history/run-2");
    const summary = page.getByTestId("run-history-summary");
    await expect(summary).toBeVisible();
    await expect(summary.locator('[data-run-fact="kind"] dd')).toHaveText("RAG 検索");
    await expect(summary.locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
    await expect(page.getByTestId("run-history-answer")).toHaveCount(0);
    await expect(page.getByTestId("run-history-citations")).toContainText("受注マニュアル.pdf");
    // 会話の無い実行には会話への導線を出さない。
    await expect(page.getByTestId("run-history-open-chat")).toHaveCount(0);

    await page.goto("/history/run-4");
    await expect(summary).toBeVisible();
    await expect(summary.locator("[data-run-status]")).toHaveCount(0);
    // 失敗は共通の失敗のカード: 止まった段を含む 1 文目・次の操作・開いた「詳細」（#2342）。
    const failure = summary.getByTestId("run-history-failure");
    await expect(failure.getByRole("alert")).toContainText("「文書から根拠の候補を検索する」で処理が止まりました。");
    await expect(failure).toContainText("実行 ID を添えて管理者に連絡してください");
    await expect(failure).toContainText("ORA-03146");
    await expect(summary.locator('[data-run-fact="errorStage"]')).toContainText("検索");
    // 技術的な詳細（処理の経過の中の処理の詳細に畳む）にも同じ名前でエラーコードを出す。
    await page.getByTestId("run-history-timeline-summary").click();
    await page.getByTestId("run-history-process-details").click();
    await expect(page.getByTestId("run-history-technical").locator('[data-run-fact="failure-errorCode"]')).toHaveCount(0);
    await expect(page.getByTestId("run-history-timeline").locator('[data-status="failed"]')).toContainText("失敗");
    await expect(page.getByTestId("run-history-evidence")).toHaveCount(0);

    await page.goto("/history/run-6");
    const storage = page.getByTestId("run-history-storage-failure");
    await expect(storage).toContainText("回答は表示していますが、一部を保存できませんでした。");
    await expect(storage).toContainText("会話に保存できなかったため");
    await expect(storage.getByRole("alert")).toHaveCount(0);

    await page.goto("/history/run-missing");
    await expect(page.getByText("実行が見つかりません。")).toBeVisible();
  });

  test("詳細: 概要にモデル・token・モデルの呼び出しを Agent と同じ形で出し、不明は未計測・一部計測と出す（Issue 3419）", async ({
    page,
  }, testInfo) => {
    await mockLocalAuth(page);
    const usage = {
      models: ["model-a", "model-rewrite"],
      requests: 4,
      input_tokens: 12000,
      output_tokens: 1500,
      total_tokens: 13500,
      cached_input_tokens: 2000,
      reasoning_output_tokens: 600,
      measurement: "complete" as const,
      measured_requests: 4,
    };
    await mockRuns(page, [
      // チャット（既定のモデルも実行のときの具体的な ID）。会話の履歴の書き換えのモデルも並べる。
      run(1, { model_id: "model-a", usage }),
      // RAG 検索（回答を作る）。一部の呼び出しの利用量が無い。
      run(2, {
        answer_generated: true,
        model_id: "model-a",
        usage: { ...usage, models: ["model-a"], requests: 3, total_tokens: 9000, measurement: "partial", measured_requests: 2 },
      }),
      // 回答を作らない RAG 検索で LLM を呼ばなかった実行は実測の 0。
      run(4, {
        usage: { ...usage, models: [], requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, measured_requests: 0 },
      }),
      // この機能より前の記録: 既定のモデルの文言を出さず、利用量は未計測。
      run(6, { answer_generated: true }),
    ]);
    const summary = page.getByTestId("run-history-summary");
    const fact = (id: string) => summary.locator(`[data-run-fact="${id}"] dd`);
    const factOrder = () =>
      summary.locator("[data-run-fact]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-run-fact")));

    await page.goto("/history/run-1");
    await expect(fact("model")).toHaveText("model-a、model-rewrite");
    await expect(fact("tokens")).toHaveText("13,500 token");
    await expect(fact("modelCalls")).toHaveText("4 回（入力 12,000 / 出力 1,500 token）");
    expect(await factOrder()).toEqual(
      expect.arrayContaining(["target", "model", "tokens", "modelCalls", "citations", "runId"])
    );
    const order = await factOrder();
    expect(order.indexOf("model")).toBe(order.indexOf("target") + 1);
    expect(order.slice(order.indexOf("model"), order.indexOf("model") + 3)).toEqual(["model", "tokens", "modelCalls"]);
    await expectNoPageOverflow(page);
    await summary.screenshot({ path: testInfo.outputPath("summary-usage-chat.png") });

    await page.goto("/history/run-2");
    await expect(fact("kind")).toHaveText("RAG 検索");
    await expect(fact("model")).toHaveText("model-a");
    await expect(fact("tokens")).toHaveText("9,000 token（一部計測）");
    await expect(fact("modelCalls")).toHaveText("3 回（うち 2 回を計測。入力 12,000 / 出力 1,500 token）");
    await expectNoPageOverflow(page);
    await summary.screenshot({ path: testInfo.outputPath("summary-usage-search.png") });

    await page.goto("/history/run-4");
    await expect(fact("kind")).toHaveText("RAG 検索");
    await expect(summary.locator('[data-run-fact="model"]')).toHaveCount(0);
    await expect(fact("tokens")).toHaveText("0 token");
    await expect(fact("modelCalls")).toHaveText("0 回（入力 0 / 出力 0 token）");

    await page.goto("/history/run-6");
    await expect(fact("kind")).toHaveText("RAG 検索");
    await expect(summary.locator('[data-run-fact="model"]')).toHaveCount(0);
    await expect(summary).not.toContainText("既定のテキストモデル");
    await expect(fact("tokens")).toHaveText("未計測");
    await expect(fact("modelCalls")).toHaveText("未計測");
  });

  test("一覧: token の列に合計の token を右寄せ・桁区切りで出し、不明は未計測・一部計測と出す。375px は列を出さない（Issue 3466）", async ({
    page,
  }, testInfo) => {
    await mockLocalAuth(page);
    const usage = {
      models: ["model-a"],
      requests: 4,
      input_tokens: 1200000,
      output_tokens: 34567,
      total_tokens: 1234567,
      cached_input_tokens: null,
      reasoning_output_tokens: null,
      measurement: "complete" as const,
      measured_requests: 4,
    };
    await mockRuns(page, [
      run(1, { usage }),
      // 一部の呼び出しの利用量が無い実行。
      run(2, { usage: { ...usage, total_tokens: 9000, measurement: "partial", measured_requests: 2 } }),
      // LLM を呼ばなかった RAG 検索は実測の 0。
      run(4, { usage: { ...usage, models: [], requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, measured_requests: 0 } }),
      // この機能より前の記録は未計測（0 にしない）。
      run(6),
    ]);

    await page.goto("/history");
    const table = page.getByRole("table", { name: "実行履歴" });
    await expect(page.getByTestId("run-history-row-run-1")).toBeVisible();
    const header = table.getByRole("columnheader", { name: "token", exact: true });
    const headers = await table.locator("thead th").allTextContents();
    const usageIndex = headers.indexOf("token");
    // 列の順は 3 製品で同じ: 所要時間 → token →（製品の列）。見出しは Agent と同じ「token」。
    expect(usageIndex).toBe(headers.indexOf("所要時間") + 1);
    expect(headers.indexOf("回答の対応")).toBeGreaterThan(usageIndex);
    // 行の見出し（題名）は th、ほかは td。
    const usageCell = (id: string) => page.getByTestId(`run-history-row-${id}`).locator(":scope > th, :scope > td").nth(usageIndex);

    if (isNarrow(page)) {
      // 375px: 題名と操作の 2 列にまとめ、token の列は出さない（詳細の概要で見る）。横にあふれない。
      await expect(header).toBeHidden();
      await expect(usageCell("run-1")).toBeHidden();
      await expect(page.getByTestId("run-history-row-run-1").locator("[data-run-history-compact-meta]")).not.toContainText(
        "token"
      );
    } else {
      await expect(header).toBeVisible();
      await expect(usageCell("run-1")).toHaveText("1,234,567 token");
      await expect(usageCell("run-2")).toHaveText("9,000 token一部計測");
      await expect(usageCell("run-2").locator("[data-run-usage-note]")).toHaveText("一部計測");
      await expect(usageCell("run-4")).toHaveText("0 token");
      await expect(usageCell("run-6")).toHaveText("未計測");
      // 数は右寄せ・等幅数字（所要時間の列と同じ）。
      for (const cell of [header, usageCell("run-1"), usageCell("run-6")]) {
        await expect(cell).toHaveCSS("text-align", "right");
      }
      await expect(usageCell("run-1")).toHaveCSS("font-variant-numeric", "tabular-nums");
    }
    await expectNoPageOverflow(page);
    await page.getByTestId("run-history").screenshot({ path: testInfo.outputPath("list-usage.png") });
  });

  test("詳細: 持ち主の会話で system_admin が実行した実行は、持ち主に実行者と会話の持ち主を出し、会話を開ける", async ({
    page,
  }) => {
    await mockDatabaseReady(page);
    await mockAuthUser(page, { permissions: ["menu.chat", "menu.search", "menu.history"] });
    await mockRuns(page, [
      run(1, {
        executed_by_me: false,
        executor_display_name: "システム管理者",
        conversation_owner_display_name: "アリス",
        owned_by_me: true,
      }),
      // 持ち主でない会話の実行（会話の API は持ち主と system_admin に閉じている）には導線を出さない。
      run(3, { conversation_owner_display_name: "ボブ", owned_by_me: false }),
    ]);

    await page.goto("/history/run-1");
    const summary = page.getByTestId("run-history-summary");
    await expect(summary.locator('[data-run-fact="executor"] dd')).toHaveText("システム管理者");
    await expect(summary.locator('[data-run-fact="owner"] dd')).toHaveText("自分");
    await expect(page.getByTestId("run-history-open-chat")).toHaveAttribute(
      "href",
      /\/chat\?search_answer_profile_id=bv-1&conversation_id=conv-1$/
    );
    await expectNoPageOverflow(page);

    await page.goto("/history/run-3");
    await expect(summary.locator('[data-run-fact="owner"] dd')).toHaveText("ボブ");
    await expect(page.getByTestId("run-history-open-chat")).toHaveCount(0);
  });

  test("詳細: 本人の RAG 検索の実行は「RAG 検索で開く」で質問と条件を入れた画面を開き、検索はしない（Issue 3309）", async ({
    page,
  }) => {
    await mockDatabaseReady(page);
    await mockAuthUser(page, { permissions: ["menu.chat", "menu.search", "menu.history"] });
    const question = "合成の質問 A の手順は？";
    await mockRuns(page, [
      run(2, {
        question,
        search_answer_profile_id: "profile-a",
        search_answer_profile_name: "プロファイル A",
        answer_generated: true,
        search_conditions: { top_k: 100, filters: { large_category: "分類 A", as_of: "2026-04-01" } },
      }),
      // system_admin が見る他の人の RAG 検索の実行（#3244）には出さない。
      run(4, { executed_by_me: false, executor_display_name: "利用者 B" }),
    ]);
    await page.route("**/api/search-answer-profiles**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith("/approved-faq/suggest")) {
        await route.fulfill({ json: apiEnvelope({ suggestions: [] }) });
        return;
      }
      await route.fulfill({
        json: apiEnvelope({
          items: [
            {
              id: "profile-a",
              name: "プロファイル A",
              description: null,
              status: "ACTIVE",
              knowledge_base_count: 1,
              created_at: "2026-10-01T00:00:00Z",
              updated_at: "2026-10-01T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        }),
      });
    });
    const searchRequests: string[] = [];
    await page.route("**/api/search/stream", async (route) => {
      searchRequests.push(route.request().url());
      await route.abort();
    });

    await page.goto("/history/run-4");
    await expect(page.getByTestId("run-history-summary")).toBeVisible();
    await expect(page.getByTestId("run-history-open-search")).toHaveCount(0);

    await page.goto("/history/run-2");
    const open = page.getByTestId("run-history-open-search");
    await expect(open).toHaveText("RAG 検索で開く");
    await expect(open).toHaveAccessibleName("この実行の質問と条件を RAG 検索の画面に入れて開く");
    // RAG 検索の実行に「会話を開く」は出さない（どちらか 1 つ）。
    await expect(page.getByTestId("run-history-open-chat")).toHaveCount(0);
    // 見出しの右の操作は他の操作と同じ高さ（タッチ端末は 44px）。
    expect((await open.boundingBox())?.height).toBe(await expectedControlHeight(page));
    await expectNoPageOverflow(page);
    await open.click();

    // URL の初期値は一度だけ入れて外す（再読込で作業状態を上書きしない）。
    await expect(page).toHaveURL(/\/search$/);
    await expect(page.locator("#search-query")).toHaveValue(question);
    await expect(page.getByRole("button", { name: /検索・回答プロファイル/ })).toContainText("プロファイル A");
    await expect(page.getByRole("switch", { name: "LLM で回答を生成する" })).toHaveAttribute("aria-checked", "true");
    // 記録の条件が入り、詳細条件が開いて見える。
    await expect(page.locator("#search-top-k")).toHaveCount(0);
    await expect(page.locator("#search-as_of")).toHaveValue("2026-04-01");
    await expect(page.locator("#search-classification-filters")).toContainText("分類 A");
    await expectNoPageOverflow(page);
    // 検索はしない（利用者が「検索」を押す）。
    await page.waitForTimeout(300);
    expect(searchRequests).toEqual([]);

    await page.reload();
    await expect(page.locator("#search-query")).toHaveValue(question);
    expect(searchRequests).toEqual([]);
  });

  test("詳細: 検索・回答プロファイルが削除済みの実行の「RAG 検索で開く」は質問だけを入れ、選び直しを案内する（Issue 3355）", async ({
    page,
  }, testInfo) => {
    await mockDatabaseReady(page);
    await mockAuthUser(page, { permissions: ["menu.chat", "menu.search", "menu.history"] });
    const question = "合成の質問 B の手順は？";
    await mockRuns(page, [
      run(2, { question, search_answer_profile_id: "deleted-profile", search_answer_profile_name: null }),
    ]);
    const profileRequests: string[] = [];
    await page.route("**/api/search-answer-profiles**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.includes("deleted-profile")) profileRequests.push(pathname);
      if (pathname.endsWith("/approved-faq/suggest")) {
        await route.fulfill({ json: apiEnvelope({ suggestions: [] }) });
        return;
      }
      await route.fulfill({
        json: apiEnvelope({
          items: [
            {
              id: "profile-a",
              name: "プロファイル A",
              description: null,
              status: "ACTIVE",
              knowledge_base_count: 1,
              created_at: "2026-10-01T00:00:00Z",
              updated_at: "2026-10-01T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        }),
      });
    });

    await page.goto("/history/run-2");
    await page.getByTestId("run-history-open-search").click();

    await expect(page).toHaveURL(/\/search$/);
    await expect(page.locator("#search-query")).toHaveValue(question);
    const notice = page.getByTestId("search-prefill-profile-unavailable");
    await expect(notice).toHaveText("元の検索・回答プロファイルは使えません。選び直してください。");
    // 別の検索・回答プロファイルへ黙って切り替えない。
    await expect(page.getByRole("button", { name: /検索・回答プロファイル/ })).not.toContainText("プロファイル A");
    // 使えない検索・回答プロファイルの API は呼ばない。
    expect(profileRequests).toEqual([]);
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("search-profile-unavailable.png"), fullPage: true });

    // 選び直すと案内を消す。
    await page.getByRole("button", { name: /検索・回答プロファイル/ }).click();
    await page.getByRole("option", { name: /プロファイル A/ }).click();
    await expect(notice).toHaveCount(0);
    await expect(page.locator("#search-query")).toHaveValue(question);
  });
});

test.describe("実行履歴の処理の経過（共通の span の表。#3226）", { tag: "@responsive" }, () => {
  test("書けなかった span がある実行は「一部の処理だけを記録しています。」、古い形式の記録は古い形式の案内を出す", async ({
    page,
  }) => {
    await mockLocalAuth(page);
    const partial = run(1);
    const legacy = run(2);
    await mockRuns(page, [partial, legacy]);
    // 実行の trace の API（mockRuns の route より後に登録して先に当てる）。古い形式の記録は終端だけを返す。
    const trace = await mockTraceApi(page, {
      [partial.run_id]: { events: detail(partial).progress_events, droppedSpans: 2 },
      [legacy.run_id]: [
        { schema_version: 1, seq: 1, target_id: legacy.run_id, attempt: 0, emitted_at: "2026-10-15T00:00:01Z", type: "terminal", status: "done" },
      ],
    });

    await page.goto(`/history/${partial.run_id}`);
    await page.getByTestId("run-history-timeline-summary").click();
    await expect(page.getByTestId("run-history-timeline-partial")).toHaveText("一部の処理だけを記録しています。");
    // 画面は実行の id で読む（任意の trace id を渡さない）。
    expect(trace.requests).toContain(partial.run_id);
    await expectNoPageOverflow(page);

    await page.goto(`/history/${legacy.run_id}`);
    // 終わった直後の記録と区別できないので、書き終わりを待つ読み直し（共通の hook。およそ 3 秒）の後に出る。
    await expect(page.getByTestId("run-history-timeline-empty")).toHaveText(
      "この実行は古い形式の記録のため、処理の経過を表示できません。",
      { timeout: 10_000 }
    );
    await expect(page.getByTestId("run-history-timeline-summary")).toHaveCount(0);
    await expectNoPageOverflow(page);
  });

  test("一致する用語の別名を検索文に加えると回答ルールの確認は別の段で、それぞれの登録・一致・影響を出す（#3451）", async ({
    page,
  }, testInfo) => {
    await mockLocalAuth(page);
    const row = run(1);
    await mockRuns(page, [row]);
    await mockTraceApi(page, {
      [row.run_id]: savedSpanProgress(
        row.run_id,
        [
          { key: "root", spec: ragSpan.root(), at: [0, 0.6] },
          { key: "answer", spec: ragSpan.stage("answer"), at: [0, 0.5] },
          { key: "understand", spec: ragSpan.step("質問の理解"), at: [0, 0.1] },
          {
            key: "terms",
            spec: ragSpan.step("用語・同義語の確認"),
            at: [0.1, 0.12],
            details: {
              fields: [
                { label: "目的", value: "登録済みの用語・同義語を質問に当て、別名を検索に使います。" },
                { label: "結果", value: "一致: 用語 A" },
                { label: "有効な登録", value: "2 件（登録 3 件）" },
                { label: "一致した用語", value: "用語 A（別名: 別名 A1・別名 A2）" },
                { label: "影響", value: "一致した用語の別名を補助の検索文に加えます。" },
              ],
            },
          },
          {
            key: "rules",
            spec: ragSpan.step("回答ルールの確認"),
            at: [0.12, 0.13],
            details: {
              fields: [
                { label: "目的", value: "登録済みの回答ルールを質問に当て、回答の参考にします。" },
                { label: "結果", value: "一致なし：登録済みの有効な回答ルールに一致しませんでした" },
                { label: "有効な登録", value: "1 件（登録 1 件）" },
              ],
            },
          },
        ],
        { base: "2026-10-15T00:00:00Z" }
      ),
    });

    await page.goto(`/history/${row.run_id}`);
    await page.getByTestId("run-history-timeline-summary").click();
    const steps = page.getByTestId("run-history-timeline").locator("[data-run-timeline-step]");
    // 包む段 → 質問から対象と条件を読み取る → 一致する用語の別名を検索文に加える → 回答ルールの確認（同じ段の子でこの順）。
    await expect(steps).toHaveCount(4);
    await expect(steps.nth(2)).toContainText("一致する用語の別名を検索文に加える");
    await expect(steps.nth(3)).toContainText("確認と回答に使うルールを選ぶ");
    await expect(steps.nth(2)).toHaveAttribute("data-level", "1");
    await expect(steps.nth(3)).toHaveAttribute("data-level", "1");
    await expect(page.getByText("用語・ルールの確認")).toHaveCount(0);

    await steps.nth(2).locator("[data-run-timeline-toggle]").click();
    const terms = steps.nth(2).locator("[data-run-timeline-details]");
    await expect(terms).toContainText("有効な登録");
    await expect(terms).toContainText("2 件（登録 3 件）");
    await expect(terms).toContainText("用語 A（別名: 別名 A1・別名 A2）");
    await expect(terms).toContainText("一致した用語の別名を補助の検索文に加えます。");
    await steps.nth(3).locator("[data-run-timeline-toggle]").click();
    const rules = steps.nth(3).locator("[data-run-timeline-details]");
    await expect(rules).toContainText("一致なし：登録済みの有効な回答ルールに一致しませんでした");
    await expect(rules).toContainText("1 件（登録 1 件）");
    await expect(rules).not.toContainText("影響");
    await page.getByTestId("run-history-timeline").screenshot({ path: testInfo.outputPath("terms-rules-steps.png") });
    await expectNoPageOverflow(page);
  });

  test("質問の理解と検索文の決定の段は、補助の検索文の無い新しい記録も、補助の検索文を含む保存済みの記録もそのまま開ける（#3496）", async ({
    page,
  }, testInfo) => {
    await mockLocalAuth(page);
    const current = run(1);
    const saved = run(2);
    await mockRuns(page, [current, saved]);
    const understandFields = [
      { label: "目的", value: "質問の文から、文書名・画面名・エラーコード・日付などの条件を規則で読み取ります（LLM は使いません）。" },
      { label: "結果", value: "質問の目的は「操作方法」" },
      { label: "答える要求", value: "1 件" },
      { label: "読み取ったファイル", value: "sample.pdf" },
      { label: "読み取ったコード・エラー", value: "E1234" },
    ];
    const progress = (runId: string, legacy: boolean) =>
      savedSpanProgress(
        runId,
        [
          { key: "root", spec: ragSpan.root(), at: [0, 0.6] },
          { key: "answer", spec: ragSpan.stage("answer"), at: [0, 0.5] },
          {
            key: "understand",
            spec: ragSpan.step("質問の理解"),
            at: [0, 0.1],
            details: {
              fields: legacy
                ? [
                    ...understandFields,
                    { label: "補助の検索文", value: "1 本" },
                    { label: "影響", value: "絞り込みに合う文書だけを検索します。補助の検索文を 1 本追加します。" },
                  ]
                : [...understandFields, { label: "影響", value: "絞り込みに合う文書だけを検索します。" }],
            },
          },
          {
            key: "queries",
            spec: ragSpan.step("検索文と検索語の確定"),
            at: [0.1, 0.2],
            details: {
              fields: [
                { label: "目的", value: "意味の近さで探す検索文と、全文検索に使うキーワードを確定します。" },
                { label: "結果", value: legacy ? "検索文 3 本" : "検索文 2 本" },
                { label: "全文検索のキーワード", value: "語 A / 語 B" },
              ],
              lines: [
                "1. [原質問] 合成の質問",
                "2. [質問拡張] 言い換えた検索文",
                ...(legacy ? ["3. [質問理解] 語 A 語 B sample.pdf"] : []),
              ],
            },
          },
        ],
        { base: "2026-10-15T00:00:00Z" }
      );
    await mockTraceApi(page, {
      [current.run_id]: progress(current.run_id, false),
      [saved.run_id]: progress(saved.run_id, true),
    });

    for (const [row, legacy] of [
      [current, false],
      [saved, true],
    ] as const) {
      await page.goto(`/history/${row.run_id}`);
      await page.getByTestId("run-history-timeline-summary").click();
      const steps = page.getByTestId("run-history-timeline").locator("[data-run-timeline-step]");
      // 包む段 → 質問の理解 → 検索文と全文検索の語を決める。
      await expect(steps).toHaveCount(3);

      await steps.nth(1).locator("[data-run-timeline-toggle]").click();
      const understand = steps.nth(1).locator("[data-run-timeline-details]");
      await expect(understand).toContainText("読み取ったコード・エラー");
      await expect(understand).toContainText("E1234");
      await expect(understand).toContainText("絞り込みに合う文書だけを検索します。");
      await steps.nth(2).locator("[data-run-timeline-toggle]").click();
      const queries = steps.nth(2).locator("[data-run-timeline-details]");
      await expect(queries).toContainText("[原質問] 合成の質問");
      await expect(queries).toContainText("[質問拡張] 言い換えた検索文");
      if (legacy) {
        // 保存済みの記録は書き換えず、保存したとおりに出す。
        await expect(understand).toContainText("補助の検索文");
        await expect(queries).toContainText("[質問理解] 語 A 語 B sample.pdf");
      } else {
        await expect(understand).not.toContainText("補助の検索文");
        await expect(queries).not.toContainText("[質問理解]");
      }
      await page
        .getByTestId("run-history-timeline")
        .screenshot({ path: testInfo.outputPath(`understanding-queries-${legacy ? "saved" : "current"}.png`) });
      await expectNoPageOverflow(page);
    }
  });
});

test.describe("実行履歴の詳細の権限の無い根拠（#3252）", { tag: "@responsive" }, () => {
  test("今の権限で見られない根拠は数と理由だけを出し、抜粋・資料名を出さない。見られる根拠が無くても数を出す", async ({
    page,
  }) => {
    await mockLocalAuth(page);
    await mockRuns(page, [
      run(1, { withheld_evidence_count: 2 }),
      run(3, { citations: [], withheld_evidence_count: 1 }),
      run(2, { citations: [], withheld_evidence_count: 1 }),
    ]);

    await page.goto("/history/run-1");
    const evidence = page.getByTestId("run-history-evidence");
    const summary = evidence.locator("summary");
    await expect(summary).toContainText("AI に渡した 3 件");
    await expect(summary).toContainText("権限の無い根拠 2 件");
    await summary.click();
    const withheld = evidence.getByTestId("evidence-withheld");
    await expect(withheld).toContainText("権限の無い根拠 2 件");
    await expect(withheld).toContainText("抜粋・資料名・原文は表示しません");
    // 見られる根拠は今どおりカードで出す。
    await expect(evidence.getByTestId("evidence-card")).toHaveCount(1);
    await expect(page.getByTestId("run-history-citations")).toContainText("受注マニュアル.pdf");
    expect(await findIconTextMisalignment(page, { root: '[data-testid="evidence-withheld"]' })).toEqual([]);
    await expectNoPageOverflow(page);

    // 見られる根拠が無い回答も、折りたたみに数だけを出す（「根拠はありません」にしない）。
    await page.goto("/history/run-3");
    await expect(evidence.locator("summary")).toContainText("AI に渡した 1 件 ／ 権限の無い根拠 1 件");
    await expect(page.getByText("根拠はありません")).toHaveCount(0);

    // 回答を作らない検索の結果も、数の行を一覧の上に出す。
    await page.goto("/history/run-2");
    await expect(evidence.getByTestId("evidence-withheld")).toContainText("権限の無い根拠 1 件");
    await expect(evidence.getByTestId("evidence-card")).toHaveCount(0);
    await expectNoPageOverflow(page);
  });
});

test.describe("実行履歴の詳細の幅とテーマ", () => {
  for (const theme of ["light", "dark"] as const) {
    test(`1280 / 1920px で横あふれが無く、見出しと本文の左端がそろう (${theme})`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "幅を指定して確かめる（desktop だけ）");
      await page.addInitScript((value) => {
        window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
      }, theme);
      await mockLocalAuth(page);
      await mockRuns(page, [run(1)]);
      for (const width of [1280, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        await page.goto("/history/run-1");
        await expect(page.getByTestId("run-history-summary")).toBeVisible();
        expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
        const titleLeft = await page.getByRole("heading", { level: 1, name: "実行の詳細" }).evaluate(
          (element) => element.getBoundingClientRect().left
        );
        const bodyLeft = await page.getByTestId("run-history-summary").evaluate((element) => element.getBoundingClientRect().left);
        expect(Math.abs(titleLeft - bodyLeft)).toBeLessThanOrEqual(1);
      }
    });
  }
});

// #3422: 章節の階層（section_path）がとても長い根拠でも、根拠のカードの一覧（run-history-citations）が横に
// スクロールしない。章節のチップはカードの幅で省略（…）し、全文は title で読める。
test.describe("実行履歴の詳細の長い章節の根拠（#3422）", { tag: "@responsive" }, () => {
  test("章節の階層が長い根拠のチップはカードの幅で省略し、根拠の一覧が横にスクロールしない", async ({ page }, testInfo) => {
    const longPath = Array.from({ length: 12 }, (_, index) => `第 ${index + 1} 章 合成の章節の見出し`).join(" > ");
    await mockLocalAuth(page);
    await mockRuns(page, [
      run(1, {
        citations: [{ ...CITATION, metadata: { section_path: longPath, page_start: 7, page_end: 7 } }],
      }),
    ]);

    await page.goto("/history/run-1");
    const evidence = page.getByTestId("run-history-evidence").getByTestId("answer-citations");
    await evidence.locator("summary").click();
    const list = page.getByTestId("run-history-citations");
    const chip = list.getByTestId("evidence-chips").locator("dd", { hasText: "第 12 章" });
    await expect(chip).toHaveAttribute("title", longPath);
    const overflow = await list.evaluate((element) => element.scrollWidth - element.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    // チップはカードの中に収まり、文字は省略される（全文の幅より狭い）。
    const [chipBox, cardBox] = await Promise.all([
      chip.boundingBox(),
      list.getByTestId("evidence-card").first().boundingBox(),
    ]);
    expect(chipBox!.x + chipBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
    expect(await chip.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
    await list.getByTestId("evidence-card").first().screenshot({ path: testInfo.outputPath("long-section-path-card.png") });
    await expectNoPageOverflow(page);
  });
});

test("保存した回答の記録から同頁の説明と全確定根拠を開ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { itemCitationText, itemCitationChunks, expectItemCitationSources } = await import("./_item-citations");
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockRuns(page, [run(0, {
    question: "合成の質問", search_answer_profile_name: "プロファイル A",
    answer_generated: true, answer: itemCitationText, citations: itemCitationChunks, citation_count: 2,
  })]);
  await page.goto("/history/run-0");
  await expectItemCitationSources(page, page.getByTestId("answer-text"));
  await page.screenshot({ path: testInfo.outputPath("item-citations-history.png"), fullPage: true });
});

test("実行履歴の主一覧は高い画面へ拡張してページ送りを保つ", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockRuns(page, Array.from({ length: 60 }, (_, index) => run(index + 1, { question: `合成の質問 ${index + 1}`, search_answer_profile_name: "プロファイル A" })));
  await page.goto("/history");
  await expectGrowingList(page, testInfo, "run-history");
});


test("資料照合の実行状態は未照合にまとめず、一覧と詳細で区別する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockLocalAuth(page);
  const states = [
    ["disabled", "設定で無効"], ["not_run", "未実行"], ["error", "確認処理でエラー"],
    ["timed_out", "確認が時間切れ"], ["passed", "確認完了・問題なし"], ["failed", "確認完了・問題あり"],
  ];
  const rows = states.map(([execution], index) => run(index + 1, { answer_generated: true, verification: "unverified", validation_execution: execution }));
  await mockRuns(page, rows);
  await page.goto("/history");
  if (testInfo.project.name === "desktop") {
    for (const [index, [, label]] of states.entries()) {
      await expect(page.getByTestId(`run-history-row-run-${index + 1}`)).toContainText(label);
    }
  }
  for (const [index] of states.entries()) {
    await page.goto(`/history/run-${index + 1}`);
    await page.getByTestId("run-history-timeline-summary").click();
    await page.getByTestId("run-history-process-details").click();
    await expect(page.getByTestId("answer-validation-status")).toHaveCount(0);
  }
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("validation-execution-history.png") });
});

test("呼び出しの詳細で入力・結果・思考・Schema・計時を確認できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockLocalAuth(page);
  const row = run(1);
  await mockRuns(page, [row]);
  await mockTraceApi(page, { [row.run_id]: savedSpanProgress(row.run_id, [
    { key: "root", spec: ragSpan.root(), at: [0, 8] },
    { key: "attachment-off", spec: ragSpan.technical("rag.attachment_evidence", "root"), status: "skipped", at: [0, 0.001] },
    { key: "stage", spec: ragSpan.technical("synthetic.stage", "root"), at: [7, 8], details: syntheticStageDetails },
    { key: "model", spec: ragSpan.technical("gen_ai.chat", "root", { "gen_ai.request.model": "synthetic-model" }), at: [0, 1], details: { call: syntheticCallDetails } },
    { key: "tool", spec: ragSpan.technical("gen_ai.execute_tool", "root", { "gen_ai.tool.name": "synthetic-tool" }), at: [1, 3], details: { call: syntheticCallDetails } },
    { key: "model-2", spec: ragSpan.technical("gen_ai.chat", "root", { "gen_ai.request.model": "synthetic-model" }), at: [3, 4], details: { call: syntheticCallDetails } },
    { key: "tool-2", spec: ragSpan.technical("gen_ai.execute_tool", "root", { "gen_ai.tool.name": "synthetic-tool-2" }), at: [4, 7], details: { call: syntheticCallDetails } },
    { key: "model-3", spec: ragSpan.technical("gen_ai.chat", "root", { "gen_ai.request.model": "synthetic-model" }), at: [7, 8], details: { call: syntheticCallDetails } },
  ]) });
  await page.goto(`/history/${row.run_id}`);
  await page.getByTestId("run-history-timeline-summary").click();
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Span name");
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Parent span ID");
  await confirmTraceExplorer(page, testInfo, expect, true);
});
