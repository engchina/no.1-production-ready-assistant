import type { Page } from "@playwright/test";

import { ProgressLog, SYNTHETIC_TRACE_ID } from "./fixtures/chat-progress-events";
import { expect, test, type MockApi } from "./fixtures/mock-api";

// #1160: チャットの回答の作成中に、処理の経過の読み出し（共通の span の表の snapshot。`GET /api/runs/{id}/trace`。
// #3226 の案 A）が応答しない・失敗し続けても、画面を止めない。「接続を確認しています」を出して読み直し、完了・失敗の
// 終端まで追う。処理の経過は Run の trace（span の木）の投影。

const MODEL_CALL = "モデルを呼び出す（用途の記録なし）";

const THREAD_ID = `thread_${"c".repeat(32)}`;
const RUN_ID = "run-refresh";

function iso(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString();
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

function complete(run: Record<string, unknown>, progress: ProgressLog) {
  run.status = "completed";
  progress.step("chat-1", "done", { kind: "respond" }).terminal("done");
  run.artifacts = [{ id: "answer-refresh", kind: "answer", name: "回答", content: { text: "契約は 1 年ごとに更新します。" } }];
  run.events = [
    ...(run.events as Record<string, unknown>[]),
    { id: "ev-done", run_id: RUN_ID, type: "run.completed", message: "実行を完了しました。", payload: {}, created_at: iso(0) },
  ];
}

/**
 * 処理の経過の読み出し（`GET /api/runs/{id}/trace`）を、応答しない・失敗させる。それ以外は mock-api に任せる。
 * 会話の取得（回答・成果物）は止めない。
 */
async function controlProgressFetch(page: Page) {
  const control = { hang: 0, fail: 0, requests: 0 };
  await page.route(`**/api/runs/${RUN_ID}/trace**`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    control.requests += 1;
    if (control.hang > 0) {
      control.hang -= 1;
      return; // 応答しない（route を終えない）。
    }
    if (control.fail > 0) {
      control.fail -= 1;
      return route.fulfill({ status: 503, json: { detail: "一時的に応答できません。" } });
    }
    return route.fallback();
  });
  return control;
}

