/**
 * 一覧の読み込み中・縦スクロール・ページングを NL2SQL の基準にそろえたことの確認（#265）。
 *
 * - 読み込み中: 「〜を読み込んでいます」と経過時間（TimedLoadingState）＋ 内容の形の Skeleton。
 * - 読み込み中に「未設定」などの誤った警告を出さない。
 * - 一覧: 表頭の固定と、md 未満 5 行・md 以上 8 行で表の中の縦スクロール。
 * - ページング: 共通の Pagination（既定 25 件/ページ。#1952）。ページ番号は作業状態に残り、5 秒ごとの再取得でも戻らない。
 */
import type { Page, Route } from "@playwright/test";

import { expect, MOCK_NOW, test, type MockApi } from "./fixtures/mock-api";
import { expectSpinnerStable } from "./fixtures/spinner-stability";
import { jumpToPage, measureHorizontalOverflow } from "../../../platform/packages/ui/e2e/layout-checks";

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800, visibleRows: 8 },
  { name: "mobile", width: 375, height: 812, visibleRows: 5 },
] as const;

/** 応答を `release()` まで止める route。止めている間の表示を確かめる。 */
async function holdResponses(page: Page, pattern: string) {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(pattern, async (route: Route) => {
    await gate;
    await route.fallback();
  });
  return () => release();
}

function seedAgents(mockApi: MockApi, count: number) {
  const base = mockApi.state.agents[0];
  for (let index = 1; index <= count; index += 1) {
    const id = `agent-${String(index).padStart(2, "0")}`;
    mockApi.state.agents.push({ ...base, id, name: `検証 Agent ${String(index).padStart(2, "0")}`, source: "runtime" });
  }
}

function seedAgentVersions(mockApi: MockApi, count: number) {
  const versions = Array.from({ length: count }, (_, index) => ({
    version: index + 1,
    name: "版の多い Agent",
    description: "",
    instructions: `v${index + 1} の指示`,
    skill_ids: [],
    model_id: "",
    data_scopes: {},
    note: "",
    published_at: MOCK_NOW,
    published_by: "local",
    skills: [],
  }));
  mockApi.state.agents.push({
    ...mockApi.state.agents[0],
    id: "agent-versions",
    name: "版の多い Agent",
    source: "runtime",
    versioned: true,
    versions,
    published_version: count,
    unpublished_changes: false,
  });
}

function seedRuns(mockApi: MockApi, count: number) {
  for (let index = 1; index <= count; index += 1) {
    const id = `run-${String(index).padStart(2, "0")}`;
    mockApi.state.runs.push({
      id,
      goal: `検証の実行 ${String(index).padStart(2, "0")}`,
      agent_id: "default",
      runtime_id: "legacy-native",
      status: "completed",
      steps: [],
      events: [],
      approvals: [],
      artifacts: [],
      pending_tool_calls: [],
      metadata: {},
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    });
  }
}

function seedAuditRecords(mockApi: MockApi, count: number) {
  for (let index = 1; index <= count; index += 1) {
    mockApi.state.auditRecords.push({
      run_id: `run-${index}`,
      run_goal: `監査の対象 ${String(index).padStart(2, "0")}`,
      run_status: "completed",
      run_created_at: MOCK_NOW,
      step_id: `step-${index}`,
      tool_name: "echo",
      status: "completed",
      approval_status: null,
      policy_decision: "allow",
      permission_level: "read",
      guardrail_warnings: [],
      duration_ms: 10,
      trace_id: `trace-${index}`,
      artifact_ids: [],
      error_code: null,
    });
  }
}

