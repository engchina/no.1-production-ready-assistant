import { inspectTraceStep } from "../../../../platform/packages/ui/e2e/trace-explorer";
import {timelineSnapshot, timelineTypography} from "../../../../platform/packages/ui/e2e/timeline-snapshot";
import { expect, test, type Locator, type Page } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import {
  confirmChatRequirement,
  mockChatRequirement,
  type ChatRequirementMockOptions,
} from "./_helpers/chat-requirement";
import { openSidebarNav, closeSidebarNav } from "./_helpers/sidebar-nav";
import {
  chooseSelectFieldOption,
  expectSelectFieldValue,
} from "./_helpers/select-field";
import { expectedControlHeight } from "./_helpers/control-height";
import {
  expectProgressTimerMonotonic,
  startProgressTimerSampler,
} from "./_helpers/progress-timer";
import {
  jobProgressEvents,
  jobProgressTraceId,
  recordMockTrace,
  turnProgressEvents,
  withJobProgress,
  withRecordedTrace,
} from "./_helpers/job-progress";
import {
  REQUIREMENT_CLARIFIED_QUESTION,
  REQUIREMENT_PENDING_HINT,
  REQUIREMENT_SESSION_ID,
  REQUIREMENT_TARGET_PENDING_HINT,
  TARGET_OTHER_TEXT,
  TARGET_QUESTION,
} from "./_helpers/requirement-session";

const profile = {
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
const now = "2026-10-02T22:00:00Z";
interface Turn {
  job_id: string;
  question: string;
  status: string;
  result: unknown;
  error_message?: string;
  error_code?: string;
  created_at: string;
  steps: unknown[];
  [key: string]: unknown;
}
async function setup(page: Page, requirementOptions: ChatRequirementMockOptions = {}) {
  await mockDatabaseGateReady(page);
  // 送信の前の要件の検査（#1442）。既定は「確認が要らない」で、聞かずに SQL を生成する。
  const requirement = await mockChatRequirement(page, requirementOptions);
  const state = {
    requirement,
    turns: [] as Turn[],
    requests: [] as Record<string, unknown>[],
    failSend: false,
    safetyChecked: true,
    pending: false,
    // 投入したジョブを、backend と同じく worker の開始前（status: pending・段階はすべて待機中）で作る。
    queued: false,
    // 回答の注意書き（#2405。backend が答えた job に付ける文）。
    notice: null as string | null,
  };
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({
      json: { data: { items: [profile], total: 1, next_cursor: null } },
    }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: profile } }),
  );
  await page.route("**/api/nl2sql/chats**", (route) => {
    const conversation = {
      id: "chat-1",
      title: state.turns[0]?.question,
      profile_id: "sales",
      created_at: now,
    };
    return route.fulfill({
      json: {
        data:
          new URL(route.request().url()).pathname === "/api/nl2sql/chats"
            ? {
                items: state.turns.length ? [conversation] : [],
                next_cursor: null,
                total: state.turns.length ? 1 : 0,
                limit: 25,
              }
            : { conversation, turns: state.turns.map(withJobProgress) },
      },
    });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    const body = route.request().postDataJSON();
    state.requests.push(body);
    if (state.failSend)
      return route.fulfill({
        status: 503,
        json: { error: "モデルへ接続できません。" },
      });
    const id = `chat-${state.turns.length + 1}`;
    const result = {
      generated_sql: `SELECT CATEGORY, SUM(AMOUNT) FROM APP.SALES GROUP BY CATEGORY${state.turns.length ? " ORDER BY SUM(AMOUNT) DESC" : ""}`,
      original_question: body.question,
      explanation: "カテゴリごとの売上合計です。",
      safety: { is_safe: true, safety_checked: state.safetyChecked },
      notice: state.notice,
    };
    const turn: Turn = {
      job_id: id,
      question: body.question,
      // 回答前の確認で確かめた質問の文（要件を反映したジョブだけ。#1442）。
      requirement_question: body.requirement_session_id ? REQUIREMENT_CLARIFIED_QUESTION : "",
      // 回答前の確認で答えた内容・飛ばしたこと（#1839。サーバーが要件から作る）。
      requirement_answers: body.requirement_session_id
        ? [{ prompt: "今年度の期間", answer: "2026-04-01〜2027-03-31" }]
        : [],
      requirement_skipped: Boolean(body.requirement_skipped),
      engine: body.engine,
      status: state.queued ? "pending" : state.pending ? "running" : "done",
      // 実行中のジョブは今の時刻に作る（処理の経過の経過時間はジョブの作成から数える）。
      created_at: state.pending || state.queued ? new Date().toISOString() : now,
      steps: state.queued ? pendingSteps() : [],
      result: state.pending ? null : result,
    };
    state.turns.push(turn);
    return route.fulfill({
      json: {
        data: {
          job_id: id,
          status: turn.status,
          created_at: turn.created_at,
          steps: [],
        },
      },
    });
  });
  await page.route("**/api/nl2sql/jobs/*/cancel", (route) => {
    Object.assign(state.turns.at(-1)!, {
      status: "error",
      error_code: "JOB_CANCELLED",
      error_message: "停止しました。",
    });
    return route.fulfill({ json: { data: state.turns.at(-1) } });
  });
  return state;
}
for (const width of [1280, 375]) {
  test(`多輪で SQL を生成し、履歴とリロードで復元する (${width}px)`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    const state = await setup(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/chat");
    await expect(
      page.getByRole("heading", { name: "チャット", level: 1 }),
    ).toBeVisible();
    const history = page.getByRole("button", {
      name: "会話の履歴",
      exact: true,
    });
    await expect(history).toHaveAttribute("aria-expanded", "false");
    await expect(history).toHaveAttribute("aria-controls", "sql-chat-history");
    await expect(page.getByTestId("sql-chat-history")).not.toBeVisible();
    // 会話の履歴の開閉はアイコンだけのボタンで、会話の欄の上端の行の左端にある（#889）。
    await expect(history).toHaveText("");
    const toggleBox = (await history.boundingBox())!;
    const newButtonBox = (await page
      .getByRole("button", { name: "新しい会話", exact: true })
      .boundingBox())!;
    const panelBox = (await page.getByTestId("sql-chat-panel").boundingBox())!;
    expect(toggleBox.x - panelBox.x).toBeLessThan(24);
    expect(toggleBox.x).toBeLessThan(newButtonBox.x);
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("カテゴリ別売上");
    await composer.press("Enter");
    await confirmChatRequirement(page);
    await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
    await composer.fill("多い順にして");
    await page.getByTestId("sql-chat-send").click();
    await confirmChatRequirement(page);
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
    await expect(page.locator("pre").last()).toContainText(
      "ORDER BY SUM(AMOUNT) DESC",
    );
    // チャットのターンとして送る（実行するかは backend が権限で決める。#1176）。
    // 生成の prompt には公開版のオントロジーの文脈を使い、画面に出さない生成後の接地確認は
    // 求めない（#1172）。
    expect(state.requests[0]).toMatchObject({
      chat: true,
      profile_id: "sales",
      previous_job_id: null,
      use_ontology_context: true,
      include_ontology_grounding: false,
    });
    expect(state.requests[1]).toMatchObject({
      chat: true,
      previous_job_id: "chat-1",
      question: "多い順にして",
      use_ontology_context: true,
      include_ontology_grounding: false,
    });
    // 送るたびに、SQL を作る前の要件の検査を会話の続き（前のターン）で行う（#1442）。確認の質問が無くても
    // 確認のカードで「この内容で SQL を生成して実行」を押してから、要件（session）を付けて SQL を生成する（#1761）。
    expect(state.requirement.creates).toMatchObject([
      { question: "カテゴリ別売上", profile_id: "sales", chat: true, clarification_mode: "guided", previous_job_id: null },
      { question: "多い順にして", profile_id: "sales", chat: true, clarification_mode: "guided", previous_job_id: "chat-1" },
    ]);
    expect(state.requirement.applies).toBe(2);
    expect(state.requests[1]).toMatchObject({ requirement_session_id: REQUIREMENT_SESSION_ID });
    await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
    await page.reload();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
    await history.click();
    await expect(
      page
        .getByTestId("sql-chat-history")
        .getByText("カテゴリ別売上", { exact: true }),
    ).toBeVisible();
    if (width < 1024) {
      await page.keyboard.press("Escape");
      await expect(history).toBeFocused();
    } else await history.click();
    await page.getByRole("button", { name: "新しい会話", exact: true }).click();
    await expect(
      page.getByText("質問を入力して会話を始めます"),
    ).toBeVisible();
    await history.click();
    await page
      .getByTestId("sql-chat-history")
      .getByText("カテゴリ別売上", { exact: true })
      .click();
    if (width >= 1024) await history.click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
    if (width < 1024)
      await expect(
        page.getByRole("dialog", { name: "会話の履歴" }),
      ).toHaveCount(0);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    ).toBeLessThanOrEqual(0);
    if (width >= 1024)
      expect(
        await page
          .locator("#pr-main")
          .evaluate((el) => el.scrollHeight - el.clientHeight),
      ).toBeLessThanOrEqual(1);
    await composer.focus();
    await expect(composer).toBeInViewport();
    await page.screenshot({
      path: testInfo.outputPath(`chat-${width}.png`),
      fullPage: true,
    });
  });
}
test("生成中は停止でき、失敗時も送った質問を会話に残して再送信できる", async ({ page }) => {
  const state = await setup(page);
  state.pending = true;
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const send = page.getByTestId("sql-chat-send");
  await expect(send).toHaveAccessibleName("停止");
  await composer.fill("次の条件");
  await composer.press("Enter");
  expect(state.requests).toHaveLength(1);
  await send.click();
  await expect(page.getByText("SQL の生成を停止しました")).toBeVisible();
  // 停止しても送った質問は会話に残る（#907）。
  await expect(
    page.getByTestId("sql-chat-turn").getByText("カテゴリ別売上", { exact: true }),
  ).toBeVisible();
  await expect(send).toHaveAccessibleName("送信");
  await expect(composer).toHaveValue("次の条件");
  state.failSend = true;
  await send.click();
  await confirmChatRequirement(page);
  // 送った質問は会話の欄に残し、入力欄は空のまま（#907）。理由と「再送信」は質問の直下に出す。
  const pendingTurn = page.getByTestId("sql-chat-pending-turn");
  await expect(pendingTurn.locator('[data-status="failed"]')).toContainText(
    "次の条件",
  );
  await expect(pendingTurn).toContainText("送信できませんでした");
  await expect(
    pendingTurn.getByTestId("sql-chat-send-error"),
  ).toContainText("モデルへ接続できません。");
  await expect(composer).toHaveValue("");
  state.failSend = false;
  state.pending = false;
  await pendingTurn.getByRole("button", { name: "再送信" }).click();
  await expect(pendingTurn).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
  await expect(page.getByTestId("sql-chat-send-error")).toHaveCount(0);
  expect(state.requests.at(-1)).toMatchObject({ question: "次の条件" });
});
/**
 * ジョブの投入の上限（120 秒）だけを短くする。ほかの要求の上限は変えない（#900）。
 */
async function shortenJobSubmitTimeout(page: Page) {
  await page.addInitScript(() => {
    const original = AbortSignal.timeout.bind(AbortSignal);
    AbortSignal.timeout = (ms: number) => original(ms === 120_000 ? 300 : ms);
  });
}

/** 応答を返さないジョブの投入。テストの終わりに abort して後始末する。 */
async function holdJobSubmit(page: Page, state: { requests: Record<string, unknown>[] }) {
  const held: import("@playwright/test").Route[] = [];
  await page.route("**/api/nl2sql/jobs", (route) => {
    state.requests.push(route.request().postDataJSON());
    held.push(route);
  });
  return async () => {
    await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
  };
}

