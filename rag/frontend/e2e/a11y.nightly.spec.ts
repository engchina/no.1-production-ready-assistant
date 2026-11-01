import AxeBuilder from "@axe-core/playwright";

import { expect, test, type Page, type TestInfo } from "./fixtures/test";
import { apiEnvelope, expandSidebarSections, mockLocalAuth, openSidebarNav, SYSTEM_TABLES_STATUS_OK } from "./_helpers";
import { expectMainActionsClearOfToast } from "./_toast-placement";

/**
 * 画面の axe（a11y）の確認（#1595・#1671）。nightly（e2e-nightly.yml）だけで実行し、PR の e2e では実行しない
 * （名前の `*.nightly.spec.ts`。platform/scripts/e2e-impact/nightly-only.ts）。ローカルは `npx playwright test e2e/a11y.nightly.spec.ts`。
 *
 * - 対象はサイドナビの全項目の画面（SCREENS。ナビとずれたら「ナビの全項目」のテストが失敗する）と、淡い色の面
 *   （選んだ行・状態の表示）が出る状態（STATES）。テーマはライトだけ（#17）。
 * - 対象は WCAG 2.x の A / AA（2.0 と 2.1）。失敗にするのは impact が serious / critical の違反だけ。
 * - 違反は rule id・impact・件数・対象（selector）を失敗の文と添付（axe-violations.json）に出す。
 * - API は mock。画面ごとの mock が無い GET は空の一覧を返す（未定義の API で画面が読み込み中のまま止まらない）。
 * - 既知の例外は KNOWN_EXCEPTIONS に rule id・画面・理由を書く（直すのに設計の判断が要るもの。別 Issue で直す）。
 * - 同じ SCREENS で、通知（Toast）の決まった置き場所が主操作と重ならないことも確かめる（#1728。desktop と 375px）。
 */

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

/** 既知の例外（画面の path → 除く rule id）。空なら除かない。 */
const KNOWN_EXCEPTIONS: Record<string, readonly string[]> = {};

const NOW = "2026-09-28T00:00:00Z";

function listPage(items: unknown[]) {
  return apiEnvelope({ items, total: items.length, limit: 50, offset: 0, has_next: false });
}

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

