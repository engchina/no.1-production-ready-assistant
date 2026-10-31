import type { Locator, Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1286: チャットの回答の下に、支援タスクの状態（確かめた条件・確認待ちの質問・使った業務ガイド）と
// 資料との照合の結果を業務の言葉で出す。成果物の JSON・内部の語（budget_exceeded など）は業務の利用者に見せない。
// #2361: 除いた内容の一覧は回答の本文の節 1 か所に全件を出し、「資料との照合」の行は件数のバッジだけ（節の項目の数と
// 一致させる）。行を開いた中に小見出し「資料で確かめた結果」・同じバッジ・除いた内容を出し直さない。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

const THREAD_ID = `thread_${"b".repeat(32)}`;
const GOAL = "経費の精算の手順は？";

/** 画面に出してはいけない内部の語（成果物の JSON の項目名・値）。 */
const INTERNAL_WORDS = [
  "budget_exceeded",
  "budget",
  "user_answer",
  "rag_guide",
  "guide_id",
  "condition_id",
  "no_rag_evidence",
  "no_tool_grounding",
  "absence_only",
  "validator_unavailable",
  "unvalidated",
  "skipped",
  "support_task",
  "answer_validation",
  "{",
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function expectNoInternalWords(locator: Locator) {
  const text = await locator.innerText();
  for (const word of INTERNAL_WORDS) expect(text, `内部の語「${word}」を出さない`).not.toContain(word);
}

function supportTask(overrides: Record<string, unknown> = {}) {
  return {
    id: "artifact-support",
    kind: "support_task",
    name: "支援タスクの状態",
    created_at: MOCK_NOW,
    content: {
      schema_version: 1,
      thread_id: THREAD_ID,
      owner_user_uuid: "local",
      run_id: "run-review-1",
      goal: GOAL,
      known_conditions: {
        expense_type: {
          value: "出張",
          label: "経費の種類",
          source: "user_answer",
          updated_at: MOCK_NOW,
          previous: [{ value: "交際費", source: "rag_guide", updated_at: MOCK_NOW }],
        },
        amount_band: { value: "5 万円未満", label: "金額", source: "rag_guide", updated_at: MOCK_NOW },
      },
      pending_clarifications: [
        {
          condition_id: "receipt",
          label: "領収書",
          question: "領収書はありますか？",
          options: ["ある", "ない"],
        },
      ],
      guide: { guide_id: "guide-expense", revision: 3, title: "経費精算ガイド", decision: "answer" },
      outcome: "needs_clarification",
      gaps: ["精算の締め日"],
      evidence: [{ document_id: "doc-1", chunk_id: "chunk-1", chunk_set_id: "", file_name: "経費規程.pdf" }],
      budget: {
        run: { tool_calls: 4, rag_calls: 4, tool_seconds: 12.5, rag_seconds: 10, budget_exceeded: 1 },
        task: { runs: 1, tool_calls: 4, rag_calls: 4, tool_seconds: 12.5, rag_seconds: 10 },
        limits: { rag_calls_per_run: 4, tool_calls_per_task: 60 },
      },
      updated_at: MOCK_NOW,
      ...overrides,
    },
  };
}

function validation(content: Record<string, unknown>, id = "artifact-validation") {
  return {
    id,
    kind: "answer_validation",
    name: "資料との照合",
    created_at: MOCK_NOW,
    content: {
      reason: null,
      message: null,
      valid: null,
      connection: "rag",
      tool_name: "rag__rag_validate_answer",
      step_id: "step-validate",
      evidence: [{ document_id: "doc-1", chunk_id: "chunk-1" }],
      result: null,
      connections: [],
      ...content,
    },
  };
}

const WITHHELD = validation({
  status: "completed",
  valid: false,
  result: {
    status: "completed",
    valid: false,
    claims: [
      { answer_quote: "申請は部長の承認が要ります。", status: "supported", reason: "" },
      { answer_quote: "上限は 10 万円です。", status: "contradicted", reason: "規程では 5 万円" },
      { answer_quote: "翌月払いです。", status: "unsupported", reason: "根拠に記載が無い" },
      // 主張ではない段落（出典の行。#1306）は、RAG が確かめていなくても資料で確認できず除いた内容に出さない。
      { answer_quote: "【経費規程.pdf p.3】", status: "unassessed", reason: "出典の行", non_claim: "citation" },
    ],
    findings: [
      { check: "requests", code: "request_missing", severity: "error", message: "「締め日」の質問に答えていません。" },
      { check: "impact", code: "impact_warning", severity: "warning", message: "出さない警告" },
    ],
    stale_evidence: [{ document_id: "doc-2", chunk_id: "chunk-2" }],
    missing_evidence: [],
  },
  withheld: { claims: 2, findings: 1, all: false },
  // 本文の節の項目の数（段落 2・指摘 1・古い版 1）。件数のバッジはこの数を使う。
  notice: { items: 4, heading: "資料で確認できず除いた内容" },
});

/** 回答の本文（資料との照合が末尾に足した節に、除いた内容を全件書いたもの。backend の `_publish` と同じ形）。 */
const WITHHELD_ITEMS = [
  "根拠と矛盾: 「上限は 10 万円です。」",
  "根拠で確かめられない: 「翌月払いです。」",
  "「締め日」の質問に答えていません。",
  "根拠の 1 件は文書の古い版です。最新の版で確かめ直してください。",
];
const WITHHELD_ANSWER = [
  "精算は申請から始めます。",
  "",
  "**資料で確認できず除いた内容**",
  "",
  "根拠で確かめられなかった次の内容は、回答に載せていません。",
  ...WITHHELD_ITEMS.map((item) => `- ${item}`),
].join("\n");

function answer(text: string, outcome?: Record<string, unknown>) {
  return {
    id: `answer-${text.length}`,
    kind: "answer",
    name: "回答",
    created_at: MOCK_NOW,
    content: outcome ? { text, outcome } : { text },
  };
}

/** 成果物 answer の対応（#1305。backend の `answer_outcome.answer_outcome` の形）。 */
function outcome(value: string) {
  return { schema_version: 1, value, basis: "rag_search", rag_outcome: value, signals: ["absence"] };
}

/** 回答の対応の内部の値（画面に出さない。#1314）。 */
const OUTCOME_INTERNAL_WORDS = [
  "answered",
  "conditional",
  "needs_clarification",
  "needs_environment_data",
  "needs_human",
  "insufficient_evidence",
  "rag_search",
  "basis",
  "signals",
];

async function expectNoOutcomeInternalWords(locator: Locator) {
  const text = await locator.innerText();
  for (const word of OUTCOME_INTERNAL_WORDS) expect(text, `内部の値「${word}」を出さない`).not.toContain(word);
}

function seedRun(mockApi: MockApi, id: string, artifacts: unknown[], overrides: Record<string, unknown> = {}) {
  mockApi.state.runs.push({
    id,
    goal: GOAL,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts,
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  });
}

async function openSeedThread(page: Page) {
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${GOAL}`) }).click();
    return;
  }
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText(GOAL).click();
}

for (const viewport of VIEWPORTS) {
  test(`回答の下に確かめた条件・確認待ち・業務ガイドを出し、除いた内容は本文の節の 1 か所に件数と一致させて出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi, "run-review-1", [answer(WITHHELD_ANSWER), supportTask(), WITHHELD]);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openSeedThread(page);

    const turn = page.getByTestId("chat-turn-run-review-1");
    await expect(turn.getByText("精算は申請から始めます。")).toBeVisible();
    // 予算の上限は控えめな案内だけ（内部の語は出さない）。
    await expect(turn.getByTestId("chat-review-run-review-1-limit")).toContainText(
      "資料を調べられる回数の上限に達したため、ここまでに集めた資料で答えています。"
    );
    // 畳んでいても、照合の結果は見出しのバッジで分かる（アイコン付き）。件数は本文の節の項目の数と一致する。
    const summary = turn.getByTestId("chat-review-run-review-1-summary");
    await expect(summary).toContainText("資料との照合");
    const badge = summary.locator("[data-status-variant]");
    await expect(badge).toHaveText(`確かめられない点 ${WITHHELD_ITEMS.length} 件`);
    await expect(badge).toHaveAttribute("data-status-variant", "warning");
    await expect(badge.locator("svg")).toHaveCount(1);
    const panel = turn.getByTestId("chat-review-run-review-1");
    await expect(panel).not.toHaveAttribute("open", "");

    await summary.click();
    await expect(panel).toHaveAttribute("open", "");
    const conditions = panel.getByRole("group", { name: "確かめた条件" });
    await expect(conditions.getByRole("listitem")).toHaveText([
      "経費の種類：出張（あなたの答えから）前の値：交際費",
      "金額：5 万円未満（質問の文から）",
    ]);
    const clarifications = panel.getByRole("group", { name: "確認待ちの質問" });
    await expect(clarifications).toContainText("領収書はありますか？");
    await expect(clarifications).toContainText("選択肢：ある / ない");
    await expect(panel.getByRole("group", { name: "使った業務ガイド" })).toHaveText(
      "使った業務ガイド経費精算ガイド（第 3 版）"
    );
    await expect(panel.getByRole("group", { name: "まだ足りない情報" })).toContainText("精算の締め日");
    // 開いた中に、小見出し「資料で確かめた結果」・同じバッジ・除いた内容（とその説明）を出し直さない。
    await expect(panel.getByRole("group", { name: "資料で確かめた結果" })).toHaveCount(0);
    await expect(panel.locator("[data-status-variant]")).toHaveCount(1);
    await expect(panel.getByTestId("answer-review-unverified")).toHaveCount(0);
    await expect(panel.getByTestId("answer-review-validation")).toHaveCount(0);
    for (const item of WITHHELD_ITEMS) await expect(panel).not.toContainText(item);
    // 除いた内容は本文の節の 1 か所に、全件を同じ書式で出す（「ほか N 件」で切らない）。件数はバッジと同じ。
    const body = turn.getByTestId("chat-answer-run-review-1");
    await expect(body.getByRole("listitem")).toHaveText(WITHHELD_ITEMS);
    await expect(body).not.toContainText("ほか");
    await expect(turn).not.toContainText("出さない警告");
    await expectNoInternalWords(turn);

    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`review-${viewport.name}.png`), fullPage: true });
  });
}