test("送信の応答が上限までに届かないと、英語の signal timed out ではなく日本語の案内と詳細を出す (#900)", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const state = await setup(page);
  await shortenJobSubmitTimeout(page);
  const release = await holdJobSubmit(page, state);
  const recovered: string[] = [];
  // backend はジョブを作り終えていない（取り直しは 404）。
  await page.route("**/api/nl2sql/jobs/*", (route) => {
    recovered.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      status: 404,
      json: { error: "指定されたジョブが見つかりません。" },
    });
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("select * from employee");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const failure = page.getByTestId("sql-chat-send-error");
  await expect(failure).toBeVisible();
  const banner = failure.getByRole("alert");
  await expect(banner).toContainText(
    "送信の応答が 120 秒以内に返りませんでした。",
  );
  await expect(banner).toContainText(
    "少し待ってから会話の履歴を確かめ、見当たらなければもう一度送信してください。",
  );
  // ブラウザの英語の文は「詳細」にだけ出す（失敗なので開いて出す）。
  const details = failure.locator("details");
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText("POST /api/nl2sql/jobs");
  await expect(details).toContainText("120 秒");
  await expect(details).toContainText("TimeoutError");
  await expect(failure.locator("p").first()).not.toContainText("signal");
  // 送信前に決めた job ID で取り直しを試みる。送った質問は会話の欄に残す（#907）。
  const clientJobId = String(state.requests[0]?.client_job_id ?? "");
  expect(clientJobId).toMatch(/^[0-9a-f-]{36}$/u);
  expect(recovered).toEqual([`/api/nl2sql/jobs/${clientJobId}`]);
  await expect(composer).toHaveValue("");
  await expect(page.getByTestId("sql-chat-send")).toHaveAccessibleName("送信");
  // 結果は送った質問の直下に、会話の欄の幅で出す（messaging.md §10.1 のチャットの扱い。#907）。
  const question = page
    .getByTestId("sql-chat-pending-turn")
    .locator('[data-status="failed"]');
  await expect(question).toContainText("select * from employee");
  await expect(failure.getByRole("button", { name: "再送信" })).toBeVisible();
  const questionBox = (await question.boundingBox())!;
  const failureBox = (await failure.boundingBox())!;
  const regionBox = (await page
    .getByTestId("sql-chat-conversation")
    .boundingBox())!;
  expect(failureBox.y).toBeGreaterThan(questionBox.y + questionBox.height - 1);
  expect(regionBox.width - failureBox.width).toBeLessThan(48);
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
  await failure.screenshot({
    path: testInfo.outputPath(`chat-send-timeout-light.png`),
  });
  await release();
});

test("送信の応答が届かなくても、作成済みのジョブを取り直して生成の結果を表示する (#900)", async ({
  page,
}) => {
  const state = await setup(page);
  await shortenJobSubmitTimeout(page);
  const release = await holdJobSubmit(page, state);
  await page.route("**/api/nl2sql/jobs/*", (route) => {
    const jobId = new URL(route.request().url()).pathname.split("/").at(-1)!;
    const turn: Turn = {
      job_id: jobId,
      question: "select * from employee",
      status: "done",
      created_at: now,
      steps: [],
      result: {
        generated_sql: "SELECT * FROM APP.EMPLOYEE",
        original_question: "select * from employee",
        explanation: "",
        safety: { is_safe: true },
      },
    };
    if (!state.turns.some((item) => item.job_id === jobId))
      state.turns.push(turn);
    return route.fulfill({ json: { data: turn } });
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("select * from employee");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
  await expect(page.locator("pre").last()).toContainText(
    "SELECT * FROM APP.EMPLOYEE",
  );
  await expect(page.getByTestId("sql-chat-send-error")).toHaveCount(0);
  await expect(composer).toHaveValue("");
  expect(state.requests).toHaveLength(1);
  await release();
});

test("サーバーに接続できないときは英語の Failed to fetch ではなく日本語で案内する (#900)", async ({
  page,
}) => {
  const state = await setup(page);
  await page.route("**/api/nl2sql/jobs", (route) => {
    state.requests.push(route.request().postDataJSON());
    return route.abort("failed");
  });
  await page.route("**/api/nl2sql/jobs/*", (route) => route.abort("failed"));
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("select * from employee");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const failure = page.getByTestId("sql-chat-send-error");
  await expect(failure.getByRole("alert")).toContainText(
    "サーバーに接続できませんでした。",
  );
  await expect(failure.getByRole("alert")).toContainText(
    "ネットワークの接続とサーバーの起動状態を確かめてから、もう一度実行してください。",
  );
  await expect(failure.locator("details")).toContainText("POST /api/nl2sql/jobs");
  await expect(failure.locator("p").first()).not.toContainText("Failed to fetch");
  // 送った質問は会話の欄に残し、入力欄は空のまま（#907）。
  await expect(
    page.getByTestId("sql-chat-pending-turn").locator('[data-status="failed"]'),
  ).toContainText("select * from employee");
  await expect(composer).toHaveValue("");
});

test("AI 活用の先頭のチャットは、生成だけの権限でも利用できる", async ({
  page,
}) => {
  await setup(page);
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: {
        data: {
          ...systemAdminMe,
          is_system_admin: false,
          role_codes: ["CHAT"],
          permissions: ["menu.chat"],
          allowed_profile_ids: ["sales"],
        },
      },
    }),
  );
  await page.goto("/chat");
  const sidebar = await openSidebarNav(page);
  await expect(
    sidebar.getByRole("link", { name: "チャット", exact: true }),
  ).toBeVisible();
  await expect(
    sidebar.getByRole("link", { name: "SQL 生成", exact: true }),
  ).toHaveCount(0);
  await closeSidebarNav(page);
  await page
    .getByRole("textbox", { name: "質問", exact: true })
    .fill("カテゴリ別売上");
  await page.getByTestId("sql-chat-send").click();
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
});

test("安全検査でブロックした SQL は未実行のまま表示する", { tag: "@responsive" }, async ({ page }) => {
  const state = await setup(page);
  state.turns.push({
    job_id: "chat-1",
    question: "許可対象外の表を参照",
    status: "error",
    created_at: now,
    steps: [],
    result: {
      generated_sql: "SELECT * FROM PRIVATE.TABLES",
      safety: { is_safe: false },
    },
    error_message: "許可された対象の範囲外です。",
    error_code: "SQL_BLOCKED",
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
  await page
    .getByTestId("sql-chat-history")
    .getByText("許可対象外の表を参照", { exact: true })
    .click();
  if ((page.viewportSize()?.width ?? 0) >= 1024)
    await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
  await expect(
    page.getByText("安全検査でブロック", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("許可された対象の範囲外です。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("安全検査済み・未実行", { exact: true }),
  ).toHaveCount(0);
});

test("利用できるプロファイルがないと会話の欄を出さず送信できない", async ({
  page,
}) => {
  const state = await setup(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({
      json: { data: { items: [], total: 0, next_cursor: null } },
    }),
  );
  await page.goto("/chat");
  // RAG・Agent のチャットと同じく、上部のカードに空の状態だけを出す（#891）。
  await expect(
    page.getByText("利用できる業務プロファイルがありません", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("#sql-chat-profile")).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-panel")).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "質問", exact: true }),
  ).toHaveCount(0);
  expect(state.requests).toHaveLength(0);
});

test("業務プロファイルの欄は RAG の検索・回答プロファイルと同じ形で、読み込み中は欄の形を出す", { tag: "@responsive" }, async ({
  page,
}) => {
  await setup(page);
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/nl2sql/profiles/search**", async (route) => {
    await released;
    await route.fulfill({
      json: { data: { items: [profile], total: 1, next_cursor: null } },
    });
  });
  await page.goto("/chat");
  // 読み込み中: ラベルと欄の形の Skeleton と経過時間（#891）。
  const loading = page.getByTestId("sql-chat-profiles-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText("業務プロファイルを読み込んでいます");
  release();
  await expect(page.getByTestId("sql-chat-profiles-loading")).toHaveCount(0);
  const field = page.locator("#sql-chat-profile");
  await expect(field).toBeVisible();
  await expect(field).toContainText("売上分析");
  // 必須の表示・カードの幅いっぱい（#635 / #891）。閉じたボタンに虫眼鏡は無く、値と開閉の印だけ（#1771）。
  await expect(field).toHaveAccessibleName(/必須/);
  await expect(field.locator("svg.lucide-search")).toHaveCount(0);
  await expect(field.locator("svg.lucide-chevron-down")).toBeVisible();
  const toolbar = page.locator("[data-chat-toolbar]");
  await expect(toolbar.locator("#sql-chat-profile")).toBeVisible();
  const fieldBox = (await field.boundingBox())!;
  expect(fieldBox.height).toBe(await expectedControlHeight(page, "sm"));
  await field.click();
  await expect(page.getByRole("option", { name: /売上分析/ })).toContainText(
    "表・ビュー 1 件",
  );
  // 虫眼鏡は開いたパネルの検索欄だけ（ボタンと検索欄で 2 つ並ばない。#1771）。
  await expect(
    page.locator("[data-searchable-select-popover] svg.lucide-search"),
  ).toHaveCount(1);
  await expect(field.locator("svg.lucide-search")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(field).toBeFocused();
});

test("生成方法は入力欄の直上で選び、Select AI Agent も送れる", { tag: "@responsive" }, async ({
  page,
  isMobile,
}) => {
  const state = await setup(page);
  await page.goto("/chat");
  const row = page.getByTestId("sql-chat-engine-row");
  const engine = page.getByRole("combobox", { name: "生成方法", exact: true });
  await expect(row).toContainText("生成方法");
  await expectSelectFieldValue(engine, "select_ai");
  // 選んだ生成方法の説明は常設せず、ラベルの横の info アイコンから出す（#901）。
  const description = page.getByTestId("sql-chat-engine-description");
  const info = row.getByRole("button", { name: "生成方法の説明", exact: true });
  await expect(description).toBeHidden();
  await expect(info).toHaveAccessibleDescription(/Oracle Select AI/);
  // マウスはポインタを乗せると出し、離すと閉じる。タッチ端末（ホバーが無い）はタップで出し、外側のタップで閉じる。
  if (isMobile) await info.tap();
  else await info.hover();
  await expect(description).toBeVisible();
  await expect(description).toContainText("Oracle Select AI");
  if (isMobile) await row.getByText("生成方法", { exact: true }).first().tap();
  else await page.mouse.move(0, 0);
  await expect(description).toBeHidden();
  // 入力欄の直上の行（RAG のチャットの「回答するモデル」と同じ位置。#890）。
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  const rowBox = (await row.boundingBox())!;
  const composerBox = (await composer.boundingBox())!;
  expect(rowBox.y + rowBox.height).toBeLessThanOrEqual(composerBox.y);
  expect(composerBox.y - (rowBox.y + rowBox.height)).toBeLessThan(24);
  // 選択欄にも、選んだ生成方法の説明を結び付ける（閉じている吹き出しの文を読む）。
  await expect(engine).toHaveAccessibleDescription(/Oracle Select AI/);
  expect((await engine.boundingBox())!.height).toBe(
    await expectedControlHeight(page, "sm"),
  );
  await engine.click();
  await expect(page.getByRole("option")).toHaveText([
    "Select AI",
    "Select AI Agent",
    "Enterprise AI",
  ]);
  await page.keyboard.press("Escape");
  await chooseSelectFieldOption(engine, "select_ai_agent");
  // キーボード: 選択欄から Shift+Tab で info アイコンへ戻るとフォーカスで出し、Escape で閉じる（フォーカスは動かさない）。
  await expect(engine).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(info).toBeFocused();
  // タッチ端末ではフォーカスだけでは出さないので、Enter で開く（マウス・キーボードの環境はフォーカスで出る）。
  if (isMobile) await page.keyboard.press("Enter");
  await expect(description).toBeVisible();
  await expect(description).toContainText(
    "Select AI Agent が SQL 生成ツールを呼び出して",
  );
  await page.keyboard.press("Escape");
  await expect(description).toBeHidden();
  await expect(info).toBeFocused();
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
  expect(state.requests[0]).toMatchObject({
    engine: "select_ai_agent",
    chat: true,
  });
  // 生成方法は作業状態に残る。
  await page.reload();
  await expectSelectFieldValue(
    page.getByRole("combobox", { name: "生成方法", exact: true }),
    "select_ai_agent",
  );
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
});

test("業務プロファイルの方針が「前提を示して SQL を生成する」なら、確認事項を聞かずに SQL を作る (#1839)", async ({
  page,
}) => {
  const state = await setup(page, { create: "needs_answer" });
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({ json: { data: { ...profile, clarification_policy: "assume" } } }),
  );
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
  expect(state.requirement.creates).toHaveLength(0);
  expect(state.requests[0]).toMatchObject({ question: "今年度の地域ごとの受注金額", chat: true });
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  // 利用者が飛ばしたのではないので、「確認せずに…」は残さない。
  await expect(page.getByTestId("sql-chat-turn-clarification")).toHaveCount(0);
});

test("生成方法の初期の選択は業務プロファイルの既定の生成方式 (#1524)", async ({
  page,
}) => {
  const state = await setup(page);
  // 後から登録した route が先に当たる。
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) =>
    route.fulfill({
      json: { data: { ...profile, default_engine: "enterprise_ai_direct" } },
    }),
  );
  await page.goto("/chat");
  const engine = page.getByRole("combobox", { name: "生成方法", exact: true });
  await expectSelectFieldValue(engine, "enterprise_ai_direct");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
  expect(state.requests[0]).toMatchObject({
    engine: "enterprise_ai_direct",
    chat: true,
  });
  // 利用者が選んだ方式は、業務プロファイルの既定より優先して作業状態に残る。
  await chooseSelectFieldOption(engine, "select_ai");
  await page.reload();
  await expectSelectFieldValue(
    page.getByRole("combobox", { name: "生成方法", exact: true }),
    "select_ai",
  );
});

// #907: 送った質問は、ジョブの投入の応答を待たずにすぐ会話の欄の末尾へ出す（楽観的な表示）。
/** ジョブの投入を止めておき、好きなときに setup の応答へ流す。 */
async function gateJobSubmit(page: Page) {
  let open: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  await page.route("**/api/nl2sql/jobs", async (route) => {
    await opened;
    await route.fallback();
  });
  return open;
}

async function expectConversationScrolledToEnd(page: Page) {
  await expect
    .poll(() =>
      page
        .getByTestId("sql-chat-conversation")
        .evaluate(
          (element) =>
            element.scrollHeight - element.clientHeight - element.scrollTop,
        ),
    )
    .toBeLessThanOrEqual(2);
}

test("送った質問はジョブの投入の応答を待たずに会話の欄へ出る（新しい会話。#907）", async ({
  page,
}, testInfo) => {
  await setup(page);
  const release = await gateJobSubmit(page);
  await page.goto("/chat");
  const empty = page.getByText("質問を入力して会話を始めます");
  await expect(empty).toBeVisible();
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);

  const pendingTurn = page.getByTestId("sql-chat-pending-turn");
  await expect(pendingTurn.locator('[data-status="sending"]')).toHaveText(
    "カテゴリ別売上",
  );
  await expect(
    // 投入の応答を待つ間は、送信の段階を今の段階として出す（#1145）。
    pendingTurn.getByTestId("sql-chat-progress-current"),
  ).toContainText("質問を送信しています");
  await expect(
    pendingTurn.getByTestId("sql-chat-progress-timer"),
  ).toBeVisible();
  await expect(empty).toHaveCount(0);
  await expect(page.getByRole("log", { name: "会話" })).toContainText(
    "カテゴリ別売上",
  );
  await expect(composer).toHaveValue("");
  await expect(composer).toBeFocused();
  // 処理中の表示は回答の場所の 1 つだけ（messaging.md §3.7）。
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
  await expect(pendingTurn.locator("svg.animate-spin:visible")).toHaveCount(1);
  await expectConversationScrolledToEnd(page);
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
  await page.getByTestId("sql-chat-panel").screenshot({
    path: testInfo.outputPath(`chat-sending-light.png`),
  });

  // 投入できたら、同じ位置のジョブの表示に置き換える（質問を二重に出さない）。
  release();
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
  await expect(pendingTurn).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  await expect(
    page
      .getByTestId("sql-chat-conversation")
      .getByText("カテゴリ別売上", { exact: true }),
  ).toHaveCount(1);
});

