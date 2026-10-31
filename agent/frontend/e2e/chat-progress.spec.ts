import { confirmTraceExplorer } from "../../../platform/packages/ui/e2e/trace-explorer";
import { syntheticCallDetails, syntheticStageDetails } from "../../../platform/packages/ui/e2e/call-fixture";
import {timelineSnapshot, timelineTypography} from "../../../platform/packages/ui/e2e/timeline-snapshot";
import type { Page } from "@playwright/test";

import { findIconTextMisalignment } from "../../../platform/packages/ui/e2e/layout-checks";

import { ProgressLog, SYNTHETIC_MODEL, SYNTHETIC_TRACE_ID } from "./fixtures/chat-progress-events";
import { expect, test, type MockApi } from "./fixtures/mock-api";
import { expectProgressTimerMonotonic, startProgressTimerSampler } from "./fixtures/progress-timer";
import { expectSpinnerStable } from "./fixtures/spinner-stability";

// #1147: チャットの回答の場所に、Run の処理の経過を共有の ChatProgress（3 製品共通。#1145）で出す。処理の経過は Run の
// trace（span の木。#3226）の投影で、共通の span の表の snapshot（`GET /api/runs/{id}/trace`。案 A）を実行中は読み直し、
// 承認待ちの間も途絶えずに進むことを確かめる。「トレースの詳細」の 2 つ目の表示は無く、「処理の詳細」には Trace ID だけを
// 出す。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

const THREAD_ID = `thread_${"b".repeat(32)}`;
const RUN_ID = "run-progress";
const MODEL_CALL = "モデルを呼び出す（用途の記録なし）";

function iso(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString();
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

/** Trace ID の文字列が折り返しても、コピーのボタンは同じ行に置く（#3219）。 */
async function expectTraceIdRowOnOneLine(page: Page) {
  const sameRow = await page.evaluate(() =>
    [...document.querySelectorAll("[data-trace-id-row]")].every((row) => {
      const [text, button] = [row.querySelector("span"), row.querySelector("button")].map((el) => el?.getBoundingClientRect());
      return text != null && button != null && button.top < text.bottom && text.top < button.bottom;
    })
  );
  expect(sameRow).toBe(true);
}

function seedRunningRun(mockApi: MockApi) {
  const startedAt = iso(-3_000);
  const progress = new ProgressLog(RUN_ID, undefined, startedAt).step("chat-1", "running", { startedAt });
  mockApi.state.runs.push({
    id: RUN_ID,
    goal: "契約の更新条件を調べて",
    agent_id: "default",
    runtime_id: "builtin",
    status: "running",
    steps: [],
    events: [
      { id: "ev-run", run_id: RUN_ID, type: "run.status_changed", message: "実行を開始しました。", payload: { status: "running" }, created_at: startedAt },
    ],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: startedAt,
    updated_at: startedAt,
    progress_events: progress.events,
  });
  return { run: mockApi.state.runs.at(-1) as Record<string, unknown>, progress };
}

async function openThread(page: Page) {
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: /^契約の更新条件を調べて/ }).click();
  } else {
    await page.getByRole("dialog", { name: "会話の履歴" }).getByText("契約の更新条件を調べて").click();
  }
}

function toolStep(id: string, name: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    run_id: RUN_ID,
    kind: "tool",
    status,
    tool_call: { name, arguments: { query: "契約" }, trace_id: `call-${id}` },
    started_at: iso(-2_000),
    completed_at: status === "completed" || status === "failed" ? iso(-1_000) : null,
    ...extra,
  };
}

/**
 * 入れ子の深さごとに字下げが違い（深い段ほど右）、どの行も名前は状態の印から同じ距離で始まる（製品名で名前がずれない。
 * #3447）。
 */
async function expectNestedIndent(list: ReturnType<Page["getByTestId"]>) {
  const rows = await list.evaluate((node) =>
    Array.from(node.querySelectorAll("[data-run-timeline-step]"), (row) => {
      const icon = row.querySelector(".pr-icon-line")!.getBoundingClientRect();
      const label = row.querySelector("[data-run-timeline-label]")!.getBoundingClientRect();
      return {level: Number(row.getAttribute("data-level") ?? 0), icon: Math.round(icon.left), gap: Math.round(label.left - icon.left)};
    })
  );
  const byLevel = new Map<number, number>();
  for (const row of rows) {
    const known = byLevel.get(row.level);
    if (known !== undefined) expect(row.icon).toBe(known);
    byLevel.set(row.level, row.icon);
  }
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  for (let index = 1; index < levels.length; index += 1) {
    expect(byLevel.get(levels[index])!).toBeGreaterThan(byLevel.get(levels[index - 1])!);
  }
  expect(new Set(rows.map((row) => row.gap)).size).toBe(1);
}

