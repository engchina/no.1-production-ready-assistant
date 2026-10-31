import { inspectTraceStep } from "../../../platform/packages/ui/e2e/trace-explorer";
import { expect, type Page, test } from "./fixtures/test";

import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  selectSearchAnswerProfile,
} from "./_helpers";
import {
  installTraceRecorder,
  mockTraceApi,
  ragSpan,
  spanProgressRecorder,
  spanStartMark,
  spanStepId,
  traceSse,
  type SpanSpec,
} from "./_chat-progress";
import { expectProgressTimerMonotonic, startProgressTimerSampler } from "./_progress-timer";

/**
 * 回答生成の進捗（今の工程と経過時間）と時間切れの表示（#375）。
 *
 * `page.route` の fulfill は本文を一度に返すため、SSE の event を時間をおいて届けるよう、
 * 画面の fetch を init script で差し替えて ReadableStream で少しずつ流す。処理の経過（span）は配信では届かず、
 * 画面が trace の API で読み直す（#3226）。mock は配信の chunk を送った時点までの span を trace の API で返す
 * （`traceSse` / `installTraceRecorder`）。
 */

interface TimedChunk {
  afterMs: number;
  text: string;
}

type StreamScenarios = Record<"search" | "chat", TimedChunk[][]>;

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const TIMEOUT_MESSAGE =
  "回答の生成が上限の 10 分以内に終わりませんでした（時間切れになった工程: 文書検索）。" +
  "時間をおいて、もう一度送信してください。";

const searchAnswerProfile = {
  id: "bv-1",
  name: "経理ビュー",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-06-19T00:00:00Z",
  updated_at: "2026-06-19T00:00:00Z",
  archived_at: null,
};

const citation = {
  document_id: "doc-1",
  chunk_id: "doc-1:0",
  text: "銀行の得点は 82 点です。",
  score: 0.9,
  rerank_score: 0.8,
  file_name: "評価表.pdf",
  category_name: null,
  metadata: {},
};

/**
 * RAG 検索の処理の経過（span のイベント。#3226）を、配信の chunk を送ったときに表へ書いたものとして記録する
 * （trace の API が返す）。時刻は配信した時刻に置き換える（mockStreams）。
 */
function searchProgressStream(traceId: string) {
  const recorder = spanProgressRecorder(traceId, () => "__NOW__", spanStartMark);
  const send = traceSse;
  return {
    start: (key: string, spec: SpanSpec) => send(recorder.start(key, spec)),
    finish: (...args: Parameters<typeof recorder.finish>) => send(recorder.finish(...args)),
    terminal: (status: "done" | "failed" | "cancelled") => send(recorder.terminal(status)),
  };
}

const slowSearch = searchProgressStream("trace-1");

/**
 * 質問の条件を整理して回答を作る（answer の段）の中を、質問の理解 → 文書検索 と時間をおいて進む検索の SSE。
 * 中の各工程は段の span の子の span として届く（#3226）。
 */
const slowSearchStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      // 最初に実行の id（処理の経過は実行の trace の API で読む）。
      sse("run", { trace_id: "trace-1" }),
      slowSearch.start("root", ragSpan.root()),
      slowSearch.start("answer", ragSpan.stage("answer")),
      slowSearch.start("understand", ragSpan.step("質問の理解")),
    ].join(""),
  },
  {
    afterMs: 1500,
    text: [
      // 終わった工程の詳細と計測値は、工程が終わったときのイベントで届く（#2373）。
      slowSearch.finish("understand", "done", {
        params: { llm_calls: 1, llm_sends: 2 },
        details: { fields: [{ label: "答える要求", value: "1 件" }], lines: ["読み取った条件: なし"] },
      }),
      slowSearch.start("search", ragSpan.step("文書検索")),
    ].join(""),
  },
  {
    afterMs: 1500,
    text: [
      slowSearch.finish("search", "done", {
        details: {
          code: [{ label: "rerank", language: "json", text: JSON.stringify({
            phase: "main_search", query_role: "original", question_index: 0,
            cache_state: "hit", status: "completed", sdk_attempt_count: 0,
            http_attempt_count: null,
          }, null, 2) }],
        },
      }),
      slowSearch.start("generate", ragSpan.step("回答の生成")),
    ].join(""),
  },
  {
    afterMs: 600,
    text: [
      slowSearch.finish("generate"),
      slowSearch.finish("answer"),
      slowSearch.finish("root"),
      slowSearch.terminal("done"),
      sse("metadata", {
        trace_id: "trace-1",
        elapsed_ms: 3600,
        guardrail_warnings: [],
        diagnostics: {
          retrieval_strategy: "hybrid",
          retrieval_strategy_adapter: "grounded",
          filter_keys: [],
          knowledge_base_count: 1,
          config_fingerprint: "fp-1",
          // 回答の記録の詳細（処理の経過の中の「処理の詳細」に出す。#2325）。
          answer: { confidence: "high", needs_human_review: false },
        },
      }),
      sse("delta", { text: "銀行の得点は 82 点です。" }),
      sse("citations", [citation]),
      sse("done", { trace_id: "trace-1" }),
    ].join(""),
  },
];

const knowledgeSearch = searchProgressStream("trace-1");

