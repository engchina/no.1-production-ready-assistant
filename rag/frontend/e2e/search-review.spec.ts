import { inspectTraceStep } from "../../../platform/packages/ui/e2e/trace-explorer";
import {timelineSnapshot, timelineTypography} from "../../../platform/packages/ui/e2e/timeline-snapshot";
import { expect, type Page, type Route, test } from "./fixtures/test";
import {
  installTraceRecorder,
  mockTraceApi,
  ragSpan,
  savedSpanProgress,
  SPAN_TRACE_ID,
  spanStepId,
  type ChatProgressEventJson,
} from "./_chat-progress";

import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  selectSearchAnswerProfile,
} from "./_helpers";

/**
 * RAG 検索のレビューで直した不具合の回帰テスト（#285）。
 * stream の途中の失敗・回答の評価先の検索・回答プロファイル・IME の確定の Enter・画面を離れたときの中断・
 * 類似 FAQ を飛ばした後の再試行。
 */

const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

const VIEWS = [
  {
    id: "bv-1",
    name: "経理ビュー",
    description: null,
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: "2026-06-19T00:00:00Z",
    updated_at: "2026-06-19T00:00:00Z",
    archived_at: null,
  },
  {
    id: "bv-2",
    name: "人事ビュー",
    description: null,
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: "2026-06-19T00:00:00Z",
    updated_at: "2026-06-19T00:00:00Z",
    archived_at: null,
  },
];

test.beforeEach(async ({ page }) => {
  // 配信の中の span の記録の印（`__trace`）を、実行の trace の API が返す（#3226）。
  await installTraceRecorder(page);
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockSearchPage(page);
});

async function mockSearchPage(
  page: Page,
  { faqSuggestions = [] as unknown[] }: { faqSuggestions?: unknown[] } = {}
) {
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/approved-faq/suggest")) {
      await route.fulfill(envelope({ suggestions: faqSuggestions }));
      return;
    }
    await route.fulfill(
      envelope({ items: VIEWS, total: VIEWS.length, limit: 50, offset: 0, has_next: false })
    );
  });
  await page.route("**/api/search/answers**", (route) =>
    route.fulfill(envelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }))
  );
  await page.route("**/api/settings/answer-records", (route) =>
    route.fulfill(envelope({ retention_days: 30 }))
  );
  await page.route("**/api/feedback/current**", (route) => route.fulfill(envelope([])));
}

function sse(events: Array<[string, unknown]>): string {
  return events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

async function fulfillStream(route: Route, body: string) {
  await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body });
}

async function search(page: Page, query: string) {
  await enableSearchAnswer(page);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill(query);
  await page.getByRole("button", { name: "検索", exact: true }).click();
}

test("stream の途中の timeout は該当なしではなくエラーとして再試行を出す", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/search/stream", async (route) => {
    calls += 1;
    await fulfillStream(
      route,
      sse([
        ["stage", { trace_id: "t1", stage: "retrieval", outcome: "started", elapsed_ms: 0, attributes: {} }],
        [
          "error",
          {
            trace_id: "t1",
            message: "検索処理がタイムアウトしました。条件を絞って再度お試しください。",
            error_type: "TimeoutError",
          },
        ],
      ])
    );
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "交通費の上限");

  const alert = page.getByRole("alert").filter({ hasText: "タイムアウトしました" });
  await expect(alert).toBeVisible();
  await expect(page.getByText("一致する根拠が見つかりませんでした。")).toHaveCount(0);
  await expectNoPageOverflow(page);

  await alert.getByRole("button", { name: "再試行" }).click();
  await expect.poll(() => calls).toBe(2);
});

for (const outcome of ["needs_clarification", "needs_human", "insufficient_evidence"] as const) {
  test(`根拠 0 件の表示は回答の状態に合わせる (${outcome})`, async ({ page }, testInfo) => {
    const answer = outcome === "needs_clarification" ? "対象の種類を教えてください。" : "資料だけでは回答を確定できません。";
    await page.route("**/api/search/stream", (route) => fulfillStream(route, sse([
      ["metadata", { trace_id: "t-empty", elapsed_ms: 5, guardrail_warnings: [], diagnostics: { answer: { outcome } } }],
      ["delta", { text: answer }],
      ["citations", []],
      ["done", { trace_id: "t-empty" }],
    ])));
    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await search(page, "対象の操作方法");
    await expect(page.getByText(answer, { exact: true })).toBeVisible();
    await expect(page.getByText("一致する根拠が見つかりませんでした。")).toHaveCount(outcome === "insufficient_evidence" ? 1 : 0);
    await expect(page.getByText("回答の前に確認が必要です。上の確認事項への回答を質問に含めて、もう一度検索してください。"))
      .toHaveCount(outcome === "needs_clarification" ? 1 : 0);
    if (outcome === "needs_clarification") {
      const hint = page.getByText("回答の前に確認が必要です。上の確認事項への回答を質問に含めて、もう一度検索してください。");
      await hint.scrollIntoViewIfNeeded();
      await expect(hint).toBeVisible();
    }
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`search-${outcome}.png`), fullPage: true });
  });
}