test("資料との照合の結果は、照合済み・確かめられない点（過去の Run）・照合できなかった・対象外を見分けて出し、理由があるときだけ開ける", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-verified", [
    answer("回答 1"),
    validation({ status: "completed", valid: true, result: { status: "completed", valid: true, claims: [] }, withheld: { claims: 0, findings: 0, all: false } }),
  ]);
  seedRun(mockApi, "run-all", [
    answer("回答 2"),
    validation({
      status: "completed",
      valid: false,
      result: { status: "no_evidence", valid: false, claims: [], findings: [] },
      withheld: { claims: 0, findings: 0, all: true },
    }),
  ]);
  seedRun(mockApi, "run-failed", [
    answer("回答 3"),
    validation({ status: "unvalidated", reason: "validator_unavailable", message: "接続できません" }),
  ]);
  seedRun(mockApi, "run-no-evidence", [answer("回答 4"), validation({ status: "unvalidated", reason: "no_rag_evidence" })]);
  seedRun(mockApi, "run-skipped", [answer("回答 5"), validation({ status: "skipped", reason: "no_rag_evidence" })]);
  // 資料に答えが無い質問の拒答（「記載がありません」の文は主張ではない。#1306）。
  seedRun(mockApi, "run-refusal", [
    answer("回答 6"),
    validation({
      status: "completed",
      valid: false,
      result: {
        status: "completed",
        valid: false,
        claims: [
          { answer_quote: "資料に記載がありません。", status: "unsupported", reason: "", non_claim: "absence" },
        ],
        findings: [],
        stale_evidence: [],
        missing_evidence: [],
      },
      withheld: { claims: 0, findings: 0, all: false },
    }),
  ]);
  seedRun(mockApi, "run-clarification", [
    answer("回答 7"),
    validation({ status: "skipped", reason: "clarification_only" }),
  ]);
  // 資料に無い・答えられないことだけを述べた回答（拒答。#1879）。
  seedRun(mockApi, "run-absence", [answer("回答 8"), validation({ status: "skipped", reason: "absence_only" })]);
  await page.goto("/chat");
  await openSeedThread(page);

  // 照合済み・除いた内容があるとき（バッジと本文で分かる）は開いて見るものが無いので、⌄ を出さずに行だけを出す。
  const cases = [
    ["run-verified", "照合済み", "success", null],
    ["run-all", "確かめられない点あり", "warning", null],
    ["run-failed", "照合できませんでした", "warning", "資料との照らし合わせが途中で止まったため、この回答は確かめられていません。内容を資料で確かめてから使ってください。"],
    ["run-no-evidence", "照合できませんでした", "warning", "この回答は資料を使わずに作ったため、資料と照らし合わせて確かめていません。"],
    ["run-skipped", "照合の対象外", "neutral", "この業務 Agent は資料を使わないため、資料との照らし合わせの対象外です。"],
    ["run-refusal", "照合済み", "success", null],
    ["run-clarification", "照合の対象外", "neutral", "確認の質問だけの回答のため、資料との照らし合わせの対象外です。"],
    [
      "run-absence",
      "照合の対象外",
      "neutral",
      "資料に根拠が見つからず答えられないことだけを伝えた回答のため、資料との照らし合わせの対象外です。",
    ],
  ] as const;
  for (const [runId, label, variant, message] of cases) {
    const turn = page.getByTestId(`chat-turn-${runId}`);
    const row = turn.getByTestId(`chat-review-${runId}`);
    await expect(row).toContainText("資料との照合");
    const badge = row.locator("[data-status-variant]");
    await expect(badge).toHaveText(label);
    await expect(badge).toHaveAttribute("data-status-variant", variant);
    if (message === null) {
      await expect(turn.getByTestId(`chat-review-${runId}-summary`)).toHaveCount(0);
      await expect(row.locator("summary")).toHaveCount(0);
    } else {
      await turn.getByTestId(`chat-review-${runId}-summary`).click();
      await expect(turn.getByTestId("answer-review-validation")).toHaveText(message);
      // 開いた中にバッジを出し直さない。
      await expect(row.locator("[data-status-variant]")).toHaveCount(1);
    }
    // 条件・確認待ちが無い回答は、その見出しを出さない。予算の案内も出さない。
    await expect(turn.getByRole("group", { name: "確かめた条件" })).toHaveCount(0);
    await expect(turn.getByTestId(`chat-review-${runId}-limit`)).toHaveCount(0);
    await expectNoInternalWords(turn);
  }
});

