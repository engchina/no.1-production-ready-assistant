import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectListColumnFitsViewport } from "./_helpers/list-detail-fit";
import { chooseSelectFieldOption } from "./_helpers/select-field";
import { closeListToolbarFilters, listToolbarUsesSheet, openListToolbarFilters } from "./_helpers/db-object-toolbar";
import {
  findClippedPlaceholders,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

/**
 * フィードバック管理（#968）: 履歴検索のフォーカス・数値欄のキーボード入力。
 */

function envelope(route: Route, data: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ data }) });
}

function feedbackItem(id: string, question: string) {
  return {
    id,
    question,
    engine: "enterprise_ai_direct",
    profile_id: "sales",
    profile_name: "売上",
    profile_category: "販売",
    generated_sql: "SELECT TOTAL_AMOUNT FROM SALES",
    executable_sql: "SELECT TOTAL_AMOUNT FROM SALES",
    feedback_rating: "good",
    feedback_comment: "",
    admin_feedback_rating: null,
    admin_feedback_content: "",
    created_at: "2026-07-19T00:00:00Z",
    feedback_updated_at: "2026-07-19T00:00:00Z",
    elapsed_ms: 120,
    training_status: "",
    training_example_id: "",
  };
}

interface MockState {
  feedbackQueries: string[];
  feedbackRequests: URL[];
  feedbackConfigPayload: unknown;
  adminReviewPayload: Record<string, unknown> | null;
}

interface MockOptions {
  feedbackDelayMs?: number;
  /** 指定するとフィードバック履歴をこの一覧から limit / offset で返す（#1764 / #1891）。 */
  items?: () => Array<Record<string, unknown> & { question: string }>;
  /** フィードバック履歴の応答を待たせる（読み込み中の表示を確かめる）。 */
  feedbackGate?: () => Promise<void> | undefined;
}

async function mockFeedbackApi(page: Page, options: MockOptions = {}) {
  const state: MockState = {
    feedbackQueries: [],
    feedbackRequests: [],
    feedbackConfigPayload: null,
    adminReviewPayload: null,
  };
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    envelope(route, {
      items: [{ id: "sales", name: "売上", category: "販売", description: "", archived: false }],
      total: 1,
    })
  );
  await page.route("**/api/nl2sql/feedback-config", (route) => {
    if (route.request().method() === "PATCH") {
      state.feedbackConfigPayload = route.request().postDataJSON();
      return envelope(route, state.feedbackConfigPayload);
    }
    return envelope(route, { similarity_threshold: 0.8, match_limit: 3 });
  });
  await page.route(/\/api\/nl2sql\/feedback\?.*$/, async (route) => {
    const url = new URL(route.request().url());
    const q = url.searchParams.get("q") ?? "";
    state.feedbackQueries.push(q);
    state.feedbackRequests.push(url);
    if (options.feedbackDelayMs) await new Promise((resolve) => setTimeout(resolve, options.feedbackDelayMs));
    await options.feedbackGate?.();
    if (options.items) {
      const everything = options.items();
      const all = everything.filter((item) => !q || item.question.includes(q));
      const limit = Number(url.searchParams.get("limit") ?? "25");
      const offset = Number(url.searchParams.get("offset") ?? "0");
      const items = all.slice(offset, offset + limit);
      return envelope(route, {
        items,
        total: all.length,
        limit,
        offset,
        has_next: offset + items.length < all.length,
        unfiltered_total: everything.length,
      });
    }
    return envelope(route, {
      items: [feedbackItem("hist-sales-1", q ? `${q} の推移を確認したい` : "売上の推移を確認したい")],
      total: 1,
      limit: 25,
      offset: 0,
      has_next: false,
      unfiltered_total: 1,
    });
  });
  await page.route("**/api/nl2sql/feedback/admin-review", (route) => {
    state.adminReviewPayload = route.request().postDataJSON() as Record<string, unknown>;
    return envelope(route, {
      history_id: "hist-sales-1",
      rating: state.adminReviewPayload.rating,
      saved: true,
      feedback_content: state.adminReviewPayload.feedback_content,
      similar_history_publish: { history_id: "hist-sales-1", status: "published", runtime: "oracle", warnings: [] },
    });
  });
  return state;
}