test("done を受けずに途切れた stream は途中終了のエラーにする", async ({ page }) => {
  await page.route("**/api/search/stream", (route) =>
    fulfillStream(
      route,
      sse([
        ["metadata", { trace_id: "t2", elapsed_ms: 5, guardrail_warnings: [], diagnostics: {} }],
        ["delta", { text: "途中まで" }],
      ])
    )
  );

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "交通費の上限");

  await expect(page.getByRole("alert").filter({ hasText: "回答の受信が途中で途切れました" })).toBeVisible();
});

test("回答の評価は検索したときの検索・回答プロファイルへ送る（検索後に選択を変えても変えない）", async ({ page }) => {
  await page.route("**/api/search/stream", (route) =>
    fulfillStream(
      route,
      sse([
        [
          "metadata",
          {
            trace_id: "t3",
            elapsed_ms: 5,
            guardrail_warnings: [],
            diagnostics: { search_answer_profile_applied: "bv-1" },
          },
        ],
        ["delta", { text: "上限は 5,000 円です。" }],
        ["citations", []],
        ["done", { trace_id: "t3" }],
      ])
    )
  );
  let feedbackBody: Record<string, unknown> | null = null;
  await page.route("**/api/feedback", async (route) => {
    feedbackBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill(
      envelope({
        feedback_id: "f1",
        trace_id: "t3",
        search_answer_profile_id: feedbackBody.search_answer_profile_id,
        target_type: "answer",
        source_surface: "search",
        document_id: null,
        chunk_id: null,
        message_id: null,
        rating: "helpful",
        reason: null,
        comment: null,
        corrected_answer: null,
      })
    );
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "交通費の上限");
  await expect(page.getByText("上限は 5,000 円です。")).toBeVisible();
  await selectSearchAnswerProfile(page, /人事ビュー/);

  await page.getByRole("button", { name: "この回答は役に立った" }).click();
  await expect.poll(() => feedbackBody?.search_answer_profile_id).toBe("bv-1");
});

test("IME の変換を確定する Enter では検索しない", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/search/stream", async (route) => {
    calls += 1;
    await fulfillStream(route, sse([["done", { trace_id: "t4" }]]));
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  const input = page.getByRole("textbox", { name: "RAG 検索" });
  await input.fill("こうつうひ");
  // 変換中の Enter（isComposing=true / keyCode=229）。
  await input.dispatchEvent("keydown", { key: "Enter", keyCode: 229, isComposing: true, bubbles: true });
  await page.waitForTimeout(300);
  expect(calls).toBe(0);

  await input.press("Enter");
  await expect.poll(() => calls).toBe(1);
});