test("支援タスクの状態も資料との照合も無い回答には、資料との照合の行を出さない", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-plain", [answer("状態の無い回答")]);
  // 作成中の Run は、前の状態が残っていても出さない（回答が出てから出す）。
  seedRun(mockApi, "run-active", [supportTask()], { status: "running" });
  await page.goto("/chat");
  await openSeedThread(page);

  await expect(page.getByTestId("chat-turn-run-plain").getByText("状態の無い回答")).toBeVisible();
  await expect(page.getByTestId("chat-review-run-plain")).toHaveCount(0);
  await expect(page.getByTestId("chat-review-run-plain-limit")).toHaveCount(0);
  await expect(page.getByTestId("chat-review-run-active")).toHaveCount(0);
  await expect(page.getByTestId("chat-review-run-active-limit")).toHaveCount(0);
});

test("確認待ちの質問だけがある回答は、見出しにバッジを出さずに確認待ちの質問を出す", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-clarify", [
    answer("領収書はありますか？"),
    supportTask({
      known_conditions: {},
      guide: null,
      gaps: [],
      budget: { run: { budget_exceeded: 0 }, task: {}, limits: {} },
    }),
  ]);
  await page.goto("/chat");
  await openSeedThread(page);

  const summary = page.getByTestId("chat-review-run-clarify-summary");
  await expect(summary.locator("[data-status-variant]")).toHaveCount(0);
  await summary.click();
  const panel = page.getByTestId("chat-review-run-clarify");
  await expect(panel.getByRole("group", { name: "確認待ちの質問" })).toContainText("領収書はありますか？");
  await expect(panel.getByRole("group", { name: "確かめた条件" })).toHaveCount(0);
  await expect(panel.getByTestId("answer-review-validation")).toHaveCount(0);
  await expect(page.getByTestId("chat-review-run-clarify-limit")).toHaveCount(0);
});