/** フィードバック履歴の一覧の列（共有 ListDetailLayout。#2283）。検索・件数・行・ページ送りを持つ。 */
function historyList(page: Page) {
  return page.getByTestId("list-detail-feedback-management-list");
}

test("履歴検索は一覧の読み込み中もフォーカスを保ち、続けて入力できる (#968)", async ({ page }) => {
  // 一覧（検索欄）と管理者レビューの欄を同時に見る 2 ペインの幅で確かめる（xl 未満は 1 ペイン。#2283）。
  await page.setViewportSize({ width: 1280, height: 900 });
  const state = await mockFeedbackApi(page, { feedbackDelayMs: 600 });
  await page.goto("/feedback-management?tab=appFeedback");
  const rows = page.getByTestId("feedback-history-row");
  await expect(rows).toHaveCount(1);
  // 先頭の行を自動で選ばない（#2283）。行を選ぶと管理者レビューの欄が出る。
  await expect(page.getByRole("button", { name: "フィードバック保存" })).toHaveCount(0);
  await rows.first().click();
  await expect(page).toHaveURL(/feedback=hist-sales-1/);

  const search = page.getByTestId("feedback-app-filters").getByRole("searchbox", { name: "履歴検索" });
  await search.click();
  await page.keyboard.type("売上");
  // debounce の後に一覧の読み込みが始まる。読み込み中も検索欄は操作でき、フォーカスが残る。
  await expect(page.getByTestId("app-feedback-load-processing")).toBeVisible();
  await expect(search).toBeEnabled();
  await expect(search).toBeFocused();
  // 読み込み中は一覧の行と編集欄を操作させない（読み込みの後に対象の履歴が変わることがあるため）。
  await expect(rows.first()).toBeDisabled();
  await expect(page.getByRole("button", { name: "フィードバック保存" })).toBeDisabled();

  await page.keyboard.type("額");
  await expect(search).toHaveValue("売上額");
  await expect(rows).toContainText("売上額 の推移を確認したい");
  await expect(search).toBeFocused();
  expect(state.feedbackQueries.at(-1)).toBe("売上額");
  await expect(page.getByRole("button", { name: "フィードバック保存" })).toBeEnabled();
});

test("類似検索の数値欄にキーボードで値を入力できる (#968)", async ({ page }) => {
  const state = await mockFeedbackApi(page);
  await page.goto("/feedback-management?tab=similarityIndex");

  const similarityMatchLimit = page.locator("#feedback-similarity-match-limit");
  await similarityMatchLimit.click();
  await similarityMatchLimit.press("ControlOrMeta+a");
  await page.keyboard.press("Backspace");
  await expect(similarityMatchLimit).toHaveValue("");
  await page.keyboard.type("12");
  await expect(similarityMatchLimit).toHaveValue("12");
  await page.getByRole("button", { name: "設定保存" }).click();
  await expect.poll(() => state.feedbackConfigPayload).toEqual({ similarity_threshold: 0.8, match_limit: 12 });
});

const LONG_QUESTIONS = [
  "selected_profile の売上を月別・地域別に集計して前年同月比も出してください",
  "sales テーブルの今月の上位 10 顧客を金額の降順で確認したい",
  "対象テーブル：部署情報を管理するテーブル 抽出項目：部署名と社員数",
];

function manyFeedbackItems(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    ...feedbackItem(`hist-${String(index + 1).padStart(2, "0")}`, `${index + 1}: ${LONG_QUESTIONS[index % LONG_QUESTIONS.length]}`),
    engine: "enterprise_ai_direct",
    feedback_rating: index % 3 === 0 ? "bad" : "good",
    feedback_comment: index % 3 === 0 ? "集計条件が違います" : "",
    admin_feedback_rating: index % 4 === 0 ? "good" : null,
  }));
}

async function box(locator: Locator) {
  const value = await locator.boundingBox();
  expect(value).not.toBeNull();
  return value!;
}