test("回答の生成中に画面を離れたら検索の stream を止める", async ({ page }) => {
  let aborted = false;
  page.on("requestfailed", (request) => {
    if (request.url().includes("/api/search/stream")) aborted = true;
  });
  // 応答を返さず、生成中のままにする。
  await page.route("**/api/search/stream", () => new Promise<void>(() => undefined));

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "交通費の上限");
  await expect(page.getByRole("button", { name: "停止" })).toBeVisible();

  // SPA 内の遷移（再読み込みではない）。
  await page.evaluate(() => {
    window.history.pushState({}, "", "/search-answer-profiles");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect.poll(() => aborted).toBe(true);
});

test("類似 FAQ を使わずに生成した検索の再試行は FAQ を出し直さない", async ({ page }) => {
  await page.unrouteAll();
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockSearchPage(page, {
    faqSuggestions: [
      { id: "faq-1", question: "交通費の上限は？", answer: "5,000 円です。", score: 0.8, direct: false },
    ],
  });
  let calls = 0;
  await page.route("**/api/search/stream", async (route) => {
    calls += 1;
    await route.fulfill({
      status: 429,
      json: {
        data: null,
        error_messages: ["リクエスト数が上限を超えました。しばらく待ってから再度お試しください。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "交通費の上限");
  await page.getByRole("button", { name: "どれでもない（資料を検索する）" }).click();

  const alert = page.getByRole("alert").filter({ hasText: "リクエスト数が上限を超えました" });
  await expect(alert).toBeVisible();
  await alert.getByRole("button", { name: "再試行" }).click();

  await expect.poll(() => calls).toBe(2);
  await expect(page.getByText("質問に近い登録済みQ&Aがあります")).toHaveCount(0);
});

test("質問から読み取った条件を「自動」のチップで出し、外して検索し直せる（#652）", async ({ page }) => {
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/search/stream", (route) => {
    requests.push(route.request().postDataJSON() as Record<string, unknown>);
    const first = requests.length === 1;
    const answer = {
      execution_steps: [],
      evidence_tree: [],
      ...(first
        ? {
            auto_field_filter: {
              conditions: [
                { name: "契約日", value_type: "date", op: "gte", value: "2025-01-01" },
                { name: "金額", value_type: "number", op: "gte", value: "100000" },
              ],
              relaxed: true,
            },
          }
        : {}),
    };
    return fulfillStream(
      route,
      sse([
        ["metadata", { trace_id: `t${requests.length}`, elapsed_ms: 5, guardrail_warnings: [], diagnostics: { answer } }],
        ["citations", []],
        ["done", { trace_id: `t${requests.length}` }],
      ])
    );
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("2025年以降の10万円以上の契約");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  const chips = page.getByTestId("auto-field-filter-chips");
  await expect(chips.getByText("自動: 契約日 ≥ 2025-01-01")).toBeVisible();
  await expect(chips.getByText("自動: 金額 ≥ 100000")).toBeVisible();
  // 読み取った条件で見つからず、外して検索したことを示す。
  await expect(chips.getByRole("status")).toContainText("条件を外して検索しました");
  await expectNoPageOverflow(page);

  await chips.getByRole("button", { name: "金額 ≥ 100000 を外して検索し直す" }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toMatchObject({
    query: "2025年以降の10万円以上の契約",
    auto_field_filter_excluded: ["金額"],
  });
  expect("auto_field_filter_excluded" in requests[0]).toBe(false);
  await expect(page.getByTestId("auto-field-filter-chips")).toHaveCount(0);
});

test("検索・回答プロファイルの読み込み中は読み込み中の状態として読み上げる", async ({ page }) => {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/search-answer-profiles?**", async (route) => {
    await gate;
    await route.fallback();
  });

  await page.goto("/search");
  // 読み込み中は文言と経過時間（TimedLoadingState）を出し、状態として読み上げる（#265）。
  const loading = page.getByTestId("search-search-answer-profiles-loading");
  await expect(loading).toBeVisible();
  await expect(loading.getByRole("timer")).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "検索・回答プロファイルを読み込んでいます。" })).toHaveCount(1);
  await expect(page.getByText("検索・回答プロファイルを作成してください")).toHaveCount(0);
  release();
  await expect(page.getByRole("button", { name: /検索・回答プロファイル/ })).toBeVisible();
});

test("詳細条件は条件を設定したままでも閉じられ、閉じると「設定中」を出す（#461）", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  const advanced = page.getByRole("button", { name: /詳細条件/ });
  await expect(advanced).toHaveAttribute("aria-expanded", "false");

  await advanced.click();
  await expect(advanced).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#search-top-k")).toHaveCount(0);
  await page.locator('button[aria-controls="search-classification-filters"]').click();
  await page.locator("#search-as_of").fill("2026-04-01");
  await page.screenshot({ path: testInfo.outputPath("search-default-conditions.png") });

  // 条件があっても閉じられる。閉じている間は「設定中」で条件が効いていることを示す。
  await advanced.click();
  await expect(advanced).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("combobox", { name: "候補取得数" })).toHaveCount(0);
  await expect(advanced).toContainText("設定中");

  // キーボード（Space / Enter）でも開閉できる。
  await advanced.focus();
  await page.keyboard.press("Space");
  await expect(advanced).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#search-as_of")).toHaveValue("2026-04-01");
  await expect(advanced).not.toContainText("設定中");
  await page.keyboard.press("Enter");
  await expect(advanced).toHaveAttribute("aria-expanded", "false");
  await expectNoPageOverflow(page);
});

test("検索のボタンは詳細条件の下にあり、実行中は同じ位置・同じ要素のまま「停止」になる（#413）", { tag: "@responsive" }, async ({ page }) => {
  let calls = 0;
  let aborted = 0;
  page.on("requestfailed", (request) => {
    if (request.url().includes("/api/search/stream")) aborted += 1;
  });
  // 応答を返さず、生成中のままにする。
  await page.route("**/api/search/stream", () => {
    calls += 1;
    return new Promise<void>(() => undefined);
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  const input = page.getByRole("textbox", { name: "RAG 検索" });
  await input.fill("交通費の上限");

  // 配置: フォームの最後（詳細条件の下）。
  const button = page.getByTestId("search-run-stop");
  const advanced = page.getByRole("button", { name: "詳細条件" });
  const [buttonBox, advancedBox] = await Promise.all([button.boundingBox(), advanced.boundingBox()]);
  if (!buttonBox || !advancedBox) throw new Error("ボタンの位置を計測できません。");
  expect(buttonBox.y).toBeGreaterThan(advancedBox.y + advancedBox.height);
  // 質問欄は検索のボタンと同じ行（詳細条件の下）で、チャットの入力欄と同じ 3.5 行の複数行入力（Shift+Enter で改行。#2008）。
  const inputBox = await input.boundingBox();
  if (!inputBox) throw new Error("質問欄の位置を計測できません。");
  expect(inputBox.y).toBeGreaterThan(advancedBox.y + advancedBox.height);
  await expect(input).toHaveJSProperty("tagName", "TEXTAREA");
  await expect(input).toHaveAttribute("data-rows", "3.5");
  // 高さ = 3.5 行 × 行の高さ + 上下の余白（1rem）+ 枠線（2px）。
  const metrics = await input.evaluate((el) => {
    const style = getComputedStyle(el);
    return {
      height: el.getBoundingClientRect().height,
      lineHeight: Number.parseFloat(style.lineHeight),
      rem: Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
    };
  });
  expect(Math.abs(metrics.height - (3.5 * metrics.lineHeight + metrics.rem + 2))).toBeLessThan(1);
  await input.press("Shift+Enter");
  await expect(input).toHaveValue("交通費の上限\n");
  expect(calls).toBe(0);
  await input.fill("交通費の上限");
  await expect(button).toHaveAccessibleName("検索");
  await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);

  // キーボードで実行 → 同じ要素のまま「停止」になり、フォーカスと位置が変わらない。
  const marker = await button.evaluate((element) => {
    (element as HTMLElement).dataset.e2eMarker = "same-element";
    return true;
  });
  expect(marker).toBe(true);
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(button).toHaveAccessibleName("停止");
  await expect(button).toHaveAttribute("data-e2e-marker", "same-element");
  await expect(button).toBeFocused();
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  const runningBox = await button.boundingBox();
  expect(runningBox?.x).toBe(buttonBox.x);
  expect(runningBox?.y).toBe(buttonBox.y);
  expect(runningBox?.width).toBe(buttonBox.width);
  // 実行中に押せる操作は 1 つだけ（押せない「検索」が並ばない）。
  await expect(page.getByRole("button", { name: "検索", exact: true })).toHaveCount(0);
  expect(calls).toBe(1);

  // 実行中の質問欄の Enter では停止しない（二重に送信もしない）。
  await input.press("Enter");
  await page.waitForTimeout(300);
  await expect(button).toHaveAccessibleName("停止");
  expect(calls).toBe(1);
  expect(aborted).toBe(0);

  // 停止で中断する。フォーカスはボタンに残る。
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(button).toHaveAccessibleName("検索");
  await expect(button).toBeFocused();
  await expect(page.getByText("検索ストリームを停止しました。")).toBeVisible();
  await expect.poll(() => aborted).toBe(1);

  // 停止の後に再び実行できる（ダブルクリックでも 1 回だけ実行し、停止しない）。
  await button.dblclick();
  await expect.poll(() => calls).toBe(2);
  await expect(button).toHaveAccessibleName("停止");
  await page.waitForTimeout(300);
  await expect(button).toHaveAccessibleName("停止");
  expect(aborted).toBe(1);
  await expectNoPageOverflow(page);
});

test.describe("送り直す操作は、表示中の結果を出した検索の条件で送る（#914）", () => {
  const AUTO_ANSWER = {
    execution_steps: [],
    evidence_tree: [],
    auto_field_filter: {
      conditions: [{ name: "金額", value_type: "number", op: "gte", value: "100000" }],
      relaxed: false,
    },
  };

  test("「自動」の条件を外すと、入力欄を書き換えた後でも前の質問・検索・回答プロファイルで検索し直す", async ({
    page,
  }) => {
    const requests: Array<Record<string, unknown>> = [];
    await page.route("**/api/search/stream", (route) => {
      requests.push(route.request().postDataJSON() as Record<string, unknown>);
      const traceId = `t${requests.length}`;
      return fulfillStream(
        route,
        sse([
          ["metadata", { trace_id: traceId, elapsed_ms: 5, guardrail_warnings: [], diagnostics: { answer: AUTO_ANSWER } }],
          ["citations", []],
          ["done", { trace_id: traceId }],
        ])
      );
    });

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    const input = page.getByRole("textbox", { name: "RAG 検索" });
    await input.fill("10万円以上の契約");
    await page.getByRole("button", { name: "検索", exact: true }).click();
    const chips = page.getByTestId("auto-field-filter-chips");
    await expect(chips).toBeVisible();

    // 次の質問を書きかけ、検索・回答プロファイルも変えてから、表示中の結果のチップを外す。
    await input.fill("全く別の質問");
    await selectSearchAnswerProfile(page, /人事ビュー/);
    await chips.getByRole("button", { name: "金額 ≥ 100000 を外して検索し直す" }).click();

    await expect.poll(() => requests.length).toBe(2);
    expect(requests[1]).toMatchObject({
      query: "10万円以上の契約",
      search_answer_profile_id: "bv-1",
      auto_field_filter_excluded: ["金額"],
    });
    // 書きかけの入力と選択は消さない。
    await expect(input).toHaveValue("全く別の質問");
    await expect(page.getByRole("button", { name: /検索・回答プロファイル/ })).toContainText("人事ビュー");
  });

  test("エラーの再試行は、入力欄を書き換えた後でも失敗した質問を送り直す", async ({ page }) => {
    const requests: Array<Record<string, unknown>> = [];
    await page.route("**/api/search/stream", (route) => {
      requests.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill({
        status: 503,
        json: { data: null, error_messages: ["一時的に利用できません。"], warning_messages: [] },
      });
    });

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await search(page, "最初の質問");
    const alert = page.getByRole("alert").filter({ hasText: "一時的に利用できません" });
    await expect(alert).toBeVisible();

    await page.getByRole("textbox", { name: "RAG 検索" }).fill("書きかけ");
    await alert.getByRole("button", { name: "再試行" }).click();

    await expect.poll(() => requests.length).toBe(2);
    expect(requests[1]).toMatchObject({
      query: "最初の質問",
      search_answer_profile_id: "bv-1",
      generate_answer: true,
    });
  });

  test("「どれでもない（資料を検索する）」は、類似 FAQ を確かめた質問を送る", async ({ page }) => {
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
      route.fulfill(
        envelope({
          suggestions: [
            { id: "faq-1", question: "交通費の上限は？", answer: "5,000 円です。", score: 0.8, direct: false },
          ],
        })
      )
    );
    const requests: Array<Record<string, unknown>> = [];
    await page.route("**/api/search/stream", (route) => {
      requests.push(route.request().postDataJSON() as Record<string, unknown>);
      return fulfillStream(
        route,
        sse([
          ["metadata", { trace_id: "t1", elapsed_ms: 5, guardrail_warnings: [], diagnostics: {} }],
          ["citations", []],
          ["done", { trace_id: "t1" }],
        ])
      );
    });

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await search(page, "交通費の上限");
    await expect(page.getByText("質問に近い登録済みQ&Aがあります")).toBeVisible();

    await page.getByRole("textbox", { name: "RAG 検索" }).fill("書きかけ");
    await page.getByRole("button", { name: "どれでもない（資料を検索する）" }).click();

    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0]).toMatchObject({ query: "交通費の上限", search_answer_profile_id: "bv-1" });
  });
});