// #2403: 支援タスクの状態・資料との照合は内部の記録。実行の詳細の「成果物」には出さず（利用者が使う出力だけ）、
// 監査ログで 1 行ずつ並べ、行を開くと元の JSON まで見られる。監査の閲覧の権限が無い利用者には監査ログを出さない。
test("実行の詳細は内部の記録を成果物に出さず、監査ログで元の JSON まで見る", async ({ page, mockApi }, testInfo) => {
  seedRun(mockApi, "run-review-1", [answer(WITHHELD_ANSWER), supportTask(), WITHHELD]);
  await page.goto("/runs?id=run-review-1");
  await expect(page.getByTestId("run-summary")).toBeVisible();

  await expect(page.getByTestId("run-answer-section")).toContainText("精算は申請から始めます。");
  // 出すものが無いので「成果物」の節ごと出さない。
  await expect(page.getByTestId("run-artifacts-section")).toHaveCount(0);
  await expect(page.getByTestId("run-audit-section")).toContainText(
    "ツールの実行の判断・権限・承認と、実行が残した内部の記録（元の JSON）を確かめます"
  );

  // 管理者（既定のローカル利用者）は監査ログを開いて、内部の記録を 1 行ずつ見る。
  await page.getByTestId("run-audit-section").locator("summary").click();
  const log = page.getByTestId("run-audit-log");
  await expect(log.getByTestId("run-audit-log-count")).toHaveText("全 2 件");
  const support = log.getByTestId("run-audit-log-event-record-artifact-support");
  await expect(support).toContainText("支援タスクの状態");
  await expect(log.getByTestId("run-audit-log-event-record-artifact-validation")).toContainText("資料との照合");
  // 行を開くと、種類・記録の ID と元の JSON の全体を出す。
  const toggle = log.getByTestId("run-audit-log-event-record-artifact-support-toggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const details = log.getByTestId("run-audit-log-event-record-artifact-support-details");
  await expect(details).toContainText("元の JSON");
  await expect(details).toContainText("support_task");
  await expect(details).toContainText("budget_exceeded");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("run-detail-audit-records.png"), fullPage: true });

  // 実行の閲覧だけの利用者には監査ログ（元の JSON）を出さず、内部の語も出さない。
  mockApi.setCurrentUser(
    dbUser({ login_user_id: "viewer.user", permissions: ["agent.runs.view"], allowed_agent_ids: ["default"] })
  );
  await page.reload();
  await expect(page.getByTestId("run-answer-section")).toContainText("精算は申請から始めます。");
  await expect(page.getByTestId("run-audit-section")).toHaveCount(0);
  await expect(page.getByTestId("run-artifacts-section")).toHaveCount(0);
  await expect(page.getByText("元の JSON")).toHaveCount(0);
  await expectNoInternalWords(page.getByTestId("run-answer-section"));
});

