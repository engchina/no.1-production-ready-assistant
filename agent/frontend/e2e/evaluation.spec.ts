import type { Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { MOCK_NOW, evaluationSummary, expect, test, type MockApi } from "./fixtures/mock-api";

// #776: 業務 Agent の品質評価（評価セットで実行し、LLM で判定して合格率とツールの選択を出す）。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

const SET = {
  id: "evset-seeded",
  agent_id: "default",
  name: "経理の評価",
  description: "月次の問い合わせ",
  cases: [
    { id: "expense-deadline", question: "経費精算の締め日はいつですか？", expected: "毎月 25 日", expected_tools: ["rag_search"] },
    { id: "sales-total", question: "今月の売上の合計はいくらですか？", expected: "合計金額（円）", expected_tools: ["nl2sql_query"] },
    { id: "register", question: "取引先を登録して", expected: "登録の手順", expected_tools: [] },
  ],
  created_by_user_uuid: "local",
  created_at: MOCK_NOW,
  updated_at: MOCK_NOW,
};

function seedSet(mockApi: MockApi) {
  mockApi.state.evaluationSets.push(structuredClone(SET));
}

type EvaluationResultFixture = {
  case: (typeof SET.cases)[number];
  status: string;
  run_id: string;
  answer: string;
  judgement: { verdict: string; score: number; summary: string; missing_points: string[] };
  tool_calls: string[];
  tool_selection_correct: boolean | null;
  error: string | null;
  duration_ms: number;
};

function seedFinishedJobs(mockApi: MockApi) {
  seedSet(mockApi);
  // 配列の要素ごとの literal の型（空の配列は never[]）にせず、ケースの結果の型にそろえる。
  const results: EvaluationResultFixture[] = [
    {
      case: SET.cases[0],
      status: "judged",
      run_id: "run-eval-1",
      answer: "毎月 25 日です。",
      judgement: { verdict: "correct", score: 1, summary: "要点を満たしています。", missing_points: [] },
      tool_calls: ["rag__rag_search"],
      tool_selection_correct: true,
      error: null,
      duration_ms: 3200,
    },
    {
      case: SET.cases[1],
      status: "judged",
      run_id: "run-eval-2",
      answer: "分かりません。",
      judgement: {
        verdict: "incorrect",
        score: 0.2,
        summary: "金額を答えていません。",
        missing_points: ["今月の売上の合計金額"],
      },
      tool_calls: [],
      tool_selection_correct: false,
      error: null,
      duration_ms: 5100,
    },
    {
      case: SET.cases[2],
      status: "judged",
      run_id: "run-eval-3",
      answer: "取引先の登録の手順は次のとおりです。",
      judgement: { verdict: "correct", score: 0.9, summary: "手順を答えています。", missing_points: [] },
      tool_calls: ["eval776_register"],
      tool_selection_correct: null,
      error: null,
      duration_ms: 2100,
    },
  ];
  const previousResults = results.map((item) => ({
    ...item,
    judgement: { ...item.judgement, verdict: "incorrect", score: 0.2 },
  }));
  const job = (id: string, items: typeof results) => ({
    id,
    agent_id: "default",
    agent_name: "資料・データ横断アシスタント",
    set_id: SET.id,
    set_name: SET.name,
    status: "completed",
    created_by_user_uuid: "local",
    results: items,
    error: null,
    summary: evaluationSummary(items),
    created_at: MOCK_NOW,
    started_at: MOCK_NOW,
    finished_at: MOCK_NOW,
    agent_version: 1 as number | "draft" | null,
    previous_job_id: null as string | null,
    previous_summary: null as unknown,
    previous_agent_version: null as number | "draft" | null,
    previous_created_at: null as string | null,
  });
  const latest = job("eval-latest", results);
  const previous = job("eval-previous", previousResults);
  latest.agent_version = "draft";
  latest.previous_job_id = previous.id;
  latest.previous_summary = previous.summary;
  latest.previous_agent_version = previous.agent_version;
  latest.previous_created_at = previous.created_at;
  mockApi.state.evaluations.push(latest, previous);
}

for (const viewport of VIEWPORTS) {
  test(`評価セットを作り、評価を始めると進み具合の後に結果を出す (${viewport.name})`, async ({ page, mockApi }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/evaluation");
    await expect(page.getByRole("heading", { name: "品質評価", level: 1 })).toBeVisible();
    await expect(page.getByText("この業務 Agent の評価セットはまだありません")).toBeVisible();

    await page.getByRole("button", { name: "評価セットを作成" }).first().click();
    await expect(page).toHaveURL(/\/evaluation\?id=new$/);
    // 必須の入力が無ければ保存しない。
    await page.getByRole("button", { name: "作成", exact: true }).first().click();
    await expect(page.getByText("名前を入力してください。")).toBeVisible();
    await expect(page.getByText("質問を入力してください。")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/evaluation-sets")).toBeUndefined();

    await page.getByLabel("名前").fill("経理の評価");
    const first = page.getByTestId("evaluation-case-1");
    await first.getByLabel("質問").fill("経費精算の締め日はいつですか？");
    await first.getByLabel("期待する回答の要点").fill("毎月 25 日");
    await first.getByLabel("期待するツール").fill("rag_search");
    await page.getByRole("button", { name: "ケースを追加" }).click();
    const second = page.getByTestId("evaluation-case-2");
    await second.getByLabel("質問").fill("今月の売上の合計はいくらですか？");
    await second.getByLabel("期待する回答の要点").fill("合計金額（円）");
    await page.getByRole("button", { name: "作成", exact: true }).first().click();

    await expect(page.getByText("評価セットを作成しました")).toBeVisible();
    await expect(page).toHaveURL(/\/evaluation\?id=evset-1$/);
    expect(mockApi.lastRequest("POST", "/api/evaluation-sets")?.body).toEqual({
      agent_id: "default",
      name: "経理の評価",
      description: "",
      cases: [
        {
          question: "経費精算の締め日はいつですか？",
          expected: "毎月 25 日",
          expected_tools: ["rag_search"],
          expected_tools_in_order: false,
          clarification_answers: [],
          source_run_id: null,
        },
        {
          question: "今月の売上の合計はいくらですか？",
          expected: "合計金額（円）",
          expected_tools: [],
          expected_tools_in_order: false,
          clarification_answers: [],
          source_run_id: null,
        },
      ],
    });

    // 評価セットから評価を始める（対象の操作は概要の ObjectActionBar）。
    const actions = page.getByTestId("evaluation-set-actions");
    await expect(actions).toBeVisible();
    const startButton = actions.getByRole("button", { name: "評価を開始" });
    if (await startButton.count()) {
      await startButton.click();
    } else {
      await page.getByTestId("evaluation-set-actions-more").click();
      await page.getByRole("menuitem", { name: "評価を開始" }).click();
    }
    await expect(page.getByText("評価を開始しました")).toBeVisible();
    await expect(page).toHaveURL(/\/evaluation$/);
    // 公開していない変更の無い業務 Agent は、既定で公開中の版を評価する（#810）。
    expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toEqual({ set_id: "evset-1", agent_version: "published" });

    await expect(page.getByTestId("evaluation-progress")).toContainText("経理の評価 を評価しています（0 / 2 件）");
    const summary = page.getByTestId("evaluation-summary");
    await expect(summary).toContainText("50%");
    await expect(summary).toContainText("ツールの選択の正しさ");
    await expect(page.getByTestId("evaluation-progress")).toHaveCount(0);
    const results = page.getByRole("table", { name: "ケース別結果" });
    await expect(results.getByRole("row", { name: /case-1/ })).toContainText("期待どおり");
    await expect(results.getByRole("row", { name: /case-2/ })).toContainText("誤り");
    await expect(page.getByRole("table", { name: "評価セットの一覧" }).getByRole("row", { name: /経理の評価/ })).toContainText(
      "50%"
    );
    await expectNoHorizontalOverflow(page);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`前回との比較・ツールの選択・ケースの詳細を出す (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      seedFinishedJobs(mockApi);
      await page.goto("/evaluation");

      const summary = page.getByTestId("evaluation-summary");
      await expect(summary).toContainText("67%");
      await expect(summary).toContainText("前回から +67 ポイント");
      await expect(summary).toContainText("50%");
      await expect(summary).toContainText("1 / 2 件");
      // どの版どうしの比較かを出す（#810）。
      await expect(page.getByTestId("evaluation-previous")).toContainText("同じ評価ケースで完了した前回の評価（v1・");
      await expect(page.getByText("経理の評価（資料・データ横断アシスタント・下書き）")).toBeVisible();
      await expect(
        page.getByRole("table", { name: "最近の評価" }).getByRole("row").nth(1)
      ).toContainText("下書き");
      const results = page.getByRole("table", { name: "ケース別結果" });
      await expect(results.getByRole("row", { name: /sales-total/ })).toContainText("期待と違う");
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`evaluation-${viewport.name}-${theme}.png`), fullPage: true });

      await results.getByRole("button", { name: /今月の売上の合計はいくらですか？/ }).click();
      const detail = page.getByRole("dialog", { name: "ケースの詳細" });
      await expect(detail).toContainText("合計金額（円）");
      await expect(detail).toContainText("金額を答えていません。");
      await expect(detail).toContainText("nl2sql_query");
      await expect(detail).toContainText("承認が要るツールを実行せずに");
      await expect(detail).toBeInViewport({ ratio: 1 });
      // ケースの詳細のシートは md（40rem = 560px。画面の幅 − 3.5rem まで。#2283）。
      const sheetBox = await detail.boundingBox();
      expect(Math.round(sheetBox!.width)).toBe(Math.min(560, viewport.width - 49));
      await expect(results.getByRole("button", { name: /今月の売上の合計はいくらですか？/ })).toHaveAttribute("aria-current", "true");
      await page.screenshot({ path: testInfo.outputPath(`evaluation-detail-${viewport.name}-${theme}.png`) });
      // 見出しの行の「前へ / 次へ」で、シートを開いたまま評価の結果の前後のケースへ移る。
      const previousCase = page.getByTestId("evaluation-case-pager-previous");
      const nextCase = page.getByTestId("evaluation-case-pager-next");
      const forward = (await nextCase.isEnabled()) ? nextCase : previousCase;
      const back = forward === nextCase ? previousCase : nextCase;
      await forward.click();
      await expect(detail).not.toContainText("金額を答えていません。");
      await back.click();
      await expect(detail).toContainText("金額を答えていません。");
    });
  }
}

for (const viewport of VIEWPORTS) {
  test(`期待するツールの順を指定でき、順の違い・判定できなかったケースを誤りと分けて出す (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    // #1845: ツールの順（先頭が最初に呼ぶツール）と、判定の打ち切りの再試行の後の「判定できなかった」。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedFinishedJobs(mockApi);
    const [latest] = mockApi.state.evaluations;
    const results = latest.results as Record<string, unknown>[];
    results[0] = {
      ...results[0],
      case: { ...SET.cases[0], expected_tools: ["rag_retrieve_evidence", "rag_search"], expected_tools_in_order: true },
      tool_calls: ["rag__rag_search", "rag__rag_retrieve_evidence"],
      tool_selection_correct: false,
      tool_order_correct: false,
    };
    results[2] = {
      ...results[2],
      status: "judge_failed",
      judgement: null,
      judge_attempts: 2,
      error: "判定のモデルの応答が、2 回送っても出力の上限で止まりました。回答の誤りではなく、判定できなかったケースです。",
    };
    latest.summary = evaluationSummary(results);
    await page.goto("/evaluation");

    const summary = page.getByTestId("evaluation-summary");
    // 判定できなかったケースは合格率の分母から除く（正しい 1 件 / 判定した 2 件）。
    await expect(summary).toContainText("50%");
    await expect(summary).toContainText("うち判定できなかった 1 件（合格率から除く）");
    const table = page.getByRole("table", { name: "ケース別結果" });
    await expect(table.getByRole("row", { name: /expense-deadline/ })).toContainText("順が違う");
    await expect(table.getByRole("row", { name: /register/ })).toContainText("判定できなかった");
    await expectNoHorizontalOverflow(page);

    await table.getByRole("button", { name: /取引先を登録して/ }).click();
    const detail = page.getByRole("dialog", { name: "ケースの詳細" });
    await expect(detail.getByTestId("evaluation-judge-retried")).toContainText("2 回送りました");
    await expect(detail).toContainText("回答の誤りではなく、判定できなかったケースです。");
    await page.screenshot({ path: testInfo.outputPath(`evaluation-unjudged-${viewport.name}.png`) });
    await detail.getByRole("button", { name: "閉じる" }).first().click();
    await table.getByRole("button", { name: /経費精算の締め日はいつですか？/ }).click();
    await expect(page.getByRole("dialog", { name: "ケースの詳細" })).toContainText("（この順に呼ぶ）");

    // エディタで順の指定を切り替えて保存する。
    await page.goto(`/evaluation?id=${SET.id}`);
    const orderSwitch = page.getByTestId("evaluation-case-1-tools-order");
    await expect(orderSwitch).toHaveAttribute("aria-checked", "false");
    await expect(orderSwitch).toHaveAccessibleName("書いた順に呼ぶ");
    await expect(orderSwitch).toHaveAccessibleDescription(/先頭が最初に呼ぶツール/);
    await orderSwitch.click();
    await expect(orderSwitch).toHaveAttribute("aria-checked", "true");
    await page.getByRole("button", { name: "保存", exact: true }).first().click();
    await expect(page.getByText("評価セットを保存しました")).toBeVisible();
    const saved = mockApi.lastRequest("PUT", `/api/evaluation-sets/${SET.id}`)?.body as { cases: Record<string, unknown>[] };
    expect(saved.cases.map((item) => item.expected_tools_in_order)).toEqual([true, false, false]);
    await expectNoHorizontalOverflow(page);
  });
}

for (const viewport of VIEWPORTS) {
  test(`評価セットをほかの業務 Agent で評価し、2 つの評価をケースごとに比べる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    // #1845: 同じ評価ケースを別の業務 Agent で評価し、評価を並べて比べる。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedFinishedJobs(mockApi);

    // エディタで評価する業務 Agent を選ぶと、その業務 Agent で評価する（版の選択もその業務 Agent に従う）。
    await page.goto(`/evaluation?id=${SET.id}`);
    const evaluated = page.getByTestId("evaluation-set-evaluated-agent");
    await expect(evaluated).toContainText("資料・データ横断アシスタント");
    await evaluated.click();
    await page.getByRole("option", { name: "資料調査アシスタント" }).click();
    await expect(evaluated).toContainText("資料調査アシスタント");
    await expectNoHorizontalOverflow(page);
    const actions = page.getByTestId("evaluation-set-actions");
    const startButton = actions.getByRole("button", { name: "評価を開始" });
    if (await startButton.count()) {
      await startButton.click();
    } else {
      await page.getByTestId("evaluation-set-actions-more").click();
      await page.getByRole("menuitem", { name: "評価を開始" }).click();
    }
    await expect(page.getByText("評価を開始しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toEqual({
      set_id: SET.id,
      agent_version: "published",
      agent_id: "rag-research",
    });
    await expect(page.getByTestId("evaluation-progress")).toHaveCount(0);
    await expect(page.getByText("経理の評価（資料調査アシスタント・v1）")).toBeVisible();

    // 評価を比べる: 基準（前の評価）と比べる評価（ほかの業務 Agent の評価）を選ぶ。
    const compare = page.getByTestId("evaluation-compare");
    await compare.scrollIntoViewIfNeeded();
    await expect(compare.getByText("基準の評価と比べる評価を選ぶと、ケースごとの差を出します。")).toBeVisible();
    await compare.getByTestId("evaluation-compare-base").click();
    await page.getByRole("option", { name: /資料・データ横断アシスタント・v1/ }).click();
    await compare.getByTestId("evaluation-compare-target").click();
    await page.getByRole("option", { name: /資料調査アシスタント/ }).click();
    const summary = compare.getByTestId("evaluation-compare-summary");
    await expect(summary).toContainText("合格率 0% → 67%");
    await expect(summary).toContainText("改善 2 件・悪化 0 件・変化なし 1 件");
    const table = compare.getByRole("table", { name: "評価の比較" });
    await expect(table.getByRole("row", { name: /経費精算の締め日はいつですか？/ })).toContainText("改善");
    await expect(table.getByRole("row", { name: /今月の売上の合計はいくらですか？/ })).toContainText("変化なし");
    const request = mockApi.lastRequest("GET", "/api/evaluations/compare");
    expect(request?.searchParams.get("base_job_id")).toBe("eval-previous");
    expect(request?.searchParams.get("target_job_id")).toBe("eval-3");
    await expectNoHorizontalOverflow(page);
    await compare.screenshot({ path: testInfo.outputPath(`evaluation-compare-${viewport.name}.png`) });
  });
}

for (const viewport of VIEWPORTS) {
  test(`評価ケースに問い返しへの答えを持たせ、往復した結果を出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    // #1845: 問い返し（回答前の確認）への答え（共通の ClarificationCard と同じ形）を評価ケースに持つ。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSet(mockApi);
    await page.goto(`/evaluation?id=${SET.id}`);

    const sales = page.getByTestId("evaluation-case-2");
    await sales.getByRole("button", { name: "答えを追加" }).click();
    const answer = page.getByTestId("evaluation-case-2-answer-1");
    await expect(answer.getByLabel("確認事項の ID")).toBeVisible();
    // 候補も自由入力も無い答えは保存しない（欄にエラーを出す）。
    await page.getByRole("button", { name: "保存", exact: true }).first().click();
    await expect(answer.getByText("選ぶ候補か自由入力のどちらかを入れてください。")).toBeVisible();
    expect(mockApi.lastRequest("PUT", `/api/evaluation-sets/${SET.id}`)).toBeUndefined();

    await answer.getByLabel("選ぶ候補").fill("受注（SALES_ORDERS）, cand-orders");
    await answer.getByLabel("その他（自由入力）").fill("今月の確定分だけ");
    await expectNoHorizontalOverflow(page);
    await sales.screenshot({ path: testInfo.outputPath(`evaluation-clarification-editor-${viewport.name}.png`) });
    await page.getByRole("button", { name: "保存", exact: true }).first().click();
    await expect(page.getByText("評価セットを保存しました")).toBeVisible();
    const saved = mockApi.lastRequest("PUT", `/api/evaluation-sets/${SET.id}`)?.body as { cases: Record<string, unknown>[] };
    expect(saved.cases[1].clarification_answers).toEqual([
      { item_id: "", option_ids: ["受注（SALES_ORDERS）", "cand-orders"], other_text: "今月の確定分だけ" },
    ]);
    expect(saved.cases[0].clarification_answers).toEqual([]);

    // 評価すると、答えたケースは往復の数と、送った答えを詳細に出す。
    const actions = page.getByTestId("evaluation-set-actions");
    const startButton = actions.getByRole("button", { name: "評価を開始" });
    if (await startButton.count()) {
      await startButton.click();
    } else {
      await page.getByTestId("evaluation-set-actions-more").click();
      await page.getByRole("menuitem", { name: "評価を開始" }).click();
    }
    await expect(page.getByTestId("evaluation-progress")).toHaveCount(0);
    const results = page.getByRole("table", { name: "ケース別結果" });
    await expect(results.getByRole("row", { name: /sales-total/ })).toContainText("問い返しに 1 回答えた");
    await expect(results.getByRole("row", { name: /expense-deadline/ })).not.toContainText("問い返し");
    await results.getByRole("button", { name: /今月の売上の合計はいくらですか？/ }).click();
    const dialog = page.getByRole("dialog", { name: "ケースの詳細" });
    const rounds = dialog.getByTestId("evaluation-clarification-rounds");
    // 1 往復は共通の ClarificationRecord（回答前の確認の履歴の折りたたみ。#1861）。題は往復の番号と実行。
    const round = rounds.getByTestId("evaluation-clarification-round-1");
    const summary = round.locator("summary");
    await expect(summary).toHaveText("1 回目（実行 run-eval-2-ask）");
    await expect(round).toHaveAttribute("open", "");
    const entry = round.getByTestId("evaluation-clarification-round-1-entry");
    await expect(entry.locator("dt")).toHaveText("対象の表: どの表の売上を使いますか？");
    await expect(entry.locator("dd")).toHaveText("受注（SALES_ORDERS）、cand-orders、その他: 今月の確定分だけ");
    await expect(rounds).toContainText("送った答え: 対象の表: 受注（SALES_ORDERS）、cand-orders、今月の確定分だけ");
    await expectNoHorizontalOverflow(page);
    await dialog.screenshot({ path: testInfo.outputPath(`evaluation-clarification-detail-${viewport.name}.png`) });
    // 閉じると確認事項を隠し、題（往復の番号）だけを残す。
    await summary.click();
    await expect(round).not.toHaveAttribute("open", "");
    await expect(entry).toBeHidden();
  });
}

for (const viewport of VIEWPORTS) {
  test(`最近の評価は削除せずに残し、サーバー側でページングする (${viewport.name})`, async ({ page, mockApi }) => {
    // #794・#3235: 評価の履歴は件数・期間で消さずに残し、一覧は offset / limit で取得する。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedFinishedJobs(mockApi);
    const [latest] = mockApi.state.evaluations;
    for (let index = 0; index < 27; index += 1) {
      mockApi.state.evaluations.push({ ...latest, id: `eval-old-${index}`, previous_job_id: null, previous_summary: null });
    }
    await page.goto("/evaluation");

    await expect(page.getByText("新しい順。終わった評価は削除せずに残します。")).toBeVisible();
    const pager = page.getByTestId("evaluation-jobs-pagination");
    await expect(pager).toContainText("1–25 / 29 件");
    await pager.getByRole("button", { name: "次へ" }).click();
    await expect(pager).toContainText("26–29 / 29 件");
    const request = mockApi.lastRequest("GET", "/api/evaluations");
    expect([request?.searchParams.get("offset"), request?.searchParams.get("limit")]).toEqual(["25", "25"]);
    // 表示している評価は最新の評価のまま（2 ページ目を見ても変わらない）。
    await expect(page.getByTestId("evaluation-summary")).toContainText("67%");
    await expectNoHorizontalOverflow(page);
  });
}

test("Excel から評価ケースを取り込み（確認して置き換え）、テンプレートと書き出しを取得できる", async ({ page, mockApi }, testInfo) => {
  seedSet(mockApi);
  await page.goto("/evaluation?id=evset-seeded");
  await expect(page.getByRole("heading", { name: "経理の評価", level: 1 })).toBeVisible();
  await expect(page.getByText("評価ケース（3 件）")).toBeVisible();

  await page.getByTestId("evaluation-set-file").setInputFiles({
    name: "cases.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from("PK-mock"),
  });
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog.getByText("評価ケースを置き換えますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "置き換える" }).click();
  await expect(page.getByText("Excel から 2 件を読み込みました。保存すると反映します。")).toBeVisible();
  await expect(page.getByText("評価ケース（2 件）")).toBeVisible();
  await expect(page.getByTestId("evaluation-case-1").getByLabel("質問")).toHaveValue("Excel の質問 1");
  await page.screenshot({ path: testInfo.outputPath("evaluation-set-editor.png"), fullPage: true });
  expect(mockApi.lastRequest("POST", "/api/evaluation-sets/parse-xlsx")).toBeDefined();

  // 保存していない変更があるときは評価を始めず、変更を破棄できる。
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expect(page.getByText("評価ケース（3 件）")).toBeVisible();

  await page.getByRole("button", { name: "テンプレートをダウンロード" }).click();
  await expect.poll(() => mockApi.lastRequest("GET", "/api/evaluation-sets/template.xlsx")).toBeDefined();
});

test("保存していない変更があるときは、一覧へ戻る前に確かめる。一覧から評価セットを削除できる", async ({ page, mockApi }) => {
  seedSet(mockApi);
  await page.goto("/evaluation?id=evset-seeded");
  await page.getByLabel("名前").fill("経理の評価（改）");
  await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog.getByText("変更を破棄しますか")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByLabel("名前")).toHaveValue("経理の評価（改）");

  await page.getByRole("button", { name: "保存", exact: true }).first().click();
  await expect(page.getByText("評価セットを保存しました")).toBeVisible();
  await page.getByRole("button", { name: "一覧へ戻る" }).last().click();
  await expect(page).toHaveURL(/\/evaluation$/);

  await page.getByTestId("evaluation-set-row-actions-evset-seeded").click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await expect(dialog.getByText("「経理の評価（改）」を削除しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByText("評価セットを削除しました")).toBeVisible();
  await expect(page.getByText("この業務 Agent の評価セットはまだありません")).toBeVisible();
});

test("読み込み中は経過時間を出す", async ({ page, mockApi }) => {
  seedFinishedJobs(mockApi);
  await page.route("**/api/evaluations/eval-latest", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await page.goto("/evaluation");
  await expect(page.getByTestId("evaluation-loading")).toContainText("評価の結果を読み込んでいます");
  await expect(page.getByTestId("evaluation-summary")).toBeVisible();
});

// ---- #810: 評価する版・テンプレートの評価ケース ----------------------------------------------------

function draftAgent(mockApi: MockApi) {
  const agent = mockApi.state.agents.find((item) => item.id === "default");
  if (!agent) throw new Error("default agent missing");
  agent.unpublished_changes = true;
  return agent;
}

for (const viewport of VIEWPORTS) {
  test(`評価する版を選んで始め、版を一覧と概要に出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSet(mockApi);
    draftAgent(mockApi);
    await page.goto("/evaluation");

    // 公開していない変更があれば、既定は下書き。公開中の版も選べる。
    const version = page.getByTestId("evaluation-version");
    await expect(version).toContainText("下書き（公開していない変更あり）");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`evaluation-version-${viewport.name}.png`), fullPage: true });
    await version.click();
    await page.getByRole("option", { name: "公開中の版（v1）" }).click();
    await expect(version).toContainText("公開中の版（v1）");

    await page.getByTestId("evaluation-set-row-actions-evset-seeded").click();
    await page.getByRole("menuitem", { name: "評価を開始" }).click();
    await expect(page.getByText("評価を開始しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toEqual({
      set_id: "evset-seeded",
      agent_version: "published",
    });
    await expect(page.getByTestId("evaluation-summary")).toBeVisible();
    await expect(page.getByText("経理の評価（資料・データ横断アシスタント・v1）")).toBeVisible();
    await expect(page.getByRole("table", { name: "最近の評価" }).getByRole("row").nth(1)).toContainText("v1");
  });
}

test("評価セットのエディタでも評価する版を選べる", async ({ page, mockApi }) => {
  seedSet(mockApi);
  draftAgent(mockApi);
  await page.goto("/evaluation?id=evset-seeded");
  await expect(page.getByTestId("evaluation-set-version")).toContainText("下書き（公開していない変更あり）");
  const actions = page.getByTestId("evaluation-set-actions");
  const startButton = actions.getByRole("button", { name: "評価を開始" });
  if (await startButton.count()) {
    await startButton.click();
  } else {
    await page.getByTestId("evaluation-set-actions-more").click();
    await page.getByRole("menuitem", { name: "評価を開始" }).click();
  }
  await expect(page.getByText("評価を開始しました")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toEqual({ set_id: "evset-seeded", agent_version: "draft" });
});

test("評価セットを URL で開いて評価を始めると、一覧の業務 Agent は評価した業務 Agent になる", async ({ page, mockApi }) => {
  // #2589: 業務 Agent の選択が既定（先頭）のまま、評価概要だけが別の業務 Agent の評価にならないこと。
  const base = mockApi.state.agents.find((item) => item.id === "default");
  if (!base) throw new Error("default agent missing");
  mockApi.state.agents.push({ ...structuredClone(base), id: "agent-2589", name: "Agent Z" });
  mockApi.state.evaluationSets.push({ ...structuredClone(SET), id: "evset-2589", agent_id: "agent-2589", name: "評価セット Z" });
  await page.goto("/evaluation?id=evset-2589");
  await expect(page.getByTestId("evaluation-set-version")).toBeVisible();
  const actions = page.getByTestId("evaluation-set-actions");
  const startButton = actions.getByRole("button", { name: "評価を開始" });
  if (await startButton.count()) {
    await startButton.click();
  } else {
    await page.getByTestId("evaluation-set-actions-more").click();
    await page.getByRole("menuitem", { name: "評価を開始" }).click();
  }
  await expect(page.getByText("評価を開始しました")).toBeVisible();
  // 評価セットの欄の業務 Agent の選択（評価概要にも業務 Agent の名前が出るので、選択の欄で確かめる）。
  await expect(page.locator("#evaluation-agent")).toContainText("Agent Z");
  await expect(page.getByText("評価セット Z").first()).toBeVisible();
});

test("公開した版の無い業務 Agent は下書きだけを選べる", async ({ page, mockApi }) => {
  seedSet(mockApi);
  const agent = draftAgent(mockApi);
  agent.published_version = null;
  agent.versions = [];
  await page.goto("/evaluation");
  const version = page.getByTestId("evaluation-version");
  await expect(version).toContainText("下書き（公開していない変更あり）");
  await expect(page.getByText("公開した版が無いため、下書きで評価します。")).toBeVisible();
  await version.click();
  await expect(page.getByRole("option")).toHaveCount(1);
});

test("Agent 管理でない利用者は下書きを選べず、公開中の版を評価する", async ({ page, mockApi }) => {
  // 下書きの評価は下書きの実行と同じく Agent 管理だけ（#2522）。
  seedSet(mockApi);
  draftAgent(mockApi);
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.evaluation"], allowed_agent_ids: ["default"] }));
  await page.goto("/evaluation");
  const version = page.getByTestId("evaluation-version");
  await expect(version).toContainText("公開中の版（v1）");
  await expect(page.getByText("公開中の版を評価します（下書きの評価は Agent 管理か、その業務 Agent の持ち主だけ）。")).toBeVisible();
  await version.click();
  await expect(page.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Escape");

  await page.getByTestId("evaluation-set-row-actions-evset-seeded").click();
  await page.getByRole("menuitem", { name: "評価を開始" }).click();
  await expect(page.getByText("評価を開始しました")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/evaluations")?.body).toEqual({
    set_id: "evset-seeded",
    agent_version: "published",
  });
});

for (const viewport of VIEWPORTS) {
  test(`テンプレートから作った業務 Agent は、評価セットが無ければテンプレートの評価ケースで作れる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const agent = mockApi.state.agents.find((item) => item.id === "default");
    if (!agent) throw new Error("default agent missing");
    agent.template_id = "internal-policy-helpdesk";
    await page.goto("/evaluation");
    await expect(page.getByText("この業務 Agent は業種テンプレートから作りました。")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`evaluation-from-template-${viewport.name}.png`), fullPage: true });
    await page.getByTestId("evaluation-sets-from-template").click();
    await expect(page.getByText("評価セット「社内規程の問い合わせ（テンプレート）」を作りました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/evaluation-sets/from-template")?.body).toEqual({ agent_id: "default" });
    await expect(
      page.getByRole("table", { name: "評価セットの一覧" }).getByRole("row", { name: /社内規程の問い合わせ（テンプレート）/ })
    ).toContainText("1");
  });
}

// #965: ケースの削除と追加・別のページの未入力・前の評価の表示中の最新の評価の終わり。
test("ケースを削除して足しても保存でき、ID の重複は欄で知らせる", async ({ page, mockApi }) => {
  mockApi.state.evaluationSets.push({
    ...structuredClone(SET),
    id: "evset-auto",
    cases: [1, 2, 3].map((number) => ({
      id: `case-${number}`,
      question: `質問 ${number}`,
      expected: "要点",
      expected_tools: [],
    })),
  });
  await page.goto("/evaluation?id=evset-auto");
  await page.getByRole("button", { name: "ケース 1 を削除" }).click();
  await page.getByRole("button", { name: "ケースを追加" }).click();
  const added = page.getByTestId("evaluation-case-3");
  // 空のまま保存すると付く ID（残ったケースの case-3 と重ならない）を placeholder に出す。
  await expect(added.getByLabel("ケース ID")).toHaveAttribute("placeholder", "case-4");
  await added.getByLabel("質問").fill("質問 4");
  await added.getByLabel("期待する回答の要点").fill("要点");

  // 明示した ID の重複は、送らずに欄で知らせる。
  await page.getByTestId("evaluation-case-1").getByLabel("ケース ID").fill("case-3");
  await page.getByRole("button", { name: "保存", exact: true }).first().click();
  await expect(page.getByText("ほかのケースと同じ ID です。")).toHaveCount(2);
  expect(mockApi.requests.filter((request) => request.method === "PUT")).toHaveLength(0);

  await page.getByTestId("evaluation-case-1").getByLabel("ケース ID").fill("case-2");
  await page.getByRole("button", { name: "保存", exact: true }).first().click();
  await expect(page.getByText("評価セットを保存しました")).toBeVisible();
  const saved = mockApi.state.evaluationSets.find((item) => item.id === "evset-auto");
  expect((saved?.cases as { id: string }[]).map((item) => item.id)).toEqual(["case-2", "case-3", "case-4"]);
});

test("別のページのケースが未入力なら、保存でそのページへ移ってエラーを出す", async ({ page, mockApi }) => {
  mockApi.state.evaluationSets.push({
    ...structuredClone(SET),
    id: "evset-many",
    cases: Array.from({ length: 26 }, (_, index) => ({
      id: `case-${index + 1}`,
      question: `質問 ${index + 1}`,
      expected: "要点",
      expected_tools: [],
    })),
  });
  await page.goto("/evaluation?id=evset-many");
  await page.getByTestId("evaluation-case-1").getByLabel("質問").fill("");
  const pager = page.getByTestId("evaluation-cases-pagination");
  await pager.getByRole("button", { name: "次へ" }).click();
  await expect(page.getByTestId("evaluation-case-26")).toBeVisible();

  await page.getByRole("button", { name: "保存", exact: true }).first().click();
  await expect(page.getByTestId("evaluation-case-1")).toContainText("質問を入力してください。");
  await expect(pager).toContainText("1–25 / 26 件");
  expect(mockApi.requests.filter((request) => request.method === "PUT")).toHaveLength(0);
});

test("前の評価を表示している間に最新の評価が終わると、評価を開始できるようになる", async ({ page, mockApi }) => {
  seedFinishedJobs(mockApi);
  const [latest] = mockApi.state.evaluations;
  mockApi.state.evaluations.unshift({ ...structuredClone(latest), id: "eval-running", status: "running", finished_at: null });
  // mock は詳細の取得の回数で評価を終わらせるため、この評価は詳細を取り直しても終わらないようにする。
  mockApi.state.evaluationPollsUntilDone = 1_000;
  await page.goto("/evaluation");
  await page.getByTestId("evaluation-row-actions-eval-previous").click();
  await page.getByRole("menuitem", { name: "結果を表示" }).click();
  const busy = page.getByText("ほかの評価を実行しています。終わってから始めてください。");
  await expect(busy).toBeVisible();

  mockApi.state.evaluations[0].status = "completed";
  mockApi.state.evaluations[0].finished_at = MOCK_NOW;
  await expect(busy).toBeHidden();
  await page.getByTestId("evaluation-set-row-actions-evset-seeded").click();
  await expect(page.getByTestId("evaluation-set-start-evset-seeded")).toBeEnabled();
});


test("承認による未実行を合格率と分け、停止の期待を保存する", { tag: "@responsive" }, async ({ page, mockApi }) => {
  seedFinishedJobs(mockApi);
  const job = mockApi.state.evaluations[0];
  job.previous_job_id = null;
  job.previous_summary = null;
  const results = [{ ...(job.results as EvaluationResultFixture[])[0], status: "approval_blocked", judgement: null,
    blocked_tool_calls: ["synthetic_tool"], executed_tool_calls: [], tool_calls: ["synthetic_tool"],
    error: "承認が必要なツールを実行していないため、回答の合否は判定しません。" }];
  job.results = results;
  job.summary = { ...evaluationSummary(results), total: 1, completed: 1, correct: 0, incorrect: 0, errors: 1, judged_cases: 0, approval_blocked: 1, pass_rate: null, average_score: null };
  await page.goto("/evaluation");
  await expect(page.getByText("承認で評価できなかった", { exact: true })).toBeVisible();
  await expect(page.getByText("1 件を合格率の分母から除外", { exact: true })).toBeVisible();
  await expect(page.getByText("承認で未実行（合格率から除く）", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: test.info().outputPath("evaluation-approval-blocked.png"), fullPage: true });
  await page.goto(`/evaluation?id=${SET.id}`);
  await page.getByTestId("evaluation-case-1-expected-approval").click();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => (mockApi.lastRequest("PUT", `/api/evaluation-sets/${SET.id}`)?.body as { cases?: { expected_approval?: boolean }[] } | undefined)?.cases?.[0]?.expected_approval).toBe(true);
});


test("実行条件の変更で無効になった評価を合格と表示しない", { tag: "@responsive" }, async ({ page, mockApi }) => {
  seedFinishedJobs(mockApi);
  const job = mockApi.state.evaluations[0];
  job.previous_job_id = null;
  job.previous_summary = null;
  job.status = "failed";
  job.error = "評価中に実行条件が変更されました。現在の設定で評価し直してください。";
  const results = [{ ...(job.results as EvaluationResultFixture[])[0], status: "definition_changed", judgement: null, tool_selection_correct: null, error: job.error }];
  job.results = results;
  job.summary = { ...evaluationSummary(results), total: 1, completed: 1, correct: 0, incorrect: 0, errors: 1, unjudged: 1, judged_cases: 0, pass_rate: null, average_score: null };
  await page.goto("/evaluation");
  await expect(page.getByText("実行条件の変更で無効", { exact: true })).toBeVisible();
  await expect(page.getByTestId("evaluation-summary")).not.toContainText("100%");
  await expect(page.getByText("うち判定できなかった 1 件（合格率から除く）", { exact: true })).toBeVisible();
  await expect(page.getByText(job.error as string, { exact: true }).first()).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByTestId("evaluation-summary").scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath("evaluation-definition-changed.png"), fullPage: true });
});
