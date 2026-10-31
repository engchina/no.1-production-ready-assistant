import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, MOCK_NOW, test, type MockApi } from "./fixtures/mock-api";
import { expandSidebarSections, openSidebarNav } from "./fixtures/nav";
import { expectMainActionsClearOfToast } from "./fixtures/toast-placement";

/**
 * 画面の axe（a11y）の確認（#1595・#1671）。nightly（e2e-nightly.yml）だけで実行し、PR の e2e では実行しない
 * （名前の `*.nightly.spec.ts`。platform/scripts/e2e-impact/nightly-only.ts）。ローカルは `npx playwright test e2e/a11y.nightly.spec.ts`。
 *
 * - 対象はサイドナビの全項目の画面（SCREENS。ナビとずれたら「ナビの全項目」のテストが失敗する）と、淡い色の面
 *   （選んだ行・状態の表示）が出る状態（STATES）。テーマはライトだけ（#17）。API は mock-api の fixture。
 * - 対象は WCAG 2.x の A / AA（2.0 と 2.1）。失敗にするのは impact が serious / critical の違反だけ。
 * - 違反は rule id・impact・件数・対象（selector）を失敗の文と添付（axe-violations.json）に出す。
 * - 既知の例外は KNOWN_EXCEPTIONS に rule id・画面・理由を書く（直すのに設計の判断が要るもの。別 Issue で直す）。
 * - 同じ SCREENS で、通知（Toast）の決まった置き場所が主操作と重ならないことも確かめる（#1728。desktop と 375px）。
 */

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

/** 既知の例外（画面の path → 除く rule id）。空なら除かない。 */
const KNOWN_EXCEPTIONS: Record<string, readonly string[]> = {};

interface Screen {
  path: string;
  /** 画面を開く前に mock の状態を足す。 */
  setup?: (mockApi: MockApi) => void;
  /** 読み込んだ後の確認・操作（淡い面が出ていることなど）。 */
  act?: (page: Page) => Promise<void>;
}

/** サイドナビの全項目（nav-config.ts の並び）。 */
const SCREENS: Screen[] = [
  { path: "/chat" },
  { path: "/approvals" },
  { path: "/runs" },
  { path: "/agents" },
  { path: "/skills" },
  { path: "/evaluation" },
  { path: "/feedback" },
  { path: "/usage" },
  { path: "/audit" },
  { path: "/settings/security/permissions" },
  { path: "/settings/security/users" },
  { path: "/settings/security/roles" },
  { path: "/settings/security/api-keys", setup: seedApiKeys },
  { path: "/settings/system-tables" },
  // 運用設定のシステムテーブルの下（#2449・#2517。3 製品の共通のサイドナビの順）。
  { path: "/settings/chat-attachments" },
  { path: "/settings/answer-notice" },
  { path: "/runtimes" },
  { path: "/settings/mcp-connections" },
  { path: "/settings/upload-storage" },
  { path: "/settings/model" },
  { path: "/settings/database" },
  { path: "/settings/appearance" },
];

/**
 * サイドナビの行に無い画面（#1873。アカウントの画面はサイドナビの下部のアカウントの欄から開く）。
 * SCREENS と同じく axe と通知の置き場所を確かめ、「ナビの全項目」の比較には含めない。
 */
const NON_NAV_SCREENS: Screen[] = [
  { path: "/account", setup: seedApiKeys },
  { path: "/account?tab=password" },
];

/** 自分の API キー（有効なキーと期限切れのキー）。 */
function seedApiKeys(mockApi: MockApi) {
  const key = (id: string, name: string, overrides: Record<string, unknown> = {}) => ({
    id,
    name,
    token_prefix: `prak_${id}_E2Ex`,
    products: ["rag", "nl2sql", "agent"],
    resource_scopes: {},
    owner_user_uuid: "11111111-1111-1111-1111-111111111111",
    owner_display_name: "利用者 一郎",
    created_at: MOCK_NOW,
    expires_at: "2027-01-12T00:00:00Z",
    last_used_at: MOCK_NOW,
    expired: false,
    ...overrides,
  });
  mockApi.state.apiKeys.keys = [
    key("0123456789abcdef", "会社の PC の Claude Code"),
    key("fedcba9876543210", "検証用", { products: ["agent"], expires_at: "2026-09-01T00:00:00Z", expired: true }),
  ];
  mockApi.state.apiKeys.publicBaseUrl = "https://203.0.113.10";
}