// #1314: 回答の対応（成果物 answer の outcome）を、RAG の回答の詳細（#1252）と同じ文言・色のバッジで出す。
for (const viewport of VIEWPORTS) {
  test(`回答の対応のバッジを照合のバッジと並べて見出しに出し、狭い幅では折り返す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi, "run-outcome-1", [
      { ...answer("精算は申請から始めます。", outcome("conditional")), id: "answer-outcome-1" },
      supportTask(),
      WITHHELD,
    ]);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openSeedThread(page);

    const turn = page.getByTestId("chat-turn-run-outcome-1");
    const summary = turn.getByTestId("chat-review-run-outcome-1-summary");
    await expect(summary).toBeVisible();
    // 見出し → 対応 → 検証の順に並ぶ（どちらもアイコン付き。色だけに頼らない）。
    const badges = summary.locator("[data-status-variant]");
    await expect(badges).toHaveText(["条件付きの回答", "確かめられない点 4 件"]);
    const outcomeBadge = turn.getByTestId("chat-review-run-outcome-1-outcome").locator("[data-status-variant]");
    await expect(outcomeBadge).toHaveAttribute("data-status-variant", "warning");
    await expect(outcomeBadge.locator("svg")).toHaveCount(1);
    await expect(badges.nth(1).locator("svg")).toHaveCount(1);

    // 見出しの文字は 1 行のまま、バッジは要約の幅に収まる（はみ出さない・切れない）。
    const title = summary.getByText("資料との照合", { exact: true });
    const titleBox = await title.boundingBox();
    const lineHeight = await title.evaluate((node) => parseFloat(getComputedStyle(node).lineHeight));
    expect(titleBox!.height).toBeLessThanOrEqual(lineHeight + 1);
    const answerBox = await turn.getByTestId("chat-review-run-outcome-1").boundingBox();
    for (const badge of await badges.all()) {
      const box = await badge.boundingBox();
      expect(box!.x + box!.width).toBeLessThanOrEqual(answerBox!.x + answerBox!.width + 1);
      expect(await badge.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    }
    if (viewport.width < 640) {
      // 375px では検証のバッジが次の行へ折り返す（見出しの文字を縦に潰さない）。
      const first = await badges.nth(0).boundingBox();
      const second = await badges.nth(1).boundingBox();
      expect(second!.y).toBeGreaterThan(first!.y);
    }
    // 資料との照合は回答の本文の下（処理の経過 → 回答 → 資料との照合。#2123 / #2324）。
    const bodyBox = await turn.getByTestId("chat-answer-run-outcome-1").boundingBox();
    expect(bodyBox!.y).toBeLessThan(answerBox!.y);
    await expectNoOutcomeInternalWords(turn);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`outcome-chat-${viewport.name}.png`), fullPage: true });

    // 実行の概要の見出しにはバッジを出さない。
    await page.goto("/runs?id=run-outcome-1");
    await expect(page.getByTestId("run-summary")).toBeVisible();
    const runBadge = page.getByTestId("run-answer-outcome-answer-outcome-1").locator("[data-status-variant]");
    await expect(runBadge).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`outcome-run-${viewport.name}.png`), fullPage: true });
  });
}

// #1835: 根拠・データのツールを呼ばずに作った回答は、backend が決まった拒答の文に置き換える。チャットと実行の詳細に
// 拒答の文・「根拠不足」のバッジ・拒答した理由を出し、内部の語（no_tool_grounding）は出さない。
const REFUSAL = [
  "社内の資料・業務データで確かめられなかったため、お答えできません。推測ではお答えしていません。",
  "この業務 Agent が扱う内容: 経費精算の質問に答えます。（スキル: 経理の調べ物）",
  "この業務 Agent が扱う内容について、文書名・システム名・期間などを入れて質問してください。例: 「経費精算規程の締め日は？」",
].join("\n\n");

for (const viewport of VIEWPORTS) {
  test(`ツールで確かめずに作った回答の拒答の文と理由を出す (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi, "run-no-tool", [
      {
        ...answer(REFUSAL, {
          schema_version: 1,
          value: "insufficient_evidence",
          basis: "no_tool_grounding",
          rag_outcome: null,
          signals: [],
        }),
        id: "answer-no-tool",
      },
      validation({ status: "skipped", reason: "no_tool_grounding", connection: null, tool_name: null, step_id: null, evidence: [] }),
    ]);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openSeedThread(page);

    const turn = page.getByTestId("chat-turn-run-no-tool");
    await expect(turn.getByText("社内の資料・業務データで確かめられなかったため、お答えできません。", { exact: false })).toBeVisible();
    await expect(turn.getByText("「経費精算規程の締め日は？」", { exact: false })).toBeVisible();
    const summary = turn.getByTestId("chat-review-run-no-tool-summary");
    await expect(summary.locator("[data-status-variant]")).toHaveText(["根拠不足", "照合の対象外"]);
    await summary.click();
    await expect(turn.getByTestId("answer-review-validation")).toContainText(
      "社内の資料・業務データのツールで確かめずに作られた回答だったため、推測の回答は出さず、お答えできない旨を返しました。"
    );
    await expectNoInternalWords(turn);
    await expectNoOutcomeInternalWords(turn);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`no-tool-chat-${viewport.name}.png`), fullPage: true });

    await page.goto("/runs?id=run-no-tool");

    await expect(page.getByTestId("run-summary")).toBeVisible();
    const runBadge = page.getByTestId("run-answer-outcome-answer-no-tool").locator("[data-status-variant]");
    await expect(runBadge).toHaveCount(0);
    await expect(page.getByText("社内の資料・業務データで確かめられなかったため、お答えできません。", { exact: false }).first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`no-tool-run-${viewport.name}.png`), fullPage: true });
  });
}