for (const viewport of VIEWPORTS) {
  test(`終わったツールの段は、段の終わりのイベントの詳細で、後の段の実行中から開ける（#2373） (${viewport.name})`, async ({ page, mockApi }) => {
    const { progress: log } = seedRunningRun(mockApi);
    // Run の step はまだ取り直していない（steps は空）。詳細は span のイベントだけで届く。
    log
      .step("chat-1", "done", { kind: "plan" })
      .step("tool:rag__rag_search", "running", { params: { tool: "rag__rag_search", title: "ナレッジベースの検索" } })
      .step("tool:rag__rag_search", "done", {
        details: {
          fields: [{ label: "ツール", value: "rag__rag_search", mono: true }],
          code: [{ label: "引数", language: "json", text: JSON.stringify({ query: "合成の語" }, null, 2) }],
        },
      })
      .step("chat-2", "running");
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);

    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const current = turn.getByTestId("chat-progress-current");
    await expect(current).toHaveAttribute("data-step-id", "chat-2");
    // 呼び出し中のモデルは、種類が決まるまで共通の名前（モデル名つき）で出す。
    await expect(current).toContainText(MODEL_CALL);
    const toggle = turn.getByTestId("chat-progress-step-tool:rag__rag_search-toggle");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const details = turn.getByTestId("chat-progress-step-tool:rag__rag_search-details");
    await expect(details.locator("dd").first()).toHaveText("rag__rag_search");
    await expect(details.getByLabel("引数")).toContainText("合成の語");
    // 経過は処理の説明だけにする。元の識別子は軌跡へ残す。
    await expect(details).not.toContainText("Span name");
    // 後の段は実行中のまま。
    await expect(current).toHaveAttribute("data-step-id", "chat-2");
    await expectNoHorizontalOverflow(page);
  });

  test(`処理の経過のツールの段は表示名で出し、開くと内部名と引数・結果の JSON を出す（#2294 / #2324） (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    const { run, progress: log } = seedRunningRun(mockApi);
    run.status = "completed";
    run.steps = [
      toolStep("step-search", "rag__rag_search", "completed", {
        tool_result: {
          name: "rag__rag_search",
          success: true,
          output: { answer: "合成の回答 A", sources: [{ title: "文書 A" }] },
          duration_ms: 800,
        },
      }),
    ];
    // ツールの表示名（MCP の title）は backend が params の title に、Run の step の id を run_step に入れる（#2324・#3226）。
    log
      .step("chat-1", "done", { kind: "plan" })
      .step("tool:rag__rag_search", "running", {
        params: { tool: "rag__rag_search", title: "ナレッジベースの検索", run_step: "step-search" },
      })
      .step("tool:rag__rag_search", "done")
      .step("chat-2", "running")
      .step("chat-2", "done", { kind: "respond" })
      .terminal("done");
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);

    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "done");
    // 使ったツールは処理の経過のツールの段で見る（「使ったツール」の一覧は出さない。#2324）。
    await expect(turn.getByText(/使ったツール/)).toHaveCount(0);
    await turn.getByTestId("chat-progress-summary").click();
    await expect(turn.getByTestId("chat-progress-step-tool:rag__rag_search")).toContainText(
      "「ナレッジベースの検索」を実行しました"
    );
    await expect(turn.getByTestId("chat-progress-step-chat-1")).toContainText("質問に応じた次の操作を選びました");
    await expect(turn.getByTestId("chat-progress-step-chat-2")).toContainText("回答を作りました");
    // 開閉のボタンは既定で閉じる。
    const toggle = turn.getByTestId("chat-progress-step-tool:rag__rag_search-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(turn.getByTestId("chat-progress-step-tool:rag__rag_search-details")).toBeHidden();
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const details = turn.getByTestId("chat-progress-step-tool:rag__rag_search-details");
    await expect(details).toBeVisible();
    // 段の名前は表示名なので、調べる用の内部名を詳細の先頭に出す。
    await expect(details.locator("dt").first()).toHaveText("ツール");
    await expect(details.locator("dd").first()).toHaveText("rag__rag_search");
    // 引数と結果は、span と結んだ Run の step の記録（取り直した結果）で出す。
    await expect(details.getByLabel("引数")).toContainText('"query": "契約"');
    await expect(details.getByLabel("結果")).toContainText("合成の回答 A");
    // 「処理の詳細」には Trace ID を出し、「トレースの詳細」の 2 つ目の表示は無い（#3226）。
    const processDetails = turn.getByTestId("chat-progress-details");
    await processDetails.click();
    await expect(turn.locator("[data-trace-id-row]")).toContainText(SYNTHETIC_TRACE_ID);
    await expect(turn.getByTestId("trace-details-toggle")).toHaveCount(0);
    await expectTraceIdRowOnOneLine(page);
    // 文字の横のアイコン（状態・開閉の印）は、文字の 1 行目の中心にそろえる（#2371）。
    expect(await findIconTextMisalignment(page, { root: `[data-testid="chat-turn-${RUN_ID}"]` })).toEqual([]);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`progress-tool-details-${viewport.name}.png`) });
    await toggle.click();
    await expect(details).toBeHidden();
  });

  for (const theme of ["light", "dark"] as const) {
    test(`回答の作成中は実行中の span を今の行で出し、ツール・承認待ち・完了へ進む (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      const { run, progress: log } = seedRunningRun(mockApi);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      // #1176: 段階が進む間、右上の経過時間（Run の作成からの通算）が減らないことを記録する。
      await startProgressTimerSampler(page, "chat-progress");
      await openThread(page);

      const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
      const progress = turn.getByTestId("chat-progress");
      const current = turn.getByTestId("chat-progress-current");
      // 1. モデルを呼び出している（実行中の span）。
      await expect(progress).toHaveAttribute("data-chat-progress-state", "running");
      await expect(current).toHaveAttribute("data-step-id", "chat-1");
      await expect(current).toContainText(MODEL_CALL);
      await expect(turn.getByTestId("chat-progress-timer")).toBeVisible();
      // 動くスピナーは今の段階の 1 つだけ。回転しても見た目の重心も箱・行も動かない（#1180）。
      await expect(turn.locator("svg.animate-spin:visible")).toHaveCount(1);
      await expectSpinnerStable(current.locator(".pr-spinner"));
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`progress-plan-${viewport.name}-${theme}.png`) });

      // 2. ツールを呼んでいる（読み直しで span が増える）。終わったモデルの呼び出しは「質問に応じた次の操作を選びました」。
      run.steps = [toolStep("step-search", "rag__rag_search", "running", { completed_at: null })];
      log
        .step("chat-1", "done", { kind: "plan" })
        .step("tool:rag__rag_search", "running", { params: { tool: "rag__rag_search", run_step: "step-search" } });
      await expect(current).toHaveAttribute("data-step-id", "tool:rag__rag_search");
      await expect(current).toContainText("選ばれたツールを呼び出しています");
      await expect(turn.getByTestId("chat-progress-running")).toContainText("処理中（1 ステップ完了）");
      await page.screenshot({ path: testInfo.outputPath(`progress-tool-${viewport.name}-${theme}.png`) });

      // 3. 承認待ち。実行の区間は閉じ、待ちの行は出さない（今の行の名前と承認の Banner で示す）。
      run.status = "waiting_approval";
      run.steps = [
        toolStep("step-search", "rag__rag_search", "completed"),
        toolStep("step-exec", "nl2sql__execute", "waiting_approval", { approval_id: "approval-progress" }),
      ];
      const requestedAt = iso(-500);
      run.approvals = [
        {
          id: "approval-progress",
          run_id: RUN_ID,
          step_id: "step-exec",
          tool_call: { name: "nl2sql__execute", arguments: { sql: "select 1" } },
          status: "pending",
          reason: "承認が必要です。",
          created_at: requestedAt,
        },
      ];
      log
        .step("tool:rag__rag_search", "done")
        .step("chat-2", "running")
        .step("chat-2", "done", { kind: "plan" })
        .pause();
      await expect(current).toHaveAttribute("data-step-id", "");
      await expect(current).toContainText("承認を待っています");
      await expect(turn.getByRole("button", { name: "承認して実行" })).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`progress-approval-${viewport.name}-${theme}.png`) });
      // 考えている・ツール・承認待ちを通る間、経過時間は減らない（段階ごとに 0 に戻さない）。承認待ちの今の行は
      // span ではない（id が空）。記録（100ms ごと）が承認待ちを 1 回は拾うまで待ってから確かめる。
      await expect
        .poll(() =>
          page.evaluate(() =>
            ((window as unknown as { __progressTimerSamples?: string[] }).__progressTimerSamples ?? []).some((sample) =>
              sample.startsWith("|")
            )
          )
        )
        .toBe(true);
      await expectProgressTimerMonotonic(page, 3);

      // 4. 承認後に再開して完了。待った区間は再開の区間の span で残る（#3226）。回答の上に「処理の経過」を閉じて
      // 出す（実行中は開く。#2096 / #2121）。承認待ちの間も読み直しは続くので、開き直さなくても終端が届き、会話を
      // 取り直して回答を出す（#1359）。
      run.status = "completed";
      run.steps = [
        toolStep("step-search", "rag__rag_search", "completed"),
        toolStep("step-exec", "nl2sql__execute", "completed", { approval_id: "approval-progress" }),
      ];
      const decidedAt = iso(-200);
      run.approvals = [{ ...(run.approvals as Record<string, unknown>[])[0], status: "approved", decided_at: decidedAt }];
      run.artifacts = [{ id: "answer-progress", kind: "answer", name: "回答", content: { text: "契約は 1 年ごとに更新します。" } }];
      log
        .resume()
        .step("approval_wait", "done", {
          params: { tools: "nl2sql__execute", round: 1 },
          startedAt: requestedAt,
          finishedAt: decidedAt,
        })
        .step("tool:nl2sql__execute", "running", { params: { tool: "nl2sql__execute", run_step: "step-exec" } })
        .step("tool:nl2sql__execute", "done")
        .step("chat-3", "running")
        .step("chat-3", "done", { kind: "respond" })
        .terminal("done");
      run.events = [
        ...(run.events as Record<string, unknown>[]),
        { id: "ev-done", run_id: RUN_ID, type: "run.completed", message: "実行を完了しました。", payload: {}, created_at: iso(0) },
      ];
      await expect(progress).toHaveAttribute("data-chat-progress-state", "done");
      await expect(current).toHaveCount(0);
      const summary = turn.getByTestId("chat-progress-summary");
      await expect(summary).toContainText("処理の経過（6 ステップ・");
      // 完了したら一覧を閉じ（#2121。RAG と同じ）、押すと開く。
      await expect(turn.getByTestId("chat-progress-step-chat-3")).toBeHidden();
      await expect(turn.getByText("契約は 1 年ごとに更新します。")).toBeVisible();
      // 処理の経過は回答の本文より上。使ったツールは処理の経過のツールの段で見る（一覧を重ねて出さない。#2324）。
      await expect(turn.getByText(/使ったツール/)).toHaveCount(0);
      const [summaryBox, answerBox] = await Promise.all([
        summary.boundingBox(),
        turn.getByText("契約は 1 年ごとに更新します。").boundingBox(),
      ]);
      expect(summaryBox!.y).toBeLessThan(answerBox!.y);
      await summary.click();
      await expect(turn.getByTestId("chat-progress-step-chat-3")).toBeVisible();
      // 開いた一覧は実行の詳細と同じ RunTimeline（時間の線つき。狭い置き場所は線を隠し、所要時間の文字だけ。#2265）。
      const bars = progress.locator("[data-run-timeline-bar]");
      await expect(bars).toHaveCount(6);
      if (viewport.width >= 1280) await expect(bars.first()).toBeVisible();
      else await expect(bars.first()).toBeHidden();
      // 承認の待ちは、待ったツールの名前と一緒に、承認の後のツールの前に出る（入れ子にしない）。
      const approval = turn.getByTestId("chat-progress-step-approval_wait");
      await expect(approval).toContainText("承認の判断を受け取りました");
      await expect(approval).toContainText("nl2sql__execute");
      expect((await approval.getAttribute("data-level")) ?? "0").toBe("0");
      await expect(turn.getByTestId("chat-progress-step-tool:nl2sql__execute")).toContainText("選ばれたツールを呼び出しました");
      await expect(turn.getByTestId("chat-progress-step-chat-3")).toHaveAttribute("data-status", "done");
      await expect(turn.getByTestId("chat-progress-step-chat-3")).toContainText("回答を作りました");
      // ツールを求めたモデルの呼び出しは、質問に応じた次の操作を選ぶとして残る。
      await expect(turn.getByTestId("chat-progress-step-chat-2")).toContainText("質問に応じた次の操作を選びました");
      // 終端が届いたら、承認待ちの後も会話を取り直して回答を出す（開き直さない）。
      expect(mockApi.requests.some((request) => request.path === `/api/runs/${RUN_ID}/trace`)).toBe(true);
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`progress-done-${viewport.name}-${theme}.png`) });
    });

    test(`失敗した回答は止まった段階を開いて出す (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      const { run, progress: log } = seedRunningRun(mockApi);
      run.status = "failed";
      run.steps = [toolStep("step-search", "rag__rag_search", "completed")];
      log
        .step("chat-1", "done", { kind: "plan" })
        .step("tool:rag__rag_search", "running", { params: { tool: "rag__rag_search", run_step: "step-search" } })
        .step("tool:rag__rag_search", "done")
        .step("chat-2", "running")
        .step("chat-2", "failed")
        .terminal("failed");
      run.events = [
        ...(run.events as Record<string, unknown>[]),
        { id: "ev-failed", run_id: RUN_ID, type: "runtime.failed", message: "モデルの呼び出しに失敗しました（APIError）。", payload: {}, created_at: iso(0) },
      ];
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      await openThread(page);

      const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
      await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "failed");
      // 失敗があれば最初から開いて出す。
      const failed = turn.getByTestId("chat-progress-step-chat-2");
      await expect(failed).toBeVisible();
      await expect(failed).toHaveAttribute("data-status", "failed");
      await expect(failed).toContainText(MODEL_CALL);
      await expect(failed).toContainText("失敗");
      // 原因は段階ではなく、回答の場所の失敗のカードに出す。
      await expect(turn.getByText("モデルの呼び出しに失敗しました（APIError）。")).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`progress-failed-${viewport.name}-${theme}.png`) });
    });
  }
}

// #3445: 実行中の Run の詳細は、処理の経過を開いても動くスピナーが 1 つ（開いた一覧の今の段）。概要の進行表示は今の段の
// 名前（ツールは表示名）で、ツールの内部名を出さない。処理の経過の見出しは実行中なら「処理中（N ステップ完了）」と経過時間。
test("実行中の Run の詳細は処理の経過を開いても動くスピナーが 1 つで、進行表示は今の段の名前を出す", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run} = seedRunningRun(mockApi);
  const start = iso(-8_000);
  run.created_at = start;
  const tool = "tool:rag__rag_retrieve_evidence";
  const rag = (parent: string, extra: Record<string, unknown> = {}) => ({startedAt: start, parent, product: "rag", ...extra});
  // ベクトル検索と全文検索は並行に実行中（#3443）。今の段は最後に始まった全文検索。
  run.progress_events = new ProgressLog(RUN_ID, undefined, start)
    .step("chat-1", "done", {startedAt: start, finishedAt: iso(-7_000), kind: "plan"})
    .step(tool, "running", {startedAt: start, params: {tool: "rag__rag_retrieve_evidence", title: "根拠の取得"}})
    .step("mcp-call", "running", {startedAt: start, parent: tool, attributes: {"mcp.method.name": "tools/call", "gen_ai.tool.name": "rag_retrieve_evidence"}})
    .step("rag-server", "running", rag("mcp-call", {name: "mcp.server", root: true}))
    .step("rag-search", "running", rag("rag-server", {name: "rag.search", root: true}))
    .step("rag-retrieval", "running", rag("rag-search", {name: "rag.retrieval", displayName: "検索", attributes: {"pr.stage": "retrieval"}}))
    .step("rag-stage", "running", rag("rag-retrieval", {name: "rag.engine.stage", kind: "answer_step", params: {name: "文書検索"}, displayName: "文書の検索", attributes: {"pr.stage": "文書検索"}}))
    .step("rag-vector", "running", rag("rag-stage", {name: "rag.engine.stage", kind: "answer_step", params: {name: "ベクトル検索"}, displayName: "ベクトル検索", attributes: {"pr.stage": "ベクトル検索"}}))
    .step("rag-text", "running", rag("rag-stage", {name: "rag.engine.stage", kind: "answer_step", params: {name: "全文検索"}, displayName: "全文検索", attributes: {"pr.stage": "全文検索"}})).events;
  // チャットの実行中の処理の経過も、MCP → RAG の入れ子を深さごとに字下げする（#3447）。
  await page.goto("/chat");
  await openThread(page);
  const live = page.getByTestId("chat-progress");
  await expect(live.getByTestId("chat-progress-current")).toContainText("全文検索");
  await expectNestedIndent(live);
  await expectNoHorizontalOverflow(page);
  await live.screenshot({path: testInfo.outputPath("chat-progress-nested-running.png")});
  await page.goto(`/runs?id=${RUN_ID}`);
  const progress = page.getByTestId("run-progress");
  await expect(progress.locator("[data-processing-label]")).toHaveText("全文検索");
  await expect(progress).not.toContainText("rag__");
  await expect(progress).toHaveAttribute("data-processing-activity-icon", "spinner");
  const section = page.getByTestId("run-timeline-section");
  const summary = page.getByTestId("run-timeline-section-summary");
  await expect(summary).toContainText("処理中（1 ステップ完了）");
  await expect(summary).not.toContainText("合計");
  // 経過時間は概要の進行表示の 1 か所だけ（見出しに重ねない。messaging.md §3.7）。
  await expect(progress.getByRole("timer")).toHaveAccessibleName(/^経過時間 00:\d\d$/);
  await expect(section.getByTestId("run-timeline-section-timer")).toHaveCount(0);
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
  // 開くと一覧の今の段（全文検索）だけが回り、進行表示は回さない実行中の印にする。
  await summary.click();
  await expect(progress).toHaveAttribute("data-processing-activity-icon", "static");
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
  await expect(section.locator("[data-run-timeline-step] svg.animate-spin")).toHaveCount(1);
  await expect(progress).toContainText("経過時間");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: testInfo.outputPath("run-detail-running.png"), fullPage: true});
  // 閉じると進行表示が回る。
  await summary.click();
  await expect(progress).toHaveAttribute("data-processing-activity-icon", "spinner");
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
});

// #3272: MCP で呼んだ RAG の span は、RAG が付けた工程の名前（`display_name`）で出し、製品の境目の行に製品名を添える。
// MCP の手続き（initialize・通知）の行は出さない。チャットと実行の詳細で同じ。
test("呼び先の製品の span は書いた製品の工程の名前で出し、製品の境目に製品名を添える", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run} = seedRunningRun(mockApi);
  const start = "2026-10-23T00:00:00.000Z";
  const end = "2026-10-23T00:00:02.000Z";
  run.created_at = start;
  run.updated_at = end;
  run.status = "completed";
  const tool = "tool:rag__rag_retrieve_evidence";
  const mcp = (method: string, extra: Record<string, string> = {}) => ({
    startedAt: start, finishedAt: end, parent: tool, attributes: {"mcp.method.name": method, ...extra},
  });
  const rag = (parent: string, extra: Record<string, unknown> = {}) => ({startedAt: start, finishedAt: end, parent, product: "rag", ...extra});
  run.progress_events = new ProgressLog(RUN_ID, () => end, start)
    .step("chat-1", "done", {startedAt: start, finishedAt: end, kind: "plan"})
    // ツールの表示名（MCP の title）は、ツールの行と MCP の呼び出しの行の両方に出す（#3468）。
    .step(tool, "running", {startedAt: start, params: {tool: "rag__rag_retrieve_evidence", title: "合成の根拠の取得"}})
    .step("mcp-init", "done", mcp("initialize"))
    .step("mcp-notify", "done", mcp("notifications/initialized"))
    .step("mcp-call", "done", mcp("tools/call", {"gen_ai.tool.name": "rag_retrieve_evidence", "pr.tool.title": "合成の根拠の取得"}))
    .step("rag-server", "done", rag("mcp-call", {name: "mcp.server", root: true}))
    .step("rag-search", "done", rag("rag-server", {name: "rag.search", root: true}))
    .step("rag-retrieval", "done", rag("rag-search", {name: "rag.retrieval", displayName: "検索", attributes: {"pr.stage": "retrieval"}}))
    .step("rag-stage", "done", rag("rag-retrieval", {name: "rag.engine.stage", kind: "answer_step", params: {name: "文書検索"}, displayName: "文書の検索", attributes: {"pr.stage": "文書検索"}}))
    .step("rag-embed", "done", rag("rag-stage", {name: "gen_ai.embeddings", attributes: {"gen_ai.request.model": "synthetic-embed"}}))
    .step("rag-vector", "done", rag("rag-stage", {name: "rag.engine.stage", kind: "answer_step", params: {name: "ベクトル検索"}, displayName: "ベクトル検索", attributes: {"pr.stage": "ベクトル検索"}}))
    .step(tool, "done", {finishedAt: end})
    .step("chat-2", "done", {startedAt: start, finishedAt: end, kind: "respond"}).terminal("done").events;
  run.events = [{id: "end", run_id: RUN_ID, type: "run.completed", message: "", payload: {}, created_at: end}];
  await page.goto("/chat");
  await openThread(page);
  await page.getByTestId("chat-progress-summary").click();
  const list = page.getByTestId("chat-progress");
  const live = await timelineSnapshot(list);
  expect(live.map((row) => [row.name + (row.product ? ` ${row.product}` : ""), row.level])).toEqual([
    ["質問に応じた次の操作を選びました", 0],
    ["「合成の根拠の取得」を実行しました", 0],
    ["MCP の呼び出し · 合成の根拠の取得", 1],
    ["検索 RAG", 2],
    ["文書の検索", 3],
    ["テキストの埋め込みを作る · synthetic-embed", 4],
    ["ベクトル検索", 4],
    ["回答を作りました", 0],
  ]);
  await expect(list.getByTestId("chat-progress-step-mcp-init")).toHaveCount(0);
  // 製品名は名前の後ろのタグ（名前の前に置かず、名前の左端を兄弟の段とそろえる。#3447）。
  await expect(list.locator("[data-run-timeline-product]")).toHaveText(["RAG"]);
  await expectNestedIndent(list);
  await expectNoHorizontalOverflow(page);
  await list.screenshot({path: testInfo.outputPath("chat-process-products.png")});
  await page.goto(`/runs?id=${RUN_ID}`);
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await page.getByTestId("run-timeline-section-summary").click();
  expect(await timelineSnapshot(page.getByTestId("run-timeline-section"))).toEqual(live);
  await expectNoHorizontalOverflow(page);
  await page.getByTestId("run-timeline-section").screenshot({path: testInfo.outputPath("history-process-products.png")});
});

test("チャットと履歴の工程の内容・階層・状態・時間が一致する", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run} = seedRunningRun(mockApi);
  const start = "2026-10-23T00:00:00.000Z";
  const end = "2026-10-23T00:00:02.000Z";
  run.created_at = start;
  run.updated_at = end;
  run.status = "completed";
  run.steps = [toolStep("synthetic-step", "synthetic_tool", "completed", {tool_result: {name: "synthetic_tool", success: true, output: {value: 1}, duration_ms: 777}})];
  run.progress_events = new ProgressLog(RUN_ID, () => end, start)
    .step("chat-1", "done", {startedAt: start, finishedAt: end, kind: "plan"})
    .step("tool:synthetic_tool", "running", {startedAt: start, params: {tool: "synthetic_tool", run_step: "synthetic-step"}})
    .step("mcp-1", "done", {startedAt: start, finishedAt: end, parent: "tool:synthetic_tool"})
    .step("tool:synthetic_tool", "done", {finishedAt: end})
    .step("chat-2", "done", {startedAt: start, finishedAt: end, kind: "respond"}).terminal("done").events;
  run.events = [{id: "end", run_id: RUN_ID, type: "run.completed", message: "", payload: {}, created_at: end}];
  await page.goto("/chat");
  await openThread(page);
  await page.getByTestId("chat-progress-summary").click();
  const live = await timelineSnapshot(page.getByTestId("chat-progress"));
  const liveTypography = await timelineTypography(page.getByTestId("chat-progress"));
  expect(liveTypography.steps.length).toBeGreaterThan(0);
  expect(liveTypography.steps.every(style => style?.fontSize === "12px" && style.fontFamily.includes("Noto Sans JP"))).toBe(true);
  expect(liveTypography.summary?.fontSize).toBe("12px");
  // span の木: ツールの中の MCP の呼び出しは 1 段下げる（実行の全体の span は行にしない）。
  expect(live.map((row) => row.level)).toEqual([0, 0, 1, 0]);
  await page.screenshot({path: testInfo.outputPath("chat-process.png")});
  await page.goto(`/runs?id=${RUN_ID}`);
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByTestId("run-summary").locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
  await page.getByTestId("run-timeline-section-summary").click();
  expect(await timelineSnapshot(page.getByTestId("run-timeline-section"))).toEqual(live);
  expect(await timelineTypography(page.getByTestId("run-timeline-section"))).toEqual(liveTypography);
  await expectNoHorizontalOverflow(page);
  // 「処理の詳細」には Trace ID を出し、「トレースの詳細」は無い（処理の経過と同じ記録を 2 回出さない。#3226）。
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(SYNTHETIC_TRACE_ID);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  await expect(page.getByTestId("trace-details")).toHaveCount(0);
  await expectTraceIdRowOnOneLine(page);
  await page.getByTestId("run-timeline-section").screenshot({path: testInfo.outputPath("history-process.png")});
});

test("共通の span の表に書く前の古い形式の記録は、処理の経過の位置に古い形式の案内を出す", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run} = seedRunningRun(mockApi);
  run.status = "completed";
  // #3226 の案 A の前の Run（span を表に書いていない）。処理の経過は読まない。
  run.trace_store = false;
  run.trace_id = null;
  run.progress_events = [];
  run.artifacts = [{ id: "answer-legacy", kind: "answer", name: "回答", content: { text: "合成の回答です。" } }];
  run.events = [{id: "end", run_id: RUN_ID, type: "run.completed", message: "", payload: {}, created_at: iso(0)}];
  await page.goto("/chat");
  await openThread(page);
  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  await expect(turn.getByText("合成の回答です。")).toBeVisible();
  await expect(turn.getByTestId("chat-progress-legacy")).toHaveText("この実行は古い形式の記録のため、処理の経過を表示できません。");
  await expect(turn.getByTestId("chat-progress")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: testInfo.outputPath("chat-legacy.png")});

  await page.goto(`/runs?id=${RUN_ID}`);
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByTestId("run-timeline-legacy")).toContainText("この実行は古い形式の記録のため、処理の経過を表示できません。");
  await expectNoHorizontalOverflow(page);
  expect(mockApi.requests.some((request) => request.path === `/api/runs/${RUN_ID}/trace`)).toBe(false);
  await page.getByTestId("run-timeline-section").screenshot({path: testInfo.outputPath("history-legacy.png")});
});

test("共通の span の表に書けなかった span がある実行は、一部だけの記録の案内を出す", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run, progress} = seedRunningRun(mockApi);
  progress.step("chat-1", "done", {kind: "respond"});
  run.status = "completed";
  // backend が実行を確定したときに残した、表に書けなかった span の数（#3226 の案 A）。
  run.trace_dropped_spans = 2;
  run.artifacts = [{ id: "answer-partial", kind: "answer", name: "回答", content: { text: "合成の回答です。" } }];
  run.events = [{id: "end", run_id: RUN_ID, type: "run.completed", message: "", payload: {}, created_at: iso(0)}];
  await page.goto("/chat");
  await openThread(page);
  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  await expect(turn.getByText("合成の回答です。")).toBeVisible();
  // 書けた span の経過は出し、全部ではないことを経過を開いた段の一覧の後ろに出す（3 製品共通の ChatProgress の partial）。
  await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "done");
  await turn.getByTestId("chat-progress-summary").click();
  await expect(turn.getByTestId("chat-progress-partial")).toHaveText("一部の処理だけを記録しています。");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: testInfo.outputPath("chat-partial.png")});

  await page.goto(`/runs?id=${RUN_ID}`);
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByTestId("run-timeline-section-summary")).toBeVisible();
  await page.getByTestId("run-timeline-section-summary").click();
  await expect(page.getByTestId("run-timeline-section-partial")).toHaveText("一部の処理だけを記録しています。");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: testInfo.outputPath("history-partial.png"), fullPage: true});
});

test("実行の詳細のモデルの呼び出しの行を開くと、入力の文脈の記録と利用量を日本語の見出しで出す", {tag: "@responsive"}, async ({page, mockApi}, testInfo) => {
  const {run} = seedRunningRun(mockApi);
  const start = "2026-10-23T00:00:00.000Z";
  const end = "2026-10-23T00:00:02.000Z";
  run.created_at = start;
  run.updated_at = end;
  run.status = "completed";
  // backend の allowlist（`pr_backend_core.observability.tracing`）にある、本文を含まない合成の値だけ（#2583）。
  const usage = {"pr.gen_ai.purpose": "agent.choose_action", "gen_ai.operation.name": "chat", "gen_ai.request.model": SYNTHETIC_MODEL, "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 30};
  const context = {
    "pr.context.instructions.static_fingerprint": "0123456789abcdef",
    "pr.context.instructions.fingerprint": "fedcba9876543210",
    "pr.context.tools.fingerprint": "00112233aabbccdd",
    "pr.context.tools.count": 4,
    "pr.context.input.items": 7,
    "pr.context.estimate.static_instructions_tokens": 300,
    "pr.context.estimate.dynamic_state_tokens": 40,
    "pr.context.estimate.tool_definitions_tokens": 500,
    "pr.context.estimate.history_tokens": 200,
    "pr.context.estimate.tool_outputs_tokens": 150,
    "pr.context.estimate.input_tokens": 1190,
    "pr.context.budget.input_tokens": 100000,
    "pr.context.budget.remaining_tokens": 98810,
    "pr.context.compaction.tokens_before": 5000,
    "pr.context.compaction.tokens_after": 1190,
    "pr.context.compaction.outputs": 2,
  };
  run.progress_events = new ProgressLog(RUN_ID, () => end, start)
    // 1 回目はプロバイダがキャッシュの値を返さなかった（不明）、2 回目は 0 を返した。
    .step("chat-1", "done", {startedAt: start, finishedAt: end, attributes: {...usage, ...context}})
    .step("chat-2", "done", {startedAt: start, finishedAt: end, kind: "respond", attributes: {...usage, "gen_ai.usage.cache_read.input_tokens": 0, "pr.context.estimate.input_tokens": 1500}})
    .terminal("done").events;
  run.events = [{id: "end", run_id: RUN_ID, type: "run.completed", message: "", payload: {}, created_at: end}];
  await page.goto(`/runs?id=${RUN_ID}`);
  await expect(page.getByTestId("run-summary")).toBeVisible();
  await page.getByTestId("run-timeline-section-summary").click();
  const timeline = page.getByTestId("run-timeline-section");
  await expect(timeline).not.toContainText("Span name");
  await timeline.getByRole("button", {name: "軌跡を調べる", exact: true}).click();
  const fieldsOf = async (stepId: string) => {
    const back = page.locator("[data-trace-inspector]").getByRole("button", {name: "一覧に戻る", exact: true});
    if (await back.isVisible()) await back.click();
    await page.locator(`[data-trace-record="${stepId}"] button`).click();
    const inspector = page.locator("[data-trace-inspector]");
    await expect(inspector.locator("[data-trace-technical]")).toContainText("入力トークン");
    const terms = await inspector.locator("[data-trace-technical] dt").allInnerTexts();
    const values = await inspector.locator("[data-trace-technical] dd").allInnerTexts();
    return Object.fromEntries(terms.map((term, index) => [term, values[index]]));
  };
  const first = await fieldsOf("chat-1");
  expect(first).toMatchObject({
    "gen_ai.request.model": SYNTHETIC_MODEL,
    "入力トークン": "1200",
    "出力トークン": "30",
    "キャッシュから読んだ入力": "不明",
    "入力の見積もり（指示・固定）": "300",
    "入力の見積もり（動的な状態）": "40",
    "入力の見積もり（ツールの定義）": "500",
    "入力の見積もり（履歴）": "200",
    "入力の見積もり（ツールの結果）": "150",
    "入力の見積もり（合計）": "1190",
    "入力の項目数": "7",
    "文脈の予算": "100000",
    "文脈の予算の残り": "98810",
    "圧縮の前": "5000",
    "圧縮の後": "1190",
    "圧縮したツールの結果の数": "2",
    "指示の指紋（固定）": "0123456789abcdef",
    "指示の指紋（全体）": "fedcba9876543210",
    "ツール定義の指紋": "00112233aabbccdd",
    "ツール定義の数": "4",
  });
  expect(Object.keys(first).some((term) => term.startsWith("pr.context.") || term.startsWith("gen_ai.usage."))).toBe(false);
  const second = await fieldsOf("chat-2");
  expect(second).toMatchObject({"キャッシュから読んだ入力": "0", "入力の見積もり（合計）": "1500"});
  // 記録の無い値（予算・圧縮・指紋）は行にしない。
  expect(Object.keys(second).some((term) => term.startsWith("文脈の予算") || term.startsWith("圧縮") || term.includes("指紋"))).toBe(false);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: testInfo.outputPath("history-model-call-context.png"), fullPage: true});
});

test("呼び出しの詳細で入力・結果・思考・Schema・計時を確認できる", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  const { run, progress: log } = seedRunningRun(mockApi);
  log.step("chat-1", "done", { kind: "plan", details: { call: syntheticCallDetails } }).step("chat-2", "running");
  log.step("synthetic-stage", "done", { name: "agent.prepare", parent: "chat-1", details: syntheticStageDetails });
  log.step("attachment-off", "skipped", {name: "agent.attachment_evidence", parent: "chat-1"});
  run.status = "completed";
  log.terminal("done");
  await page.goto(`/runs?id=${RUN_ID}`);
  await page.getByTestId("run-timeline-section-summary").click();
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Span name");
  await expect(page.locator("[data-run-timeline]")).not.toContainText("Parent span ID");
  await confirmTraceExplorer(page, testInfo, expect);
});