test("続きの会話でも、送った質問は投入の応答を待たずに末尾へ出る（#907）", async ({
  page,
}) => {
  await setup(page);
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();

  const release = await gateJobSubmit(page);
  await composer.fill("多い順にして");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const pendingTurn = page.getByTestId("sql-chat-pending-turn");
  await expect(pendingTurn.locator('[data-status="sending"]')).toHaveText(
    "多い順にして",
  );
  await expect(composer).toHaveValue("");
  const text = await page.getByTestId("sql-chat-conversation").innerText();
  expect(text.indexOf("カテゴリ別売上")).toBeGreaterThanOrEqual(0);
  expect(text.indexOf("カテゴリ別売上")).toBeLessThan(
    text.indexOf("多い順にして"),
  );
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
  await expectConversationScrolledToEnd(page);

  release();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
  await expect(pendingTurn).toHaveCount(0);
});

/**
 * 応答だけが届かないジョブの投入（backend はジョブを作り終える）。job ID は画面が決めた client_job_id で、
 * 同じ ID の再送は作成済みのジョブを返す（backend の `_replay_client_job` と同じ）。
 */
async function loseJobSubmitResponse(
  page: Page,
  state: Awaited<ReturnType<typeof setup>>,
) {
  const control = { lose: true };
  await page.route("**/api/nl2sql/jobs", (route) => {
    const body = route.request().postDataJSON();
    state.requests.push(body);
    const id = String(body.client_job_id);
    if (!state.turns.some((turn) => turn.job_id === id))
      state.turns.push({
        job_id: id,
        question: body.question,
        status: "done",
        created_at: now,
        steps: [],
        result: {
          generated_sql: "SELECT CATEGORY FROM APP.SALES",
          original_question: body.question,
          explanation: "",
          safety: { is_safe: true },
        },
      });
    if (control.lose) return route.abort("failed");
    return route.fulfill({
      json: {
        data: {
          job_id: id,
          status: "done",
          created_at: now,
          steps: [],
        },
      },
    });
  });
  // 取り直しも届かない（通信が戻る前）。
  await page.route("**/api/nl2sql/jobs/*", (route) => route.abort("failed"));
  return control;
}

test("応答が届かなかった送信のジョブが会話に入っていれば、質問を二重に出さず再送信も出さない (#900 / #907)", async ({
  page,
}) => {
  const state = await setup(page);
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();

  await loseJobSubmitResponse(page, state);
  await composer.fill("多い順にして");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  // 失敗の後に取り直した会話に、作成済みのジョブが入る。送った質問は 1 回だけ出す。
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(2);
  await expect(page.getByTestId("sql-chat-pending-turn")).toHaveCount(0);
  await expect(
    page
      .getByTestId("sql-chat-conversation")
      .getByText("多い順にして", { exact: true }),
  ).toHaveCount(1);
  await expect(page.getByRole("button", { name: "再送信" })).toHaveCount(0);
  expect(state.requests).toHaveLength(2);
  expect(state.turns).toHaveLength(2);
});

test("応答が届かなかった送信の再送信は同じ job ID で送り、作成済みのジョブを二重に生成しない (#900 / #907)", async ({
  page,
}) => {
  const state = await setup(page);
  const control = await loseJobSubmitResponse(page, state);
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const failure = page.getByTestId("sql-chat-send-error");
  await expect(failure).toBeVisible();

  control.lose = false;
  await failure.getByRole("button", { name: "再送信" }).click();
  await expect(page.getByText("安全検査済み・未実行")).toBeVisible();
  await expect(page.getByTestId("sql-chat-pending-turn")).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  expect(state.requests).toHaveLength(2);
  expect(state.requests[1]).toMatchObject({
    question: "カテゴリ別売上",
    client_job_id: state.requests[0].client_job_id,
  });
  expect(state.turns).toHaveLength(1);
});

test("会話の履歴と会話の読み込み中は、文言と経過時間を出し内容の形の Skeleton で覆う", async ({
  page,
}) => {
  const state = await setup(page);
  state.turns.push({
    job_id: "chat-1",
    question: "カテゴリ別売上",
    status: "done",
    created_at: now,
    steps: [],
    result: {
      generated_sql: "SELECT CATEGORY FROM APP.SALES",
      original_question: "カテゴリ別売上",
      explanation: "",
      safety: { is_safe: true },
    },
  });
  let release: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/nl2sql/chats**", async (route) => {
    await opened;
    await route.fallback();
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
  const historyLoading = page.getByTestId("sql-chat-history-loading");
  await expect(historyLoading).toContainText("会話の履歴を読み込んでいます");
  await expect(historyLoading.locator(".animate-pulse").first()).toBeVisible();
  release();
  await page
    .getByTestId("sql-chat-history")
    .getByRole("button", { name: /^カテゴリ別売上/ })
    .click();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);

  // 再読込で開いている会話を読み込む間（会話の内容の取得を止める）。
  let releaseConversation: () => void = () => undefined;
  const conversationOpened = new Promise<void>((resolve) => {
    releaseConversation = resolve;
  });
  await page.route("**/api/nl2sql/chats/*", async (route) => {
    await conversationOpened;
    await route.fallback();
  });
  await page.reload();
  const conversationLoading = page.getByTestId("sql-chat-conversation-loading");
  await expect(conversationLoading).toContainText("会話を読み込んでいます");
  await expect(
    conversationLoading.locator(".animate-pulse").first(),
  ).toBeVisible();
  // 会話の内容の読み込み中も入力欄と生成方法は使える（書いている途中で無効にしない）。送信だけを止める（#1188）。
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await expect(composer).toBeEnabled();
  await expect(page.locator("#sql-chat-engine")).toBeEnabled();
  await composer.fill("続きの質問");
  await expect(page.getByTestId("sql-chat-send")).toBeDisabled();
  releaseConversation();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  await expect(conversationLoading).toHaveCount(0);
  await expect(composer).toHaveValue("続きの質問");
  await expect(page.getByTestId("sql-chat-send")).toBeEnabled();
});

// #1145: 回答の場所に backend の処理の段階を出す（3 製品共通の ChatProgress）。
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString();
const pendingSteps = () =>
  ["prepare_context", "generate_sql", "safety_check", "execute_sql", "format_results"].map(
    (stage) => ({ stage, status: "pending" }),
  );

