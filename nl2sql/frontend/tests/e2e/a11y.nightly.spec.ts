import AxeBuilder from "@axe-core/playwright";

import { expect, test, type Page, type TestInfo } from "./_helpers/test";
import { mockChatRequirement } from "./_helpers/chat-requirement";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { expectMainActionsClearOfToast } from "./_helpers/toast";

/**
 * 画面の axe（a11y）の確認（#1595・#1671）。nightly（e2e-nightly.yml）だけで実行し、PR の e2e では実行しない
 * （名前の `*.nightly.spec.ts`。platform/scripts/e2e-impact/nightly-only.ts）。
 * ローカルは `npx playwright test tests/e2e/a11y.nightly.spec.ts`。
 *
 * - 対象はサイドナビの全項目の画面（SCREENS。ナビとずれたら「ナビの全項目」のテストが失敗する）と、淡い色の面
 *   （選んだ行・状態の表示）が出る状態（STATES）。それぞれライトとダークで確かめる。
 * - API は mock。画面ごとの mock が無い API は 503（取得の失敗の表示）にする。
 * - 対象は WCAG 2.x の A / AA（2.0 と 2.1）。失敗にするのは impact が serious / critical の違反だけ。
 * - 違反は rule id・impact・件数・対象（selector）を失敗の文と添付（axe-violations.json）に出す。
 * - 既知の例外は KNOWN_EXCEPTIONS に rule id・画面・理由を書く（直すのに設計の判断が要るもの。別 Issue で直す）。
 * - 同じ SCREENS で、通知（Toast）の決まった置き場所が主操作と重ならないことも確かめる（#1728。ライトだけ、desktop と 375px）。
 */

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

/** 既知の例外（画面の path → 除く rule id）。空なら除かない。 */
const KNOWN_EXCEPTIONS: Record<string, readonly string[]> = {};

const PROFILE = {
  id: "sales",
  name: "売上分析",
  description: "売上の集計",
  archived: false,
  allowed_tables: ["APP.SALES"],
  allowed_views: [],
  allowed_table_count: 1,
  allowed_view_count: 0,
  version: 1,
};

const HISTORY = [
  {
    id: "history-1",
    question: "今月の売上を確認",
    engine: "select_ai",
    generated_sql: "SELECT SUM(AMOUNT) FROM APP.SALES",
    executable_sql: "SELECT SUM(AMOUNT) FROM APP.SALES",
    created_at: "2026-06-22T10:00:00.000Z",
    elapsed_ms: 250,
    feedback_rating: "good",
    profile_id: "sales",
    profile_name: "売上分析",
    rewritten_question: "今月の売上",
    safety_is_safe: true,
    result_row_count: 1,
    result_columns: ["SUM(AMOUNT)"],
    feedback_comment: "",
  },
  {
    id: "history-2",
    question: "顧客を削除",
    engine: "select_ai",
    generated_sql: "DELETE FROM APP.CUSTOMERS",
    executable_sql: "",
    created_at: "2026-06-21T10:00:00.000Z",
    elapsed_ms: 120,
    feedback_rating: "",
    profile_id: "sales",
    profile_name: "売上分析",
    rewritten_question: "",
    safety_is_safe: false,
    result_row_count: 0,
    result_columns: [],
    feedback_comment: "",
  },
];

/**
 * 画面ごとの mock が無い API は 503（取得の失敗）にする（後から登録した route が優先）。画面は取得の失敗の表示
 * （danger の淡い面の ErrorState / Banner）になり、それも axe で確かめる。形の合わない応答で画面を壊さない。
 */
async function mockFallbackApi(page: Page) {
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) =>
      route.fulfill({
        status: 503,
        json: { data: null, error_messages: ["e2e: この API の応答は用意していません。"], warning_messages: [] },
      }),
  );
}

/** 主要な画面の取得を数件ずつ返す（読み込んだ後の画面を確かめる）。 */
async function mockMainScreens(page: Page) {
  await mockDatabaseGateReady(page);
  await mockChatRequirement(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [PROFILE], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => route.fulfill({ json: { data: PROFILE } }));
  await page.route("**/api/nl2sql/chats**", (route) =>
    route.fulfill({ json: { data: { items: [], next_cursor: null, total: 0, limit: 25 } } }),
  );
  await page.route("**/api/nl2sql/history**", (route) => {
    const id = /\/api\/nl2sql\/history\/([^/?]+)$/.exec(new URL(route.request().url()).pathname)?.[1];
    const item = id ? HISTORY.find((entry) => entry.id === decodeURIComponent(id)) : undefined;
    return route.fulfill({
      json: { data: item ?? { items: HISTORY, total: HISTORY.length, limit: 25, offset: 0, has_next: false, unfiltered_total: HISTORY.length } },
    });
  });
}