test.describe("類似 FAQ の照会中も、検索を始めたことを示す（#915）", () => {
  test("押した直後に「停止」と進行を出し、FAQ があれば提示する", async ({ page }) => {
    let releaseFaq: () => void = () => undefined;
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", async (route) => {
      await new Promise<void>((resolve) => {
        releaseFaq = resolve;
      });
      await route.fulfill(
        envelope({
          suggestions: [
            { id: "faq-1", question: "交通費の上限は？", answer: "5,000 円です。", score: 0.8, direct: false },
          ],
        })
      );
    });
    let streamCalls = 0;
    await page.route("**/api/search/stream", (route) => {
      streamCalls += 1;
      return route.abort();
    });

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限");
    const button = page.getByTestId("search-run-stop");
    await button.click();

    // 照会の応答を待つ間も実行中（停止できる・進行を出す・条件を変えられない）。
    await expect(button).toHaveAccessibleName("停止");
    await expect(page.getByTestId("search-run-progress")).toBeVisible();
    await expect(page.getByRole("switch", { name: "LLM で回答を生成する" })).toBeDisabled();
    await expectNoPageOverflow(page);

    releaseFaq();
    await expect(page.getByText("質問に近い登録済みQ&Aがあります")).toBeVisible();
    await expect(button).toHaveAccessibleName("検索");
    await expect(page.getByTestId("search-run-progress")).toHaveCount(0);
    expect(streamCalls).toBe(0);
  });

  test("照会中に停止したら、照会の結果を捨てて検索を送らない", async ({ page }) => {
    let releaseFaq: () => void = () => undefined;
    await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", async (route) => {
      await new Promise<void>((resolve) => {
        releaseFaq = resolve;
      });
      await route.fulfill(envelope({ suggestions: [] }));
    });
    let streamCalls = 0;
    await page.route("**/api/search/stream", (route) => {
      streamCalls += 1;
      return route.abort();
    });

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限");
    const button = page.getByTestId("search-run-stop");
    await button.click();
    await expect(button).toHaveAccessibleName("停止");
    // ダブルクリックの 2 回目として無視されないよう、間を空けて押す。
    await page.waitForTimeout(600);
    await button.click();

    await expect(button).toHaveAccessibleName("検索");
    await expect(page.getByText("検索ストリームを停止しました。")).toBeVisible();
    releaseFaq();
    await page.waitForTimeout(300);
    expect(streamCalls).toBe(0);
    await expect(button).toHaveAccessibleName("検索");
  });
});