test("回答の対応ごとに RAG と同じ文言・色のバッジを出し、「答えた」・対応の無い古い Run・知らない値では出さない", async ({ page, mockApi }) => {
  const cases = [
    ["run-o-conditional", "conditional", "条件付きの回答", "warning"],
    ["run-o-clarification", "needs_clarification", "確認が必要", "info"],
    ["run-o-environment", "needs_environment_data", "現場のデータが必要", "warning"],
    ["run-o-human", "needs_human", "人への引き継ぎ", "warning"],
    ["run-o-insufficient", "insufficient_evidence", "根拠不足", "danger"],
  ] as const;
  const verified = validation({
    status: "completed",
    valid: true,
    result: { status: "completed", valid: true, claims: [] },
    withheld: { claims: 0, findings: 0, all: false },
  });
  cases.forEach(([runId, value], index) => seedRun(mockApi, runId, [answer(`対応の回答 ${index + 1}`, outcome(value)), verified]));
  seedRun(mockApi, "run-o-answered", [answer("答えた回答", outcome("answered")), verified]);
  seedRun(mockApi, "run-o-legacy", [answer("古い回答"), verified]);
  seedRun(mockApi, "run-o-unknown", [answer("知らない対応の回答", outcome("future_value")), verified]);
  // 照合も支援タスクの状態も無い回答は、開いても中身が無いので、見出しとバッジだけを畳まずに出す。
  seedRun(mockApi, "run-o-only", [answer("対応だけの回答", outcome("needs_human"))]);
  // 対応も照合も無い古い Run には、資料との照合の行を出さない。
  seedRun(mockApi, "run-o-plain", [answer("対応も検証も無い回答", outcome("answered"))]);
  await page.goto("/chat");
  await openSeedThread(page);

  for (const [runId, , label, variant] of cases) {
    const turn = page.getByTestId(`chat-turn-${runId}`);
    const badge = turn.getByTestId(`chat-review-${runId}-outcome`).locator("[data-status-variant]");
    await expect(badge).toHaveText(label);
    await expect(badge).toHaveAttribute("data-status-variant", variant);
    await expect(badge.locator("svg")).toHaveCount(1);
    // 照合のバッジと並ぶ（照合済みだけなら開いて見るものが無いので ⌄ を出さない）。
    await expect(turn.getByTestId(`chat-review-${runId}`).locator("[data-status-variant]")).toHaveText([
      label,
      "照合済み",
    ]);
    await expect(turn.getByTestId(`chat-review-${runId}-summary`)).toHaveCount(0);
    await expectNoOutcomeInternalWords(turn);
  }
  for (const runId of ["run-o-answered", "run-o-legacy", "run-o-unknown"]) {
    const turn = page.getByTestId(`chat-turn-${runId}`);
    await expect(turn.getByTestId(`chat-review-${runId}-outcome`)).toHaveCount(0);
    await expect(turn.getByTestId(`chat-review-${runId}`).locator("[data-status-variant]")).toHaveText([
      "照合済み",
    ]);
    await expectNoOutcomeInternalWords(turn);
  }
  await expect(page.getByTestId("chat-turn-run-o-unknown")).not.toContainText("future_value");

  const only = page.getByTestId("chat-review-run-o-only");
  await expect(only).toHaveText("資料との照合人への引き継ぎ");
  await expect(only.locator("summary")).toHaveCount(0);
  await expect(page.getByTestId("chat-review-run-o-only-outcome").locator("[data-status-variant]")).toHaveAttribute(
    "data-status-variant",
    "warning"
  );
  await expect(page.getByTestId("chat-review-run-o-plain")).toHaveCount(0);
  await expect(page.getByTestId("chat-turn-run-o-plain").getByText("対応も検証も無い回答")).toBeVisible();
});