async function openThread(page: Page, title = "契約の更新条件を調べて") {
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${title}`) }).click();
  } else {
    await page.getByRole("dialog", { name: "会話の履歴" }).getByText(title).click();
  }
}

test(`段階の配信が応答しなくなったら「接続を確認しています」を出して取り直し、完了が出る`, async ({
  page,
  mockApi,
}, testInfo) => {
  test.setTimeout(90_000);
  const { run, progress } = seedRunningRun(mockApi);
  const control = await controlProgressFetch(page);
  await page.goto("/chat");
  await openThread(page);

  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  const current = turn.getByTestId("chat-progress-current");
  await expect(current).toContainText(MODEL_CALL);
  // 応答を止める（3 回の読み出し。1 回の読み出しは時間の上限で打ち切って読み直す）。修正前（#1160）は 1 回目の
  // 取得を待ったまま取り直しが止まった。
  control.hang = 3;
  const reconnecting = turn.getByTestId("chat-progress-reconnecting");
  await expect(reconnecting).toHaveText("接続を確認しています。", { timeout: 30_000 });
  // 案内は見出し「処理中（N ステップ完了）」の直下（左の見出しの側の 2 行目）に出す。今の段階の行の下・右の経過時間の
  // 列には出さない（#2426）。
  const timerBox = await turn.getByTestId("chat-progress-timer").boundingBox();
  const noticeBox = await reconnecting.boundingBox();
  expect(timerBox && noticeBox && noticeBox.y >= timerBox.y + timerBox.height - 1).toBeTruthy();
  expect(timerBox && noticeBox && noticeBox.x < timerBox.x).toBeTruthy();
  await expect(turn.getByTestId("chat-progress-current").getByTestId("chat-progress-reconnecting")).toHaveCount(0);
  await expect(turn.getByTestId("chat-progress-running").getByTestId("chat-progress-reconnecting")).toHaveCount(1);
  await expect(current).toHaveAttribute("data-reconnecting", "true");
  await expect(turn.getByTestId("chat-progress-timer")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`progress-reconnecting.png`) });
  complete(run, progress);
  await expect(turn.getByText("契約は 1 年ごとに更新します。")).toBeVisible({ timeout: 40_000 });
  await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "done");
  await expect(reconnecting).toHaveCount(0);
  await expect(page.getByTestId("chat-send")).toHaveAccessibleName("送信");
});

test(`段階の配信の失敗が続いても backoff して再開し、失敗の終端が出る`, async ({ page, mockApi }) => {
  test.setTimeout(90_000);
  const { run, progress } = seedRunningRun(mockApi);
  const control = await controlProgressFetch(page);
  await page.goto("/chat");
  await openThread(page);

  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  await expect(turn.getByTestId("chat-progress-current")).toContainText(MODEL_CALL);
  control.fail = 6;
  await expect.poll(() => control.fail, { timeout: 60_000 }).toBe(0);
  run.status = "failed";
  progress.step("chat-1", "failed").terminal("failed");
  run.events = [
    ...(run.events as Record<string, unknown>[]),
    { id: "ev-failed", run_id: RUN_ID, type: "runtime.failed", message: "モデルの呼び出しに失敗しました（APIError）。", payload: {}, created_at: iso(0) },
  ];
  await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "failed", { timeout: 30_000 });
  await expect(turn.getByText("モデルの呼び出しに失敗しました（APIError）。")).toBeVisible();
  await expect(turn.getByTestId("chat-progress-reconnecting")).toHaveCount(0);
  // 一時的な失敗で会話が消えて新しい会話に戻らない。
  await expect(turn).toBeVisible();
});

// 処理の経過（span の木）の名前と Trace ID は、実際の会話の API と画面を通して確かめる（#3226）。
test("処理の経過は span の木を日本語の名前で出し、処理の詳細に Trace ID を出す", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  const { run, progress } = seedRunningRun(mockApi);
  progress
    .step("chat-1", "done", { kind: "plan" })
    .step("tool:synthetic_tool", "running")
    .step("mcp-1", "done", { parent: "tool:synthetic_tool" })
    .step("tool:synthetic_tool", "done")
    .step("chat-2", "running");
  complete(run, progress);
  await page.goto("/chat");
  await openThread(page);
  await page.getByTestId("chat-progress-summary").click();
  const list = page.getByTestId("chat-progress");
  // 実行の全体（root）は行にせず、技術の span は日本語の名前にする（#3219）。英語の span の名前は開いた詳細の
  // Span name だけ。
  await expect(list.getByTestId("chat-progress-step-mcp-1")).toContainText("MCP の呼び出し");
  await expect(list.getByText("実行の全体")).toHaveCount(0);
  await expect(list.locator("[data-run-timeline-label]").filter({ hasText: /gen_ai\.|mcp\.|agent\.execute/ })).toHaveCount(0);
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(SYNTHETIC_TRACE_ID);
  // 「トレースの詳細」の 2 つ目の表示は無い。
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  await expect(page.locator("body")).not.toHaveText(/private-payload/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("agent-trace-progress.png"), fullPage: true });
});


// 実際の parent ID を持つ深い木を、新規配信と保存後の再読込で同じ共有部品に表示する。
test("深いリランク要求とSDKの親子を一覧内でたどれ、保存後も階層を保つ", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  const { run, progress } = seedRunningRun(mockApi);
  run.goal = "合成の質問";
  progress.step("chat-1", "done");
  let parent = "";
  for (let i = 0; i < 6; i++) {
    const id = `deep-${i}`;
    progress.step(id, "done", { name: i === 0 ? "gen_ai.execute_tool" : "synthetic.stage", ...(parent ? { parent } : {}), displayName: `合成工程 ${i}` });
    parent = id;
  }
  for (const [id, cached] of [["rank-a", false], ["rank-cache", true], ["rank-b", false]] as const) {
    progress.step(id, "done", { name: "rerank.request", parent });
    if (!cached) progress.step(`${id}-sdk`, id === "rank-b" ? "running" : "done", { name: "gen_ai.rerank", parent: id, attributes: { "gen_ai.request.model": "synthetic-rerank", "pr.attempt": 2 } });
  }
  progress.step("orphan-sdk", "done", { name: "gen_ai.rerank", parent: "missing-request" });
  await page.goto("/chat");
  await openThread(page, "合成の質問");
  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  await expect(turn.getByTestId("chat-progress-step-rank-b-sdk")).toBeVisible();

  async function verify() {
    const request = turn.getByTestId("chat-progress-step-rank-a");
    const sdk = turn.getByTestId("chat-progress-step-rank-a-sdk");
    await expect(request).toHaveAttribute("data-level", "6");
    await expect(sdk).toHaveAttribute("data-level", "7");
    await expect(sdk).toHaveAttribute("data-parent-id", "rank-a");
    await expect(turn.getByTestId("chat-progress-step-rank-b-sdk")).toHaveAttribute("data-parent-id", "rank-b");
    await expect(turn.getByTestId("chat-progress-step-rank-cache-sdk")).toHaveCount(0);
    await expect(turn.getByTestId("chat-progress-step-orphan-sdk")).not.toHaveAttribute("data-level");
    await expect(turn.getByTestId("chat-progress-step-orphan-sdk")).toHaveAttribute("data-parent-id", "missing-request");
    const viewport = turn.getByTestId("chat-progress-timeline").locator("[data-run-timeline-scroll]");
    await expect(viewport).toHaveAttribute("tabindex", "0");
    await viewport.focus();
    await page.keyboard.press("ArrowRight");
    if (testInfo.project.name === "mobile-375") await expect.poll(() => viewport.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    const x = async (locator: typeof request) => locator.locator("[data-run-timeline-label]").evaluate(el => el.getBoundingClientRect().x);
    expect(await x(sdk)).toBeGreaterThan(await x(request));
    await turn.getByRole("button", {name: "軌跡を調べる", exact: true}).first().click();
    await turn.locator('[data-trace-record="rank-a-sdk"] button').click();
    await expect(turn.locator("[data-trace-technical]")).toContainText("Parent span ID");
    await expect(turn.locator("[data-trace-technical]")).toContainText("rank-a");
    await turn.getByRole("button", {name: "経過に戻る", exact: true}).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await sdk.scrollIntoViewIfNeeded();
  }
  async function screenshots(phase: string) {
    await page.screenshot({ path: testInfo.outputPath(`deep-rerank-${phase}.png`), fullPage: true });
  }
  await verify();
  await screenshots("live");
  progress.step("rank-b-sdk", "done");
  complete(run, progress);
  await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "done");
  await page.reload();
  await expect(turn).toBeVisible();
  await page.getByTestId("chat-progress-summary").click();
  if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 1920, height: 1080 });
  await verify();
  await screenshots("saved");
});
