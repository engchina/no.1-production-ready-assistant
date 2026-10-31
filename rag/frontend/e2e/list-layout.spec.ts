import { openFileListFilters, closeFileListFilters } from "./_helpers";
import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../platform/packages/ui/e2e/layout-checks";

// Issue 1891（段 2）: RAG の一覧の件数・ページ送り・欄の幅を 3 製品の基準にそろえる。
// - 件数はツールバーの右の ListCount 1 か所（「全 N 件」/「N 件（全 M 件）」）。1 ページでも出す。
// - ページ送りは共通の advanced（範囲・表示件数 10 / 25 / 50 / 100・ページ番号の入力）。ページと表示件数は作業状態に残す。
// - 横あふれ・placeholder の切れ・幅の段の外の欄が無い（desktop の 1280 / 1920px と 375px、ライト / ダーク）。
// Issue 1933（段 2）: ツールバーの部品は同じ高さで縦の中心をそろえ、縦に積んだときは欄が左の群れの幅いっぱい
// （findToolbarMisalignment / findToolbarFieldWidthMismatch）。実行履歴も #1955 の共通の部品で対象にする。

const NOW = "2026-10-15T00:00:00Z";
// 既定の 25 件/ページ（#1952）で 3 ページになる件数。
const TOTAL = 55;

type Theme = "light" | "dark";

function envelope<T>(data: T) {
  return { json: { data, error_messages: [], warning_messages: [] } };
}

/**
 * offset / limit / total の一覧 API。検索語（q）か `filterParams` の条件が付いた要求は先頭の `filteredTotal` 件にする。
 * `archived` は状態の絞り込み（`status`）が無い要求（「すべて」と、件数の「全 M 件」）にだけ足す行。
 */
async function mockPagedList<T>(
  page: Page,
  pattern: string,
  path: string,
  items: T[],
  { filterParams = [] as string[], filteredTotal = 5, archived = [] as T[] } = {}
) {
  await page.route(pattern, async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET" || url.pathname !== path) {
      await route.fallback();
      return;
    }
    const q = url.searchParams.get("q") ?? "";
    const filtered = Boolean(q) || filterParams.some((name) => url.searchParams.get(name));
    const all = url.searchParams.get("status") ? items : [...items, ...archived];
    const rows = filtered ? items.slice(0, filteredTotal) : all;
    const limit = Number(url.searchParams.get("limit") ?? 25);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    await route.fulfill(
      envelope({
        items: rows.slice(offset, offset + limit),
        total: rows.length,
        limit,
        offset,
        has_next: offset + limit < rows.length,
      })
    );
  });
}

const documents = Array.from({ length: TOTAL }, (_, index) => ({
  id: `doc-${index + 1}`,
  file_name: `doc-${String(index + 1).padStart(2, "0")}.txt`,
  status: "INDEXED",
  category_name: null,
  content_type: "text/plain",
  file_size_bytes: 128,
  content_sha256: "a".repeat(64),
  duplicate_of_document_id: null,
  uploaded_at: NOW,
  indexed_at: NOW,
  knowledge_bases: [],
  source_profile: null,
}));

const knowledgeBases = Array.from({ length: TOTAL }, (_, index) => ({
  id: `kb-${index + 1}`,
  name: `ナレッジベース ${String(index + 1).padStart(2, "0")}`,
  description: "社内規程",
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 1,
  indexed_document_count: 1,
  error_document_count: 0,
  searchable_chunk_count: 4,
  created_at: NOW,
  updated_at: NOW,
  archived_at: null,
}));

const profiles = Array.from({ length: TOTAL }, (_, index) => ({
  id: `bv-${index + 1}`,
  name: `検索・回答プロファイル ${String(index + 1).padStart(2, "0")}`,
  description: "",
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: NOW,
  updated_at: NOW,
  archived_at: null,
}));

const runs = Array.from({ length: TOTAL }, (_, index) => ({
  run_id: `run-${index + 1}`,
  surface: index % 2 ? "search" : "chat",
  search_answer_profile_id: "bv-1",
  search_answer_profile_name: "受注業務",
  question: `受注の登録方法 ${index + 1}`,
  answer_generated: index % 2 === 0,
  status: "success",
  answer_outcome: null,
  verification: null,
  confidence: null,
  needs_human_review: null,
  citation_count: 2,
  elapsed_ms: 1200,
  error_stage: null,
  model_id: null,
  conversation_id: null,
  message_id: null,
  created_at: NOW,
  executed_by_me: true,
  executor_display_name: "ローカル利用者",
}));