/** API キー（全員の一覧・自分のキー。#1805・#1873）。有効なキーと期限切れのキー（状態の表示）。 */
async function mockApiKeys(page: Page) {
  const apiKey = (id: string, name: string, overrides: Record<string, unknown> = {}) => ({
    id,
    name,
    token_prefix: `prak_${id}_SSSS`,
    products: ["rag", "nl2sql", "agent"],
    owner_user_uuid: "11111111-1111-1111-1111-111111111111",
    owner_display_name: "利用者 一郎",
    created_at: "2026-06-22T10:00:00.000Z",
    expires_at: "2027-01-12T00:00:00Z",
    last_used_at: "2026-06-22T10:00:00.000Z",
    expired: false,
    ...overrides,
  });
  const keys = [
    apiKey("0123456789abcdef", "会社の PC の Claude Code"),
    apiKey("fedcba9876543210", "検証用", { products: ["nl2sql"], expires_at: "2026-06-01T00:00:00Z", expired: true }),
  ];
  await page.route("**/api/security/api-keys**", (route) => route.fulfill({ json: { data: { keys } } }));
  await page.route("**/api/account/api-keys**", (route) =>
    route.fulfill({ json: { data: { keys, public_base_url: "https://203.0.113.10" } } }),
  );
}

interface Screen {
  path: string;
  /** 画面を開く前に足す mock（後から登録した route が優先する）。 */
  setup?: (page: Page) => Promise<void>;
  /** 読み込んだ後の確認・操作（淡い面が出ていることなど）。 */
  act?: (page: Page) => Promise<void>;
}

/** サイドナビの全項目（nav-config.ts の並び）。 */
const SCREENS: Screen[] = [
  { path: "/chat" },
  { path: "/query" },
  { path: "/direct-sql" },
  { path: "/sql-to-question" },
  { path: "/history" },
  { path: "/db-admin-chat" },
  { path: "/admin-sql" },
  { path: "/table-management" },
  { path: "/view-management" },
  { path: "/data-management" },
  { path: "/comment-management" },
  { path: "/annotation-management" },
  { path: "/domain-management" },
  { path: "/glossary-rules" },
  { path: "/global-rules" },
  { path: "/sample-data" },
  { path: "/profiles" },
  { path: "/ontology-build" },
  { path: "/feedback-management" },
  { path: "/question-classifier-models" },
  { path: "/evaluation" },
  { path: "/settings/security/permissions" },
  { path: "/settings/security/deepsec" },
  { path: "/settings/security/users" },
  { path: "/settings/security/roles" },
  { path: "/settings/security/api-keys" },
  { path: "/settings/system-tables" },
  // 運用設定のシステムテーブルの下（#2449・#2517。3 製品の共通のサイドナビの順）。
  { path: "/settings/chat-attachments" },
  { path: "/settings/answer-notice" },
  { path: "/settings/select-ai-credential" },
  { path: "/settings/oci" },
  { path: "/settings/upload-storage" },
  { path: "/settings/model" },
  { path: "/settings/database" },
];

/**
 * サイドナビの行に無い画面（#1873。アカウントの画面はサイドナビの下部のアカウントの欄から開く）。
 * SCREENS と同じく axe と通知の置き場所を確かめ、「ナビの全項目」の比較には含めない。
 */
const NON_NAV_SCREENS: Screen[] = [{ path: "/account" }, { path: "/account?tab=password" }];

/** 淡い色の面（選んだ行・状態の表示）が出る状態。 */
const STATES: (Screen & { name: string })[] = [
  {
    name: "実行履歴の開いた行と状態",
    path: "/history",
    act: async (page) => {
      // 詳細（?id=）を開いて一覧へ戻ると、開いていた行に選択の面が付く（#1955）。
      await page.getByRole("link", { name: /今月の売上を確認/ }).first().click();
      await page.getByRole("button", { name: "実行履歴の一覧へ戻る" }).click();
      await expect(page.locator('[data-surface-tint="accent"]').first()).toBeAttached();
    },
  },
];

const pathOf = (screen: Screen) => screen.path.split("?")[0];

/** テーマは ui-store（localStorage）の保存値で決める（既定はライト）。 */
async function useTheme(page: Page, theme: Theme) {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

/** 表示の途中（フェードイン・色の transition）の色で color-contrast を測らない。無限の animation（Spinner など）は待たない。 */
async function waitForSettledAnimations(page: Page) {
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every((animation) => animation.playState !== "running" || animation.effect?.getComputedTiming().iterations === Infinity),
  );
}