test("実行の概要は回答の対応の有無によらずバッジを出さない", async ({ page, mockApi }) => {
  seedRun(mockApi, "run-detail-outcome", [
    { ...answer("現場の値を確かめてください。", outcome("needs_environment_data")), id: "answer-detail-1" },
  ]);
  seedRun(mockApi, "run-detail-legacy", [{ ...answer("古い実行の回答"), id: "answer-detail-2" }]);
  seedRun(mockApi, "run-detail-answered", [{ ...answer("答えた実行の回答", outcome("answered")), id: "answer-detail-3" }]);

  await page.goto("/runs?id=run-detail-outcome");

  await expect(page.getByTestId("run-summary")).toBeVisible();
  const badge = page.getByTestId("run-answer-outcome-answer-detail-1").locator("[data-status-variant]");
  await expect(badge).toHaveCount(0);

  await page.goto("/runs?id=run-detail-legacy");

  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByText("古い実行の回答")).toBeVisible();
  await expect(page.getByTestId("run-answer-outcome-answer-detail-2")).toHaveCount(0);

  await page.goto("/runs?id=run-detail-answered");

  await expect(page.getByTestId("run-summary")).toBeVisible();
  await expect(page.getByText("答えた実行の回答")).toBeVisible();
  await expect(page.getByTestId("run-answer-outcome-answer-detail-3")).toHaveCount(0);
});
