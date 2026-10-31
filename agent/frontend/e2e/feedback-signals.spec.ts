import type { Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { withListFilters } from "./fixtures/select-field";
import { findToolbarMisalignment } from "../../../platform/packages/ui/e2e/layout-checks";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1822: フィードバックの「確かめるべき実行」（暗黙のシグナル）。評価の付いていない実行も、回答の検証・回答の対応・
// ツール・承認・実行の状態から集め、実行の詳細と評価ケースへの追加に進める。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "1920px", width: 1920, height: 1080 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

const KINDS = [
  "not_helpful",
  "validation_failed",
  "unvalidated",
  "ungrounded_no_tools",
  "unanswered",
  "approval_rejected",
  "tool_failed",
  "run_failed",
] as const;

function counts(values: number[]) {
  return KINDS.map((kind, index) => ({ kind, count: values[index] ?? 0 }));
}

function signalItem(runId: string, signals: string[], overrides: Record<string, unknown> = {}) {
  return {
    run_id: runId,
    thread_id: null,
    agent_id: "default",
    agent_name: "資料・データ横断アシスタント",
    user_uuid: "u-1",
    display_name: "山田 太郎",
    question: `${runId} の質問：先月の解約件数は？`,
    answer: "**確認結果**\n\n確認できた値です。\n\n| 項目 | 値 |\n| --- | --- |\n| A | 1200 |",
    status: "completed",
    signals,
    answer_outcome: null,
    validation_result: null,
    tool_failures: 0,
    approvals_rejected: 0,
    data_tool_calls: 0,
    feedback: null,
    admin_review: null,
    created_at: MOCK_NOW,
    ...overrides,
  };
}

function seedSignals(mockApi: MockApi, items = [
  signalItem("run-tool", ["tool_failed", "unanswered"], { tool_failures: 2, answer_outcome: "insufficient_evidence" }),
  signalItem("run-failed", ["run_failed"], { status: "failed", answer: "" }),
]) {
  mockApi.state.signalReport = {
    days: 30,
    since: MOCK_NOW,
    until: MOCK_NOW,
    kind: null,
    summary: { runs: 40, flagged: 6, flagged_rate: 0.15, counts: counts([1, 1, 0, 2, 2, 1, 3, 1]) },
    previous: { runs: 32, flagged: 8, flagged_rate: 0.25, counts: counts([1, 0, 0, 0, 3, 0, 4, 1]) },
    items,
    total: items.length,
  };
}

for (const viewport of VIEWPORTS) {
  test(`確かめるべき実行の集計・種類・一覧と詳細を出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSignals(mockApi);
    await page.goto("/feedback");

    // 既定の見方は「確かめるべき実行」。
    await expect(page.getByRole("heading", { name: "フィードバック", level: 1 })).toBeVisible();
    await expect(page.getByRole("tab", { name: "確かめるべき実行" })).toHaveAttribute("aria-selected", "true");
    const summary = page.getByTestId("signals-summary");
    await expect(summary).toContainText("確かめるべき実行");
    await expect(summary).toContainText("15%");
    await expect(summary).toContainText("前の期間から −10 ポイント");
    await expect(summary).toContainText("前の期間から −2");
    await expect(summary).toContainText("40");
    // 種類ごとの件数と前の期間からの増減（色だけに頼らず文言と数で出す）。
    await expect(page.getByTestId("signals-kind-tool_failed")).toContainText("ツールの失敗");
    await expect(page.getByTestId("signals-kind-tool_failed")).toContainText("前の期間から −1");
    await expect(page.getByTestId("signals-kind-validation_failed")).toContainText("前の期間から +1");
    await expect(page.getByTestId("signals-kind-unvalidated")).toContainText("前の期間と同じ");
    await expect(page.getByTestId("signals-kind-ungrounded_no_tools")).toContainText("根拠を使わずに答えた");
    await expect(page.getByTestId("signals-kind-ungrounded_no_tools")).toContainText("前の期間から +2");

    const layout = page.getByTestId("list-detail-feedback-signals");
    const list = page.getByTestId("list-detail-feedback-signals-list");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
    await expect(page.getByTestId("list-detail-feedback-signals-detail")).toBeHidden();
    const [listBox, layoutBox] = await Promise.all([list.boundingBox(), layout.boundingBox()]);
    expect(listBox!.width).toBeCloseTo(layoutBox!.width, 0);
    const countToolbar = page.getByTestId("signals-list-toolbar");
    expect(await countToolbar.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
    // 主一覧は外側のカードを持たず、件数は表の右端、見出しは共通 Section の大きさにする。
    const appearance = await list.evaluate((element) => {
      const style = getComputedStyle(element);
      const heading = element.querySelector("h2")!;
      return { border: style.borderTopWidth, shadow: style.boxShadow, position: style.position, fontSize: getComputedStyle(heading).fontSize };
    });
    expect(appearance).toEqual({ border: "0px", shadow: "none", position: "static", fontSize: "16px" });
    const [toolbarBox, mainListBox] = await Promise.all([countToolbar.boundingBox(), list.boundingBox()]);
    expect(toolbarBox!.x + toolbarBox!.width).toBeCloseTo(mainListBox!.x + mainListBox!.width, 0);
    const table = page.getByRole("table", { name: "確かめるべき実行の一覧" });
    await expect(table.getByRole("columnheader", { name: "シグナル", exact: true })).toHaveCount(viewport.width >= 1024 ? 1 : 0);
    const toolRow = table.getByRole("row", { name: /run-tool/ });
    await expect(toolRow).toContainText("ツールの失敗");
    await expect(toolRow).toContainText("答えられなかった");
    // 主一覧は質問とシグナル・実行日時を比較し、業務 Agent と利用者は詳細で見る。
    await expect(toolRow).not.toContainText("山田 太郎");
    await expect(table.getByRole("row", { name: /run-failed/ })).toContainText("実行の失敗・取り消し");

    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`feedback-signals-${viewport.name}.png`), fullPage: true });
    await list.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`feedback-signals-list-${viewport.name}.png`) });

    // 行から全幅の詳細へ進み、一覧と集計・条件を隠して内容を読む。
    await toolRow.getByRole("button", { name: /run-tool の質問/ }).click();
    await expect(page).toHaveURL(/signal=run-tool/);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const detail = page.getByRole("region", { name: "確かめるべき実行の詳細" });
    const signals = detail.getByTestId("signals-detail-signals");
    await expect(signals).toContainText("ツールの呼び出しが失敗しました。");
    await expect(signals).toContainText("失敗したツールの呼び出し: 2 件");
    await expect(signals).toContainText("回答の対応: 根拠不足");
    await expect(detail.locator("strong").filter({ hasText: "確認結果" })).toBeVisible();
    await expect(detail.getByRole("table", { name: "回答の表", exact: true })).toContainText("1200");
    await expect(detail.getByTestId("signals-open-run")).toHaveAttribute("href", "/runs?id=run-tool");
    await expect(detail.getByTestId("signals-add-case-toggle")).toBeVisible();
    await expect(detail).toContainText("山田 太郎");
    await expect(list).toBeHidden();
    await expect(summary).toBeHidden();
    await expect(page.getByTestId("signals-filters")).toBeHidden();
    await expect(detail.getByRole("heading", { name: "確かめるべき実行の詳細" })).toBeInViewport();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`feedback-signals-detail-${viewport.name}.png`) });
    await page.getByTestId("signals-detail-pager-next").click();
    await expect(page).toHaveURL(/signal=run-failed/);
    await expect(page.getByTestId("signals-detail-pager-next")).toBeDisabled();
    await page.getByTestId("feedback-detail-back").click();
    await expect(list).toBeVisible();
    await expect(table.getByRole("button", { name: /run-failed の質問/ })).toBeFocused();
    await expect(summary).toBeVisible();
    await toolRow.getByRole("button", { name: /run-tool の質問/ }).click();
    await page.goBack();
    await expect(list).toBeVisible();
    await expect(toolRow.getByRole("button", { name: /run-tool の質問/ })).toBeFocused();
    await page.goForward();
    await expect(detail).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("signals-detail")).toContainText("失敗したツールの呼び出し: 2 件");
  });
}

for (const viewport of VIEWPORTS) {
  test(`確かめるべき実行の一覧はサーバー側でページングし、種類で絞り込める (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSignals(
      mockApi,
      Array.from({ length: 53 }, (_, index) =>
        signalItem(`run-${String(index).padStart(2, "0")}`, index === 52 ? ["run_failed"] : ["tool_failed"], {
          tool_failures: 1,
        })
      )
    );
    await page.goto("/feedback");

    const table = page.getByRole("table", { name: "確かめるべき実行の一覧" });
    await expect(table.getByRole("row", { name: /run-00/ })).toBeVisible();
    const pager = page.getByTestId("signals-pagination");
    await expect(pager).toContainText("1–25 / 53 件");
    await page.screenshot({ path: testInfo.outputPath(`signals-paginated-${viewport.name}.png`), fullPage: true });
    await pager.getByRole("button", { name: "次へ" }).click();
    await expect(table.getByRole("row", { name: /run-25/ })).toBeVisible();
    await expect(pager).toContainText("26–50 / 53 件");
    let request = mockApi.lastRequest("GET", "/api/feedback/signals");
    expect([request?.searchParams.get("offset"), request?.searchParams.get("limit")]).toEqual(["25", "25"]);

    // 見方とページは作業状態に残す（再読込しても 2 ページ目）。
    await page.reload();
    await expect(page.getByTestId("signals-pagination")).toContainText("26–50 / 53 件");
    const selected = table.getByRole("button", { name: /run-25 の質問/ });
    await selected.click();
    await expect(page.getByTestId("signals-detail")).toBeVisible();
    await page.getByTestId("feedback-detail-back").click();
    await expect(selected).toBeFocused();
    await expect(pager).toContainText("26–50 / 53 件");

    // 種類で絞ると 1 ページ目から読み直す（1 ページなら pager を出さない）。
    await withListFilters(page.getByTestId("signals-filters"), async (filters) => {
      await filters.locator("#feedback-signal").click();
      await page.getByRole("option", { name: "実行の失敗・取り消し" }).click();
    });
    await expect(table.getByRole("row", { name: /run-52/ })).toBeVisible();
    await expect(table.getByRole("row", { name: /run-00/ })).toHaveCount(0);
    request = mockApi.lastRequest("GET", "/api/feedback/signals");
    expect([request?.searchParams.get("kind"), request?.searchParams.get("offset")]).toEqual(["run_failed", null]);
    await expectNoHorizontalOverflow(page);
  });
}

test("選んだシグナルの実行が無ければ絞り込みをクリアでき、シグナルが無い期間はその旨を出す", async ({ page, mockApi }) => {
  seedSignals(mockApi);
  await page.goto("/feedback");
  await expect(page.getByRole("table", { name: "確かめるべき実行の一覧" })).toBeVisible();

  await withListFilters(page.getByTestId("signals-filters"), async (filters) => {
    await filters.locator("#feedback-signal").click();
    await page.getByRole("option", { name: "承認の却下" }).click();
  });
  await expect(page.getByText("選んだシグナルの実行はありません")).toBeVisible();
  expect(mockApi.lastRequest("GET", "/api/feedback/signals")?.searchParams.get("kind")).toBe("approval_rejected");
  await page.getByRole("button", { name: "絞り込みをクリア" }).click();
  await withListFilters(page.getByTestId("signals-filters"), async (filters) => {
    await expect(filters.locator("#feedback-signal")).toContainText("すべて");
  });
  await expect(page.getByRole("table", { name: "確かめるべき実行の一覧" })).toBeVisible();

  // 実行はあるがシグナルが無い期間。
  mockApi.state.signalReport = {
    ...mockApi.state.signalReport,
    summary: { runs: 12, flagged: 0, flagged_rate: 0, counts: counts([]) },
    items: [],
    total: 0,
  };
  await page.getByRole("button", { name: "最新情報を取得" }).click();
  await expect(page.getByText("確かめるべき実行はありません")).toBeVisible();
  await expect(page.getByRole("button", { name: "絞り込みをクリア" })).toHaveCount(0);
});

test("実行の無い期間は空の状態を出し、読み込み中は経過時間を出す", async ({ page }) => {
  await page.route("**/api/feedback/signals?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/feedback");
  await expect(page.getByTestId("signals-loading")).toContainText("確かめるべき実行を読み込んでいます");
  await expect(page.getByText("この期間の実行はありません")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

for (const viewport of VIEWPORTS) {
  test(
    `絞り込みはツールバーの 1 行に中心でそろえ、既定と違う条件があるときだけ「条件をクリア」を出す (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page, mockApi }) => {
      // #1933: 期間・業務 Agent・シグナルの 3 つは FilterSelect（ラベルは欄の中）。狭い置き場所ではシートに畳む。
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      seedSignals(mockApi);
      await page.goto("/feedback");
      const toolbar = page.getByTestId("signals-filters");
      await expect(toolbar).toBeVisible();
      expect(await findToolbarMisalignment(page)).toEqual([]);
      const clear = toolbar.getByRole("button", { name: "条件をクリア" });
      await expect(clear).toHaveCount(0);
      if (viewport.width < 640) {
        await expect(toolbar.getByRole("button", { name: "絞り込み", exact: true })).toBeVisible();
      }

      await withListFilters(toolbar, async (filters) => {
        await expect(filters.locator("#feedback-period")).toContainText(/期間:\s*直近 30 日/);
        await filters.locator("#feedback-period").click();
        await page.getByRole("option", { name: "直近 7 日" }).click();
        await filters.locator("#feedback-signal").click();
        await page.getByRole("option", { name: "ツールの失敗" }).click();
      });
      expect(mockApi.lastRequest("GET", "/api/feedback/signals")?.searchParams.get("days")).toBe("7");
      if (viewport.width < 640) {
        await expect(toolbar.getByRole("button", { name: "絞り込み（2）" })).toBeVisible();
      }
      expect(await findToolbarMisalignment(page)).toEqual([]);

      await toolbar.getByRole("button", { name: "条件をクリア" }).click();
      const request = mockApi.lastRequest("GET", "/api/feedback/signals");
      expect([request?.searchParams.get("days"), request?.searchParams.get("kind")]).toEqual(["30", null]);
      await expect(clear).toHaveCount(0);
      await withListFilters(toolbar, async (filters) => {
        await expect(filters.locator("#feedback-signal")).toContainText(/シグナル:\s*すべて/);
      });
      await expectNoHorizontalOverflow(page);
    }
  );
}

test("見方のタブは矢印キーで切り替え、期間と業務 Agent は 2 つの見方で共通にする", async ({ page, mockApi }) => {
  seedSignals(mockApi);
  await page.goto("/feedback");
  const signalsTab = page.getByRole("tab", { name: "確かめるべき実行" });
  await expect(signalsTab).toHaveAttribute("aria-selected", "true");
  await withListFilters(page.getByTestId("signals-filters"), async (filters) => {
    await filters.locator("#feedback-period").click();
    await page.getByRole("option", { name: "直近 90 日" }).click();
  });
  expect(mockApi.lastRequest("GET", "/api/feedback/signals")?.searchParams.get("days")).toBe("90");

  await signalsTab.focus();
  await page.keyboard.press("ArrowRight");
  const ratingsTab = page.getByRole("tab", { name: "利用者の評価" });
  await expect(ratingsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("signals-filters")).toHaveCount(0);
  await withListFilters(page.getByTestId("feedback-filters"), async (filters) => {
    await expect(filters.locator("#feedback-period")).toContainText("直近 90 日");
    await expect(filters.locator("#feedback-rating")).toBeVisible();
  });
  expect(mockApi.lastRequest("GET", "/api/feedback")?.searchParams.get("days")).toBe("90");
  // 狭い画面ではシートを開いてフォーカスが移るので、タブに戻してから Home を押す。
  await ratingsTab.focus();
  await page.keyboard.press("Home");
  await expect(signalsTab).toHaveAttribute("aria-selected", "true");
});

function seedFailedRun(mockApi: MockApi) {
  mockApi.state.runs.push({
    id: "run-signal-1822",
    goal: "在庫の引当を更新して",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [
      { id: "step-1", run_id: "run-signal-1822", kind: "tool", status: "failed", tool_call: { name: "nl2sql__nl2sql_query", arguments: {} } },
    ],
    events: [],
    approvals: [
      {
        id: "approval-1",
        run_id: "run-signal-1822",
        step_id: "step-2",
        tool_call: { name: "inventory__update", arguments: {} },
        status: "rejected",
        reason: "書き込み",
        created_at: MOCK_NOW,
      },
    ],
    artifacts: [{ id: "answer-1", name: "回答", kind: "answer", content: { text: "引当は更新していません。" }, created_at: MOCK_NOW }],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "u-2",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

test("評価の付いていない実行から、実行の詳細と評価ケースへの追加に進める", async ({ page, mockApi }) => {
  seedFailedRun(mockApi);
  await page.goto("/feedback");
  const row = page.getByRole("table", { name: "確かめるべき実行の一覧" }).getByRole("row", { name: /在庫の引当を更新して/ });
  await expect(row).toContainText("承認の却下");
  await expect(row).toContainText("ツールの失敗");
  await row.getByRole("button", { name: /在庫の引当を更新して/ }).click();
  const detail = page.getByRole("region", { name: "確かめるべき実行の詳細" });
  await expect(detail.getByTestId("signals-detail-signals")).toContainText("却下した承認: 1 件");

  // 役に立たなかった評価が無くても評価ケースにできる。
  await detail.getByTestId("signals-add-case-toggle").click();
  await expect(detail.getByTestId("signals-add-case-form").getByLabel("質問")).toHaveValue("在庫の引当を更新して");

  await detail.getByTestId("signals-open-run").click();
  await expect(page).toHaveURL(/\/runs\?id=run-signal-1822/);
});

test("品質評価・実行履歴の権限が無い利用者には、評価ケースへの追加と実行の詳細へのリンクを出さない", async ({ page, mockApi }) => {
  seedSignals(mockApi);
  mockApi.setCurrentUser(dbUser({ permissions: ["menu.feedback"] }));
  await page.goto("/feedback");
  await page
    .getByRole("table", { name: "確かめるべき実行の一覧" })
    .getByRole("button", { name: /run-tool の質問/ })
    .click();
  const detail = page.getByRole("region", { name: "確かめるべき実行の詳細" });
  await expect(detail.getByTestId("signals-detail-signals")).toContainText("ツールの失敗");
  await expect(detail.getByTestId("signals-add-case-toggle")).toHaveCount(0);
  await expect(detail.getByTestId("signals-open-run")).toHaveCount(0);
});

// ---- #1834: 資料の根拠も業務データの道具も使わずに答えた回答 -------------------------------------------

function seedUngroundedRun(mockApi: MockApi, id: string, goal: string, steps: Record<string, unknown>[], outcome?: string) {
  mockApi.state.runs.push({
    id,
    goal,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps,
    events: [],
    approvals: [],
    artifacts: [
      {
        id: `${id}-validation`,
        name: "資料との照合",
        kind: "answer_validation",
        content: { status: "unvalidated", reason: "no_rag_evidence" },
        created_at: MOCK_NOW,
      },
      {
        id: `${id}-answer`,
        name: "回答",
        kind: "answer",
        content: { text: "回答です。", ...(outcome ? { outcome: { value: outcome } } : {}) },
        created_at: MOCK_NOW,
      },
    ],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "u-2",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

for (const viewport of VIEWPORTS) {
  test(`根拠も業務データの道具も使わずに答えた回答だけを出し、業務データの道具で答えた回答は出さない (${viewport.name})`, async ({ page, mockApi }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedUngroundedRun(mockApi, "run-no-tools-1834", "就業規則の休日は？", []);
    seedUngroundedRun(mockApi, "run-data-1834", "先月の売上は？", [
      { id: "step-1", run_id: "run-data-1834", kind: "tool", status: "completed", tool_call: { name: "nl2sql__nl2sql_query", arguments: {} } },
    ]);
    // ツールを呼ばずに答えた回答を拒答に置き換えた Run（#1835）は「答えられなかった」だけに数える。
    seedUngroundedRun(mockApi, "run-refused-1834", "来期の採用計画は？", [], "insufficient_evidence");
    await page.goto("/feedback");

    await expect(page.getByTestId("signals-kind-ungrounded_no_tools")).toContainText("1");
    const refused = page.getByRole("table", { name: "確かめるべき実行の一覧" }).getByRole("row", { name: /来期の採用計画は？/ });
    await expect(refused).toContainText("答えられなかった");
    await expect(refused).not.toContainText("根拠を使わずに答えた");
    const table = page.getByRole("table", { name: "確かめるべき実行の一覧" });
    const row = table.getByRole("row", { name: /就業規則の休日は？/ });
    await expect(row).toContainText("根拠を使わずに答えた");
    await expect(table.getByRole("row", { name: /先月の売上は？/ })).toHaveCount(0);

    // 種類で絞り込め、詳細に説明を出す。
    await withListFilters(page.getByTestId("signals-filters"), async (filters) => {
      await filters.locator("#feedback-signal").click();
      await page.getByRole("option", { name: "根拠を使わずに答えた" }).click();
    });
    expect(mockApi.lastRequest("GET", "/api/feedback/signals")?.searchParams.get("kind")).toBe("ungrounded_no_tools");
    await row.getByRole("button", { name: /就業規則の休日は？/ }).click();
    const detail = page.getByRole("region", { name: "確かめるべき実行の詳細" });
    await expect(detail.getByTestId("signals-detail-signals")).toContainText(
      "資料の検索も業務データの道具（データ問い合わせなど）も使わずに答えました。"
    );
    await expect(detail.getByRole("heading", { name: "確かめるべき実行の詳細" })).toBeInViewport();
    await expectNoHorizontalOverflow(page);
  });
}


test("シグナルの詳細 URL を開くと保存した評価のタブから切り替わる", { tag: "@responsive" }, async ({ page, mockApi }) => {
  seedSignals(mockApi);
  await page.goto("/feedback");
  await page.getByRole("tab", { name: "利用者の評価", exact: true }).click();
  await page.goto("/feedback?signal=run-tool");
  await expect(page.getByRole("tab", { name: "確かめるべき実行", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("signals-detail")).toBeVisible();
  if (await page.getByTestId("feedback-detail-back").isVisible()) await page.getByTestId("feedback-detail-back").click();
  await expect(page.getByRole("tab", { name: "確かめるべき実行", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "利用者の評価", exact: true }).click();
  await expect(page).not.toHaveURL(/signal=/);
  await expect(page.getByRole("tab", { name: "利用者の評価", exact: true })).toHaveAttribute("aria-selected", "true");
});

test("スクロール後も最初のクリックで評価のタブへ切り替わる", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  seedSignals(mockApi, Array.from({ length: 25 }, (_, index) => signalItem(`run-${index}`, ["tool_failed"])));
  await page.goto("/feedback");
  await expect(page.getByTestId("signals-summary")).toBeVisible();
  const ratings = page.getByRole("tab", { name: "利用者の評価", exact: true });
  const signals = page.getByRole("tab", { name: "確かめるべき実行", exact: true });
  const main = page.getByRole("main");
  await main.evaluate((element) => { element.scrollTop = 500; });
  // desktop は貼り付いたヘッダー、375px はタブを再び見せた位置から操作する。
  if (testInfo.project.name !== "desktop") await ratings.scrollIntoViewIfNeeded();
  if (testInfo.project.name === "desktop") {
    expect(await main.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  }
  const box = await ratings.boundingBox();
  if (!box) throw new Error("評価のタブがありません");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(ratings).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("signals-summary")).not.toBeVisible();
  await signals.click();
  await expect(signals).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("signals-summary")).toBeVisible();
});

for (const signal of ["", "run-tool"]) {
  test(`シグナル URL から最初のクリックで評価へ切り替わる (${signal || "選択なし"})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
    seedSignals(mockApi);
    await page.goto(`/feedback?signal=${signal}`);
    await expect(page.getByTestId(signal ? "signals-detail" : "signals-summary")).toBeVisible();
    const ratings = page.getByRole("tab", { name: "利用者の評価", exact: true });
    const signals = page.getByRole("tab", { name: "確かめるべき実行", exact: true });
    await ratings.click();
    await expect(ratings).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tabpanel", { name: "利用者の評価", exact: true })).toBeVisible();
    await expect(page).not.toHaveURL(/signal=/);
    await page.reload();
    await expect(ratings).toHaveAttribute("aria-selected", "true");
    await ratings.focus();
    await page.keyboard.press("ArrowLeft");
    await expect(signals).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("End");
    await expect(ratings).toHaveAttribute("aria-selected", "true");
  });
}