function seedRun(mockApi: MockApi, id: string, status: string, approvals: Record<string, unknown>[] = []) {
  mockApi.state.runs.unshift({
    id,
    goal: `目標 ${id}`,
    agent_id: "default",
    runtime_id: "builtin",
    status,
    steps: [],
    events: [],
    approvals,
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

function approval(id: string, runId: string, status: string) {
  return {
    id,
    run_id: runId,
    step_id: `${id}-step`,
    tool_call: { name: `tool_${id}`, arguments: {} },
    status,
    reason: "",
    created_at: MOCK_NOW,
  };
}

/** 実行の状態の表示（success / danger / warning / neutral）と承認待ちを並べる。 */
function seedRuns(mockApi: MockApi) {
  seedRun(mockApi, "run-completed", "completed");
  seedRun(mockApi, "run-failed", "failed");
  seedRun(mockApi, "run-cancelled", "cancelled");
  seedRun(mockApi, "run-waiting", "waiting_approval", [approval("approval-1", "run-waiting", "pending")]);
}

/** 淡い色の面（選んだ行・状態の表示）が出る状態。 */
const STATES: (Screen & { name: string })[] = [
  {
    name: "実行の一覧の状態の表示と選んだ実行",
    path: "/runs?id=run-failed",
    setup: seedRuns,
    act: async (page) => {
      await expect(page.locator('[data-surface-tint="danger"]').first()).toBeAttached();
      await expect(page.locator("[data-surface-tint]:visible").first()).toBeVisible();
    },
  },
  {
    name: "承認待ちの一覧と選んだ承認",
    path: "/approvals?id=approval-1",
    setup: seedRuns,
    act: async (page) => {
      await expect(page.locator('[data-surface-tint="warning"]').first()).toBeAttached();
    },
  },
  {
    name: "業務 Agent の選んだ行",
    path: "/agents?id=default",
    act: async (page) => {
      await expect(page.locator("[data-surface-tint]").first()).toBeAttached();
    },
  },
];

const pathOf = (screen: Screen) => screen.path.split("?")[0];

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
async function openScreen(page: Page, mockApi: MockApi, screen: Screen) {
  screen.setup?.(mockApi);
  await page.goto(screen.path);
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  // 読み込み中の Skeleton ではなく、読み込んだ後の画面を確かめる。
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  await screen.act?.(page);
}

async function openAndCheck(page: Page, mockApi: MockApi, testInfo: TestInfo, screen: Screen) {
  await openScreen(page, mockApi, screen);
  await expectNoSeriousViolations(page, testInfo, pathOf(screen));
}

test.describe("画面の a11y（axe）", { tag: "@a11y" }, () => {
  test("SCREENS はサイドナビの全項目と同じ", async ({ page }) => {
    await page.goto("/chat");
    const sidebar = await openSidebarNav(page);
    await expandSidebarSections(sidebar);
    const hrefs = await sidebar
      .locator("nav a[href]")
      .evaluateAll((links) => links.map((link) => new URL((link as HTMLAnchorElement).href).pathname));
    expect(hrefs).toEqual(SCREENS.map(pathOf));
  });

  for (const screen of SCREENS) {
    test(`${pathOf(screen)}に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page, mockApi }, testInfo) => {
      await openAndCheck(page, mockApi, testInfo, screen);
    });
  }
  for (const screen of NON_NAV_SCREENS) {
    test(`${screen.path}に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page, mockApi }, testInfo) => {
      await openAndCheck(page, mockApi, testInfo, screen);
    });
  }
  for (const state of STATES) {
    test(`${state.name}に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page, mockApi }, testInfo) => {
      await openAndCheck(page, mockApi, testInfo, state);
    });
  }
  // #1728: 通知（Toast）の決まった置き場所（desktop は画面の右下、375px は上端のバー）が、主操作（PageHeader の
  // ページの操作・FormActionBar・ObjectActionBar・チャットの入力欄と送信・ページの末尾の操作）と重ならない。
  // 通知は本文より上に重なるので、重なると操作が隠れる（WCAG 2.2 の 2.4.11 Focus Not Obscured の考え方）。
  for (const screen of [...SCREENS, ...NON_NAV_SCREENS]) {
    test(`${screen.path} の主操作は通知の置き場所と重ならない`, { tag: "@responsive" }, async ({ page, mockApi }) => {
      await openScreen(page, mockApi, screen);
      await expectMainActionsClearOfToast(page);
    });
  }
});