/** 一致する用語の別名を検索文に加える → 回答ルールの確認 を別の工程（別の span）で進む検索の SSE（#3451）。 */
const knowledgeStepsSearchStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("run", { trace_id: "trace-1" }),
      knowledgeSearch.start("root", ragSpan.root()),
      knowledgeSearch.start("answer", ragSpan.stage("answer")),
      knowledgeSearch.start("terms", ragSpan.step("用語・同義語の確認")),
      knowledgeSearch.finish("terms", "done", {
        details: {
          fields: [
            { label: "目的", value: "登録済みの用語・同義語を質問に当て、別名を検索に使います。" },
            { label: "結果", value: "一致: 用語 A" },
            { label: "有効な登録", value: "1 件（登録 1 件）" },
            { label: "一致した用語", value: "用語 A（別名: 別名 A）" },
            { label: "影響", value: "一致した用語の別名を補助の検索文に加えます。" },
          ],
        },
      }),
      knowledgeSearch.start("rules", ragSpan.step("回答ルールの確認")),
    ].join(""),
  },
  {
    afterMs: 1500,
    text: [
      knowledgeSearch.finish("rules", "done", {
        details: {
          fields: [
            { label: "目的", value: "登録済みの回答ルールを質問に当て、回答の参考にします。" },
            { label: "結果", value: "一致: 回答ルール A" },
            { label: "有効な登録", value: "1 件（登録 2 件）" },
            { label: "一致した回答ルール", value: "回答ルール A" },
            { label: "影響", value: "一致した回答ルールを回答時の参考情報として渡します。" },
          ],
        },
      }),
      knowledgeSearch.finish("answer"),
      knowledgeSearch.finish("root"),
      knowledgeSearch.terminal("done"),
      sse("metadata", {
        trace_id: "trace-1",
        elapsed_ms: 1600,
        guardrail_warnings: [],
        diagnostics: {
          retrieval_strategy: "hybrid",
          retrieval_strategy_adapter: "grounded",
          filter_keys: [],
          knowledge_base_count: 1,
          config_fingerprint: "fp-1",
          answer: { confidence: "high", needs_human_review: false },
        },
      }),
      sse("delta", { text: "合成の回答です。" }),
      sse("citations", [citation]),
      sse("done", { trace_id: "trace-1" }),
    ].join(""),
  },
];

const retrievalSearch = searchProgressStream("trace-1");

// 回答を作らない検索（検索結果だけ）。処理の経過は検索結果の一覧の上に出す。チャットと同じ流れを回答の生成の手前で
// 止める（#3506）: 全体は evidence の段、その下に質問の理解などと「根拠検索フロー」、文書検索はその子。
const retrievalOnlySearchStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("run", { trace_id: "trace-1" }),
      retrievalSearch.start("root", ragSpan.root(false)),
      retrievalSearch.start("evidence", ragSpan.stage("evidence")),
      retrievalSearch.start("understand", ragSpan.step("質問の理解", "evidence")),
      retrievalSearch.finish("understand"),
      retrievalSearch.start("flow", ragSpan.step("根拠検索フロー", "evidence")),
      retrievalSearch.start("search", ragSpan.step("文書検索", "flow")),
      retrievalSearch.start("rerank", ragSpan.step("Rerank", "search")),
      retrievalSearch.finish("rerank"),
      retrievalSearch.finish("search"),
      retrievalSearch.start("coverage", ragSpan.step("要求ごとの根拠の確認", "flow")),
      retrievalSearch.finish("coverage"),
      retrievalSearch.finish("flow"),
      retrievalSearch.finish("evidence"),
      retrievalSearch.finish("root"),
      retrievalSearch.terminal("done"),
      sse("metadata", {
        trace_id: "trace-1",
        elapsed_ms: 400,
        guardrail_warnings: [],
        diagnostics: {
          retrieval_strategy: "hybrid",
          retrieval_strategy_adapter: "grounded",
          filter_keys: [],
          knowledge_base_count: 1,
          config_fingerprint: "fp-1",
        },
      }),
      sse("citations", [citation, { ...citation, chunk_id: "doc-1:1", text: "支店の得点は 75 点です。" }]),
      sse("done", { trace_id: "trace-1" }),
    ].join(""),
  },
];

const timedOutSearch = searchProgressStream("trace-1");
const timedOutSearchStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("run", { trace_id: "trace-1" }),
      timedOutSearch.start("root", ragSpan.root()),
      timedOutSearch.start("answer", ragSpan.stage("answer")),
      timedOutSearch.start("search", ragSpan.step("文書検索")),
    ].join(""),
  },
  {
    afterMs: 800,
    text: [
      // 時間切れで止まった span は取り消しの印で終わり、終端は失敗（#3226）。
      timedOutSearch.finish("search", "cancelled"),
      timedOutSearch.finish("answer", "cancelled"),
      timedOutSearch.finish("root", "cancelled"),
      timedOutSearch.terminal("failed"),
    ].join("") + sse("error", {
      trace_id: "trace-1",
      message: TIMEOUT_MESSAGE,
      error_type: "TimeoutError",
      stage: "answer_step:文書検索",
    }),
  },
];

const incompleteSearch = searchProgressStream("trace-incomplete");

/** 回答が送り直しても出力の上限で未完了だった検索の SSE（backend の `describe_answer_failure` の形。#2023）。 */
const incompleteSearchStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("run", { trace_id: "trace-incomplete" }),
      incompleteSearch.start("root", ragSpan.root()),
      incompleteSearch.start("answer", ragSpan.stage("answer")),
    ].join(""),
  },
  {
    afterMs: 300,
    text: sse("error", {
      trace_id: "trace-incomplete",
      message:
        "回答が長くなり、モデル「stub-model」の出力の上限に達して途中で止まりました" +
        "（出力の上限を広げ、推論の量を下げて送り直しても終わりませんでした）。" +
        "推論のあるモデルは推論の token も出力の上限に数えます。" +
        "システム設定 › モデル で、このモデルの「出力の上限（tokens）」を大きくするか、質問を分けてやり直してください。",
      error_type: "IncompleteResponse",
      reason: "max_output_tokens",
      stage: "回答の作成",
      detail:
        "status=incomplete・理由=max_output_tokens・出力の上限=65536 tokens" +
        "・使った出力=65536 tokens（うち推論 65000）・推論の量=low",
    }),
  },
];