// 回答の注意書き（設定は製品ごと。#2405・#2449）。回答の本文と根拠の後に出す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`RAG 検索の回答の末尾に注意書きを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const notice = "この回答は AI が生成したものです。\n業務に使う前に根拠で確かめてください。";
    await page.route("**/api/search/stream", (route) =>
      fulfillStream(
        route,
        sse([
          ["metadata", { trace_id: "t-notice", elapsed_ms: 5, guardrail_warnings: [], diagnostics: {}, notice }],
          ["delta", { text: "交通費の上限は 1 万円です。" }],
          ["citations", []],
          ["done", { trace_id: "t-notice" }],
        ])
      )
    );

    await page.goto("/search");
    await selectSearchAnswerProfile(page, /経理ビュー/);
    await search(page, "交通費の上限");

    await expect(page.getByText("交通費の上限は 1 万円です。")).toBeVisible();
    const noticeBlock = page.getByRole("complementary", { name: "回答の注意書き" });
    await expect(noticeBlock).toBeVisible();
    await expect(noticeBlock).toContainText("業務に使う前に根拠で確かめてください。");
    await expect(page.getByText("交通費の上限は 1 万円です。")).not.toContainText("AI が生成");
    await expectNoPageOverflow(page);
  });
}


test("FAQ の提示対象が無いときは質問を止めず検索する", { tag: "@responsive" }, async ({ page }) => {
  let searched = false;
  await page.route("**/api/search/stream", (route) => {
    searched = true;
    return fulfillStream(route, sse([
      ["metadata", { trace_id: "faq-filtered", elapsed_ms: 5, guardrail_warnings: [], diagnostics: {} }],
      ["citations", []], ["done", { trace_id: "faq-filtered" }],
    ]));
  });
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await search(page, "合成の設定 A");
  await expect.poll(() => searched).toBe(true);
  await expect(page.getByText("質問に近い登録済みQ&Aがあります")).toHaveCount(0);
});