/** 主要な画面の一覧を数件ずつ返す（読み込んだ後の画面を確かめる）。 */
async function mockMainScreens(page: Page) {
  const profile = {
    id: "bv-1",
    name: "経理アシスタント",
    description: "経理の規程を答える",
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: NOW,
    updated_at: NOW,
    archived_at: null,
  };
  // 状態の表示（StatusBadge の success / warning / danger / neutral）を並べる。処理中の状態は自動の再取得が続くので使わない。
  const statuses = ["INDEXED", "REVIEW", "ERROR", "UPLOADED"];
  const documents = statuses.map((status, index) => ({
    id: `doc-${index + 1}`,
    file_name: `doc-${index + 1}.txt`,
    status,
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
  const conversations = Array.from({ length: 3 }, (_, index) => ({
    id: `conv-${index + 1}`,
    search_answer_profile_id: "bv-1",
    title: `会話 ${index + 1}`,
    status: "ACTIVE",
    message_count: 2,
    created_at: NOW,
    updated_at: NOW,
  }));
  await page.route("**/api/knowledge-bases**", (route) => route.fulfill({ json: listPage([]) }));
  await page.route("**/api/documents**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/documents") {
      await route.fulfill({ json: listPage(documents) });
      return;
    }
    await route.fulfill({ json: apiEnvelope([]) });
  });
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/search-answer-profiles") {
      await route.fulfill({ json: listPage([profile]) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  await page.route("**/api/chat/models", (route) => route.fulfill({ json: apiEnvelope([]) }));
  await page.route("**/api/chat/conversations**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname === "/api/chat/conversations") {
      await route.fulfill({ json: listPage(conversations) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });
}

/** 設定の画面（選んだカード = accent の淡い面、方針のチップ、状態の表示）と、フィードバックの一覧・詳細。 */
async function mockSettingsAndFeedback(page: Page) {
  const strategies = ["structure_aware", "recursive_character", "small_to_big", "fixed_size"];
  await page.route("**/api/settings/chunking", (route) =>
    route.fulfill({
      json: apiEnvelope({
        strategy: "small_to_big",
        chunk_size: 800,
        overlap: 120,
        min_chars: 120,
        delimiter: "\\n\\n",
        context_header_enabled: true,
        chunk_child_target_chars: 400,
        chunk_table_child_target_chars: 600,
        chunk_parent_target_chars: 2400,
        chunk_parent_max_pages: 2,
        chunk_parent_max_children: 8,
        strategies: strategies.map((name) => ({
          name,
          origin: name,
          recommended_for: ["pdf"],
          selected: name === "small_to_big",
        })),
        config_source: "runtime",
      }),
    }),
  );
  await page.route("**/api/settings/answer-prompts**", (route) =>
    route.fulfill({
      json: apiEnvelope({
        prompts: [
          {
            key: "vlm_answer",
            content: "保存版 {{question}} {{answer_context}}",
            default_content: "既定のテンプレート {{question}} {{answer_context}}",
            customized: true,
            required_placeholders: ["question", "answer_context"],
            updated_at: NOW,
          },
        ],
        stages: [{ id: "routing", phase: "question", prompts: [{ id: "system", content: "ルーティングの system" }] }],
      }),
    }),
  );
  await page.route("**/api/settings/database/system-tables", (route) => route.fulfill({ json: SYSTEM_TABLES_STATUS_OK }));

  const feedbackItem = (id: string, rating: "helpful" | "not_helpful", targetType: "answer" | "citation") => ({
    feedback_id: id,
    trace_id: `trace-${id}`,
    search_answer_profile_id: "bv-1",
    search_answer_profile_name: "経理アシスタント",
    target_type: targetType,
    source_surface: targetType === "answer" ? "chat" : "search",
    document_id: targetType === "citation" ? "doc-1" : null,
    chunk_id: targetType === "citation" ? "doc-1:0" : null,
    message_id: targetType === "answer" ? "msg-1" : null,
    rating,
    reason: rating === "not_helpful" ? "incorrect" : null,
    comment: rating === "not_helpful" ? "規程が更新されています。" : null,
    created_at: NOW,
    conversation_id: targetType === "answer" ? "conv-1" : null,
    conversation_title: targetType === "answer" ? "経費申請" : null,
    model: "deepseek-chat",
    file_name: targetType === "citation" ? "経費規程.pdf" : null,
    question_preview: "最新の経費申請期限を教えて",
    comment_preview: rating === "not_helpful" ? "規程が更新されています。" : null,
    has_comment: rating === "not_helpful",
  });
  const summary = {
    total: 3,
    helpful_count: 1,
    not_helpful_count: 2,
    helpful_rate: 0.3333,
    answer_total: 2,
    answer_helpful_rate: 0.5,
    citation_total: 1,
    citation_helpful_rate: 0,
    reason_counts: [{ reason: "incorrect", count: 2 }],
  };
  const feedbackItems = [
    feedbackItem("feedback-answer", "not_helpful", "answer"),
    feedbackItem("feedback-citation", "not_helpful", "citation"),
    feedbackItem("feedback-helpful", "helpful", "answer"),
  ];
  await page.route("**/api/feedback**", async (route) => {
    const url = new URL(route.request().url());
    const item = feedbackItems.find((candidate) => url.pathname === `/api/feedback/${candidate.feedback_id}`);
    if (item) {
      await route.fulfill({
        json: apiEnvelope({
          ...item,
          content_source: "chat_message",
          question: "最新の経費申請期限を教えて",
          answer: "2024年版の経費規程では翌月10日です。",
          citations: [
            {
              document_id: "doc-1",
              chunk_id: "doc-1:0",
              file_name: "経費規程.pdf",
              section_title: "申請期限",
              page_number: 3,
              content_preview: "経費申請は利用月の翌月5日までに行います。",
              rerank_score: 0.94,
            },
          ],
          execution: {
            outcome: "success",
            search_mode: "hybrid",
            elapsed_ms: 842,
            retrieved_count: 20,
            reranked_count: 5,
            citation_count: 1,
            guardrail_codes: [],
            config_fingerprint: "9f3a57a3d91bdaf0",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      json: apiEnvelope({
        summary,
        previous_summary: summary,
        items: { items: feedbackItems, total: feedbackItems.length, limit: 50, offset: 0, has_next: false },
      }),
    });
  });
}

/** 実行履歴（#1890）の一覧と詳細、API キー（全員の一覧・自分のキー。#1805・#1873）。 */
async function mockHistoryAndApiKeys(page: Page) {
  // 状態の表示（success / blocked / error）と、チャット・検索の両方を並べる。
  const runs = (["success", "blocked", "error"] as const).map((status, index) => ({
    run_id: `run-${index + 1}`,
    surface: index % 2 === 0 ? "chat" : "search",
    search_answer_profile_id: "bv-1",
    search_answer_profile_name: "経理アシスタント",
    question: `経費申請の期限 ${index + 1}`,
    answer_generated: index % 2 === 0,
    status,
    answer_outcome: index % 2 === 0 ? "answered" : null,
    verification: index % 2 === 0 ? "verified" : null,
    confidence: index % 2 === 0 ? "high" : null,
    needs_human_review: index % 2 === 0 ? false : null,
    citation_count: 1,
    elapsed_ms: 1200,
    error_stage: status === "error" ? "retrieval" : null,
    model_id: null,
    conversation_id: index % 2 === 0 ? `conv-${index + 1}` : null,
    message_id: index % 2 === 0 ? `msg-${index + 1}` : null,
    created_at: NOW,
    executed_by_me: true,
    executor_display_name: "ローカル利用者",
  }));
  await page.route("**/api/history/runs**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/history/runs") {
      await route.fulfill({ status: 404, json: { data: null, error_messages: ["実行が見つかりません。"], warning_messages: [] } });
      return;
    }
    await route.fulfill({ json: listPage(runs) });
  });

  const apiKey = (id: string, name: string, overrides: Record<string, unknown> = {}) => ({
    id,
    name,
    token_prefix: `prak_${id}_SSSS`,
    products: ["rag", "nl2sql", "agent"],
    owner_user_uuid: "11111111-1111-1111-1111-111111111111",
    owner_display_name: "利用者 一郎",
    created_at: NOW,
    expires_at: "2027-01-12T00:00:00Z",
    last_used_at: NOW,
    expired: false,
    ...overrides,
  });
  // 有効なキーと期限切れのキー（状態の表示）。
  const keys = [
    apiKey("0123456789abcdef", "会社の PC の Claude Code"),
    apiKey("fedcba9876543210", "検証用", { products: ["rag"], expires_at: "2026-09-01T00:00:00Z", expired: true }),
  ];
  await page.route("**/api/security/api-keys**", (route) => route.fulfill({ json: apiEnvelope({ keys }) }));
  await page.route("**/api/account/api-keys**", (route) =>
    route.fulfill({ json: apiEnvelope({ keys, public_base_url: "https://203.0.113.10" }) }),
  );
}

interface Screen {
  path: string;
  /** 画面を開く前に足す mock（後から登録した route が優先する）。 */
  setup?: (page: Page) => Promise<void>;
  /** 読み込んだ後の操作（行を選ぶなど）。 */
  act?: (page: Page) => Promise<void>;
}

/** サイドナビの全項目（nav-config.ts の並び）。 */
const SCREENS: Screen[] = [
  { path: "/chat?search_answer_profile_id=bv-1" },
  { path: "/search" },
  { path: "/history" },
  { path: "/upload" },
  { path: "/file-list" },
  { path: "/document-categories" },
  { path: "/knowledge-bases" },
  { path: "/search-answer-profiles" },
  { path: "/settings/pipeline" },
  { path: "/settings/language-pack" },
  { path: "/settings/preprocess" },
  { path: "/settings/parser-adapters" },
  { path: "/settings/chunking" },
  { path: "/settings/vector-index" },
  { path: "/settings/entity-index" },
  { path: "/settings/graph" },
  { path: "/settings/retrieval" },
  { path: "/settings/prompts" },
  { path: "/settings/evaluation" },
  { path: "/evaluation" },
  { path: "/feedback" },
  { path: "/settings/security/permissions" },
  { path: "/settings/security/users" },
  { path: "/settings/security/roles" },
  { path: "/settings/security/api-keys" },
  { path: "/settings/system-tables" },
  // 運用設定のシステムテーブルの下（#2449・#2517。3 製品の共通のサイドナビの順）。
  { path: "/settings/chat-attachments" },
  { path: "/settings/answer-notice" },
  { path: "/settings/huggingface" },
  { path: "/settings/services" },
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
    name: "フィードバックの選んだカードと詳細",
    path: "/feedback?period=30&feedback=feedback-answer",
    act: async (page) => {
      // 選んだカード（accent の淡い面）と、評価の状態の表示（danger）がある。375px は詳細だけを出す（一覧は隠れる）。
      await expect(page.locator('[data-surface-tint="accent"]').first()).toBeAttached();
      await expect(page.locator('[data-surface-tint="danger"]').first()).toBeAttached();
      await expect(page.locator("[data-surface-tint]:visible").first()).toBeVisible();
    },
  },
  {
    name: "文書インデックスの状態の表示",
    path: "/file-list",
    act: async (page) => {
      for (const tint of ["success", "warning", "danger"]) {
        await expect(page.locator(`[data-surface-tint="${tint}"]`).first()).toBeVisible();
      }
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
async function openScreen(page: Page, screen: Screen) {
  await screen.setup?.(page);
  await page.goto(screen.path);
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  // 読み込み中の Skeleton ではなく、読み込んだ後の画面を確かめる。描画の失敗（CardErrorBoundary）は mock の不足。
  // mock の無い API は 503 で、TanStack Query の再試行（既定 3 回）を終えてから失敗の表示になる。
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 20_000 });
  await expect(page.getByText(/^(この項目|「.+」)を表示できません$/)).toHaveCount(0);
  await screen.act?.(page);
}

async function openAndCheck(page: Page, testInfo: TestInfo, screen: Screen) {
  await openScreen(page, screen);
  await expectNoSeriousViolations(page, testInfo, pathOf(screen));
}

test.describe("画面の a11y（axe）", { tag: "@a11y" }, () => {
  test.beforeEach(async ({ page }) => {
    await mockFallbackApi(page);
    await mockLocalAuth(page);
    await mockMainScreens(page);
    await mockSettingsAndFeedback(page);
    await mockHistoryAndApiKeys(page);
  });

  test("SCREENS はサイドナビの全項目と同じ", async ({ page }) => {
    await page.goto("/chat?search_answer_profile_id=bv-1");
    const sidebar = await openSidebarNav(page);
    await expandSidebarSections(sidebar);
    const hrefs = await sidebar
      .locator("nav a[href]")
      .evaluateAll((links) => links.map((link) => new URL((link as HTMLAnchorElement).href).pathname));
    expect(hrefs).toEqual(SCREENS.map(pathOf));
  });

  for (const screen of SCREENS) {
    test(`${pathOf(screen)} に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
      await openAndCheck(page, testInfo, screen);
    });
  }
  for (const screen of NON_NAV_SCREENS) {
    test(`${screen.path} に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
      await openAndCheck(page, testInfo, screen);
    });
  }
  for (const state of STATES) {
    test(`${state.name} に WCAG 2.1 A / AA の serious / critical の違反が無い`, async ({ page }, testInfo) => {
      await openAndCheck(page, testInfo, state);
    });
  }
  // #1728: 通知（Toast）の決まった置き場所（desktop は画面の右下、375px は上端のバー）が、主操作（PageHeader の
  // ページの操作・FormActionBar・ObjectActionBar・チャットの入力欄と送信・ページの末尾の操作）と重ならない。
  // 通知は本文より上に重なるので、重なると操作が隠れる（WCAG 2.2 の 2.4.11 Focus Not Obscured の考え方）。
  for (const screen of [...SCREENS, ...NON_NAV_SCREENS]) {
    test(`${screen.path} の主操作は通知の置き場所と重ならない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      await openScreen(page, screen);
      if (screen.path === "/feedback") {
        // 少数の一覧では共通標準に従い、単一ページのページ送りを表示しない。
        await expect(page.getByTestId("feedback-pagination")).toHaveCount(0);
      }
      await expectMainActionsClearOfToast(page);
      if (screen.path === "/feedback") {
        await page.screenshot({ path: testInfo.outputPath("feedback-single-page-toast.png"), fullPage: true });
      }
    });
  }
});