async function mockLists(page: Page) {
  await mockPagedList(page, "**/api/documents**", "/api/documents", documents, {
    filterParams: ["status", "knowledge_base_id"],
  });
  // 既定の絞り込みは「有効」（status=ACTIVE）。状態を外すとアーカイブ済みの 3 件が加わる。
  await mockPagedList(page, "**/api/knowledge-bases**", "/api/knowledge-bases", knowledgeBases, {
    archived: knowledgeBases.slice(0, 3).map((item, index) => ({ ...item, id: `kb-archived-${index}`, status: "ARCHIVED" })),
  });
  await mockPagedList(page, "**/api/search-answer-profiles**", "/api/search-answer-profiles", profiles, {
    archived: profiles.slice(0, 3).map((item, index) => ({ ...item, id: `bv-archived-${index}`, status: "ARCHIVED" })),
  });
  await mockPagedList(page, "**/api/history/runs**", "/api/history/runs", runs, {
    filterParams: ["status", "surface"],
  });
  await page.route("**/api/feedback**", async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") ?? 25);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const total = url.searchParams.get("period_days") || url.searchParams.get("rating") ? 40 : 120;
    const items = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, index) => ({
      feedback_id: `fb-${offset + index}`,
      trace_id: `trace-${offset + index}`,
      search_answer_profile_id: "bv-1",
      search_answer_profile_name: "経理ビュー",
      target_type: "answer",
      source_surface: "chat",
      document_id: null,
      chunk_id: null,
      message_id: "m",
      rating: "not_helpful",
      reason: "incorrect",
      comment: null,
      created_at: NOW,
      conversation_id: null,
      conversation_title: null,
      model: null,
      file_name: null,
      question_preview: `質問 ${offset + index + 1}`,
      comment_preview: null,
      has_comment: false,
    }));
    const summary = {
      total,
      helpful_count: 0,
      not_helpful_count: total,
      helpful_rate: 0,
      answer_total: total,
      answer_helpful_rate: 0,
      citation_total: 0,
      citation_helpful_rate: 0,
      reason_counts: [],
    };
    await route.fulfill(
      envelope({ summary, previous_summary: null, items: { items, total, limit, offset, has_next: offset + limit < total } })
    );
  });
}