/**
 * 検索の配信の最初の実行の id（`run`）と、表へ書いた span の記録の印（実行の trace の API が返す。#3226）。
 * 実行の id は span のイベントの対象（`target_id`）。
 */
function progressSse(events: ChatProgressEventJson[]): [string, unknown][] {
  return [
    ["run", { trace_id: events[0]?.target_id ?? "" }],
    ...events.map((event): [string, unknown] => ["__trace", event]),
  ];
}

test("RAG 検索の処理の経過は span の木で、2 つ目の「トレースの詳細」は出さない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const progress = savedSpanProgress(
    "synthetic-a",
    [
      { key: "root", spec: ragSpan.root(), at: [0, 2] },
      { key: "filter", spec: ragSpan.stage("field_filter"), at: [0, 2] },
      { key: "chat", spec: ragSpan.technical("gen_ai.chat", "filter", { "gen_ai.request.model": "synthetic-model", "pr.gen_ai.purpose": "query.conditions" }), at: [0, 2] },
    ],
    { terminal: "done", base: "2026-01-01T00:00:00Z" }
  );
  await page.route("**/api/search/stream", route => fulfillStream(route, sse([
    ...progressSse(progress),
    ["metadata", { trace_id: "synthetic-a", elapsed_ms: 2000, guardrail_warnings: [], diagnostics: { answer: { outcome: "answered" } } }],
    ["delta", { text: "合成の回答です。" }], ["citations", []], ["done", { trace_id: "synthetic-a" }],
  ])));
  await page.goto("/search");
  await selectSearchAnswerProfile(page, VIEWS[0].name);
  await search(page, "合成データを確認");
  await page.getByTestId("search-run-progress-summary").click();
  const process = page.getByTestId("search-run-progress");
  // 段の名前と技術の span の日本語の名前・モデル名。入れ子は span の親子（root は行にしない）。
  await expect(process.locator("[data-run-timeline-label]")).toHaveText(["質問から検索の絞り込み条件を読み取る", "質問から絞り込み条件を読み取る"]);
  await expect(process.locator("[data-run-timeline-step]").nth(1)).toHaveAttribute("data-level", "1");
  await expect(process.locator("[data-run-timeline-step]").nth(1)).toContainText("2.0 秒");
  const modelStep = process.locator("[data-run-timeline-step]").nth(1);
  await inspectTraceStep(page, process, (await modelStep.getAttribute("data-run-timeline-step"))!);
  await expect(page.locator("[data-trace-technical]")).toContainText("synthetic-model");
  await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${SPAN_TRACE_ID}`);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  await expect(page.getByTestId("trace-details")).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("rag-search-span-tree.png"), fullPage: true });
});


for (const scenario of [
  { name: "無効", outcome: "skipped", status: "skipped", verdict: "無効のため省略", enabled: false },
  { name: "正常", outcome: "success", status: "done", verdict: "問題なし", enabled: true },
  { name: "検出による停止", outcome: "success", status: "done", verdict: "回答を止めました", enabled: true },
  { name: "検査不能", outcome: "error", status: "failed", verdict: "安全検査を完了できず、回答を止めました", enabled: true },
]) {
  test(`出力の安全チェックの実施結果を区別する: ${scenario.name}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    // 検査の段の span の状態と詳細（#3226）。
    const progress = savedSpanProgress(
      "synthetic-guardrail",
      [
        { key: "root", spec: ragSpan.root() },
        {
          key: "guardrail",
          spec: ragSpan.stage("answer_guardrail"),
          status: scenario.status as "done" | "failed" | "skipped",
          details: { fields: [{ label: "結果", value: scenario.verdict }] },
        },
      ],
      { terminal: "done" }
    );
    await page.route("**/api/search/stream", route => fulfillStream(route, sse([
      ...progressSse(progress),
      ["metadata", { trace_id: "synthetic-guardrail", elapsed_ms: 2, guardrail_warnings: [], diagnostics: { guardrails_output_enabled: scenario.enabled, answer: { outcome: "answered" } } }],
      ["delta", { text: "合成の回答です。" }], ["citations", []], ["done", { trace_id: "synthetic-guardrail" }],
    ])));
    await page.goto("/search");
    await selectSearchAnswerProfile(page, VIEWS[0].name);
    await search(page, "合成データを確認");
    await expect(page.getByText("合成の回答です。", { exact: true })).toBeVisible();
    // 処理の経過は実行の trace の API で読む（回答の本文の後に届くことがある。#3226）。段が出てから開閉を決める。
    await expect(page.getByTestId("search-run-progress-summary")).toContainText("1 ステップ");
    const row = page.getByTestId(`search-run-progress-step-${spanStepId("guardrail")}`);
    if (!(await row.isVisible())) await page.getByTestId("search-run-progress-summary").click();
    await expect(row).toHaveAttribute("data-status", scenario.status);
    const toggle = row.getByRole("button").first();
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await expect(row).toContainText(scenario.verdict);
    if (!scenario.enabled) await expect(row).not.toContainText("問題なし");
    await row.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("answer-guardrail-state.png") });
    await expectNoPageOverflow(page);
  });
}