async function mockStreams(page: Page, scenarios: StreamScenarios): Promise<void> {
  await installTraceRecorder(page);
  await page.addInitScript((scenarioMap: StreamScenarios) => {
    const w = window as unknown as {
      __sseRequests: Array<{ key: string; body: unknown }>;
      __ragTraceRecord: (text: string) => string;
    };
    w.__sseRequests = [];
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const key = url.includes("/api/search/stream")
        ? "search"
        : url.includes("/messages/stream")
          ? "chat"
          : null;
      if (!key) return originalFetch(input, init);
      w.__sseRequests.push({ key, body: init?.body ? JSON.parse(String(init.body)) : null });
      const index = w.__sseRequests.filter((request) => request.key === key).length - 1;
      const list = scenarioMap[key];
      const chunks = list[Math.min(index, list.length - 1)];
      const encoder = new TextEncoder();
      const signal = init?.signal ?? undefined;
      // span の開始の時刻（印 → 最初に送った時刻）。回答ごとに作り直す。
      const startMarks: Record<string, string> = {};
      const stream = new ReadableStream<Uint8Array>({
        async start(controller) {
          for (const chunk of chunks) {
            if (chunk.afterMs) await new Promise((resolve) => setTimeout(resolve, chunk.afterMs));
            if (signal?.aborted) {
              controller.error(new DOMException("aborted", "AbortError"));
              return;
            }
            // 段階の時刻（`__NOW__`）は送った時刻にする（段階の時刻で経過時間を数え直すと減ることを確かめる。#1176）。
            // span の開始の印（`__START_<key>__`）は最初に送った時刻にし、終了のイベントでも同じ時刻にする。
            const sentAt = new Date().toISOString();
            const text = chunk.text
              .replaceAll("__NOW__", sentAt)
              .replace(/__START_([A-Za-z0-9-]+)__/g, (_mark, key: string) => (startMarks[key] ??= sentAt));
            // span は表へ書いたもの（trace の API が返す）として記録し、配信からは外す（#3226）。
            controller.enqueue(encoder.encode(w.__ragTraceRecord(text)));
          }
          controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    };
  }, scenarios);
}

async function streamRequests(page: Page, key: "search" | "chat") {
  return page.evaluate(
    (requestKey) =>
      (
        window as unknown as { __sseRequests: Array<{ key: string; body: unknown }> }
      ).__sseRequests.filter((request) => request.key === requestKey),
    key
  );
}

async function mockSearchPage(page: Page, scenarios: TimedChunk[][]) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await mockStreams(page, { search: scenarios, chat: [[]] });
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`RAG 検索は回答生成中に今の工程と経過時間を出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchPage(page, [slowSearchStream]);

    await enableSearchAnswer(page);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("自分の銀行の得点はなんですか");
    await page.getByRole("button", { name: "検索", exact: true }).click();

    const progress = page.getByTestId("search-run-progress");
    const current = page.getByTestId("search-run-progress-current");
    const timer = page.getByTestId("search-run-progress-timer");
    // 処理の経過（#2096。チャットと同じ ChatProgress）: 回答の中の工程（LLM）の間も今の工程と経過時間を出す。
    await expect(current).toContainText("質問から対象と条件を読み取る");
    await expect(timer).toContainText("経過時間");
    const firstElapsed = await timer.textContent();
    await expect(current).toContainText("検索文から根拠の候補を集める", { timeout: 4_000 });
    await expect.poll(() => timer.textContent(), { timeout: 4_000 }).not.toBe(firstElapsed);
    // 終わった工程は一覧に増えていく（既定で開く）。行は span で、実行中の span は実行中の行になる（#3226）。
    await expect(page.getByTestId("search-run-progress-running")).toContainText("処理中（1 ステップ完了）");
    const understand = page.getByTestId(`search-run-progress-step-${spanStepId("understand")}`);
    await expect(understand).toContainText(/1\.[5-9] 秒/);
    // 今の工程は実行中の span（文書検索）。
    await expect(current).toHaveAttribute("data-step-id", spanStepId("search"));
    // 包む段（質問の条件を整理して回答を作る）も実行中の行で、中の工程はその子（字下げ）。
    await expect(page.getByTestId(`search-run-progress-step-${spanStepId("answer")}`)).toContainText(
      "質問の条件を整理して回答を作る"
    );
    // 終わった工程の詳細は、後の工程の実行中から開ける（回答の完了を待たない。#2373）。計測値も実行中から出す。
    await expect(current).toContainText("検索文から根拠の候補を集める");
    // 送り直しで API への送信が多いときは送信の回数も示す（#2966）。
    await expect(understand).toContainText("LLM 1 回・送信 2 回");
    const liveToggle = page.getByTestId(`search-run-progress-step-${spanStepId("understand")}-toggle`);
    await liveToggle.click();
    await expect(liveToggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId(`search-run-progress-step-${spanStepId("understand")}-details`)).toContainText(
      "答える要求"
    );
    await expect(current).toContainText("検索文から根拠の候補を集める");
    // 検索のボタンは実行中に「停止」になりスピナーを出さないため、動くスピナーは今の工程の 1 つだけ（#413）。
    await expect(progress.locator("svg.animate-spin")).toHaveCount(1);
    await expect(page.getByTestId("search-run-stop")).toHaveAccessibleName("停止");
    await expect(page.getByTestId("search-run-stop")).not.toHaveAttribute("aria-busy", "true");
    await page.screenshot({
      path: `test-results/answer-progress-search-${viewport.name}.png`,
      fullPage: false,
    });

    const summary = page.getByTestId("search-run-progress-summary");
    // 包む段（質問の条件を整理して回答を作る）も span の行になる（実行の全体の span は行にしない。#3226）。
    await expect(summary).toHaveText(/^処理の経過（4 ステップ・3\.6 秒）$/, { timeout: 4_000 });
    await expect(page.getByText("銀行の得点は 82 点です。").first()).toBeVisible();
    // 完了したら一覧を閉じ（チャットと同じ。#2118）、押すと開く。
    const process = page.getByRole("region", { name: "処理の経過" });
    await expect(process.getByText("検索文から根拠の候補を集める", { exact: true })).toBeHidden();
    // 「処理の詳細」は処理の経過の中にあり、閉じているときは一緒に隠れる（回答の本文との間に独立して置かない。#2325）。
    const details = page.getByTestId("search-run-progress-details");
    await expect(details).toBeHidden();
    await summary.click();
    await expect(process.getByText("検索文から根拠の候補を集める", { exact: true })).toBeVisible();
    // 論理的なリランク要求と実SDK開始を既存の詳細部品で区別する。
    await page.getByTestId(`search-run-progress-step-${spanStepId("search")}-toggle`).click();
    const rerankDetails = page.getByTestId(`search-run-progress-step-${spanStepId("search")}-details`);
    for (const expected of ['"phase": "main_search"', '"query_role": "original"', '"cache_state": "hit"', '"status": "completed"', '"sdk_attempt_count": 0', '"http_attempt_count": null']) {
      await expect(rerankDetails).toContainText(expected);
    }
    await expectNoPageOverflow(page);
    await process.screenshot({ path: `test-results/rerank-receipt-${viewport.name}.png` });
    // 開くとタイムラインの後ろ（中の最後の要素）に「処理の詳細」が出る。
    await expect(details).toHaveText("処理の詳細");
    const timelineBox = await page.getByTestId("search-run-progress-timeline").boundingBox();
    const detailsBox = await details.boundingBox();
    expect(timelineBox && detailsBox && detailsBox.y >= timelineBox.y + timelineBox.height).toBeTruthy();
    await details.click();
    await expect(process.getByRole("region", { name: "処理の詳細" }).getByText("信頼度: high")).toHaveCount(0);
    // 回答を読む場には回答の記録の削除（「その他の操作」）を出さない（#2325）。
    await expect(process.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
    await process.screenshot({ path: `test-results/answer-progress-search-details-${viewport.name}.png` });
    // 「トレースの詳細」の 2 つ目の表示は出さず、Trace ID は「処理の詳細」の中に出す（#3226）。
    await expect(process.getByText("トレースの詳細")).toHaveCount(0);
    await expect(process.getByText(/Trace ID: 0123456789abcdef/)).toBeVisible();
    // 名前は「処理の経過」1 つ（旧「検索実行」「実行記録」「ステージ」は出さない）。
    await expect(page.getByRole("region", { name: "検索実行" })).toHaveCount(0);
    await expect(page.getByText("検索の工程と使ったモデル")).toHaveCount(0);
    await expect(page.getByText(/実行記録|ステージ/)).toHaveCount(0);
    // 処理の経過 → 回答の本文 の順（作成中の「N ステップ完了」の場所のまま。チャットと同じ。#2115）。処理の経過は 1 か所だけ。
    await expect(page.getByRole("region", { name: "処理の経過" })).toHaveCount(1);
    const answerBox = await page.getByText("銀行の得点は 82 点です。").first().boundingBox();
    const processBox = await process.boundingBox();
    expect(answerBox && processBox && processBox.y < answerBox.y).toBeTruthy();
    // 「処理の詳細」はチャットと同じ回答の記録の詳細だけで、開始・トレース ID は出さない（#2118）。
    await expect(process.getByText("トレース ID")).toHaveCount(0);
    await expectNoPageOverflow(page);
  });

  test(`一致する用語の別名を検索文に加えると回答ルールの確認を別の工程で順に出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchPage(page, [knowledgeStepsSearchStream]);

    await enableSearchAnswer(page);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("合成の質問");
    await page.getByRole("button", { name: "検索", exact: true }).click();

    // 実行中: 一致する用語の別名を検索文に加えるが終わり、回答ルールの確認が今の工程（#3451）。
    const current = page.getByTestId("search-run-progress-current");
    await expect(current).toContainText("確認と回答に使うルールを選ぶ");
    await expect(current).toHaveAttribute("data-step-id", spanStepId("rules"));
    const terms = page.getByTestId(`search-run-progress-step-${spanStepId("terms")}`);
    await expect(terms).toContainText("一致する用語の別名を検索文に加える");
    await page.getByTestId(`search-run-progress-step-${spanStepId("terms")}-toggle`).click();
    const termsDetails = page.getByTestId(`search-run-progress-step-${spanStepId("terms")}-details`);
    await expect(termsDetails).toContainText("用語 A（別名: 別名 A）");
    await expect(termsDetails).toContainText("一致した用語の別名を補助の検索文に加えます。");

    // 完了後: 2 つの工程はこの順に別の行で、回答ルールの詳細は回答ルールの工程だけに出す。
    const summary = page.getByTestId("search-run-progress-summary");
    await expect(summary).toContainText("処理の経過（3 ステップ", { timeout: 4_000 });
    await summary.click();
    const process = page.getByRole("region", { name: "処理の経過" });
    const termsBox = await terms.boundingBox();
    const rulesBox = await page.getByTestId(`search-run-progress-step-${spanStepId("rules")}`).boundingBox();
    expect(termsBox && rulesBox && termsBox.y < rulesBox.y).toBeTruthy();
    await expect(process.getByText("用語・ルールの確認")).toHaveCount(0);
    await page.getByTestId(`search-run-progress-step-${spanStepId("rules")}-toggle`).click();
    const rulesDetails = page.getByTestId(`search-run-progress-step-${spanStepId("rules")}-details`);
    await expect(rulesDetails).toContainText("一致した回答ルール");
    await expect(rulesDetails).toContainText("一致した回答ルールを回答時の参考情報として渡します。");
    await expect(rulesDetails).not.toContainText("一致した用語");
    await process.screenshot({ path: `test-results/answer-progress-terms-rules-${viewport.name}.png` });
    await expectNoPageOverflow(page);
  });

  test(`回答を作らない RAG 検索は、処理の経過を検索結果の上に出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchPage(page, [retrievalOnlySearchStream]);

    await page.getByRole("textbox", { name: "RAG 検索" }).fill("自分の銀行の得点はなんですか");
    await page.getByRole("button", { name: "検索", exact: true }).click();

    const results = page.getByRole("heading", { name: "検索結果（2）" });
    await expect(results).toBeVisible();
    // 処理の経過は 1 か所だけで、検索結果の見出しより上（実行中の場所のまま。#2115）。
    const process = page.getByRole("region", { name: "処理の経過" });
    await expect(process).toHaveCount(1);
    await expect(page.getByTestId("search-run-progress-summary")).toContainText("処理の経過（");
    // チャットと同じ工程名・入れ子（#3506）。全体は「質問の条件を整理して根拠を探す」、検索文から根拠の候補を集めるは回答に使う根拠を検索するの子。
    await page.getByTestId("search-run-progress-summary").click();
    for (const [key, label, level] of [
      ["evidence", "質問の条件を整理して根拠を探す", null],
      ["understand", "質問から対象と条件を読み取る", "1"],
      ["flow", "回答に使う根拠を検索する", "1"],
      ["search", "検索文から根拠の候補を集める", "2"],
      ["rerank", "質問との関連度で候補を並べ替える", "3"],
      ["coverage", "要求ごとに根拠の対応を確認する", "2"],
    ] as const) {
      const row = page.getByTestId(`search-run-progress-step-${spanStepId(key)}`);
      await expect(row).toBeVisible();
      await expect(row.locator("[data-run-timeline-label]").first()).toHaveText(label);
      if (level) await expect(row).toHaveAttribute("data-level", level);
      else await expect(row).not.toHaveAttribute("data-level", /.+/);
    }
    await expect(process.getByText("根拠を検索して回答を作る")).toHaveCount(0);
    await process.screenshot({ path: `test-results/answer-progress-search-evidence-${viewport.name}.png` });
    const processBox = await process.boundingBox();
    const resultsBox = await results.boundingBox();
    expect(processBox && resultsBox && processBox.y + processBox.height <= resultsBox.y).toBeTruthy();
    await expectNoPageOverflow(page);
  });

  test(`RAG 検索が時間切れになったら工程と再試行を示す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchPage(page, [timedOutSearchStream, slowSearchStream]);

    await enableSearchAnswer(page);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("自分の銀行の得点はなんですか");
    await page.getByRole("button", { name: "検索", exact: true }).click();

    await expect(page.getByTestId("search-run-progress")).toContainText("検索文から根拠の候補を集める");
    const error = page.getByRole("alert").filter({ hasText: "時間切れになった工程: 文書検索" });
    await expect(error).toBeVisible({ timeout: 4_000 });
    await expect(error).toContainText("もう一度送信してください");
    await page.screenshot({
      path: `test-results/answer-progress-search-timeout-${viewport.name}.png`,
      fullPage: false,
    });
    await expectNoPageOverflow(page);

    await error.getByRole("button", { name: "再試行" }).click();
    await expect(page.getByTestId("search-run-progress-summary")).toContainText("処理の経過（", {
      timeout: 8_000,
    });
    expect(await streamRequests(page, "search")).toHaveLength(2);
  });

  test(`RAG 検索の回答が出力の上限で未完了なら、理由と直し方と「詳細」を示す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchPage(page, [incompleteSearchStream]);

    await enableSearchAnswer(page);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("条件の多い質問");
    await page.getByRole("button", { name: "検索", exact: true }).click();

    const error = page.getByRole("alert").filter({ hasText: "出力の上限に達して途中で止まりました" });
    await expect(error).toBeVisible({ timeout: 4_000 });
    // 直し方（出力の上限・質問を分ける）を本文に、技術的な値は開いた「詳細」に出す（messaging.md §10.3）。
    await expect(error).toContainText("「出力の上限（tokens）」を大きくする");
    await expect(error).toContainText("質問を分けて");
    await expect(error).not.toContainText("検索処理中にエラーが発生しました");
    const details = error.locator("details");
    await expect(details).toHaveAttribute("open", "");
    for (const [label, value] of [
      ["トレース ID", "trace-incomplete"],
      ["エラー種別", "IncompleteResponse"],
      ["失敗した工程", "回答の作成"],
      ["未完了の理由", "max_output_tokens"],
      ["出力の状況", "出力の上限=65536 tokens"],
    ]) {
      await expect(details.getByText(label, { exact: true })).toBeVisible();
      await expect(details).toContainText(value);
    }
    await expect(error.getByRole("button", { name: "再試行" })).toBeVisible();
    await page.screenshot({
      path: `test-results/answer-progress-search-incomplete-${viewport.name}.png`,
      fullPage: false,
    });
    await expectNoPageOverflow(page);
  });
}

// --- チャット ---

const userMessage = {
  message_id: "u1",
  conversation_id: "conv-1",
  role: "USER",
  content: "自分の銀行の得点はなんですか",
  model: null,
  citations: [],
  guardrail_warnings: [],
  trace_id: null,
  status: "COMPLETE",
  reply_to_message_id: null,
  created_at: "2026-01-01T00:00:00Z",
};

const errorReply = {
  message_id: "a1",
  conversation_id: "conv-1",
  role: "ASSISTANT",
  content: TIMEOUT_MESSAGE,
  model: "m1",
  citations: [],
  guardrail_warnings: [],
  trace_id: "t1",
  status: "ERROR",
  reply_to_message_id: "u1",
  created_at: "2026-01-01T00:00:01Z",
};

const retryUserMessage = { ...userMessage, message_id: "u2", created_at: "2026-01-01T00:01:00Z" };
const okReply = {
  ...errorReply,
  message_id: "a2",
  content: "銀行の得点は 82 点です。",
  citations: [citation],
  trace_id: "t2",
  status: "COMPLETE",
  reply_to_message_id: "u2",
  created_at: "2026-01-01T00:01:05Z",
};


/**
 * 回答（`messageId`）の処理の経過（span のイベント。#3226）を、配信の chunk を送ったときに表へ書いたものとして
 * 記録する（回答の trace の API が返す）。時刻は配信した時刻に置き換える（mockStreams）。
 */
function chatProgressStream(messageId: string) {
  const recorder = spanProgressRecorder(messageId, () => "__NOW__", spanStartMark);
  const send = traceSse;
  return {
    start: (key: string, spec: SpanSpec) => send(recorder.start(key, spec)),
    finish: (...args: Parameters<typeof recorder.finish>) => send(recorder.finish(...args)),
    terminal: (status: "done" | "failed" | "cancelled") => send(recorder.terminal(status)),
  };
}

const timedOutProgress = chatProgressStream("a1");
const okProgress = chatProgressStream("a2");

const timedOutChatStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("start", {
        conversation_id: "conv-1",
        user_message: userMessage,
        columns: [{ model_id: "m1", label: "MODEL 1", message_id: "a1" }],
      }),
      timedOutProgress.start("root", ragSpan.root()),
      timedOutProgress.start("rewrite", ragSpan.stage("history_rewrite")),
    ].join(""),
  },
  {
    afterMs: 1500,
    text: [
      // 終わった段の詳細は、終わったときのイベントで届く（後の段の実行中から開ける。#2373）。
      timedOutProgress.finish("rewrite", "done", { details: { fields: [{ label: "答える要求", value: "1 件" }] } }),
      timedOutProgress.start("answer", ragSpan.stage("answer")),
      timedOutProgress.start("search", ragSpan.step("文書検索")),
    ].join(""),
  },
  {
    afterMs: 1500,
    text: [
      timedOutProgress.finish("search", "failed"),
      timedOutProgress.finish("answer", "failed"),
      timedOutProgress.finish("root", "failed"),
      timedOutProgress.terminal("failed"),
      sse("error", {
        model_id: "m1",
        message: TIMEOUT_MESSAGE,
        error_type: "AnswerTimeoutError",
        stage: "answer_step:文書検索",
      }),
    ].join(""),
  },
  {
    // 失敗した段を確かめる間をとってから、会話を取り直す。
    afterMs: 1500,
    text: sse("all_done", { conversation_id: "conv-1" }),
  },
];

const okChatStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("start", {
        conversation_id: "conv-1",
        user_message: retryUserMessage,
        columns: [{ model_id: "m1", label: "MODEL 1", message_id: "a2" }],
      }),
      okProgress.start("root", ragSpan.root()),
      okProgress.start("rewrite", ragSpan.stage("history_rewrite")),
    ].join(""),
  },
  {
    afterMs: 500,
    text: [
      okProgress.finish("rewrite"),
      okProgress.start("answer", ragSpan.stage("answer")),
      okProgress.start("search", ragSpan.step("文書検索")),
      // LLM の回数・結果の説明は値で届き、画面が i18n で補足にする。
      okProgress.finish("search", "done", { params: { llm_calls: 1, note: "根拠 1 件" } }),
      okProgress.finish("answer"),
      okProgress.start("guardrail", ragSpan.stage("answer_guardrail")),
      okProgress.finish("guardrail"),
      okProgress.finish("root"),
      okProgress.terminal("done"),
      sse("metadata", {
        model_id: "m1",
        message_id: "a2",
        trace_id: "t2",
        elapsed_ms: 500,
        guardrail_warnings: [],
      }),
      sse("delta", { model_id: "m1", text: okReply.content }),
      sse("citations", { model_id: "m1", citations: [citation] }),
      sse("done", { model_id: "m1", message_id: "a2" }),
      sse("all_done", { conversation_id: "conv-1" }),
    ].join(""),
  },
];

async function mockChatPage(page: Page, chat: TimedChunk[][] = [timedOutChatStream, okChatStream]) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route("**/api/chat/models", (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  let created = false;
  await page.route("**/api/chat/conversations**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const sent = (await streamRequests(page, "chat").catch(() => [])).length;
    const messages =
      sent >= 2
        ? [userMessage, errorReply, retryUserMessage, okReply]
        : sent === 1
          ? [userMessage, errorReply]
          : [];
    const detail = {
      id: "conv-1",
      search_answer_profile_id: "bv-1",
      title: sent ? userMessage.content : null,
      status: "ACTIVE",
      message_count: messages.length,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:02Z",
      messages,
    };
    if (path === "/api/chat/conversations" && request.method() === "POST") {
      created = true;
      await route.fulfill({ json: { data: detail, error_messages: [], warning_messages: [] } });
      return;
    }
    if (path === "/api/chat/conversations") {
      const items = created ? [{ ...detail, messages: undefined }] : [];
      await route.fulfill({
        json: {
          data: { items, total: items.length, limit: 50, offset: 0, has_next: false },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({ json: { data: detail, error_messages: [], warning_messages: [] } });
  });
  await mockStreams(page, { search: [[]], chat });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`チャットは回答生成の工程と経過時間を出し、時間切れを再送できる (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChatPage(page);

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理ビュー");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill(userMessage.content);
    // #1176: 段階が進む間、右上の経過時間（送信からの通算）が減らないことを記録する。
    await startProgressTimerSampler(page, "chat-answer-progress");
    await page.getByRole("button", { name: "送信" }).click();

    // 処理の段階（3 製品共通の ChatProgress。#1146）。今の段階を 1 行で出し、段階が進むと入れ替わる。
    const progress = page.getByTestId("chat-answer-progress");
    const current = page.getByTestId("chat-answer-progress-current");
    const timer = page.getByTestId("chat-answer-progress-timer");
    await expect(current).toContainText("前の会話を踏まえて質問を補う");
    await expect(timer).toContainText("経過時間");
    const firstElapsed = await timer.textContent();
    await expect(current).toContainText("検索文から根拠の候補を集める", { timeout: 4_000 });
    await expect(page.getByTestId("chat-answer-progress-running")).toContainText("処理中（1 ステップ完了）");
    // 終わった段の詳細は、後の段の実行中から開ける（回答の完了・失敗を待たない。#2373）。
    const rewriteId = spanStepId("rewrite");
    const liveToggle = page.getByTestId(`chat-answer-progress-step-${rewriteId}-toggle`);
    await liveToggle.click();
    await expect(liveToggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId(`chat-answer-progress-step-${rewriteId}-details`)).toContainText("答える要求");
    await expect(current).toContainText("検索文から根拠の候補を集める");
    await expect.poll(() => timer.textContent(), { timeout: 4_000 }).not.toBe(firstElapsed);
    await progress.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `test-results/answer-progress-chat-${viewport.name}.png`,
      fullPage: false,
    });

    // 時間切れの文言（工程と再試行の案内）は ERROR の回答として残り、同じ質問を送り直せる。
    const error = page.getByRole("alert").filter({ hasText: "時間切れになった工程: 文書検索" });
    await expect(error).toBeVisible({ timeout: 5_000 });
    // 質問の整理・検索文から根拠の候補を集めるの段階を通る間、経過時間は減らない（段階ごとに 0 に戻さない。#1176）。
    await expectProgressTimerMonotonic(page, 2);
    // 止まった段階を開いて出す（原因は段階ではなく Banner に出す）。
    await expect(progress).toHaveAttribute("data-chat-progress-state", "failed");
    await expect(page.getByTestId(`chat-answer-progress-step-${spanStepId("search")}`)).toHaveAttribute(
      "data-status",
      "failed"
    );
    await progress.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/answer-progress-chat-failed-${viewport.name}.png`, fullPage: false });
    // 会話を取り直すと保存した失敗の回答に置き換わり、処理の経過は表の span（回答の trace の API）から同じ失敗で
    // 出し直す（#3226）。
    await expect(page.getByTestId("chat-live-turn")).toHaveCount(0, { timeout: 5_000 });
    await expect(progress).toHaveAttribute("data-chat-progress-state", "failed");
    await expect(error).toBeVisible();
    await error.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `test-results/answer-progress-chat-timeout-${viewport.name}.png`,
      fullPage: false,
    });
    await expectNoPageOverflow(page);

    await error.getByRole("button", { name: "もう一度送信" }).click();
    await expect(page.getByText(okReply.content).first()).toBeVisible({ timeout: 5_000 });
    // 完了した回答の上（作成中の「N ステップ完了」の場所のまま）に「処理の経過」を閉じて残す（#1146 / #2115 / #2118）。
    // 失敗した前の回答（a1）も、表の span から処理の経過（失敗）を出したまま残る（#3226）。
    const okAnswer = page.locator("#message-a2");
    const summary = okAnswer.getByTestId("chat-answer-progress-summary");
    await expect(summary).toContainText("処理の経過（4 ステップ・");
    await expect(page.locator("#message-a1").getByTestId("chat-answer-progress")).toHaveAttribute(
      "data-chat-progress-state",
      "failed"
    );
    await summary.click();
    await expect(okAnswer.getByTestId(`chat-answer-progress-step-${spanStepId("search")}`)).toContainText(
      "根拠 1 件 · LLM 1 回"
    );
    const answerBox = await page.getByText(okReply.content).first().boundingBox();
    const summaryBox = await summary.boundingBox();
    expect(answerBox && summaryBox && summaryBox.y < answerBox.y).toBeTruthy();
    await summary.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/answer-progress-chat-done-${viewport.name}.png`, fullPage: false });
    await expectNoPageOverflow(page);
    const requests = await streamRequests(page, "chat");
    expect(requests).toHaveLength(2);
    expect(requests[1].body).toMatchObject({ content: userMessage.content });
    // 再送に成功したら、最新のターンではなくなった失敗には再送の操作を出さない。
    await expect(page.getByRole("button", { name: "もう一度送信" })).toHaveCount(0);
  });
}