for (const width of [1280, 375]) {
  test(`回答の場所に開始待ち・実行中の段階を出し、完了後は「処理の経過」に畳む (#1145, ${width}px)`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page);
    state.pending = true;
    // 段階は進む方へだけ表示する（#1358）ので、開始待ちから始まるジョブを投入する（backend の start_job と同じ）。
    state.queued = true;
    await page.goto("/chat");
    // #1176: 段階が進む間、右上の経過時間（処理全体の通算）が減らないことを記録する。
    await startProgressTimerSampler(page, "sql-chat-progress");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("select * from employee");
    await composer.press("Enter");
    await confirmChatRequirement(page);
    const turn = page.getByTestId("sql-chat-turn");
    const progress = turn.getByTestId("sql-chat-progress");
    const current = progress.getByTestId("sql-chat-progress-current");

    await expect.poll(() => state.turns.length).toBe(1);
    // worker が始めていない間は「処理の開始を待っています」（生成ではなく開始で待っていることが分かる）。
    Object.assign(state.turns[0], {
      status: "pending",
      created_at: iso(2_000),
      steps: pendingSteps(),
    });
    await expect(current).toHaveAttribute("data-step-id", "queue");
    await expect(current).toContainText("処理の開始を待っています");
    await expect(progress.getByRole("status")).toHaveText("処理の開始を待っています");
    // 遅延の案内の行は、案内が出る前から見出しの直下に高さを予約する（#2426。出ても見出しが動かない）。
    const notice = progress.getByTestId("sql-chat-progress-notice");
    await expect(notice).toHaveAttribute("data-slow", "false");
    const reservedHeight = (await notice.boundingBox())?.height ?? 0;
    expect(reservedHeight).toBeGreaterThan(0);

    // 実行中: 今の段階 1 行（補足に生成方法）と、完了した段階の畳んだ見出し。遅延の案内は見出しの直下に出す。
    Object.assign(state.turns[0], {
      status: "running",
      created_at: iso(20_000),
      started_at: iso(19_000),
      steps: [
        { stage: "prepare_context", status: "done", started_at: iso(19_000), finished_at: iso(18_000) },
        { stage: "generate_sql", status: "running", started_at: iso(18_000) },
        { stage: "safety_check", status: "pending" },
        { stage: "execute_sql", status: "pending" },
        { stage: "format_results", status: "pending" },
      ],
    });
    await expect(current).toHaveAttribute("data-step-id", "generate_sql");
    await expect(current).toContainText("SQL を生成しています（Select AI）");
    // 遅延の案内は見出し「処理中（N ステップ完了）」の直下に、段階の名前を添えずに出す（#2426）。
    const slowNotice = progress.getByTestId("sql-chat-progress-slow");
    await expect(slowNotice).toHaveText("通常より時間がかかっています。");
    await expect(progress.getByTestId("sql-chat-progress-running").getByTestId("sql-chat-progress-slow")).toHaveCount(1);
    await expect(current.getByTestId("sql-chat-progress-slow")).toHaveCount(0);
    {
      const timerBox = await progress.getByTestId("sql-chat-progress-timer").boundingBox();
      const noticeBox = await slowNotice.boundingBox();
      // 案内が出ても予約した行の高さは変わらない（375px でも 1 行。見出し・一覧が動かない）。
      expect((await notice.boundingBox())?.height).toBe(reservedHeight);
      // 見出しの 2 行目（経過時間の行の下）で、左の見出しの側から始まる。
      expect(timerBox && noticeBox && noticeBox.y >= timerBox.y + timerBox.height - 1).toBeTruthy();
      expect(timerBox && noticeBox && noticeBox.x < timerBox.x).toBeTruthy();
    }
    await expect(progress.getByRole("status")).toHaveText("SQL を生成しています");
    const completed = progress.getByTestId("sql-chat-progress-running");
    await expect(completed).toContainText("処理中（2 ステップ完了）");
    // 動くスピナーは今の段階の 1 つだけ（messaging.md §3.7）。
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
    // 完了した段階の一覧は既定で開く（#2096。行が増えていく）。
    await expect(progress.locator("details").first()).toHaveAttribute("open", "");
    const prepared = progress.getByTestId("sql-chat-progress-step-prepare_context");
    await expect(prepared).toContainText("質問と参照できる表を整理しました");
    await expect(prepared).toContainText("1.0 秒");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBeLessThanOrEqual(0);
    await turn.screenshot({
      path: testInfo.outputPath(`chat-progress-running-light.png`),
    });

    // #1176: 経過時間は処理全体（送信）から数え、段階が変わっても 0 に戻さない。安全性の確認の後
    // （実行・結果の整形）も今の段階の行を出す（「N ステップ完了」だけにしない）。
    const timer = progress.getByTestId("sql-chat-progress-timer");
    await expect(timer).toHaveAttribute("aria-label", /経過時間 00:(0[2-9]|[1-5]\d)/);
    const seconds = async () =>
      Number((await timer.getAttribute("aria-label"))!.match(/(\d+):(\d+)$/)!.slice(1).reduce(
        (total, part) => total * 60 + Number(part),
        0,
      ));
    const before = await seconds();
    Object.assign(state.turns[0], {
      // 安全性の確認の完了で backend が記録する参照した表（mock の backend が補足の値にする。#1359）。
      referenced_tables: ["APP.EMPLOYEE"],
      steps: [
        { stage: "prepare_context", status: "done", started_at: iso(19_000), finished_at: iso(18_000) },
        { stage: "generate_sql", status: "done", started_at: iso(18_000), finished_at: iso(2_000) },
        { stage: "safety_check", status: "done", started_at: iso(2_000), finished_at: iso(1_000) },
        { stage: "execute_sql", status: "done", started_at: iso(1_000), finished_at: iso(0) },
        { stage: "format_results", status: "running", started_at: iso(0) },
      ],
    });
    await expect(current).toHaveAttribute("data-step-id", "format_results");
    await expect(current).toContainText("SQL の実行結果を表示用に整えています");
    await expect(completed).toContainText("処理中（5 ステップ完了）");
    // 一度出た遅延の案内は、始まったばかりの段階に変わっても処理が終わるまで消さない（#2426）。
    await expect(slowNotice).toHaveText("通常より時間がかかっています。");
    expect(await seconds()).toBeGreaterThanOrEqual(before);
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
    // 送信・開始待ち・生成・結果の整形の段階を通る間、経過時間は減らない。記録（100ms ごと）が
    // 結果の整形の段階を拾ってから確かめる（開始待ちから始まるので、通る段階は開始待ち・生成・整形）。
    await expect
      .poll(() =>
        page.evaluate(() =>
          (window as unknown as { __progressTimerSamples: string[] }).__progressTimerSamples.some(
            (sample) => sample.startsWith("format_results|"),
          ),
        ),
      )
      .toBe(true);
    await expectProgressTimerMonotonic(page, 3);

    // 完了: 回答の上に「処理の経過（N ステップ・M 秒）」の一覧を既定で開いて出す（#2096）。
    Object.assign(state.turns[0], {
      status: "done",
      finished_at: iso(0),
      steps: [
        { stage: "prepare_context", status: "done", started_at: iso(19_000), finished_at: iso(18_000) },
        { stage: "generate_sql", status: "done", started_at: iso(18_000), finished_at: iso(2_000) },
        { stage: "safety_check", status: "done", started_at: iso(2_000), finished_at: iso(1_000) },
        { stage: "execute_sql", status: "done", started_at: iso(1_000), finished_at: iso(500) },
        { stage: "format_results", status: "done", started_at: iso(500), finished_at: iso(0) },
      ],
      result: {
        generated_sql: "SELECT * FROM APP.EMPLOYEE",
        original_question: "select * from employee",
        explanation: "",
        safety: { is_safe: true, referenced_tables: ["APP.EMPLOYEE"] },
      },
    });
    await expect(turn.getByText("安全検査済み・未実行")).toBeVisible();
    const summary = progress.getByTestId("sql-chat-progress-summary");
    await expect(summary).toHaveText(/^処理の経過（6 ステップ・2\d 秒）$/);
    await expect(progress).toHaveAttribute("data-chat-progress-state", "done");
    // 完了したら閉じ（3 製品で同じ。#2118 / #2265）、押すと実行の詳細と同じ一覧（時間の線つき）を開く。
    await expect(progress.locator("details").first()).not.toHaveAttribute("open", "");
    await expect(progress.getByTestId("sql-chat-progress-step-safety_check")).toBeHidden();
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(0);
    await summary.click();
    await expect(progress.locator("details").first()).toHaveAttribute("open", "");
    await expect(progress.getByTestId("sql-chat-progress-step-safety_check")).toContainText(
      "SQL の種類と参照範囲を検査しました",
    );
    const bars = progress.locator("[data-run-timeline-bar]");
    await expect(bars).toHaveCount(6);
    if (width >= 1280) {
      // 時間の線は全体の中の位置と長さ（広い置き場所だけ。375px は隠して所要時間の文字だけ）。
      await expect(bars.first()).toBeVisible();
    } else {
      await expect(bars.first()).toBeHidden();
    }
    await expect(progress.getByTestId("sql-chat-progress-step-safety_check")).toContainText(/\d(\.\d)? 秒/);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBeLessThanOrEqual(0);
    await turn.screenshot({
      path: testInfo.outputPath(`chat-progress-done-light.png`),
    });
  });
}

test("生成に失敗した段階は「処理の経過」を開いて失敗を文字とアイコンで出す (#1145)", async ({
  page,
}, testInfo) => {
  const state = await setup(page);
  state.pending = true;
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("select * from employee");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const turn = page.getByTestId("sql-chat-turn");
  await expect(turn.getByTestId("sql-chat-progress-current")).toBeVisible();
  Object.assign(state.turns[0], {
    status: "error",
    created_at: iso(6_000),
    started_at: iso(5_000),
    finished_at: iso(0),
    error_message: "Select AI で SQL を生成できませんでした。",
    steps: [
      { stage: "prepare_context", status: "done", started_at: iso(5_000), finished_at: iso(4_000) },
      { stage: "generate_sql", status: "error", started_at: iso(4_000), finished_at: iso(0) },
      { stage: "safety_check", status: "pending" },
      { stage: "execute_sql", status: "pending" },
      { stage: "format_results", status: "pending" },
    ],
  });
  const progress = turn.getByTestId("sql-chat-progress");
  await expect(progress).toHaveAttribute("data-chat-progress-state", "failed");
  await expect(progress.locator("details").first()).toHaveAttribute("open", "");
  const failed = progress.getByTestId("sql-chat-progress-step-generate_sql");
  await expect(failed).toHaveAttribute("data-status", "failed");
  await expect(failed).toContainText("SQL を生成できませんでした");
  await expect(failed).toContainText("失敗");
  // 後の段は始まっていないので span が無い（行を出さない。#3226）。
  await expect(progress.getByTestId("sql-chat-progress-step-safety_check")).toHaveCount(0);
  await expect(turn.getByText("Select AI で SQL を生成できませんでした。")).toBeVisible();
  // 失敗は 3 製品共通の失敗のカード: 止まった段・原因・次の操作・開いた「詳細」（#2342）。
  const failure = turn.getByTestId("sql-chat-turn-failure");
  await expect(failure.getByRole("alert")).toContainText("「SQL の生成」で処理が止まりました。");
  await expect(failure).toContainText("ジョブ ID を添えて管理者に連絡してください");
  await expect(failure.locator("details")).toHaveAttribute("open", "");
  await expect(failure.locator("details")).toContainText("止まった段");
  await turn.screenshot({ path: testInfo.outputPath("chat-progress-failed.png") });
});

test("上を読んでいる間に回答が届いても引き戻さず「最新のメッセージへ」を出し、押すと末尾へ戻る (#1161)", async ({
  page,
}) => {
  const state = await setup(page);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  const log = page.getByRole("log", { name: "会話" });
  const distanceFromBottom = () =>
    log.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
  // 会話の欄がスクロールするだけの往復を作る。
  // 送信の前に要件の検査があるので（#1442）、ジョブは検査の後に投入される。数は送った回数で待つ。
  for (const [index, question] of ["売上", "地域別", "月別", "商品別", "前年比"].entries()) {
    await composer.fill(question);
    await composer.press("Enter");
    await confirmChatRequirement(page);
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(index + 1);
  }
  state.pending = true;
  await composer.fill("担当者別");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect(page.getByTestId("sql-chat-send")).toHaveAccessibleName("停止");
  // 送った直後は末尾にいる。
  await expect.poll(distanceFromBottom).toBeLessThan(48);
  const latest = page.getByTestId("sql-chat-latest");
  await expect(latest).toHaveCount(0);

  // 上を読んでいる間に回答が届く（ポーリングで状態が変わる）。
  await log.evaluate((el) => el.scrollTo({ top: 0 }));
  const running = state.turns.at(-1)!;
  Object.assign(running, {
    status: "done",
    result: {
      generated_sql: "SELECT OWNER, SUM(AMOUNT) FROM APP.SALES GROUP BY OWNER",
      original_question: running.question,
      explanation: "担当者ごとの売上合計です。",
      safety: { is_safe: true },
    },
  });
  await expect(page.getByTestId("sql-chat-send")).toHaveAccessibleName("送信");
  await expect(latest).toBeVisible();
  await expect(latest).toHaveAccessibleName("最新のメッセージへ");
  // 引き戻さない。
  expect(await log.evaluate((el) => el.scrollTop)).toBeLessThan(48);

  await latest.click();
  await expect.poll(distanceFromBottom).toBeLessThan(48);
  await expect(latest).toHaveCount(0);
});