test("RAG 検索と履歴の工程の内容・階層・状態・時間が一致する", {tag: "@responsive"}, async ({page}, testInfo) => {
  // 工程の span の親子（回答生成フロー > 文書検索 > Rerank）と回答に安全上の問題がないか検査する（#3226）。
  const progress_events = savedSpanProgress(
    "synthetic-parity",
    [
      { key: "root", spec: ragSpan.root(), at: [0, 1.001] },
      { key: "flow", spec: ragSpan.step("回答生成フロー", "root"), at: [0, 1], params: { note: "根拠不足で終了", llm_calls: 1 } },
      { key: "search", spec: ragSpan.step("文書検索", "flow"), at: [0.1, 0.5] },
      { key: "rerank", spec: ragSpan.step("Rerank", "search"), at: [0.2, 0.4] },
      {
        key: "guardrail",
        spec: ragSpan.stage("answer_guardrail"),
        at: [1, 1.001],
        status: "skipped",
        details: { fields: [{ label: "結果", value: "無効のため省略" }] },
      },
    ],
    { terminal: "done", base: "2026-10-23T00:00:00Z" }
  );
  await page.route("**/api/search/stream", (route) => fulfillStream(route, sse([
    ...progressSse(progress_events),
    ["metadata", {trace_id: "synthetic-parity", elapsed_ms: 1001, guardrail_warnings: [], diagnostics: {answer: {outcome: "insufficient_evidence"}}}],
    ["delta", {text: "合成の回答です。"}], ["citations", []], ["done", {trace_id: "synthetic-parity"}],
  ])));
  await page.goto("/search");
  await selectSearchAnswerProfile(page, VIEWS[0].name);
  await search(page, "合成の質問");
  await expect(page.getByText("合成の回答です。", {exact: true})).toBeVisible();
  await page.getByTestId("search-run-progress-summary").click();
  const live = await timelineSnapshot(page.getByTestId("search-run-progress"));
  const liveTypography = await timelineTypography(page.getByTestId("search-run-progress"));
  expect(liveTypography.steps.length).toBeGreaterThan(0);
  expect(liveTypography.steps.every(style => style?.fontSize === "12px" && style.fontFamily.includes("Noto Sans JP"))).toBe(true);
  expect(liveTypography.summary?.fontSize).toBe("12px");
  expect(live.map((row) => row.level)).toEqual([0,1,2,0]);
  await page.screenshot({path: testInfo.outputPath("search-process.png")});
  await page.route("**/api/history/runs/synthetic-parity", (route) => route.fulfill(envelope({
    citation_count: 0, answer_outcome: "insufficient_evidence", search_answer_profile_id: "bv-1", search_answer_profile_name: "プロファイル A",
    run_id: "synthetic-parity", surface: "search", question: "合成の質問", status: "success", answer_generated: true,
    created_at: "2026-10-23T00:00:01.001Z", elapsed_ms: 1001, executed_by_me: true, executor_display_name: "利用者 A",
    feedback: [], citations: [], answer: "合成の回答です。", answer_available: true,
    answer_diagnostics: null, conversation_owner_display_name: null, owned_by_me: null,
  })));
  // 実行の trace の API は RAG 検索の画面と同じ span を返す（#3226）。
  await mockTraceApi(page, { "synthetic-parity": progress_events });
  await page.goto("/history/synthetic-parity");
  await page.getByTestId("run-history-timeline-summary").click();
  expect(await timelineSnapshot(page.getByTestId("run-history-timeline"))).toEqual(live);
  expect(await timelineTypography(page.getByTestId("run-history-timeline"))).toEqual(liveTypography);
  await expectNoPageOverflow(page);
  await page.getByTestId("run-history-timeline").screenshot({path: testInfo.outputPath("history-process.png")});
});


