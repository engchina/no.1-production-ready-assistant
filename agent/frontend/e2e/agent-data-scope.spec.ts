import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./fixtures/select-field";
import { measureSearchablePanels } from "../../../platform/packages/ui/e2e/layout-checks";

// #1378: 業務 Agent のデータの範囲。NL2SQL の業務プロファイル・RAG の検索・回答プロファイルを複数選び、
// 既定を決めて保存する。候補の読み込み中・失敗・空と、実行の step の範囲の記録を確かめる。

const AGENT_ID = "agent-1378";

function seedAgent(mockApi: MockApi, overrides: Record<string, unknown> = {}) {
  mockApi.state.agents.push({
    id: AGENT_ID,
    name: "売上分析の Agent",
    description: "売上の質問に答える",
    instructions: "売上を集計する",
    skill_ids: [],
    model_id: "",
    data_scopes: {},
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [],
    published_version: null,
    unpublished_changes: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...overrides,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function openAgent(page: Page) {
  await page.goto(`/agents?id=${AGENT_ID}`);
  await expect(page.getByRole("heading", { name: "売上分析の Agent", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "データの範囲", level: 2 })).toBeVisible();
}

/** 検索できる複数選択で候補を選ぶ（一覧は選んでも閉じない）。 */
async function pickProfiles(page: Page, fieldId: string, names: string[]) {
  const combobox = page.locator(`#${fieldId}`);
  await combobox.click();
  for (const name of names) {
    await page.getByRole("option", { name: new RegExp(name) }).click();
  }
  await page.keyboard.press("Escape");
}

test("業務プロファイルを複数選んで既定を決め、検索・回答プロファイルを 1 つに固定して保存できる", async ({
  page,
  mockApi,
}) => {
  seedAgent(mockApi);
  await openAgent(page);
  const nl2sql = page.getByTestId("agent-data-scope-nl2sql");
  const rag = page.getByTestId("agent-data-scope-rag");
  // 何も選ばないうちは「範囲なし」（今までどおり利用者が使えるすべてから選ぶ）。
  await expect(nl2sql).toContainText("範囲なし");
  await expect(nl2sql).toContainText("選ばないと、実行する利用者が使えるすべてのプロファイルから");
  await expectNoHorizontalOverflow(page);

  await pickProfiles(page, `${AGENT_ID}-data-scope-nl2sql`, ["売上", "原価"]);
  await expect(nl2sql).toContainText("2 件から選ぶ");
  const defaultField = page.getByTestId(`${AGENT_ID}-data-scope-nl2sql-default`);
  await expectSelectFieldValue(defaultField, "profile-sales");
  await chooseSelectFieldOption(defaultField, "profile-cost");

  await pickProfiles(page, `${AGENT_ID}-data-scope-rag`, ["営業の検索・回答プロファイル"]);
  await expect(rag).toContainText("1 件に固定");
  await expect(rag).toContainText("このプロファイルだけを使います");
  // 1 つだけなら既定の選択欄は出さない（それが既定）。
  await expect(page.getByTestId(`${AGENT_ID}-data-scope-rag-default`)).toHaveCount(0);
  await expectNoHorizontalOverflow(page);

  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  const saved = mockApi.lastRequest("PATCH", `/api/agents/${AGENT_ID}`);
  expect(saved?.body).toMatchObject({
    data_scopes: {
      nl2sql: { profile_ids: ["profile-cost", "profile-sales"], default_profile_id: "profile-cost" },
      rag: { profile_ids: ["bv-sales"], default_profile_id: "bv-sales" },
    },
  });
  // 保存した内容が基準になり、変更を破棄は押せない。
  await expect(page.getByRole("button", { name: "変更を破棄" })).toBeDisabled();
});

// #2091: データの範囲の検索つきの複数選択は、開いた一覧の下部（N 件選択中・表示中をすべて選択・クリア・完了）が
// 1 行に並び（375px は件数の文と操作の組の 2 段まで、操作の組は分けない）、画面からはみ出さない。
test("データの範囲の開いた一覧の下部が 1 行に並び、画面からはみ出さない", { tag: "@responsive" }, async ({
  page,
  mockApi,
}) => {
  seedAgent(mockApi);
  await openAgent(page);
  const wide = (page.viewportSize()?.width ?? 0) >= 640;
  for (const connection of ["nl2sql", "rag"]) {
    await page.locator(`#${AGENT_ID}-data-scope-${connection}`).click();
    const footer = page.locator("[data-searchable-panel-footer]");
    await expect(footer).toContainText(/\d+ 件選択中/);
    const panels = await measureSearchablePanels(page);
    expect(panels).toHaveLength(1);
    const [panel] = panels;
    expect(panel, JSON.stringify(panel)).toMatchObject({
      countWrapped: false,
      actionsSplit: false,
      ...(wide ? { footerOneRow: true } : {}),
    });
    expect(panel.panelWidth).toBeGreaterThanOrEqual(panel.fieldWidth);
    expect(panel.left).toBeGreaterThanOrEqual(0);
    expect(panel.right).toBeLessThanOrEqual(panel.viewport);
    await expectNoHorizontalOverflow(page);
    await footer.getByRole("button", { name: "完了" }).click();
    await expect(footer).toHaveCount(0);
  }
});

test("候補の読み込み中は経過時間つきの表示を出し、読み込んだら選べる", async ({ page, mockApi }) => {
  seedAgent(mockApi);
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/agent-data-scopes/nl2sql/candidates", async (route) => {
    await gate;
    await route.fallback();
  });
  await openAgent(page);

  const loading = page.getByTestId("agent-data-scope-nl2sql-loading");
  await expect(loading).toContainText("業務プロファイルを読み込んでいます");
  // 読み込みの遅い接続があっても、ほかの接続は選べる。
  await expect(page.locator(`#${AGENT_ID}-data-scope-rag`)).toBeVisible();
  await expectNoHorizontalOverflow(page);

  release();
  await expect(loading).toHaveCount(0);
  await expect(page.locator(`#${AGENT_ID}-data-scope-nl2sql`)).toBeVisible();
});

test("候補を読み込めないときは理由と再試行を出し、範囲にある ID は外せる", async ({ page, mockApi }) => {
  seedAgent(mockApi, {
    data_scopes: { nl2sql: { profile_ids: ["profile-sales"], default_profile_id: "profile-sales" } },
  });
  mockApi.state.dataScopeCandidateErrors.nl2sql = {
    status: 409,
    message: "MCP 接続「データ問い合わせ（NL2SQL）」の URL が設定されていないため、プロファイルを選べません。",
  };
  await openAgent(page);

  const error = page.getByTestId("agent-data-scope-nl2sql-error");
  await expect(error).toContainText("URL が設定されていないため、プロファイルを選べません");
  await expectNoHorizontalOverflow(page);
  // 選んである ID は chip で出し、外せる。
  const nl2sql = page.getByTestId("agent-data-scope-nl2sql");
  await expect(nl2sql.getByRole("list", { name: /選択中/ })).toContainText("profile-sales");

  // 設定を直してから再試行すると候補が出る。
  delete mockApi.state.dataScopeCandidateErrors.nl2sql;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(error).toHaveCount(0);
  await expect(nl2sql.getByRole("list", { name: /選択中/ })).toContainText("売上");
});

test("使えるプロファイルが無いときは空の候補を出し、使えない ID は印を付ける", async ({ page, mockApi }) => {
  seedAgent(mockApi, {
    data_scopes: { rag: { profile_ids: ["bv-gone"], default_profile_id: "bv-gone" } },
  });
  mockApi.state.dataScopeCandidates.rag = [];
  await openAgent(page);

  const rag = page.getByTestId("agent-data-scope-rag");
  await expect(page.getByTestId("agent-data-scope-rag-missing")).toContainText("bv-gone");
  await expect(rag.getByRole("list", { name: /選択中/ })).toContainText("使えません");
  await page.locator(`#${AGENT_ID}-data-scope-rag`).click();
  await expect(page.getByText("あなたが使えるプロファイルがありません")).toBeVisible();
  await page.keyboard.press("Escape");
  await expectNoHorizontalOverflow(page);

  // 使えない ID を外して保存すると、範囲なしに戻る。
  await rag.getByRole("button", { name: /bv-gone/ }).click();
  await expect(rag).toContainText("範囲なし");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  expect(mockApi.lastRequest("PATCH", `/api/agents/${AGENT_ID}`)?.body).toMatchObject({ data_scopes: {} });
});

test(
  "説明の長い候補があっても、開いた候補の一覧はカードの中に収まる",
  { tag: "@responsive" },
  async ({ page, mockApi }) => {
    // #1794: 説明は 1 行で省略する（nowrap）ため、部品が親の grid の列を広げてカードからはみ出していた。
    seedAgent(mockApi);
    mockApi.state.dataScopeCandidates.rag = [
      {
        id: "bv-long",
        name: "評価: 多段の質問 拡張 on（#1517 見出しの深さ、1012）",
        description:
          "#1517（見出しの深さの規則）の後で取り込み直した多段の評価の KB（実体・章節の拡張 on。評価用）".repeat(4),
      },
      { id: "bv-sales", name: "営業の検索・回答プロファイル", description: "営業部門の文書" },
    ];
    await openAgent(page);

    const rag = page.getByTestId("agent-data-scope-rag");
    await page.locator(`#${AGENT_ID}-data-scope-rag`).click();
    const panel = rag.locator("[data-searchable-panel]");
    await expect(panel.getByRole("option", { name: /多段の質問/ })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    const cardBox = await rag.boundingBox();
    const panelBox = await panel.boundingBox();
    expect(cardBox && panelBox).toBeTruthy();
    expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
    // 一覧の下端の操作も画面の中にある。
    await expect(panel.getByRole("button", { name: "完了" })).toBeInViewport();
  }
);

test("処理の経過のツールの段に、使ったプロファイルと範囲の補完・拒否が出る", {tag: "@responsive"}, async ({ page, mockApi }, testInfo) => {
  const step = (id: string, status: string, dataScope: Record<string, unknown>, error?: string) => ({
    id,
    run_id: "run-1378",
    kind: "tool",
    status,
    tool_call: { name: "nl2sql__nl2sql_query", arguments: { question: "売上" }, data_scope: dataScope },
    tool_result: {
      name: "nl2sql__nl2sql_query",
      success: !error,
      output: error ? null : { job_id: "job-1", status: "done", columns: [], rows: [] },
      error: error ?? null,
      error_code: error ? "agent_data_scope_violation" : null,
      error_details: {},
      started_at: MOCK_NOW,
      completed_at: MOCK_NOW,
      duration_ms: 10,
      policy_decision: "allow",
      approval_required: false,
      guardrail_warnings: [],
      audit_metadata: {},
    },
    started_at: MOCK_NOW,
    completed_at: MOCK_NOW,
  });
  const allowed = ["profile-sales", "profile-cost"];
  mockApi.state.runs.push({
    id: "run-1378",
    goal: "人事の数は？",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [
      step(
        "step-rejected",
        "failed",
        {
          connection: "nl2sql",
          allowed_profile_ids: allowed,
          argument: "profile_id",
          requested_profile_id: "profile-hr",
          action: "rejected",
        },
        "業務プロファイル「profile-hr」はこの業務 Agent では使えません。"
      ),
      step("step-filled", "completed", {
        connection: "nl2sql",
        allowed_profile_ids: allowed,
        argument: "profile_id",
        profile_id: "profile-cost",
        action: "filled",
      }),
    ],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: `thread_${"c".repeat(32)}`,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
  await page.goto("/runs?id=run-1378");
  // 処理の経過の段（#1955）。失敗した段は最初から開き、完了した段は行を開いて中身を見る。
  const timeline = page.getByTestId("run-timeline-section");
  await page.getByTestId("run-timeline-section-summary").click();
  await timeline.getByRole("button", { name: /選ばれたツールを呼び出しました/ }).click();

  await expect(timeline.getByText("profile-hr は範囲外のため実行していません", {exact: true})).toBeVisible();
  await expect(timeline.getByText("profile-cost を範囲から補完", {exact: true})).toBeVisible();
  await timeline.screenshot({path: testInfo.outputPath("tool-data-scope.png")});
  await expectNoHorizontalOverflow(page);
});


test("権限のない候補は障害や再試行として表示しない", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  seedAgent(mockApi);
  mockApi.state.dataScopeCandidateErrors.nl2sql = { status: 403, message: "この製品を使う権限がありません。" };
  await openAgent(page);
  const card = page.getByTestId("agent-data-scope-nl2sql");
  await expect(card.getByTestId("agent-data-scope-nl2sql-forbidden")).toBeVisible();
  await expect(card.getByRole("button", { name: "再試行" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("candidate-forbidden.png"), fullPage: true });
});