test("フィードバック履歴の行は質問（2 行まで）と補足 1 行の狭い一覧で、質問を切り詰めずに読める (#1764・#2283)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockFeedbackApi(page, { items: () => manyFeedbackItems(3) });
  await page.goto("/feedback-management?tab=appFeedback");
  const rows = page.getByTestId("feedback-history-row");
  await expect(rows).toHaveCount(3);

  for (const index of [0, 1, 2]) {
    const row = rows.nth(index);
    const question = row.getByTestId("feedback-history-question");
    // 質問は 1 行目に行の全幅で出し（2 行まで折り返す）、評価のバッジは質問の下の補足の行に並べる。
    await expect(question.locator("xpath=..")).toHaveCSS("-webkit-line-clamp", "2");
    const questionBox = await box(question.locator("xpath=.."));
    const rowBox = await box(row);
    expect(questionBox.width).toBeGreaterThanOrEqual(rowBox.width - 1);
    const badge = row.getByText(/^利用者評価: /);
    expect((await box(badge)).y).toBeGreaterThanOrEqual(questionBox.y + questionBox.height - 1);
    // 質問は 1〜2 文字に切り詰めず、2 行まで（2 行に収まるなら全文）見える。
    const lines = await question.locator("xpath=..").evaluate((node) => {
      const lineHeight = Number.parseFloat(getComputedStyle(node).lineHeight);
      return { visible: node.clientHeight / lineHeight, full: node.scrollHeight / lineHeight };
    });
    expect(lines.visible).toBeGreaterThanOrEqual(Math.min(lines.full, 2) - 0.1);
  }
  // コメント・実行エンジンは一覧の行に出さず、選んだ詳細で読む。
  await expect(rows.first()).not.toContainText("集計条件が違います");
  // 一覧の行は一覧の列の中で縦にスクロールする（列は sticky で画面の高さまで）。
  await expect(historyList(page).locator("[data-list-detail-body]")).toHaveCSS("overflow-y", "auto");
  await rows.first().click();
  await expect(page.getByTestId("app-feedback-selected-engine")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "利用者コメント（feedback_content）" })).toHaveValue("集計条件が違います");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("feedback-history-rows.png"), fullPage: true });
});

test("フィードバック履歴は 26 件以上で 25 件ずつのページを一覧の直下で移り、ページを作業状態に残す (#1764)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await mockFeedbackApi(page, { items: () => manyFeedbackItems(27) });
  await page.goto("/feedback-management?tab=appFeedback");
  const pane = historyList(page);
  const rows = pane.getByTestId("feedback-history-row");
  const pagination = pane.getByTestId("app-feedback-pagination");
  await expect(rows).toHaveCount(25);
  expect(state.feedbackRequests[0]?.searchParams.get("limit")).toBe("25");
  await expect(pagination).toContainText("1–25 / 27 件");
  await expect(pagination).toContainText("1 / 2 ページ");
  // ページ送りは一覧の列の下（行の直下）。
  const surface = await box(pane.locator("[data-list-detail-body]"));
  const pager = await box(pagination);
  expect(pager.y).toBeGreaterThanOrEqual(surface.y + surface.height - 1);
  expect(pager.y - (surface.y + surface.height)).toBeLessThanOrEqual(24);
  // 2 ペインでは一覧の列が画面の高さに収まり、ページ送りは本文をスクロールせずに見える（#2330）。
  if (testInfo.project.name === "desktop") await expectListColumnFitsViewport(page, "feedback-management");

  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(rows).toHaveCount(2);
  await expect(pagination).toContainText("26–27 / 27 件");
  // 先頭の行を自動で選ばない（#2283）。
  await expect(rows.first()).not.toHaveAttribute("aria-current", /.*/);
  await rows.first().click();
  await expect(page.getByTestId("app-feedback-selected-question")).toContainText("26:");
  if (await page.getByTestId("app-feedback-detail-back").isVisible()) await page.getByTestId("app-feedback-detail-back").click();

  await page.reload();
  await expect(rows).toHaveCount(2);
  await expect(pagination).toContainText("2 / 2 ページ");
  expect(state.feedbackRequests.at(-1)?.searchParams.get("offset")).toBe("25");
  // 件数は一覧のツールバーの右の 1 か所（ListCount。#1891）。
  await expect(pane.getByTestId("app-feedback-list-count")).toHaveText("全 27 件");

  // ページ番号を入れて移る（#1891）。
  await jumpToPage(pagination, 1);
  await expect(rows).toHaveCount(25);
  await expect(pagination).toContainText("1–25 / 27 件");
  expect(state.feedbackRequests.at(-1)?.searchParams.get("offset")).toBe("0");

  // 絞り込みを変えたら先頭のページへ戻り、件数は「N 件（全 M 件）」。
  await pane.getByRole("searchbox", { name: "履歴検索" }).fill("sales");
  await expect(rows).toHaveCount(9);
  await expect(pagination).toHaveCount(0);
  await expect(pane.getByTestId("app-feedback-list-count")).toHaveText("9 件（全 27 件）");
  expect(state.feedbackRequests.at(-1)?.searchParams.get("offset")).toBe("0");
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  expect(await findClippedPlaceholders(page)).toEqual([]);
});