// --- 処理の経過の実行中の段のアイコン（#3436） ---

const nestedProgress = chatProgressStream("a3");

/**
 * 回答フローの中の検索文から根拠の候補を集めるで、子の段（埋め込み → ベクトル検索 → 子が途切れた間 → 全文検索）が順に進むチャットの SSE。
 * 子の段を持つ親の工程（質問の条件を整理して回答を作る・根拠を検索して回答を作る・検索文から根拠の候補を集める）は回さず、実行中の
 * 末端の段だけが回ることを確かめる。最後の chunk は送らない（確かめる間は作成中のまま）。
 */
const nestedChatStream: TimedChunk[] = [
  {
    afterMs: 0,
    text: [
      sse("start", {
        conversation_id: "conv-1",
        user_message: userMessage,
        columns: [{ model_id: "m1", label: "MODEL 1", message_id: "a3" }],
      }),
      nestedProgress.start("root", ragSpan.root()),
      nestedProgress.start("answer", ragSpan.stage("answer")),
      nestedProgress.start("flow", ragSpan.step("回答生成フロー")),
      nestedProgress.start("search", ragSpan.step("文書検索", "flow")),
      nestedProgress.start(
        "embed",
        ragSpan.technical("gen_ai.embeddings", "search", {
          "gen_ai.operation.name": "embeddings",
          "gen_ai.request.model": "cohere.embed-v4.0",
        })
      ),
    ].join(""),
  },
  {
    afterMs: 2500,
    text: [nestedProgress.finish("embed"), nestedProgress.start("vector", ragSpan.step("ベクトル検索", "search"))].join(""),
  },
  // ベクトル検索が終わり、次の子がまだ始まっていない間（親の工程の中で子が途切れた間）。
  { afterMs: 4000, text: nestedProgress.finish("vector") },
  { afterMs: 4000, text: nestedProgress.start("text", ragSpan.step("全文検索", "search")) },
  { afterMs: 60_000, text: sse("all_done", { conversation_id: "conv-1" }) },
];

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`チャットの処理の経過は子の段を持つ親の工程を回さず、実行中の末端の段だけを回す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockChatPage(page, [nestedChatStream]);

    await page.goto("/chat");
    await selectSearchAnswerProfile(page, "経理ビュー");
    await page.getByRole("button", { name: "新しい会話" }).click();
    await page.getByRole("textbox").fill(userMessage.content);
    await page.getByRole("button", { name: "送信" }).click();

    const progress = page.getByTestId("chat-answer-progress");
    const current = page.getByTestId("chat-answer-progress-current");
    const row = (key: string) => page.getByTestId(`chat-answer-progress-step-${spanStepId(key)}`);
    const parentsAreStill = async () => {
      for (const key of ["answer", "flow", "search"]) {
        await expect(row(key)).toHaveAttribute("data-status", "running");
        await expect(row(key).locator("svg.animate-spin")).toHaveCount(0);
        await expect(row(key).locator("svg.lucide-circle-play")).toHaveCount(1);
      }
      // 動くスピナーは今の行の 1 つだけ。
      await expect(progress.locator("svg.animate-spin")).toHaveCount(1);
      await expect(current.locator("svg.animate-spin")).toHaveCount(1);
    };

    // 1. 埋め込みが終わり、ベクトル検索が今の段階（検索文から根拠の候補を集めるの子）。
    await expect(current).toContainText("質問に意味が近い文書の箇所を探す", { timeout: 6_000 });
    await expect(current).toHaveAttribute("data-step-id", spanStepId("vector"));
    await expect(row("embed")).toHaveAttribute("data-status", "done");
    await parentsAreStill();
    await progress.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/answer-progress-nested-leaf-${viewport.name}.png`, fullPage: false });

    // 2. ベクトル検索が終わり、次の子がまだ無い間: 検索文から根拠の候補を集めるは回さず、その 1 段下に「処理を続けています」を回す。
    await expect(current).toContainText("処理を続けています", { timeout: 6_000 });
    await expect(current).toHaveAttribute("data-step-id", "");
    await expect(row("vector")).toHaveAttribute("data-status", "done");
    await parentsAreStill();
    // 今の行は親（検索文から根拠の候補を集める）の 1 段下に字下げする。
    const searchIcon = await row("search").locator("svg.lucide-circle-play").boundingBox();
    const workingIcon = await current.locator("svg.animate-spin").boundingBox();
    expect(searchIcon && workingIcon && workingIcon.x > searchIcon.x).toBeTruthy();
    await page.screenshot({ path: `test-results/answer-progress-nested-gap-${viewport.name}.png`, fullPage: false });

    // 3. 次の子（全文検索）が始まると、それが今の段階になる。
    await expect(current).toContainText("検索語を含む文書の箇所を探す", { timeout: 6_000 });
    await expect(current).toHaveAttribute("data-step-id", spanStepId("text"));
    await parentsAreStill();
    await expectNoPageOverflow(page);
  });
}


