import { expect, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";
import { installTraceRecorder, ragSpan, savedSpanProgress, traceSse } from "./_chat-progress";

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

function searchStreamBody(chunkId: string, extraMetadata: Record<string, unknown> = {}): string {
  const citation = {
    document_id: "doc-1",
    chunk_id: chunkId,
    text: "料金表の交通費は 1000 円です。申請時は領収書を添付し、利用日、経路、目的を記載してください。承認後に精算されます。長い根拠本文でもカード内では三行に収まり、全文はプレビューから確認できます。",
    score: 0.048,
    rerank_score: 0.869,
    file_name: "policy.txt",
    category_name: null,
    metadata: {
      context_header: "文書 A > 分類 A > 機能 A > 説明",
      context_header_enabled: true,
      page_start: 2,
      content_kind: "table",
      context_role: "evidence",
      retrieval_mode: "hybrid",
      vector_rank: 1,
      vector_score: 0.91,
      keyword_rank: 1,
      keyword_score: 0.82,
      rrf_score: 0.032,
      rerank_rank: 1,
      recipe_id: "recipe-1",
      recipe_slot_no: 1,
      ...extraMetadata,
    },
  };
  // 処理の経過は span のイベント（#3226）。
  // 文書検索は 42 ms（1 秒未満も秒で書く）。
  const progress = savedSpanProgress(
    "trace-1",
    [
      { key: "root", spec: ragSpan.root(), at: [0, 0.06] },
      { key: "answer", spec: ragSpan.stage("answer"), at: [0, 0.06] },
      { key: "search", spec: ragSpan.step("文書検索"), at: [0, 0.042] },
    ],
    { terminal: "done", base: "2026-01-01T00:00:00Z" }
  );
  return [
    // 実行の id と、表へ書いた span の記録の印（実行の trace の API が返す。#3226）。
    `event: run\ndata: ${JSON.stringify({ trace_id: "trace-1" })}\n\n`,
    traceSse(progress),
    `event: metadata\ndata: ${JSON.stringify({
      trace_id: "trace-1",
      elapsed_ms: 12,
      guardrail_warnings: [],
      diagnostics: {
        retrieval_strategy: "hybrid",
        retrieval_strategy_adapter: "grounded",
        filter_keys: ["content_kind", "section_title", "section_path"],
        knowledge_base_count: 1,
        search_answer_profile_applied: "bv-1",
        config_fingerprint: "fp-1",
      },
    })}\n\n`,
    `event: citations\ndata: ${JSON.stringify([citation])}\n\n`,
    `event: done\ndata: ${JSON.stringify({ trace_id: "trace-1" })}\n\n`,
  ].join("");
}

test("引用カードに variant(chunk_set)バッジが出る", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await installTraceRecorder(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  const searchRequests: Array<Record<string, unknown>> = [];
  await page.route("**/api/search/stream", async (route) => {
    searchRequests.push(route.request().postDataJSON() as Record<string, unknown>);
    await new Promise((resolve) => setTimeout(resolve, 2200));
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      // chunk_id は document:chunk_set:index 形式 → variant バッジが出る。
      body: searchStreamBody("doc-1:cs_recipe1:1"),
    });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  // 検索・回答プロファイルの一覧は選んでも開いたままなので閉じてから操作する（外側を押すと一覧を閉じるだけになる）。
  await page.keyboard.press("Escape");

  await page.getByText("詳細条件", { exact: true }).click();
  await expect(page.locator("#search-top-k")).toHaveCount(0);
  // 内容種別と見出しの条件は #649 で外した。
  await expect(page.getByRole("combobox", { name: "内容種別" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "見出しで絞り込む" })).toHaveCount(0);
  // 検索の方式と Rerank の件数は回答エンジンが使わないため選ばせない（#595）。
  await expect(page.getByRole("combobox", { name: "Rerank 採用数" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "ハイブリッド" })).toHaveCount(0);


  await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  await expect.poll(() => searchRequests.length).toBe(1);
  for (const removed of ["mode", "strategy", "rerank_top_n", "generation_profile", "top_k"]) {
    expect(removed in searchRequests[0], removed).toBe(false);
  }
  // 既定は回答を生成しない（検索結果まで。#649）。
  expect(searchRequests[0]).toMatchObject({ generate_answer: false });
  expect("filters" in searchRequests[0]).toBe(false);

  // 処理の経過（#2096。チャットと同じ ChatProgress）: 実行中は今の工程と経過時間。
  const current = page.getByTestId("search-run-progress-current");
  await expect(current).toBeVisible();
  const elapsed = page.getByTestId("search-run-progress-timer");
  await expect(elapsed).toContainText("経過時間");
  const firstElapsed = await elapsed.textContent();
  await expect.poll(() => elapsed.textContent(), { timeout: 4_000 }).not.toBe(firstElapsed);

  await expect(page.getByRole("heading", { name: /検索結果/ })).toBeVisible();
  // 終わったら同じ場所（検索結果の上）で「処理の経過」（工程の行と所要時間。1 秒未満も秒で書く）に切り替わり、
  // 検索結果の後ろには出し直さない（#2115）。
  const process = page.getByRole("region", { name: "処理の経過" });
  await expect(process).toHaveCount(1);
  const [processBox, resultsBox] = await Promise.all([
    process.boundingBox(),
    page.getByRole("heading", { name: /検索結果/ }).boundingBox(),
  ]);
  expect(resultsBox?.y ?? Number.NEGATIVE_INFINITY).toBeGreaterThan(processBox?.y ?? Number.POSITIVE_INFINITY);
  // 完了したら工程の一覧を閉じる（チャットと同じ。#2118）。押すと開く。
  const searchStep = process.locator("li").filter({ hasText: "検索文から根拠の候補を集める" });
  await expect(searchStep).toBeHidden();
  await page.getByTestId("search-run-progress-summary").click();
  await expect(searchStep).toBeVisible();
  await expect(searchStep).toContainText("0.1 秒未満");
  await expect(process.getByText(/ ms$/)).toHaveCount(0);
  // 開始は出さない（「詳細」はチャットと同じ回答の記録の詳細。#2118）。
  await expect(process.getByText("開始", { exact: true })).toHaveCount(0);
  // 以前の検索の内訳（検索キーワード・検索フロー・候補の表）は出さない（#595）。
  await expect(page.locator('[aria-label="検索キーワード"]')).toHaveCount(0);
  await expect(page.getByText("検索フロー")).toHaveCount(0);
  await expect(page.getByText("診断", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("table", { name: "候補詳細" })).toHaveCount(0);
  await expect(page.getByRole("meter", { name: /取得スコア/ })).toHaveCount(0);
  const rerankMeter = page.getByRole("meter", { name: "Rerank スコア: 0.869" });
  await expect(rerankMeter).toBeVisible();
  await expect(page.getByText("Both")).toBeVisible();
  await expect(page.getByText("Vector #1")).toBeVisible();
  await expect(page.getByText("Keyword #1")).toBeVisible();
  await expect(page.getByText("Rerank #1")).toBeVisible();
  // レシピバッジ(recipe_slot_no)が引用カードに表示される。
  await expect(page.getByText("レシピ1")).toBeVisible();
  await expect(page.getByText("文書 A > 分類 A > 機能 A > 説明", {exact:true})).toBeVisible();
  await expect(page.getByText("有効", {exact:true})).toBeVisible();

  const citationText = page.getByTestId("citation-text");
  const citation = citationText.locator("xpath=ancestor::li[1]");
  const citationMain = citation.getByTestId("citation-main");
  const scorePanel = citation.getByTestId("citation-score-panel");
  const rerankFill = citation.getByTestId("citation-rerank-fill");
  await expect(citationText).toBeVisible();
  await expect(scorePanel).toBeVisible();
  await expect(scorePanel.getByText("0.048", { exact: true })).toBeVisible();
  await expect
    .poll(() => citationText.evaluate((element) => getComputedStyle(element).webkitLineClamp))
    .toBe("3");

  const citationBox = await citation.boundingBox();
  const citationMainBox = await citationMain.boundingBox();
  const citationTextBox = await citationText.boundingBox();
  const scorePanelBox = await scorePanel.boundingBox();
  const rerankMeterBox = await rerankMeter.boundingBox();
  const rerankFillBox = await rerankFill.boundingBox();
  expect(citationBox).not.toBeNull();
  expect(citationMainBox).not.toBeNull();
  expect(citationTextBox).not.toBeNull();
  expect(scorePanelBox).not.toBeNull();
  expect(rerankMeterBox).not.toBeNull();
  expect(rerankFillBox).not.toBeNull();
  expect(rerankFillBox!.width / rerankMeterBox!.width).toBeCloseTo(0.869, 1);
  if (testInfo.project.name === "mobile") {
    expect(scorePanelBox!.width).toBeGreaterThan(citationBox!.width - 32);
    expect(scorePanelBox!.y).toBeGreaterThanOrEqual(citationMainBox!.y + citationMainBox!.height);
    for (const control of [
      citation.getByRole("button", { name: "policy.txt の根拠を表示" }),
      citation.getByRole("button", { name: "この引用は役に立った" }),
      citation.getByRole("button", { name: "この引用は役に立たなかった" }),
    ]) {
      const controlBox = await control.boundingBox();
      expect(controlBox).not.toBeNull();
      expect(controlBox!.height).toBeGreaterThanOrEqual(44);
    }
  } else {
    expect(scorePanelBox!.width).toBeCloseTo(176, 0);
    expect(scorePanelBox!.x).toBeGreaterThan(citationMainBox!.x + citationMainBox!.width);
    expect(citationTextBox!.y).toBeLessThan(scorePanelBox!.y + scorePanelBox!.height);
  }
  await expectNoPageOverflow(page);
  await citation.scrollIntoViewIfNeeded();
  await citation.screenshot({
    path: testInfo.outputPath(`citation-card-${testInfo.project.name}.png`),
  });
  await page.screenshot({
    path: testInfo.outputPath(`citation-page-${testInfo.project.name}.png`),
  });
});

test("旧版の文書の根拠には、引用カードに旧版の印を出す（#1248）", async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await installTraceRecorder(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: { items: [searchAnswerProfile], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody("doc-1:cs_recipe1:1", {
        document_superseded: true,
        superseded_by_document_id: "doc-2",
      }),
    })
  );

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await page.keyboard.press("Escape");
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("交通費の上限");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  const citation = page.getByTestId("citation-main");
  await expect(citation.getByText("旧版", { exact: true })).toBeVisible();
  await expect(citation.getByText("policy.txt")).toBeVisible();
  await expectNoPageOverflow(page);
});