test("フィードバック履歴は表示件数を変えると 1 ページ目に戻り、件数を作業状態に残す (#1891)", { tag: "@responsive" }, async ({ page }) => {
  const state = await mockFeedbackApi(page, { items: () => manyFeedbackItems(60) });
  await page.goto("/feedback-management?tab=appFeedback");
  const pane = historyList(page);
  const rows = pane.getByTestId("feedback-history-row");
  const pagination = pane.getByTestId("app-feedback-pagination");
  await expect(rows).toHaveCount(25);
  await jumpToPage(pagination, 3);
  await expect(pagination).toContainText("51–60 / 60 件");
  await chooseSelectFieldOption(pagination.getByRole("combobox", { name: "表示件数" }), "50");
  await expect(rows).toHaveCount(50);
  await expect(pagination).toContainText("1–50 / 60 件");
  expect(state.feedbackRequests.at(-1)?.searchParams.get("limit")).toBe("50");
  expect(state.feedbackRequests.at(-1)?.searchParams.get("offset")).toBe("0");
  await page.reload();
  await expect(rows).toHaveCount(50);
  await expect(pagination).toContainText("1 / 2 ページ");
});

test("フィードバック履歴はページ送りの取得の間、前のページを出したまま処理中を示す (#1764)", { tag: "@responsive" }, async ({ page }) => {
  let release: (() => void) | undefined;
  let gate: Promise<void> | undefined;
  await mockFeedbackApi(page, { items: () => manyFeedbackItems(26), feedbackGate: () => gate });
  await page.goto("/feedback-management?tab=appFeedback");
  const rows = page.getByTestId("feedback-history-row");
  await expect(rows).toHaveCount(25);

  gate = new Promise<void>((resolve) => { release = resolve; });
  const pagination = page.getByTestId("app-feedback-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  const processing = page.getByTestId("app-feedback-load-processing");
  await expect(processing).toBeVisible();
  await expect(processing).toContainText("フィードバック履歴を読み込んでいます");
  await expect(rows).toHaveCount(25);
  await expect(rows.first()).toBeDisabled();
  release?.();
  await expect(rows).toHaveCount(1);
  await expect(processing).toHaveCount(0);
});

test("フィードバック履歴が 0 件なら空の状態を出し、ページ送りを出さない (#1764)", { tag: "@responsive" }, async ({ page }) => {
  await mockFeedbackApi(page, { items: () => [] });
  await page.goto("/feedback-management?tab=appFeedback");
  const pane = historyList(page);
  await expect(pane.getByText("フィードバックはまだありません")).toBeVisible();
  await expect(pane.getByTestId("feedback-history-row")).toHaveCount(0);
  await expect(pane.getByTestId("app-feedback-pagination")).toHaveCount(0);

  await pane.getByRole("searchbox", { name: "履歴検索" }).fill("一致しない");
  await expect(pane.getByText("一致する履歴がありません")).toBeVisible();
});

test("履歴の絞り込みは FilterSelect で 1 行にそろい、狭い画面はシート、条件をクリアで既定に戻す (#1933)", { tag: "@responsive" }, async ({ page, isMobile }) => {
  const state = await mockFeedbackApi(page);
  const widths = isMobile ? [375] : [1280, 1920];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/feedback-management?tab=appFeedback");
    const toolbar = page.getByTestId("feedback-app-filters");
    await expect(toolbar.getByRole("searchbox", { name: "履歴検索" })).toBeVisible();
    await expect(page.getByTestId("feedback-history-row")).toHaveCount(1);
    // 検索・利用者評価・業務プロファイルの 3 つ。ツールバーが 28rem 未満ならシートにまとめる
    // （一覧の列は既定 20rem なので、広い画面でもシート。#2283）。
    expect(await listToolbarUsesSheet(toolbar), `${width}px`).toBe(true);
    expect(await findToolbarMisalignment(page), `${width}px`).toEqual([]);
    expect(await findToolbarFieldWidthMismatch(page), `${width}px`).toEqual([]);
    expect(await findClippedPlaceholders(page), `${width}px`).toEqual([]);
    expect(await measureHorizontalOverflow(page), `${width}px`).toBeLessThanOrEqual(1);
  }

  const toolbar = page.getByTestId("feedback-app-filters");
  const clear = toolbar.getByRole("button", { name: "条件をクリア", exact: true });
  await expect(clear).toHaveCount(0);
  const filters = await openListToolbarFilters(toolbar);
  const rating = filters.getByRole("combobox", { name: "利用者評価", exact: true });
  await expect(rating).toContainText(/利用者評価:\s*すべて/);
  await expect(filters.getByRole("combobox", { name: "業務プロファイル", exact: true })).toContainText(/業務プロファイル:\s*すべて/);
  await chooseSelectFieldOption(rating, "bad");
  await expect.poll(() => state.feedbackRequests.at(-1)?.searchParams.get("rating")).toBe("bad");
  // 既定以外を選ぶと値を強調する（`FilterSelect` の外枠の data-filter-active）。
  await expect(rating.locator("xpath=ancestor::*[@data-filter-active][1]")).toHaveAttribute("data-filter-active", "true");
  await closeListToolbarFilters(page);
  await expect(toolbar.getByRole("button", { name: "絞り込み（1）", exact: true })).toBeVisible();

  // 絞り込みは作業状態に残す（再読込の後も同じ条件）。
  await page.reload();
  await expect.poll(() => state.feedbackRequests.at(-1)?.searchParams.get("rating")).toBe("bad");
  await clear.click();
  await expect.poll(() => state.feedbackRequests.at(-1)?.searchParams.get("rating")).toBe("all");
  await expect(clear).toHaveCount(0);
});