// #1359・#3226: 処理の段階は worker が共通の span の表に書いた span を読み直して受け取る（会話の取り直しを待たない）。
for (const width of [1280, 375]) {
  test(`処理中の段階を span の表から受け取り、会話の取り直しを待たない (#1359・#3226, ${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page);
    state.queued = true;
    const createdAt = iso(5_000);
    const generating = jobProgressEvents("chat-1", {
      at: createdAt,
      steps: {
        queue: "done",
        prepare_context: "done",
        generate_sql: { status: "running", params: { engine: "select_ai" } },
      },
    });
    // span は worker が共通の span の表に書く（会話のターンの保存を待たない）。画面は処理の経過の API を読み直す。
    let traceReads = 0;
    page.on("request", (request) => {
      if (/\/api\/nl2sql\/jobs\/chat-1\/trace/.test(request.url())) traceReads += 1;
    });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("カテゴリ別売上");
    await composer.press("Enter");
    await confirmChatRequirement(page);
    await expect.poll(() => state.turns.length).toBe(1);
    // 会話のターンは実行中のまま（段階を持たない）。span の表には生成の途中まで入っている。
    Object.assign(state.turns[0], {
      status: "running",
      engine: "select_ai",
      created_at: createdAt,
      started_at: createdAt,
    });
    recordMockTrace("chat-1", generating, { pinned: true });
    const current = page.getByTestId("sql-chat-turn").getByTestId("sql-chat-progress-current");
    await expect(current).toHaveAttribute("data-step-id", "generate_sql");
    await expect(current).toContainText("SQL を生成しています（Select AI）");
    expect(traceReads).toBeGreaterThan(0);
    // 完了は会話の取り直しで届く（ジョブの状態が正本）。
    const generatedSql = "SELECT CATEGORY, SUM(AMOUNT) FROM APP.SALES GROUP BY CATEGORY";
    // 終わった段階の詳細は段階の終わりの記録で届き、後の段階の実行中から開ける（ジョブの終わりを待たない。#2373）。
    recordMockTrace(
      "chat-1",
      jobProgressEvents("chat-1", {
        at: createdAt,
        steps: {
          queue: "done",
          prepare_context: "done",
          generate_sql: {
            status: "done",
            params: { engine: "select_ai" },
            details: { code: [{ label: "生成した SQL", language: "sql", text: generatedSql }] },
          },
          safety_check: "running",
        },
      }),
      { pinned: true },
    );
    const liveProgress = page.getByTestId("sql-chat-turn").getByTestId("sql-chat-progress");
    await expect(current).toHaveAttribute("data-step-id", "safety_check");
    const liveToggle = liveProgress.getByTestId("sql-chat-progress-step-generate_sql-toggle");
    await liveToggle.click();
    await expect(liveToggle).toHaveAttribute("aria-expanded", "true");
    await expect(
      liveProgress.getByTestId("sql-chat-progress-step-generate_sql-details").getByLabel("生成した SQL"),
    ).toHaveText(generatedSql);
    await expect(current).toHaveAttribute("data-step-id", "safety_check");
    // 終わった段階には backend が段を開いて見る詳細を付ける（#2294。生成した SQL・安全性の確認の結果）。
    recordMockTrace(
      "chat-1",
      jobProgressEvents("chat-1", {
        at: createdAt,
        terminal: "done",
        steps: {
          queue: "done",
          prepare_context: "done",
          generate_sql: {
            status: "done",
            params: { engine: "select_ai" },
            details: { code: [{ label: "生成した SQL", language: "sql", text: generatedSql }] },
          },
          safety_check: {
            status: "done",
            params: { tables: "APP.SALES", table_count: 1 },
            details: { fields: [{ label: "結果", value: "安全" }, { label: "参照した表", value: "APP.SALES", mono: true }] },
          },
          execute_sql: { status: "done", params: { rows: 3 } },
          format_results: "done",
        },
      }),
      { pinned: true },
    );
    Object.assign(state.turns[0], {
      status: "done",
      finished_at: iso(0),
      steps: [],
      result: {
        generated_sql: generatedSql,
        original_question: "カテゴリ別売上",
        explanation: "",
        safety: { is_safe: true, referenced_tables: ["APP.SALES"] },
      },
    });
    const progress = page.getByTestId("sql-chat-turn").getByTestId("sql-chat-progress");
    await expect(progress).toHaveAttribute("data-chat-progress-state", "done");
    await expect(progress.getByTestId("sql-chat-progress-summary")).toHaveText(/^処理の経過（6 ステップ・/);
    // 詳細のある段だけに開閉のボタンが付き、既定で閉じる（#2294）。
    await progress.getByTestId("sql-chat-progress-summary").click();
    // span の行は開くと span の名前（技術の値）も見られる（#3226）。既定で閉じる。
    await expect(progress.getByTestId("sql-chat-progress-step-queue-toggle")).toHaveCount(0);
    // 実行中に開いた段（生成）は、完了した一覧でも利用者の開閉のまま（処理の詳細が続く一覧は同じ行を保つ）。
    const toggle = progress.getByTestId("sql-chat-progress-step-generate_sql-toggle");
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(progress.getByTestId("sql-chat-progress-step-generate_sql-details").getByLabel("生成した SQL")).toHaveText(
      generatedSql,
    );
    await progress.getByTestId("sql-chat-progress-step-safety_check-toggle").click();
    await expect(progress.getByTestId("sql-chat-progress-step-safety_check-details")).toContainText("安全");
    expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
}

// #1442: チャットで送った質問は、SQL を作る前に要件を自動で検査し、確認が要るときは会話の中で 1 問ずつ聞く。
// 全部に答えたら、まとめ（書き直した質問・SQL の骨組み）を確かめてから SQL を作る。
const noHorizontalOverflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

/** 縦に長い領域を切らずに撮る（幅はそのまま、高さだけを一時的に広げる）。 */
async function screenshotWhole(page: Page, locator: Locator, path: string) {
  const viewport = page.viewportSize();
  const box = await locator.boundingBox();
  if (viewport && box && box.height + 200 > viewport.height) {
    await page.setViewportSize({ width: viewport.width, height: Math.ceil(box.height + 400) });
    await locator.scrollIntoViewIfNeeded();
    await locator.screenshot({ path });
    await page.setViewportSize(viewport);
    return;
  }
  await locator.screenshot({ path });
}

// #1718: チャットの「SQL の要件（AI の理解）」は SQL 生成の画面と同じ部品・同じ振る舞い（違いは置き場所と幅）。
test(`チャットの要件は要確認の行で候補を押して答え、答えたら畳んだ要約にする (#1718)`, { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const viewport = testInfo.project.name;
  const state = await setup(page, {
    create: "date_column",
    answers: ["date_column_basis_answered", "date_column_answered"],
  });
  await page.goto("/chat");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("直近 1 年の社員");
  await composer.press("Enter");

  const turn = page.getByTestId("sql-chat-requirement-turn");
  const requirements = turn.getByTestId("nl2sql-clarification-requirements");
  await expect(requirements).toHaveAttribute("data-needs-answer", "true");
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText("要確認 2 件");
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question).toHaveAttribute("data-item-key", "period_date_column");
  const row = requirements.locator('[data-testid="nl2sql-requirement-item"][data-item-key="period_reference_basis"]');
  await expect(row.getByText("「直近 1 年」は、いつから数えますか？")).toBeVisible();
  await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toContainText(
    "指定なし: 結合・絞り込み・集計・並び順・サブクエリ・その他"
  );
  expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  await screenshotWhole(page, turn, testInfo.outputPath(`chat-date-column-needs-answer-${viewport}-light.png`));

  await row.getByRole("button", { name: "「実行日（2026-10-11）から」と答える" }).click();
  await expect.poll(() => state.requirement.answers.length).toBe(1);
  expect(state.requirement.answers[0]).toMatchObject({
    item_id: "item-e-basis",
    selected_candidate_ids: ["candidate-basis-execution"],
    free_text: "",
  });
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText("要確認 1 件");
  await question.getByRole("radio", { name: /EMPLOYEE\.HIRE_DATE/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();
  await expect.poll(() => state.requirement.answers.length).toBe(2);
  await expect(requirements).toHaveAttribute("data-needs-answer", "false");
  const toggle = requirements.getByTestId("nl2sql-clarification-requirements-toggle");
  await expect(toggle).toContainText("確認の必要なし");
  await expect(toggle).toContainText("使用テーブル ADMIN.EMPLOYEE ／ 表示列 ADMIN.EMPLOYEE.*");
  await expect(turn.getByRole("button", { name: /この内容で SQL を生成/ })).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  await screenshotWhole(page, turn, testInfo.outputPath(`chat-date-column-answered-${viewport}-light.png`));
  await toggle.click();
  await expect(requirements.getByTestId("nl2sql-requirement-item")).toHaveCount(5);
  await screenshotWhole(page, turn, testInfo.outputPath(`chat-date-column-answered-open-${viewport}-light.png`));
});

// desktop（1280px）と 375px は playwright の project で分ける。
test("チャットで確認の質問・回答・まとめ・骨組みを経て SQL を作る (#1442)", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const width = page.viewportSize()?.width ?? 0;
  const state = await setup(page, { create: "needs_answer", answers: ["ready"] });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");

  // 「回答前の確認」のボタンは無く、送ると会話の中に確認の質問が出る。
  await expect(page.getByRole("button", { name: "回答前の確認", exact: true })).toHaveCount(0);
  const turn = page.getByTestId("sql-chat-requirement-turn");
  await expect(turn.getByText("今年度の地域ごとの受注金額", { exact: true })).toBeVisible();
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  await expect(question.getByText("確認事項 1 / 1")).toBeVisible();
  // チャットは「確認をやめる」を出さず、確認を終えるのは「確認せずに SQL を生成」だけ（#2303）。
  await expect(turn.getByRole("button", { name: "確認せずに SQL を生成" })).toBeVisible();
  await expect(turn.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  // 確認に答えるまでは次の質問を送らない（ジョブも作らない）。
  await expect(page.getByTestId("sql-chat-send")).toBeDisabled();
  expect(state.requests).toHaveLength(0);
  expect(state.requirement.creates[0]).toMatchObject({ chat: true, previous_job_id: null });
  // 回答前の確認の表示は SQL 生成の画面と同じ部品（#1715）。SQL の要件（AI の理解）も同じ部品（#1718）で、
  // 要確認があるので開き、物理名の一覧を見られる。
  await expect(turn.getByTestId("nl2sql-guided-clarification")).toBeVisible();
  const requirements = turn.getByTestId("nl2sql-clarification-requirements");
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toHaveText(/SQL の要件（AI の理解）/);
  await expect(requirements).toHaveAttribute("data-needs-answer", "true");
  await expect(
    requirements.locator('[data-testid="nl2sql-requirement-item"][data-item-key="select_region_total"]')
  ).toContainText("CUSTOMERS.REGION");
  expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  await turn.screenshot({ path: testInfo.outputPath(`chat-requirement-question-${width}.png`) });

  await question.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();
  await expect.poll(() => state.requirement.answers.length).toBe(1);
  expect(state.requirement.answers[0]).toMatchObject({
    item_id: "item-period",
    selected_candidate_ids: ["candidate-fy-april"],
    free_text: "",
  });

  // 全部に答えたら、まとめ（書き直した質問・SQL の骨組み）を確かめてから SQL を作る。
  const summary = turn.getByTestId("nl2sql-clarification-summary");
  await expect(summary.getByTestId("nl2sql-clarified-question")).toHaveText(REQUIREMENT_CLARIFIED_QUESTION);
  await expect(summary.getByTestId("nl2sql-clarification-skeleton")).toContainText("GROUP BY CUSTOMERS.REGION");
  await expect(turn.getByTestId("nl2sql-clarification-question")).toHaveCount(0);
  expect(state.requests).toHaveLength(0);
  expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  await turn.screenshot({ path: testInfo.outputPath(`chat-requirement-summary-${width}.png`) });
  await turn.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();

  // 要件（session）を付けて SQL を生成する。吹き出しは利用者の質問のまま。
  const answered = page.getByTestId("sql-chat-turn");
  await expect(answered).toHaveCount(1);
  await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
  expect(state.requirement.applies).toBe(1);
  expect(state.requests[0]).toMatchObject({
    chat: true,
    question: "今年度の地域ごとの受注金額",
    requirement_session_id: REQUIREMENT_SESSION_ID,
  });
  await expect(answered.getByText("今年度の地域ごとの受注金額", { exact: true })).toBeVisible();
  // 回答前に確認した内容は、利用者の発言として質問の下に残す（#1839）。
  const record = answered.getByTestId("sql-chat-turn-clarification");
  await expect(record.getByRole("group", { name: "SQL の生成前に確認した内容" })).toBeVisible();
  await expect(record).toContainText("今年度の期間");
  await expect(record).toContainText("2026-04-01〜2027-03-31");
  const applied = answered.getByTestId("sql-chat-turn-requirement");
  await expect(applied).toHaveText(/SQL の生成前に確認した内容/);
  await applied.click();
  await expect(answered.getByText(REQUIREMENT_CLARIFIED_QUESTION)).toBeVisible();
  // 回答して確定しても、入力欄に確かめた質問の文を入れない（#1727）。
  await expect(composer).toHaveValue("");
  // SQL を作ったら次の質問を送れる。
  await composer.fill("多い順にして");
  await expect(page.getByTestId("sql-chat-send")).toBeEnabled();
});

test("確認の質問は複数選択・自由入力で答えられ、答えるたびに次の質問へ進む (#1442)", async ({ page }) => {
  const state = await setup(page, { create: "multi_select", answers: ["needs_answer", "ready"] });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("状態ごとの今年度の受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("heading", { name: "どの受注状態（ORDERS.STATUS）に絞りますか？" })).toBeVisible();
  await question.getByRole("checkbox", { name: /出荷済み（SHIPPED）/ }).check();
  await question.getByRole("checkbox", { name: /取消（CANCELLED）/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();
  const next = question.getByRole("heading", { name: "「今年度」は、どの期間ですか？" });
  await expect(next).toBeVisible();
  // 新しい質問の見出しにフォーカスを移す（読み上げで次の質問が分かる）。
  await expect(next).toBeFocused();
  await question.getByRole("textbox", { name: "その他（自由入力）" }).fill("2026 年度（4 月始まり）");
  await question.getByRole("button", { name: "この内容で回答" }).click();
  await expect(turn.getByTestId("nl2sql-clarification-summary")).toBeVisible();
  expect(state.requirement.answers).toMatchObject([
    { selected_candidate_ids: ["candidate-shipped", "candidate-cancelled"], free_text: "" },
    { selected_candidate_ids: [], free_text: "2026 年度（4 月始まり）" },
  ]);
  expect(state.requests).toHaveLength(0);
});

test("候補の無い確認事項は、自由入力だけの言い方で聞く (#3366)", async ({ page }, testInfo) => {
  const state = await setup(page, { create: "free_text_question", answers: ["ready"] });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  await expect(question.getByRole("radio")).toHaveCount(0);
  // 候補が無いので「その他」「選択肢に無い」と言わない。
  const answer = question.getByRole("textbox", { name: "答え", exact: true });
  await expect(answer).toHaveAttribute("placeholder", /^答えを入力してください。/);
  await expect(question.getByText("答えを書くと押せます。")).toBeVisible();
  await expect(question.getByRole("textbox", { name: "その他（自由入力）" })).toHaveCount(0);
  await expect(question).not.toContainText("選択肢");
  await question.screenshot({ path: testInfo.outputPath("nl2sql-clarification-free-text.png") });
  await answer.fill("2026 年度（4 月始まり）");
  await question.getByRole("button", { name: "この内容で回答" }).click();
  await expect(turn.getByTestId("nl2sql-clarification-summary")).toBeVisible();
  expect(state.requirement.answers).toMatchObject([{ selected_candidate_ids: [], free_text: "2026 年度（4 月始まり）" }]);
});

test("確認を終えると、質問を取り消さずに確認せずに SQL を生成する (#2303)", async ({ page }) => {
  const state = await setup(page, { create: "needs_answer" });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question).toBeVisible();
  await expect(composer).toHaveValue("");
  await expect(turn.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  // 同じ操作のボタンを 2 つ並べない（確認事項のカードのスキップだけ）。
  await expect(turn.getByRole("button", { name: "確認せずに SQL を生成" })).toHaveCount(1);
  await question.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(turn).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  // 開いていた確認はサーバーで中止し、質問のまま（要件を使わずに）SQL を生成する。入力欄には戻さない。
  expect(state.requirement.cancels).toBe(1);
  expect(state.requests[0]).toMatchObject({ question: "今年度の地域ごとの受注金額", requirement_skipped: true });
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  await expect(composer).toHaveValue("");
  await expect(page.getByTestId("sql-chat-turn-clarification")).toContainText(
    "確認せずに SQL を生成しました（前提を示して SQL を生成）",
  );
});

test("要件の下書きの最中も、経過の行の「確認せずに SQL を生成」で待たずに SQL を生成する (#2303)", async ({ page }) => {
  const state = await setup(page, { create: "needs_answer", createDelayMs: 2_000 });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  const progress = turn.getByTestId("nl2sql-guided-start-progress");
  await expect(progress).toContainText("SQL の要件の下書きを作っています");
  await expect(turn.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  await progress.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(turn).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  expect(state.requests[0]).toMatchObject({ question: "今年度の地域ごとの受注金額", requirement_skipped: true });
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  // 下書きの応答が届いたら、その確認をサーバーで中止する（確認のカードは出さない）。
  await expect.poll(() => state.requirement.cancels, { timeout: 10_000 }).toBe(1);
  await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
  expect(state.requests).toHaveLength(1);
});

test("「この内容で回答」を押すと、フォームと警告を消して答えた内容と経過の行 1 つだけを出す (#2311)", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const width = page.viewportSize()?.width ?? 0;
  await setup(page, { create: "minimum", answers: ["ready"], answerDelayMs: 3_000 });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("受注の一覧");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  const question = turn.getByTestId("nl2sql-clarification-question");
  const warning = turn.getByText("SELECT（何を表示するか）と FROM（どの表から）が明確になるまで、SQL の生成に進めません。");
  await expect(question.getByRole("heading", { name: "どの表のデータを使いますか？" })).toBeVisible();
  await expect(warning).toBeVisible();
  await question.getByRole("checkbox", { name: /APP\.ORDERS（受注）/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();

  const card = question.getByTestId("nl2sql-clarification-card");
  const progress = card.getByTestId("nl2sql-clarification-card-progress");
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("回答を反映しています");
  await expect(progress.getByRole("timer")).toContainText(/00:0[01]/);
  await expect(progress.getByRole("button", { name: "確認せずに SQL を生成" })).toBeVisible();
  // 送ったフォーム（選択肢・自由入力・操作の行）と、送った確認事項の警告は出さない。
  await expect(card.getByRole("checkbox")).toHaveCount(0);
  await expect(card.getByRole("textbox", { name: "その他（自由入力）" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "この内容で回答" })).toHaveCount(0);
  await expect(warning).toHaveCount(0);
  // 答えた内容の記録。スピナーは確認の中に 1 つだけ（1 つの処理にスピナー 1 つ）。
  const answered = card.getByTestId("nl2sql-clarification-card-answered-entry");
  await expect(answered).toHaveCount(1);
  await expect(answered.locator("dt")).toHaveText("どの表のデータを使いますか？");
  await expect(answered.locator("dd")).toHaveText("APP.ORDERS（受注）");
  await expect(turn.locator(".pr-spinner")).toHaveCount(1);
  await expect(turn.getByTestId("nl2sql-guided-answer-progress")).toHaveCount(0);
  expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  await turn.screenshot({ path: testInfo.outputPath(`chat-requirement-confirming-${width}.png`) });

  // 応答が届いたら次の状態（ここでは確認の完了）を出し直す。
  await expect(turn.getByTestId("nl2sql-clarification-summary")).toBeVisible({ timeout: 10_000 });
  await expect(progress).toHaveCount(0);
  await expect(warning).toHaveCount(0);
});

test("回答の後の再検査の経過時間は回答を送った時から数え、待たずに SQL を生成できる (#2303)", async ({ page }) => {
  const state = await setup(page, {
    create: "needs_answer",
    createDelayMs: 2_500,
    answers: ["ready"],
    answerDelayMs: 5_000,
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  await expect(turn.getByTestId("nl2sql-guided-start-progress-timer")).toContainText(/00:0[2-9]/);
  const question = turn.getByTestId("nl2sql-clarification-question");
  await expect(question).toBeVisible();
  // 下書きが終わったら経過の表示を外す（「下書きを作っています」に戻らない）。
  await expect(turn.getByTestId("nl2sql-guided-start-progress")).toHaveCount(0);
  await question.getByRole("textbox", { name: "その他（自由入力）" }).fill("2026 年度（4 月始まり）");
  await question.getByRole("button", { name: "この内容で回答" }).click();
  // 確認のカードはフォームを答えた内容と経過の行 1 つに置き換える（#2311）。
  const answerProgress = turn.getByTestId("nl2sql-clarification-card-progress");
  await expect(answerProgress).toBeVisible();
  await expect(turn.getByTestId("nl2sql-guided-answer-progress")).toHaveCount(0);
  // 下書きの時間・答えていた時間を足さず、回答を送った時から数える。
  await expect(answerProgress.getByRole("timer")).toContainText(/00:0[01]/);
  await expect(turn.getByTestId("nl2sql-guided-start-progress")).toHaveCount(0);
  await answerProgress.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(turn).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  expect(state.requests[0]).toMatchObject({ requirement_skipped: true });
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  expect(state.requirement.cancels).toBe(1);
  // 取りやめた回答の応答・失敗は出さない。
  await page.waitForTimeout(5_500);
  await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
  await expect(page.getByText("回答を反映できませんでした。")).toHaveCount(0);
  expect(state.requests).toHaveLength(1);
});

test("要件の検査の最中は送信のボタンが「停止」になり、押すと確認を中止する (#1442)", async ({ page }) => {
  const state = await setup(page, { create: "needs_answer", createDelayMs: 2_000 });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const turn = page.getByTestId("sql-chat-requirement-turn");
  // 検査の経過は SQL 生成の画面の回答前の確認と同じ表示（#1715）。
  await expect(turn.getByTestId("nl2sql-guided-start-progress")).toContainText("SQL の要件の下書きを作っています");
  const send = page.getByTestId("sql-chat-send");
  await expect(send).toHaveAccessibleName("停止");
  await send.click();
  await expect(turn).toHaveCount(0);
  await expect(composer).toHaveValue("今年度の地域ごとの受注金額");
  // 作成の応答が届いたら、その確認をサーバーで中止する。
  await expect.poll(() => state.requirement.cancels, { timeout: 10_000 }).toBe(1);
  expect(state.requests).toHaveLength(0);
});

test("要件の検査ができなかったときは、もう一度確認するか、確認せずに SQL を生成できる (#1442)", async ({ page }) => {
  const state = await setup(page, {
    createFailure: { status: 503, error: "回答前の確認の LLM の呼び出しに失敗しました。(REQUIREMENT_AI_FAILED)" },
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  const error = page.getByTestId("nl2sql-guided-clarification-error");
  await expect(error).toContainText("回答前の確認の LLM の呼び出しに失敗しました。");
  await expect(error).toContainText("もう一度確認するか、確認せずに SQL を生成してください。");
  await expect(error.getByRole("button", { name: "もう一度確認" })).toBeVisible();
  await error.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  expect(state.requests[0]).toMatchObject({ question: "カテゴリ別売上", chat: true, requirement_skipped: true });
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  // 確認を飛ばしたことを、利用者の発言として質問の下に残す（#1839）。
  await expect(page.getByTestId("sql-chat-turn-clarification")).toContainText(
    "確認せずに SQL を生成しました（前提を示して SQL を生成）",
  );
});

test("要件の検査の失敗は、理由と「詳細」（種類・HTTP ステータス・モデル・所要時間・request ID）を出す (#1715)", async ({ page }) => {
  await setup(page, {
    createFailure: {
      status: 503,
      error: "",
      body: {
        data: null,
        error_messages: [
          "回答前の確認の LLM（openai.gpt-oss-120b）の応答が、出力の上限（8000 token）に達して途中で終わりました。",
        ],
        error_code: "REQUIREMENT_AI_FAILED",
        problem: {
          type: "urn:nl2sql:problem:REQUIREMENT_AI_FAILED",
          title: "Service Unavailable",
          status: 503,
          detail: "回答前の確認の LLM の応答が途中で終わりました。",
          code: "REQUIREMENT_AI_FAILED",
          request_id: "req-chat-1",
          retryable: true,
          field_errors: [],
        },
        error_details: {
          kind: "incomplete",
          kind_label_ja: "応答が途中で終わった",
          model_id: "openai.gpt-oss-120b",
          elapsed_ms: 35600,
          max_output_tokens: 8000,
          upstream_request_id: "resp_incomplete_1",
          provider_message: "status=incomplete, reason=max_output_tokens",
        },
      },
    },
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("最近入社した社員");
  await composer.press("Enter");
  const error = page.getByTestId("nl2sql-guided-clarification-error");
  await expect(error).toContainText("出力の上限（8000 token）に達して途中で終わりました。");
  await expect(error).toContainText("もう一度確認するか、確認せずに SQL を生成してください。");
  await expect(error).toContainText("応答が途中で終わった");
  await expect(error).toContainText("openai.gpt-oss-120b");
  await expect(error).toContainText("35.6 秒");
  await expect(error).toContainText("8000 token");
  await expect(error).toContainText("resp_incomplete_1");
  await expect(error).toContainText("req-chat-1");
  await expect(error).toContainText("REQUIREMENT_AI_FAILED");
  // 要約に code を重ねない（code は「詳細」のエラーコードだけ）。
  await expect(error).not.toContainText("(REQUIREMENT_AI_FAILED)");
  await expect(error.getByRole("button", { name: "もう一度確認" })).toBeVisible();
  await expect(error.getByRole("button", { name: "確認せずに SQL を生成" })).toBeVisible();
  // チャットは「確認をやめる」を出さない（#2303）。
  await expect(error.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
});

test("質問が SQL の文でも同じに要件を確かめ、確認の質問が無くても確認のカードで確かめてから SQL を作る。送った質問は書き換えない (#1727 / #1761)", async ({ page }) => {
  // 既定の mock は「確認の質問が無い」（質問 0 件）。SQL の文かどうかで確認を飛ばさない。
  const state = await setup(page);
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("select * from employee");
  await composer.press("Enter");
  await confirmChatRequirement(page);
  const turn = page.getByTestId("sql-chat-turn");
  await expect(turn).toHaveCount(1);
  expect(state.requirement.creates).toHaveLength(1);
  expect(state.requirement.creates[0]).toMatchObject({ question: "select * from employee", chat: true });
  expect(state.requests[0]).toMatchObject({
    question: "select * from employee",
    chat: true,
    requirement_session_id: REQUIREMENT_SESSION_ID,
  });
  // 吹き出しは利用者が送った質問のまま、入力欄に確かめた質問の文を入れない（#1727）。
  await expect(turn.getByText("select * from employee", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");
  await expect(turn.getByTestId("sql-chat-turn-requirement")).toBeVisible();
});

test("OCI Enterprise AI が無い環境では、要件の検査を使わずにそのまま SQL を作る (#1442)", async ({ page }) => {
  const state = await setup(page, {
    createFailure: {
      status: 503,
      error: "OCI Enterprise AI が設定されていないため、回答前の確認を使えません。(REQUIREMENT_AI_NOT_CONFIGURED)",
    },
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("カテゴリ別売上");
  await composer.press("Enter");
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  await expect(page.getByTestId("nl2sql-guided-clarification-error")).toHaveCount(0);
  expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
});

// #1761: 確認の質問が 0 件でも自動では SQL を作らず、SQL 生成の画面と同じ確認のカード（同じ部品・同じ文言）で
// 対象の選び直し・その他（自由入力）・「この内容で SQL を生成して実行」を出す。チャットは「確認をやめる」の代わりに
// 「確認せずに SQL を生成」を出す（#2303。質問を取り消さずに生成へ進む）。
test.describe("チャットの回答前の確認の確認のカード (#1761)", { tag: "@responsive" }, () => {
  test("質問 0 件でも確認のカードを出し、押すまで SQL を作らない", async ({ page }, testInfo) => {
    const state = await setup(page, { create: "target_choice" });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const requirementTurn = page.getByTestId("sql-chat-requirement-turn");
    const confirm = requirementTurn.getByTestId("nl2sql-clarification-confirm");
    await expect(confirm).toBeVisible();
    await expect(confirm.getByRole("group", { name: "使用テーブルはこの表でよいですか？" }).getByRole("radio")).toHaveCount(2);
    await expect(confirm.getByRole("radio", { name: /ADMIN\.V_EMP_DEPT/ })).toBeChecked();
    await expect(confirm.getByRole("textbox", { name: "その他（自由入力）" })).toBeVisible();
    // SQL 生成の画面と同じ並び・同じ文言。
    await expect(confirm.getByRole("button")).toHaveText([
      "この内容で SQL を生成して実行",
      "直しを伝える",
      "確認せずに SQL を生成",
    ]);
    // 送った質問は吹き出しのまま（書き換えない）。押すまで反映もジョブの投入もしない。
    await expect(requirementTurn.getByText(TARGET_QUESTION, { exact: true })).toBeVisible();
    expect(state.requirement.applies).toBe(0);
    expect(state.requests).toHaveLength(0);
    await expectChatNoOverflow(page);
    await requirementTurn.screenshot({ path: testInfo.outputPath(`chat-confirm-card-${testInfo.project.name}.png`) });

    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    expect(state.requirement.applies).toBe(1);
    expect(state.requests[0]).toMatchObject({
      question: TARGET_QUESTION,
      requirement_session_id: REQUIREMENT_SESSION_ID,
    });
    await expect(page.getByTestId("sql-chat-turn").getByText(TARGET_QUESTION, { exact: true })).toBeVisible();
  });

  test("「確認せずに SQL を生成」は確かめた要件を使わず、質問を取り消さずに SQL を作る (#2303)", async ({ page }) => {
    const state = await setup(page, { create: "target_choice" });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const confirm = page.getByTestId("sql-chat-requirement-turn").getByTestId("nl2sql-clarification-confirm");
    await expect(confirm.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
    await confirm.getByRole("button", { name: "確認せずに SQL を生成" }).click();
    await expect(page.getByTestId("sql-chat-requirement-turn")).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    await expect(composer).toHaveValue("");
    expect(state.requirement.cancels).toBe(1);
    expect(state.requirement.applies).toBe(0);
    expect(state.requests[0]).toMatchObject({ question: TARGET_QUESTION, requirement_skipped: true });
    expect(state.requests[0]).not.toHaveProperty("requirement_session_id");
  });

  test("対象の表を選び直すと再検査に回し、直した要件を確かめてから SQL を作る", async ({ page }) => {
    const state = await setup(page, { create: "target_choice", answers: ["target_chosen"] });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const requirementTurn = page.getByTestId("sql-chat-requirement-turn");
    const confirm = requirementTurn.getByTestId("nl2sql-clarification-confirm");
    await confirm.getByRole("radio", { name: /ADMIN\.V_DEPT_PROJECT/ }).check();
    await expect(confirm.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeDisabled();
    await confirm.getByRole("button", { name: "直しを伝える" }).click();
    await expect(requirementTurn.getByTestId("nl2sql-clarification-skeleton")).toContainText("FROM ADMIN.V_DEPT_PROJECT");
    expect(state.requirement.answers[0]).toEqual({
      base_version: 1,
      item_id: "item-view-from",
      selected_candidate_ids: ["candidate-v-dept-project"],
      free_text: "",
    });
    expect(state.requests).toHaveLength(0);
    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    expect(state.requests[0]).toMatchObject({ requirement_session_id: REQUIREMENT_SESSION_ID });
  });

  test("「その他（自由入力）」で AI の解釈を直せる", async ({ page }, testInfo) => {
    const state = await setup(page, { create: "target_choice", answers: ["other_corrected"] });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const requirementTurn = page.getByTestId("sql-chat-requirement-turn");
    const confirm = requirementTurn.getByTestId("nl2sql-clarification-confirm");
    await confirm.getByRole("textbox", { name: "その他（自由入力）" }).fill(TARGET_OTHER_TEXT);
    await expect(confirm.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeDisabled();
    await confirm.getByRole("button", { name: "直しを伝える" }).click();
    await expect(requirementTurn.getByTestId("nl2sql-clarified-question")).toHaveText(
      "ADMIN.V_EMP_DEPT の部署ごとの社員数を表示してください。",
    );
    expect(state.requirement.answers[0]).toEqual({
      base_version: 1,
      item_id: "add:other",
      selected_candidate_ids: [],
      free_text: TARGET_OTHER_TEXT,
    });
    await expect(requirementTurn.getByText("確認 2 回目（上限 4 回）")).toBeVisible();
    await expect(confirm.getByRole("textbox", { name: "その他（自由入力）" })).toHaveValue("");
    await expectChatNoOverflow(page);
    await requirementTurn.screenshot({ path: testInfo.outputPath(`chat-confirm-other-${testInfo.project.name}.png`) });
    expect(state.requests).toHaveLength(0);
    // 送った質問の吹き出しも入力欄も、直しの文や AI の書き直した文に変えない。
    await expect(requirementTurn.getByText(TARGET_QUESTION, { exact: true })).toBeVisible();
    await expect(composer).toHaveValue("");
    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
    expect(state.requests[0]).toMatchObject({ question: TARGET_QUESTION, requirement_session_id: REQUIREMENT_SESSION_ID });
  });

  // #1772: SQL 生成の画面と同じ部品・同じ文言で、自由入力の間だけ生成のボタンが押せない理由を出す。
  test("自由入力の間だけ「この内容で回答」を促す説明を出し、生成のボタンの説明につなぐ (#1772)", async ({ page }, testInfo) => {
    const state = await setup(page, { create: "target_choice" });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const requirementTurn = page.getByTestId("sql-chat-requirement-turn");
    const confirm = requirementTurn.getByTestId("nl2sql-clarification-confirm");
    const freeText = confirm.getByRole("textbox", { name: "その他（自由入力）" });
    const generate = confirm.getByRole("button", { name: "この内容で SQL を生成して実行" });
    const hint = confirm.getByTestId("nl2sql-clarification-card-blocked");
    await expect(freeText).toBeVisible();
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);

    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toHaveText(REQUIREMENT_PENDING_HINT);
    await expect(generate).toBeDisabled();
    await expect(generate).toHaveAccessibleDescription(REQUIREMENT_PENDING_HINT);
    await expect(confirm.getByRole("button")).toHaveText([
      "この内容で SQL を生成して実行",
      "直しを伝える",
      "確認せずに SQL を生成",
    ]);
    const hintBox = await hint.boundingBox();
    const textBox = await freeText.boundingBox();
    const buttonBox = await generate.boundingBox();
    expect(hintBox && textBox && buttonBox).toBeTruthy();
    // 押せない理由は操作の行の直下（#1839 の共通のカード）。入力欄より下、確定のボタンより下。
    expect(hintBox!.y).toBeGreaterThanOrEqual(textBox!.y + textBox!.height);
    expect(hintBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height);
    await expectChatNoOverflow(page);
    // テーマの切り替えの transition の途中を撮らない。カードは 375px では画面より高く、上端が固定の見出しの
    // 下に入るので、説明とボタンの行が見える位置の画面を撮る。
    await page.emulateMedia({ reducedMotion: "reduce" });
    await generate.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`chat-confirm-pending-hint-${testInfo.project.name}-light.png`),
    });

    await freeText.fill("   ");
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toBeVisible();
    await freeText.fill("");
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);
    expect(state.requirement.answers).toHaveLength(0);
    expect(state.requests).toHaveLength(0);
  });

  // #1776: SQL 生成の画面と同じ部品・同じ文言で、別の表を選んだだけ（未送信）の間も押せない理由を出す。
  test("表を選び直した間だけ「この内容で回答」を促す説明を出し、元の表に戻すと消える (#1776)", async ({ page }, testInfo) => {
    const state = await setup(page, { create: "target_choice" });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill(TARGET_QUESTION);
    await composer.press("Enter");
    const requirementTurn = page.getByTestId("sql-chat-requirement-turn");
    const confirm = requirementTurn.getByTestId("nl2sql-clarification-confirm");
    const freeText = confirm.getByRole("textbox", { name: "その他（自由入力）" });
    const generate = confirm.getByRole("button", { name: "この内容で SQL を生成して実行" });
    const hint = confirm.getByTestId("nl2sql-clarification-card-blocked");
    const original = confirm.getByRole("radio", { name: /ADMIN\.V_EMP_DEPT/ });
    const other = confirm.getByRole("radio", { name: /ADMIN\.V_DEPT_PROJECT/ });
    await expect(original).toBeChecked();
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();

    await other.check();
    await expect(hint).toHaveText(REQUIREMENT_TARGET_PENDING_HINT);
    await expect(generate).toBeDisabled();
    await expect(generate).toHaveAccessibleDescription(REQUIREMENT_TARGET_PENDING_HINT);
    await expect(confirm.getByRole("button")).toHaveText([
      "この内容で SQL を生成して実行",
      "直しを伝える",
      "確認せずに SQL を生成",
    ]);
    const hintBox = await hint.boundingBox();
    const textBox = await freeText.boundingBox();
    const buttonBox = await generate.boundingBox();
    expect(hintBox && textBox && buttonBox).toBeTruthy();
    // 押せない理由は操作の行の直下（#1839 の共通のカード）。入力欄より下、確定のボタンより下。
    expect(hintBox!.y).toBeGreaterThanOrEqual(textBox!.y + textBox!.height);
    expect(hintBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height);
    await expectChatNoOverflow(page);
    // #1772 の test と同じく、transition の途中を撮らず、説明とボタンの行が見える位置の画面を撮る。
    await page.emulateMedia({ reducedMotion: "reduce" });
    await generate.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`chat-confirm-target-hint-${testInfo.project.name}-light.png`),
    });

    await original.check();
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);
    await other.check();
    await expect(hint).toHaveText(REQUIREMENT_TARGET_PENDING_HINT);
    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toHaveText(REQUIREMENT_PENDING_HINT);
    await expect(generate).toHaveAccessibleDescription(REQUIREMENT_PENDING_HINT);
    await freeText.fill("");
    await expect(hint).toHaveCount(0);
    await expect(original).toBeChecked();
    await expect(generate).toBeEnabled();
    expect(state.requirement.answers).toHaveLength(0);
    expect(state.requests).toHaveLength(0);
  });
});

async function expectChatNoOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
}

test("確認の途中で再読込しても、同じ確認を続けられる (#1442)", async ({ page }) => {
  const state = await setup(page, { create: "needs_answer", answers: ["ready"] });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  await composer.fill("今年度の地域ごとの受注金額");
  await composer.press("Enter");
  const heading = page
    .getByTestId("sql-chat-requirement-turn")
    .getByRole("heading", { name: "「今年度」は、どの期間ですか？" });
  await expect(heading).toBeVisible();
  await page.reload();
  await expect(heading).toBeVisible();
  // 作った確認を読み直して続ける（作り直さない・中止しない）。
  expect(state.requirement.creates).toHaveLength(1);
  expect(state.requirement.cancels).toBe(0);
  await page.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await page.getByRole("button", { name: "この内容で回答" }).click();
  await page.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
  await expect(page.getByTestId("sql-chat-turn")).toHaveCount(1);
  expect(state.requests[0]).toMatchObject({ requirement_session_id: REQUIREMENT_SESSION_ID });
});

test("Select AI Credential が無いと Select AI の送信を止めて入力欄の下に事前条件を示し、Enterprise AI は送れる (#1688)", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  const state = await setup(page);
  const readiness = { exists: false };
  await page.route("**/api/nl2sql/select-ai/credential-readiness", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          credential_required: true,
          credential_name: "NL2SQL_OCI_CRED",
          credential_exists: readiness.exists,
        },
      }),
    }),
  );
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問", exact: true });
  const send = page.getByTestId("sql-chat-send");
  const precondition = page.getByTestId("sql-chat-credential-precondition");
  await expect(precondition).toContainText("Select AI Credential が作成されていません");
  await expect(precondition).toContainText("作成するまで送信できません。");
  await composer.fill("カテゴリ別売上");
  // 書いた質問は残し、送信だけを止める（Enter でも送らない）。
  await expect(send).toHaveAttribute("aria-disabled", "true");
  await composer.press("Enter");
  await expect(composer).toHaveValue("カテゴリ別売上");
  expect(state.requests).toHaveLength(0);
  // 入力欄の下（送信の行の直下）に全幅で出す。
  const composerBox = (await composer.boundingBox())!;
  const noticeBox = (await precondition.boundingBox())!;
  expect(noticeBox.y).toBeGreaterThanOrEqual(composerBox.y + composerBox.height);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
  await precondition.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("chat-credential-missing.png") });

  // OCI Enterprise AI の直接の生成は Credential を使わないので送れる。
  await chooseSelectFieldOption(
    page.getByRole("combobox", { name: "生成方法", exact: true }),
    "enterprise_ai_direct",
  );
  await expect(precondition).toHaveCount(0);
  await chooseSelectFieldOption(
    page.getByRole("combobox", { name: "生成方法", exact: true }),
    "select_ai",
  );
  readiness.exists = true;
  await precondition.getByRole("button", { name: "再試行" }).click();
  await expect(precondition).toHaveCount(0);
  await composer.press("Enter");
  await confirmChatRequirement(page);
  await expect.poll(() => state.requests.length).toBe(1);
  expect(state.requests[0]).toMatchObject({ engine: "select_ai", chat: true });
});

test("実行の詳細の「会話を開く」の URL で、その会話をチャットで開く (#2343)", { tag: "@responsive" }, async ({ page }) => {
  const state = await setup(page);
  state.turns.push({
    job_id: "chat-1",
    question: "カテゴリ別売上",
    status: "done",
    created_at: now,
    steps: [],
    result: {
      generated_sql: "SELECT CATEGORY, SUM(AMOUNT) FROM APP.SALES GROUP BY CATEGORY",
      original_question: "カテゴリ別売上",
      explanation: "カテゴリごとの売上合計です。",
      safety: { is_safe: true },
    },
  });
  await page.goto("/chat?profile_id=sales&conversation_id=chat-1");
  // URL の会話を選んで開き、URL からは会話の指定を外す（作業状態に残る）。
  await expect(page.getByTestId("sql-chat-turn").first()).toContainText("カテゴリ別売上");
  await expect(page).toHaveURL(/\/chat$/);
  await page.reload();
  await expect(page.getByTestId("sql-chat-turn").first()).toContainText("カテゴリ別売上");
});

for (const width of [1280, 375]) {
  test(`回答の注意書きを SQL と説明の後に出す (${width}px)`, { tag: "@responsive" }, async ({ page }) => {
    const state = await setup(page);
    state.notice = "この回答は AI が生成したものです。\n業務に使う前に SQL と結果で確かめてください。";
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問", exact: true });
    await composer.fill("カテゴリ別売上");
    await composer.press("Enter");
    await confirmChatRequirement(page);
    const turn = page.getByTestId("sql-chat-turn").last();
    const notice = turn.getByRole("complementary", { name: "回答の注意書き" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("SQL と結果で確かめてください。");
    // SQL・説明の後に出す（説明の本文には混ぜない）。
    const explanation = turn.getByText("カテゴリごとの売上合計です。");
    const [explanationBox, noticeBox] = await Promise.all([explanation.boundingBox(), notice.boundingBox()]);
    expect(noticeBox!.y).toBeGreaterThan(explanationBox!.y);
    await expect(explanation).not.toContainText("AI が生成");
    expect(await noHorizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
}


test("安全確認を省略した SQL を安全検査済みと表示しない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await setup(page);
  state.safetyChecked = false;
  await page.goto("/chat");
  await page.getByRole("textbox", { name: "質問", exact: true }).fill("合成の値を集計してください");
  await page.getByTestId("sql-chat-send").click();
  await confirmChatRequirement(page);
  await expect(page.getByText("安全確認を省略・未実行", { exact: true })).toBeVisible();
  await expect(page.getByText("安全検査済み・未実行", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("sql-safety-skipped.png"), fullPage: true });
});

test("処理の経過は span の木で、段の中のモデルの呼び出しを入れ子で出し、Trace ID を処理の詳細に出す (#3226)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await setup(page);
  state.turns.push({ job_id: "chat-1", question: "合成データの件数", status: "done", created_at: now, steps: [],
    progress_events: jobProgressEvents("chat-1", { at: now, terminal: "done", steps: {
      queue: "done",
      prepare_context: "done",
      generate_sql: { status: "done", params: { engine: "enterprise_ai_direct" }, children: [{ name: "gen_ai.chat", status: "done", attributes: { "gen_ai.request.model": "synthetic-model", "pr.gen_ai.purpose": "sql.generate" } }] },
      safety_check: "done",
      execute_sql: "done",
      format_results: "done",
    } }),
    result: { generated_sql: "SELECT 1 FROM DUAL", original_question: "合成データの件数", explanation: "合成の応答です。", safety: { is_safe: true, safety_checked: true, referenced_tables: [], referenced_columns: [] } } });
  await page.goto("/chat");
  await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
  await page.getByTestId("sql-chat-history").getByText("合成データの件数", { exact: true }).click();
  if ((page.viewportSize()?.width ?? 0) >= 1024) await page.getByRole("button", { name: "会話の履歴", exact: true }).click();
  const progress = page.getByTestId("sql-chat-progress");
  // 実行の全体は行にせず、段の中の span は段の下に入れ子（数は工程 6 とモデルの呼び出し 1）。
  await expect(page.getByTestId("sql-chat-progress-summary")).toHaveText(/^処理の経過（7 ステップ・/);
  await page.getByTestId("sql-chat-progress-summary").click();
  const generate = progress.getByTestId("sql-chat-progress-step-generate_sql");
  await expect(generate).toContainText("SQL を生成しました");
  const model = progress.getByTestId("sql-chat-progress-step-generate_sql.1");
  await expect(model).toContainText("質問に答える SQL を作る");
  await expect(model).not.toContainText("用途の記録なし");
  await inspectTraceStep(page, progress, "generate_sql.1");
  await expect(page.locator("[data-trace-technical]")).toContainText("synthetic-model");
  await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
  await expect(progress.locator('[data-level="1"]')).toContainText("質問に答える SQL を作る");
  // 「トレースの詳細」の 2 つ目の表示は無い。Trace ID は処理の詳細にコピーのボタンと出す。
  await expect(page.getByText("トレースの詳細")).toHaveCount(0);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${jobProgressTraceId("chat-1")}`);
  await expect(details.locator("[data-trace-id-row]").getByRole("button")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("sql-chat-span-tree.png"), fullPage: true });
});

test("終わったジョブの span が読んだ後に書かれても、読み直して処理の経過を出す (#3226)", async ({ page, context }) => {
  const state = await setup(page);
  state.turns.push({ job_id: "chat-1", question: "合成データの件数", status: "done", created_at: now, steps: [],
    result: { generated_sql: "SELECT 1 FROM DUAL", original_question: "合成データの件数", explanation: "合成の応答です。", safety: { is_safe: true, safety_checked: true, referenced_tables: [], referenced_columns: [] } } });
  // worker の書き手は span をまとめて後から書く: 最初の読み出しの時点では表にまだ span が無い（終端だけ）。
  let reads = 0;
  await context.route(/\/api\/nl2sql\/jobs\/chat-1\/trace/, (route) => {
    reads += 1;
    // 開発の build は StrictMode で最初の読み出しが 2 回になるので、2 回目までは span の無い応答にする。
    if (reads > 2) return route.fallback();
    return route.fulfill({ json: { data: { events: [
      { schema_version: 1, seq: 1, target_id: "chat-1", attempt: 0, emitted_at: now, type: "terminal", status: "done" },
    ], dropped_spans: 0 } } });
  });
  await page.goto("/chat?profile_id=sales&conversation_id=chat-1");
  // 読み直しを終えるまで古い形式の案内を出さない。
  await expect.poll(() => reads).toBeGreaterThan(0);
  await expect(page.getByTestId("sql-chat-progress-legacy")).toHaveCount(0);
  await expect(page.getByTestId("sql-chat-progress-summary")).toHaveText(/^処理の経過（6 ステップ・/);
  expect(reads).toBeGreaterThan(2);
});

test("書けなかった span がある記録は、処理の経過の中に一部だけの記録と出す (#3226)", async ({ page }) => {
  const state = await setup(page);
  state.turns.push({ job_id: "chat-1", question: "合成データの件数", status: "done", created_at: now, steps: [],
    result: { generated_sql: "SELECT 1 FROM DUAL", original_question: "合成データの件数", explanation: "合成の応答です。", safety: { is_safe: true, safety_checked: true, referenced_tables: [], referenced_columns: [] } } });
  // span の表に書けなかった span がある（書き手の queue が満杯など）。終端は会話のターン（ジョブの状態）で決まる。
  recordMockTrace(
    "chat-1",
    jobProgressEvents("chat-1", { at: now, terminal: "done", steps: { queue: "done", prepare_context: "done", generate_sql: "done" } }),
    { droppedSpans: 2, pinned: true },
  );
  await page.goto("/chat?profile_id=sales&conversation_id=chat-1");
  const progress = page.getByTestId("sql-chat-progress");
  await expect(progress).toHaveAttribute("data-chat-progress-state", "done");
  await page.getByTestId("sql-chat-progress-summary").click();
  await expect(page.getByTestId("sql-chat-progress-partial")).toHaveText("一部の処理だけを記録しています。");
  await expect(page.getByTestId("sql-chat-progress-legacy")).toHaveCount(0);
});

test("チャットと履歴の工程の内容・階層・状態・時間が一致する", {tag: "@responsive"}, async ({page}, testInfo) => {
  const state = await setup(page);
  const turn: Turn = {job_id: "synthetic-parity", question: "合成の質問", status: "done", created_at: now, engine: "enterprise_ai_direct", steps: [{stage: "safety_check", status: "skipped"}],
    result: {generated_sql: "SELECT 1 AS VALUE FROM DUAL", original_question: "合成の質問", explanation: "合成の回答", safety: {is_safe: true, safety_checked: false}}};
  state.turns.push(turn);
  const events = turnProgressEvents(turn);
  await page.goto("/chat?profile_id=sales&conversation_id=chat-1");
  await page.getByTestId("sql-chat-progress-summary").click();
  const live = await timelineSnapshot(page.getByTestId("sql-chat-progress"));
  const liveTypography = await timelineTypography(page.getByTestId("sql-chat-progress"));
  expect(liveTypography.steps.length).toBeGreaterThan(0);
  expect(liveTypography.steps.every(style => style?.fontSize === "12px" && style.fontFamily.includes("Noto Sans JP"))).toBe(true);
  expect(liveTypography.summary?.fontSize).toBe("12px");
  expect(live).toHaveLength(6);
  await page.screenshot({path: testInfo.outputPath("chat-process.png")});
  await page.route("**/api/nl2sql/history/synthetic-parity", (route) => route.fulfill({json: {data: withRecordedTrace("synthetic-parity", {
    id: "synthetic-parity", kind: "chat", session_id: "chat-1", question: "合成の質問", engine: "enterprise_ai_direct", generated_sql: "SELECT 1 AS VALUE FROM DUAL",
    executable_sql: "SELECT 1 AS VALUE FROM DUAL", created_at: now, elapsed_ms: 0, engine_timings: [], stage_timings: [],
    profile_id: "sales", profile_name: "プロファイル A", rewritten_question: "合成の質問", safety_is_safe: true,
    result_row_count: 1, result_columns: ["VALUE"], feedback_comment: "", feedback_updated_at: "", progress_events: events,
  })}}));
  await page.goto("/history?id=synthetic-parity");
  await expect(page.getByTestId("history-detail-summary")).toBeVisible();
  await expect(page.getByTestId("history-detail-summary").locator("[data-run-summary-heading] [data-status-variant]")).toHaveCount(0);
  await page.getByTestId("history-detail-timeline-summary").click();
  expect(await timelineSnapshot(page.getByTestId("history-detail-timeline"))).toEqual(live);
  expect(await timelineTypography(page.getByTestId("history-detail-timeline"))).toEqual(liveTypography);
  await expectChatNoOverflow(page);
  // 実行の詳細もチャットと同じ記録（span の木）で、Trace ID を処理の詳細に出す（「トレースの詳細」は無い。#3226）。
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${jobProgressTraceId("synthetic-parity")}`);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  await page.getByTestId("history-detail-timeline").screenshot({path: testInfo.outputPath("history-process.png")});
});