async function expectNoSeriousViolations(page: Page, testInfo: TestInfo, path: string) {
  const exceptions = KNOWN_EXCEPTIONS[path] ?? [];
  await waitForSettledAnimations(page);
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).disableRules([...exceptions]).analyze();
  const blocking = results.violations.filter((violation) => BLOCKING_IMPACTS.has(violation.impact ?? ""));
  const summary = blocking.map((violation) => ({
    rule: violation.id,
    impact: violation.impact,
    count: violation.nodes.length,
    help: violation.help,
    // 対象の selector と、axe の理由の 1 行目（color-contrast なら実測の比と色）。
    targets: violation.nodes
      .slice(0, 5)
      .map((node) => `${node.target.join(" ")}（${node.failureSummary?.split("\n").at(1)?.trim() ?? ""}）`),
  }));
  if (summary.length) {
    await testInfo.attach("axe-violations.json", { body: JSON.stringify(summary, null, 2), contentType: "application/json" });
  }
  const message = summary
    .map((item) => `${item.rule}（${item.impact}・${item.count} 件）: ${item.help}\n    ${item.targets.join("\n    ")}`)
    .join("\n");
  expect(summary, `${path} の serious / critical の a11y 違反:\n${message}`).toEqual([]);
  if (path === "/evaluation") await page.screenshot({ path: test.info().outputPath("evaluation-a11y.png"), fullPage: true });
}

/** 画面を開き、読み込んだ後の状態にする。 */
async function openScreen(page: Page, screen: Screen, theme: Theme) {
  await useTheme(page, theme);
  await screen.setup?.(page);
  await page.goto(screen.path);
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  if (theme === "dark") await expect(page.locator("html")).toHaveClass(/\bdark\b/);
  else await expect(page.locator("html")).not.toHaveClass(/\bdark\b/);
  // 読み込み中の Skeleton ではなく、読み込んだ後の画面を確かめる。
  // mock の無い API は 503 で、TanStack Query の再試行（既定 3 回）を終えてから失敗の表示になる。
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 20_000 });
  await screen.act?.(page);
}

async function openAndCheck(page: Page, testInfo: TestInfo, screen: Screen, theme: Theme) {
  await openScreen(page, screen, theme);
  await expectNoSeriousViolations(page, testInfo, pathOf(screen));
}

test.describe("画面の a11y（axe）", { tag: "@a11y" }, () => {
  test.beforeEach(async ({ page }) => {
    await mockFallbackApi(page);
    await mockMainScreens(page);
    await mockApiKeys(page);
  });

  test("SCREENS はサイドナビの全項目と同じ", async ({ page }) => {
    await page.goto("/chat");
    const sidebar = await openSidebarNav(page);
    const collapsed = sidebar.locator('button[aria-controls^="nav-section-"][aria-expanded="false"]');
    while ((await collapsed.count()) > 0) await collapsed.first().click();
    const hrefs = await sidebar
      .locator("nav a[href]")
      .evaluateAll((links) => links.map((link) => new URL((link as HTMLAnchorElement).href).pathname));
    expect(hrefs).toEqual(SCREENS.map(pathOf));
  });

  for (const theme of THEMES) {
    for (const screen of SCREENS) {
      test(`${pathOf(screen)}（${theme}）に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
        await openAndCheck(page, testInfo, screen, theme);
      });
    }
    for (const screen of NON_NAV_SCREENS) {
      test(`${screen.path}（${theme}）に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
        await openAndCheck(page, testInfo, screen, theme);
      });
    }
    for (const state of STATES) {
      test(`${state.name}（${theme}）に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
        await openAndCheck(page, testInfo, state, theme);
      });
    }
  }
  // #1728: 通知（Toast）の決まった置き場所（desktop は画面の右下、375px は上端のバー）が、主操作（PageHeader の
  // ページの操作・FormActionBar・ObjectActionBar・チャットの入力欄と送信・ページの末尾の操作）と重ならない。
  // 通知は本文より上に重なるので、重なると操作が隠れる（WCAG 2.2 の 2.4.11 Focus Not Obscured の考え方）。
  for (const screen of [...SCREENS, ...NON_NAV_SCREENS]) {
    test(`${screen.path} の主操作は通知の置き場所と重ならない`, { tag: "@responsive" }, async ({ page }) => {
      await openScreen(page, screen, "light");
      await expectMainActionsClearOfToast(page);
    });
  }
});