test("再読込の失敗の「再試行」は案内を残したまま押したボタンだけを回し、取り直せたら案内を消す (#2034)", async ({ page }) => {
  await mockFeedbackApi(page);
  let mode: "ok" | "fail" | "gate" = "ok";
  let releaseRetry: () => void = () => undefined;
  const retryGate = new Promise<void>((resolve) => {
    releaseRetry = resolve;
  });
  await page.route("**/api/nl2sql/feedback-config", async (route) => {
    if (mode === "fail") {
      mode = "gate";
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "一時的に読み込めません。" }),
      });
      return;
    }
    if (mode === "gate") await retryGate;
    await envelope(route, { similarity_threshold: 0.8, match_limit: 3 });
  });
  await page.goto("/feedback-management");
  const header = page.getByTestId("feedback-management-actions");
  const headerRefresh = header.getByRole("button", { name: "最新情報を取得" });
  await expect(historyList(page)).toBeVisible();
  mode = "fail";
  if (await headerRefresh.isVisible()) {
    await headerRefresh.click();
  } else {
    await header.getByRole("button", { name: "その他の操作" }).click();
    await page.getByRole("menuitem", { name: "最新情報を取得" }).click();
  }
  const retry = page.getByRole("button", { name: "再試行" });
  await expect(retry).toBeVisible();
  await retry.click();
  // 押しても案内は消えず、進行は案内の中のボタンの直下だけに出る（パネルの先頭に重ねない）。
  const processing = page.getByTestId("feedback-management-retry-processing");
  await expect(processing).toBeVisible();
  await expect(retry).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("feedback-management-refresh-processing")).toHaveCount(0);
  const buttonBox = await retry.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  releaseRetry();
  await expect(retry).toHaveCount(0);
  await expect(processing).toHaveCount(0);
});