test("リランクの論理要求は新規配信と保存したtraceで同じ名前・階層を保つ", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const recorder = spanProgressRecorder("trace-rerank");
  const begin = [
    ...recorder.start("root", ragSpan.root()),
    ...recorder.start("answer", ragSpan.stage("answer")),
    ...recorder.start("rank-parent", ragSpan.step("Rerank")),
    ...recorder.start("rank-request", ragSpan.technical("rerank.request", "rank-parent")),
    ...recorder.start("rank-sdk", ragSpan.technical("gen_ai.rerank", "rank-request", { "gen_ai.request.model": "synthetic-rerank", "pr.attempt": 2 })),
  ];
  const finish = [
    ...recorder.finish("rank-sdk"),
    ...recorder.finish("rank-request", "done", { details: { code: [{ label: "rerank", language: "json", text: JSON.stringify({ cache_state: "miss", sdk_attempt_count: 1, sdk_attempts: [{ retry_number: 1, status: "completed" }] }, null, 2) }] } }),
    ...recorder.start("rank-cache", ragSpan.technical("rerank.request", "rank-parent", { "pr.stage": "rerank.request" })),
    ...recorder.finish("rank-cache", "done", { details: { code: [{ label: "rerank", language: "json", text: JSON.stringify({ cache_state: "hit", sdk_attempt_count: 0 }, null, 2) }] } }),
    ...recorder.finish("rank-parent"), ...recorder.finish("answer"), ...recorder.finish("root"), ...recorder.terminal("done"),
  ];
  const content = "合成の回答です。";
  const stream: TimedChunk[] = [
    { afterMs: 0, text: sse("run", { trace_id: "trace-rerank" }) + traceSse(begin) },
    { afterMs: 1200, text: traceSse(finish) + sse("metadata", { trace_id: "trace-rerank", elapsed_ms: 1200, guardrail_warnings: [], diagnostics: {} }) + sse("delta", { text: content }) + sse("done", { answer: content, citations: [] }) },
  ];
  await mockSearchPage(page, [stream]);
  await enableSearchAnswer(page);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("合成の質問");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect(page.getByTestId(`search-run-progress-step-${spanStepId("rank-request")}`)).toContainText("候補の並べ替えを要求する");
  await expect(page.getByTestId("search-run-progress-summary")).toBeVisible();
  await expect(page.getByText(content, { exact: true })).toBeVisible();
  await page.getByTestId("search-run-progress-summary").click();

  async function checkRows(prefix: string) {
    const request = page.getByTestId(`${prefix}-step-${spanStepId("rank-request")}`);
    const sdk = page.getByTestId(`${prefix}-step-${spanStepId("rank-sdk")}`);
    const cached = page.getByTestId(`${prefix}-step-${spanStepId("rank-cache")}`);
    await expect(request).toContainText("候補の並べ替えを要求する");
    await expect(cached).toContainText("候補の並べ替えを要求する");
    await expect(sdk).toContainText("質問との関連度で候補を並べ替える · synthetic-rerank（2 回目）");
    await expect(request).toHaveAttribute("data-level", "2");
    await expect(sdk).toHaveAttribute("data-level", "3");
    await expect(cached).toHaveAttribute("data-level", "2");
    await page.getByTestId(`${prefix}-step-${spanStepId("rank-request")}-toggle`).click();
    const details = page.getByTestId(`${prefix}-step-${spanStepId("rank-request")}-details`);
    await expect(details).not.toContainText("Span name");
    await inspectTraceStep(page, page.getByTestId(prefix), spanStepId("rank-request"));
    await expect(page.locator("[data-trace-technical]")).toContainText("rerank.request");
    await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
    await expect(details).toContainText('"cache_state": "miss"');
    await expect(details).toContainText('"retry_number": 1');
    await page.getByTestId(`${prefix}-step-${spanStepId("rank-cache")}-toggle`).click();
    await expect(page.getByTestId(`${prefix}-step-${spanStepId("rank-cache")}-details`)).toContainText('"sdk_attempt_count": 0');
    await request.scrollIntoViewIfNeeded();
    await expectNoPageOverflow(page);
  }
  await checkRows("search-run-progress");
  await page.screenshot({ path: testInfo.outputPath("rerank-live.png"), fullPage: false });

  // 同じ保存済みイベントを会話のtrace APIから再読込する。記録は書き換えない。
  await page.route("**/api/chat/models", route => route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } }));
  const base = { conversation_id: "conv-rerank", model: null, citations: [], guardrail_warnings: [], status: "COMPLETE", created_at: new Date().toISOString() };
  const conversation = { id: "conv-rerank", search_answer_profile_id: "bv-1", title: "合成の質問", status: "ACTIVE", message_count: 2, created_at: base.created_at, updated_at: base.created_at, messages: [
    { ...base, message_id: "user-rerank", role: "USER", content: "合成の質問", reply_to_message_id: null, trace_id: null },
    { ...base, message_id: "trace-rerank", role: "ASSISTANT", content, reply_to_message_id: "user-rerank", trace_id: "0123456789abcdef0123456789abcdef" },
  ] };
  await page.route("**/api/chat/conversations**", route => route.fulfill({ json: { data: new URL(route.request().url()).pathname.endsWith("/conversations") ? { items: [conversation], total: 1, limit: 50, offset: 0, has_next: false } : conversation, error_messages: [], warning_messages: [] } }));
  await mockTraceApi(page, { "trace-rerank": [...begin, ...finish] });
  await page.goto("/chat?search_answer_profile_id=bv-1&conversation_id=conv-rerank");
  await expect(page.getByText(content, { exact: true })).toBeVisible();
  await page.getByTestId("chat-answer-progress-summary").click();
  await checkRows("chat-answer-progress");
  await page.screenshot({ path: testInfo.outputPath("rerank-saved.png"), fullPage: false });
});