test("旧取得件数の URL・保存値を無視し、検索・再試行に top_k を送らない", { tag: "@responsive" }, async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem("production-ready-rag.workspace.v1:search.topK", JSON.stringify({ value: "100", savedAt: Date.now() }));
  });
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/search/stream", async (route) => {
    requests.push(route.request().postDataJSON());
    await fulfillStream(route, sse([["error", { trace_id: "t1", message: "検索処理がタイムアウトしました。", error_type: "TimeoutError" }]]));
  });
  await page.goto("/search?question=sample&search_answer_profile_id=bv-1&top_k=100");
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.locator("#search-query")).toHaveValue("sample");
  await page.getByRole("button", { name: /詳細条件/ }).click();
  await expect(page.locator("#search-top-k")).toHaveCount(0);
  await page.getByRole("button", { name: "検索", exact: true }).click();
  const alert = page.getByRole("alert").filter({ hasText: "タイムアウトしました" });
  await expect(alert).toBeVisible();
  await alert.getByRole("button", { name: "再試行" }).click();
  await expect.poll(() => requests.length).toBe(2);
  for (const request of requests) {
    expect(request).not.toHaveProperty("top_k");
    expect(request).toMatchObject({ query: "sample", search_answer_profile_id: "bv-1" });
  }
  expect(await page.evaluate(() => sessionStorage.getItem("production-ready-rag.workspace.v1:search.topK"))).toBeNull();
  await expectNoPageOverflow(page);
});