/** 表の中の縦スクロール領域が、表示行数ぶんの高さで止まり、中でスクロールできること。 */
async function expectScrollsInsideTable(page: Page, regionName: string, visibleRows: number) {
  const region = page.getByRole("region", { name: regionName });
  await expect(region).toBeVisible();
  const metrics = await region.evaluate((element, count) => {
    const header = element.querySelector("thead");
    const rows = Array.from(element.querySelectorAll("tbody tr"));
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      headerHeight: header?.getBoundingClientRect().height ?? 0,
      rowHeight: rows[0]?.getBoundingClientRect().height ?? 0,
      visibleHeight: rows.slice(0, count).reduce((height, row) => height + row.getBoundingClientRect().height, 0),
      rowCount: rows.length,
      stickyHeader: header ? getComputedStyle(header).position : "",
    };
  }, visibleRows);
  expect(metrics.rowCount).toBe(25);
  expect(metrics.stickyHeader).toBe("sticky");
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  // 名前順で先頭の行が変わる。折り返しで行高が違うため、表示する N 行の実際の高さを合計する。
  const expected = metrics.headerHeight + metrics.visibleHeight;
  expect(Math.abs(metrics.clientHeight - expected)).toBeLessThan(metrics.rowHeight);
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const viewport of VIEWPORTS) {
  test.describe(`一覧の読み込み中・縦スクロール・ページング (${viewport.name})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("業務 Agent: 読み込み中は形の Skeleton と経過時間を出す", async ({ page, mockApi }) => {
      seedAgents(mockApi, 52);
      const releaseAgents = await holdResponses(page, "**/api/agents");
      await page.goto("/agents");

      const loading = page.getByTestId("query-loading");
      await expect(loading).toBeVisible();
      await expect(loading).toContainText("業務 Agent を読み込んでいます");
      await expect(loading).toContainText("経過時間");
      await expect(loading.locator('[data-skeleton="table"]')).toBeVisible();
      // 見出しは隠さない（messaging.md §3.7）。
      await expect(page.getByRole("heading", { name: "業務 Agent 一覧" })).toBeVisible();
      // 読み込み中のスピナーは回転しても見た目の重心も箱・行も動かない（#1180）。
      await expectSpinnerStable(loading.locator(".pr-spinner"));

      releaseAgents();
      await expect(page.getByTestId("agent-row-agent-01")).toBeVisible();

      await expectScrollsInsideTable(page, "業務 Agent 一覧。スクロールできます。", viewport.visibleRows);
      const pager = page.getByTestId("agent-list-pagination");
      await expect(pager).toContainText("1–25 / 55 件");
      await expect(pager).toContainText("1 / 3 ページ");
      await expectNoHorizontalOverflow(page);
    });

    test("業務 Agent: ページはエディタとの往復と再読込で保つ", async ({ page, mockApi }) => {
      seedAgents(mockApi, 52);
      await page.goto("/agents");
      const pager = page.getByTestId("agent-list-pagination");
      await pager.getByRole("button", { name: "次へ" }).click();
      await expect(pager).toContainText("26–50 / 55 件");

      await page.getByRole("link", { name: /^検証 Agent 30 agent-30/ }).click();
      await expect(page).toHaveURL(/\/agents\?id=agent-30$/);
      // 375px ではヘッダーの操作がメニューに入るため、ブラウザの戻るで一覧へ戻る。
      await page.goBack();
      await expect(page).toHaveURL(/\/agents$/);
      await expect(pager).toContainText("26–50 / 55 件");

      await page.reload();
      await expect(pager).toContainText("26–50 / 55 件");
      await expect(page.getByRole("link", { name: /^検証 Agent 30 agent-30/ })).toBeVisible();
    });

    test("業務 Agent: ページ番号を入れて移り、範囲の外は直前のページに戻す (#1891)", async ({ page, mockApi }) => {
      seedAgents(mockApi, 52);
      await page.goto("/agents");
      const pager = page.getByTestId("agent-list-pagination");
      await expect(pager).toContainText("1–25 / 55 件");
      await jumpToPage(pager, 3);
      await expect(pager).toContainText("51–55 / 55 件");
      await expect(pager).toContainText("3 / 3 ページ");
      await jumpToPage(pager, 0);
      await expect(pager.getByRole("textbox", { name: "ページ番号" })).toHaveValue("3");
      await expect(pager).toContainText("1〜3 のページ番号を入力してください");
      // 375px では最初へ・最後へを畳む（前へ・入力・次へで足りる）。
      const last = pager.getByRole("button", { name: "最後のページ" });
      if (viewport.name === "mobile") await expect(last).toBeHidden();
      else await expect(last).toBeDisabled();
      expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    });

    test("業務 Agent の版の履歴: 表の中を縦スクロールにし、直下でページを送る (#2135)", async ({ page, mockApi }) => {
      seedAgentVersions(mockApi, 30);
      await page.goto("/agents?id=agent-versions");
      await expect(page.getByRole("heading", { name: "版の多い Agent", level: 1 })).toBeVisible();
      await expectScrollsInsideTable(page, "版の履歴。スクロールできます。", viewport.visibleRows);
      const pager = page.getByTestId("agent-version-pagination");
      await expect(pager).toContainText("1–25 / 30 件");
      await pager.getByRole("button", { name: "次へ" }).click();
      await expect(pager).toContainText("26–30 / 30 件");
      expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    });

    test("実行履歴: 再取得で行が増えてもページが戻らない", async ({ page, mockApi }) => {
      seedRuns(mockApi, 53);
      await page.goto("/runs");
      const pager = page.getByTestId("run-history-pagination");
      await expect(pager).toContainText("1–25 / 53 件");
      // 実行履歴は共通の RunHistoryTable（スクロールの領域の名前は表と同じ。#1955）。
      await expectScrollsInsideTable(page, "実行履歴", viewport.visibleRows);
      await pager.getByRole("button", { name: "次へ" }).click();
      await expect(pager).toContainText("26–50 / 53 件");

      // 5 秒ごとの再取得と同じく、一覧を取り直す（行も増える）。
      seedRuns(mockApi, 1);
      const refetched = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/runs");
      await page.getByRole("button", { name: "最新情報を取得" }).click();
      await refetched;
      await expect(pager).toContainText("26–50 / 54 件");
      await expect(pager).toContainText("2 / 3 ページ");
    });

    test("監査: API の offset でページを送り、読み込み中は表の形の Skeleton を出す", async ({ page, mockApi }) => {
      seedAuditRecords(mockApi, 55);
      const release = await holdResponses(page, "**/api/audit/tool-calls?**");
      await page.goto("/audit");
      const loading = page.getByTestId("query-loading");
      await expect(loading).toContainText("監査記録を読み込んでいます");
      await expect(loading.locator('[data-skeleton="table"]')).toBeVisible();
      release();

      const pager = page.getByTestId("audit-pagination");
      await expect(pager).toContainText("1–25 / 55 件");
      await expect.poll(() => mockApi.lastRequest("GET", "/api/audit/tool-calls")?.searchParams.get("limit")).toBe("25");
      await expectScrollsInsideTable(page, "監査レコード。スクロールできます。", viewport.visibleRows);

      await pager.getByRole("button", { name: "次へ" }).click();
      await expect(pager).toContainText("26–50 / 55 件");
      await expect
        .poll(() => mockApi.lastRequest("GET", "/api/audit/tool-calls")?.searchParams.get("offset"))
        .toBe("25");
      await expect(page.getByText("監査の対象 26")).toBeVisible();

      // ページも作業状態に残る。
      await page.reload();
      await expect(pager).toContainText("26–50 / 55 件");
      await expectNoHorizontalOverflow(page);
    });

    test("監査: 「フィルター適用」と「最新情報を取得」は押した側だけが回り、ページの切り替えではどちらも回さない（#819）", async ({ page, mockApi }) => {
      seedAuditRecords(mockApi, 55);
      await page.goto("/audit");
      const pager = page.getByTestId("audit-pagination");
      await expect(pager).toContainText("1–25 / 55 件");
      const apply = page.getByRole("button", { name: "フィルター適用", exact: true });
      const refresh = page.getByRole("button", { name: "最新情報を取得" });

      // フィルター適用: フィルター適用だけが回り、最新情報を取得は押せないだけ。
      let release = await holdResponses(page, "**/api/audit/tool-calls?**");
      await apply.click();
      await expect(apply).toHaveAttribute("aria-busy", "true");
      await expect(refresh).toBeDisabled();
      await expect(refresh).not.toHaveAttribute("aria-busy", /.*/);
      release();
      await expect(apply).not.toHaveAttribute("aria-busy", /.*/);

      // ページの切り替え（前のページを出したままの取り直し）では、どちらも回さない。
      release = await holdResponses(page, "**/api/audit/tool-calls?**");
      const nextPageRequested = page.waitForRequest(
        (request) =>
          new URL(request.url()).pathname === "/api/audit/tool-calls" &&
          new URL(request.url()).searchParams.get("offset") === "25"
      );
      await pager.getByRole("button", { name: "次へ" }).click();
      await nextPageRequested;
      await expect(apply).not.toHaveAttribute("aria-busy", /.*/);
      await expect(refresh).not.toHaveAttribute("aria-busy", /.*/);
      release();
      await expect(pager).toContainText("26–50 / 55 件");

      // 最新情報を取得: 最新情報を取得だけが回り、フィルター適用は押せないだけ。
      release = await holdResponses(page, "**/api/audit/tool-calls?**");
      await refresh.click();
      await expect(refresh).toHaveAttribute("aria-busy", "true");
      await expect(apply).toBeDisabled();
      await expect(apply).not.toHaveAttribute("aria-busy", /.*/);
      release();
      await expect(refresh).not.toHaveAttribute("aria-busy", /.*/);
      await expectNoHorizontalOverflow(page);
    });
  });
}