async function useTheme(page: Page, theme: Theme) {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

interface ListScreen {
  name: string;
  path: string;
  toolbar: string;
  pagination: string;
  /** 条件なしで開いたときの件数（既定の絞り込みがある一覧は「N 件（全 M 件）」）。 */
  count: string;
  /** #1933 の絞り込みの部品に移した一覧か。 */
  filtersMigrated?: boolean;
}

const SCREENS: ListScreen[] = [
  { name: "文書一覧", path: "/file-list", toolbar: "file-list-toolbar", filtersMigrated: true, pagination: "file-list-pagination", count: "全 55 件" },
  {
    name: "ナレッジベース",
    path: "/knowledge-bases",
    toolbar: "knowledge-base-list-toolbar",
    filtersMigrated: true,
    pagination: "knowledge-bases-pagination",
    // 既定は「有効」だけ（アーカイブ済みを除く）の絞り込み。
    count: "55 件（全 58 件）",
  },
  {
    name: "検索・回答プロファイル",
    path: "/search-answer-profiles",
    toolbar: "search-answer-profile-list-toolbar",
    filtersMigrated: true,
    pagination: "search-answer-profiles-pagination",
    count: "55 件（全 58 件）",
  },
  {
    name: "実行履歴",
    path: "/history",
    toolbar: "run-history-toolbar",
    // 共通の RunHistoryToolbar（#1955）。
    filtersMigrated: true,
    pagination: "run-history-pagination",
    count: "全 55 件",
  },
];

/**
 * 横あふれ・placeholder の切れ・幅の段の外の欄（sm 以上だけ）が無い。`toolbar` を渡すと、そのツールバーの
 * 部品の高さ・縦の中心のずれと、縦に積んだときの欄の幅のずれも無いことを確かめる（#1933）。
 */
async function expectListLayout(page: Page, desktop: boolean, toolbar?: string) {
  expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  expect(await findClippedPlaceholders(page)).toEqual([]);
  if (desktop) expect(await findFieldsOffWidthScale(page)).toEqual([]);
  if (toolbar) {
    const root = `[data-testid="${toolbar}"]`;
    expect(await findToolbarMisalignment(page, { root }), toolbar).toEqual([]);
    expect(await findToolbarFieldWidthMismatch(page, { root }), toolbar).toEqual([]);
  }
}

test.describe("一覧の件数・ページ送り・欄の幅 (#1891)", { tag: "@responsive" }, () => {
  // 1 つの test で 5 つの一覧を 2 つの幅で回すので、既定の 30 秒では足りない。
  test.describe.configure({ timeout: 120_000 });

  test.beforeEach(async ({ page }) => {
    await mockDatabaseReady(page);
    await mockLocalAuth(page);
    await mockLists(page);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`一覧の画面は件数をツールバーの右に出し、崩れない (${theme})`, async ({ page }, testInfo) => {
      await useTheme(page, theme);
      const desktop = testInfo.project.name === "desktop";
      const widths = desktop ? [1280, 1920] : [null];
      for (const width of widths) {
        if (width) await page.setViewportSize({ width, height: 900 });
        for (const screen of SCREENS) {
          await page.goto(screen.path);
          const pagination = page.getByTestId(screen.pagination);
          await expect(pagination, screen.name).toContainText("1–25 / 55 件");
          await expect(page.getByTestId(screen.toolbar), screen.name).toContainText(screen.count);
          // 表示件数（10 / 25 / 50 / 100）はページ送りの左。
          await expect(pagination.getByRole("combobox", { name: "表示件数" }), screen.name).toBeVisible();
          await expectListLayout(page, desktop, screen.filtersMigrated ? screen.toolbar : undefined);
        }
        await page.goto("/feedback?period=all&sort=newest&size=25&page=1");
        await expect(page.getByTestId("feedback-filters-toolbar")).toContainText("全 120 件");
        await expect(page.getByTestId("feedback-pagination")).toContainText("1–25 / 120 件");
        await expectListLayout(page, desktop, "feedback-filters-toolbar");
        await expectListLayout(page, desktop, "feedback-list-toolbar");
      }
    });
  }

  test("ページ番号を入れて移り、表示件数を変えると 1 ページ目へ戻り、再読込でも残る", async ({ page }) => {
    for (const screen of SCREENS) {
      await page.goto(screen.path);
      const pagination = page.getByTestId(screen.pagination);
      await expect(pagination, screen.name).toContainText("1–25 / 55 件");
      await jumpToPage(pagination, 3);
      await expect(pagination, screen.name).toContainText("51–55 / 55 件");

      await pagination.getByRole("combobox", { name: "表示件数" }).click();
      await page.getByRole("option", { name: "100 件" }).click();
      // 1 ページに収まると、ページ送りは範囲と表示件数だけを出す（戻せるように）。
      await expect(pagination, screen.name).toContainText("1–55 / 55 件");
      await expect(pagination.getByRole("textbox", { name: "ページ番号" })).toHaveCount(0);

      await page.reload();
      await expect(page.getByTestId(screen.pagination), screen.name).toContainText("1–55 / 55 件");
    }
  });

  test("絞り込むと件数は「N 件（全 M 件）」になる", async ({ page }) => {
    await page.goto("/file-list");
    const toolbar = page.getByTestId("file-list-toolbar");
    await expect(toolbar).toContainText("全 55 件");
    await page.getByRole("searchbox", { name: "ファイル名で検索" }).fill("doc");
    await expect(toolbar).toContainText("5 件（全 55 件）");

    await page.goto("/history");
    const history = page.getByTestId("run-history-toolbar");
    await expect(history).toContainText("全 55 件");
    // 4 つの絞り込み（#1955）は 375px で「絞り込み」のシートにまとまる。
    const sheet = (await history.getAttribute("data-list-toolbar-filters-layout")) === "sheet";
    if (sheet) await history.getByRole("button", { name: /^絞り込み/ }).click();
    await page.getByRole("combobox", { name: "状態" }).click();
    await page.getByRole("option", { name: "失敗", exact: true }).click();
    if (sheet) await page.getByRole("button", { name: "絞り込みを閉じる" }).click();
    await expect(history).toContainText("5 件（全 55 件）");

    await page.goto("/feedback?period=all&sort=newest&size=10&page=1&rating=not_helpful");
    await expect(page.getByTestId("feedback-filters-toolbar")).toContainText("40 件（全 120 件）");
  });
});

test.describe("一覧の絞り込みの部品と条件のクリア (#1933)", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await mockDatabaseReady(page);
    await mockLocalAuth(page);
    await mockLists(page);
  });

  test("文書一覧: 選択の絞り込みはボタンの中にラベルを出し、条件があるときだけ「条件をクリア」で既定へ戻す", async ({ page }, testInfo) => {
    await page.goto("/file-list");
    const toolbar = page.getByTestId("file-list-toolbar");
    await openFileListFilters(page);
    const status = page.getByRole("combobox", { name: "状態" });
    await expect(status).toContainText("状態:すべて");
    // 数百件から選ぶナレッジベースは検索できる選択のまま、ラベルをボタンの中に出す。
    await expect(page.locator("#file-list-knowledge-base")).toContainText("ナレッジベース:利用できるすべてのナレッジベース");
    // 条件が無いあいだは「条件をクリア」を出さない。
    await expect(toolbar.getByRole("button", { name: "条件をクリア" })).toHaveCount(0);

    await status.click();
    await page.getByRole("option", { name: "索引済み" }).click();
    await closeFileListFilters(page);
    await expect(toolbar).toContainText("5 件（全 55 件）");
    // 既定以外は値を強調する（文字でも分かる）。
    if (testInfo.project.name === "mobile") await expect(toolbar.getByRole("button", { name: "絞り込み（1）" })).toBeVisible();
    else await expect(toolbar.locator('[data-filter-active="true"]')).toHaveCount(1);
    // 作業状態に残る（再読込しても同じ絞り込み）。
    await page.reload();
    await openFileListFilters(page);
    await expect(page.getByRole("combobox", { name: "状態" })).toHaveAttribute("data-value", "INDEXED");
    await closeFileListFilters(page);
    const search = page.getByRole("searchbox", { name: "ファイル名で検索" });
    await search.fill("doc");
    await search.press("Enter");
    expect(await findToolbarMisalignment(page, { root: '[data-testid="file-list-toolbar"]' })).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`file-list-filters-${testInfo.project.name}.png`) });

    await toolbar.getByRole("button", { name: "条件をクリア" }).click();
    await openFileListFilters(page);
    await expect(page.getByRole("combobox", { name: "状態" })).toHaveAttribute("data-value", "ALL");
    await closeFileListFilters(page);
    await expect(page.getByRole("searchbox", { name: "ファイル名で検索" })).toHaveValue("");
    await expect(toolbar).toContainText("全 55 件");
    await expect(toolbar.getByRole("button", { name: "条件をクリア" })).toHaveCount(0);
  });

  test("ナレッジベース: 状態のチップは検索欄と同じ高さで、選んだ状態は「条件をクリア」で戻せる", async ({ page }) => {
    await page.goto("/knowledge-bases");
    const toolbar = page.getByTestId("knowledge-base-list-toolbar");
    const group = page.getByRole("group", { name: "ナレッジベース状態フィルター" });
    await expect(group.getByRole("button", { name: "有効", pressed: true })).toBeVisible();
    const [searchBox, chipBox] = await Promise.all([
      page.getByRole("searchbox", { name: "名前・説明で検索" }).boundingBox(),
      group.getByRole("button", { name: "すべて" }).boundingBox(),
    ]);
    expect(Math.abs(searchBox!.height - chipBox!.height)).toBeLessThanOrEqual(0.5);

    await group.getByRole("button", { name: "アーカイブ済み" }).click();
    await expect(group.getByRole("button", { name: "アーカイブ済み", pressed: true })).toBeVisible();
    await toolbar.getByRole("button", { name: "条件をクリア" }).click();
    await expect(group.getByRole("button", { name: "すべて", pressed: true })).toBeVisible();
  });

  test("フィードバック: 絞り込みはツールバーに並び、狭い画面では「絞り込み（N）」のシートにまとめる", async ({ page }, testInfo) => {
    await page.goto("/feedback?period=all&sort=oldest&size=10&page=1&rating=not_helpful");
    const toolbar = page.getByTestId("feedback-filters-toolbar");
    await expect(toolbar).toContainText("40 件（全 120 件）");

    if (testInfo.project.name === "mobile") {
      await expect(toolbar).toHaveAttribute("data-list-toolbar-filters-layout", "sheet");
      // 期間（既定は 30 日）と評価の 2 つが既定と違う。
      await toolbar.getByRole("button", { name: "絞り込み（2）" }).click();
      const sheet = page.getByTestId("feedback-filters-toolbar-filters-sheet");
      await expect(sheet.getByRole("combobox", { name: "評価", exact: true })).toContainText("評価:役に立たなかった");
      await expect(sheet.getByRole("combobox", { name: "期間" })).toBeVisible();
      expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: testInfo.outputPath("feedback-filters-sheet-mobile.png") });
      await sheet.getByRole("button", { name: "40 件を表示" }).click();
      await expect(sheet).toBeHidden();
    } else {
      await expect(toolbar).toHaveAttribute("data-list-toolbar-filters-layout", "inline");
      await expect(toolbar.getByRole("combobox", { name: "評価", exact: true })).toContainText("評価:役に立たなかった");
      expect(await findToolbarMisalignment(page, { root: '[data-testid="feedback-filters-toolbar"]' })).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath("feedback-filters-desktop.png") });
    }

    // 条件をクリアすると絞り込みと検索語を既定（期間は 30 日）に戻し、並び順と表示件数は残す。
    await toolbar.getByRole("button", { name: "条件をクリア" }).click();
    await expect(page).toHaveURL(/\/feedback\?period=30&sort=oldest&size=10&page=1$/);
    await expect(toolbar.getByRole("button", { name: "条件をクリア" })).toHaveCount(0);
  });
});
