import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #768: 業務利用者のチャット。会話（スレッド）の続き・出典・処理の経過のツールの段・承認待ちを確かめる。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

const THREAD_ID = `thread_${"a".repeat(32)}`;

for (const viewport of [...VIEWPORTS, { name: "desktop-wide", width: 1920, height: 1080 }, { name: "desktop-short", width: 1280, height: 480 }, { name: "desktop-zoomed", width: 1280, height: 400 }]) {
  for (const theme of ["light", "dark"] as const) {
    test(`長い会話でもページ外へスクロールせず入力欄へ到達できる (${viewport.width === 375 ? "375px" : viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      seedThread(mockApi, {
        status: "running",
        artifacts: [{ id: "answer-long", kind: "answer", name: "回答", content: { text: "回答の本文。".repeat(1000) } }],
      });
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      await openSeedThread(page);
      const conversation = page.getByTestId("chat-conversation");
      await expect.poll(() => conversation.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(0);
      const dimensions = () => page.evaluate(() => {
        const main = document.getElementById("pr-main")!;
        const conversation = document.querySelector('[data-testid="chat-conversation"]')!;
        return {
          mainTop: main.scrollTop, mainOverflow: main.scrollHeight - main.clientHeight,
          documentOverflow: document.documentElement.scrollHeight - window.innerHeight,
          conversationHeight: conversation.clientHeight,
        };
      });
      let measured = await dimensions();
      expect(measured.documentOverflow, JSON.stringify(measured)).toBeLessThanOrEqual(1);
      expect(measured.conversationHeight).toBeLessThan(viewport.height);
      if (viewport.width >= 1024 && viewport.height >= 600) {
        expect(measured.mainTop, JSON.stringify(measured)).toBe(0);
        expect(measured.mainOverflow, JSON.stringify(measured)).toBeLessThanOrEqual(1);
      }
      if (viewport.width >= 1024 && viewport.height < 600) {
        // 拡大や低いウィンドウでは、会話を潰さず main の中でページもスクロールする。
        expect(measured.mainOverflow).toBeGreaterThan(0);
        expect(measured.conversationHeight).toBeGreaterThan(150);
      }
      // 内側の末尾で wheel を続けても document の下に空白を作らない。
      await conversation.hover();
      await page.mouse.wheel(0, 10000);
      const composer = page.getByRole("textbox", { name: "質問" });
      await composer.focus();
      await composer.fill("複数行の質問\n追加の条件");
      await expect(composer).toBeInViewport();
      await expect(page.getByTestId("chat-send")).toBeInViewport();
      measured = await dimensions();
      expect(measured.documentOverflow, JSON.stringify(measured)).toBeLessThanOrEqual(1);
      if (viewport.width >= 1024 && viewport.height >= 600) expect(measured.mainTop).toBe(0);
      // polling で完了へ変わっても外側を動かさない。
      mockApi.state.runs[0].status = "completed";
      await expect(page.getByTestId("chat-send")).toHaveAccessibleName("送信");
      expect((await dimensions()).documentOverflow).toBeLessThanOrEqual(1);
      await page.screenshot({ path: testInfo.outputPath(`scroll-${viewport.name}-${theme}.png`), fullPage: true });
    });
  }
}

function seedThread(mockApi: MockApi, run: Record<string, unknown>) {
  mockApi.state.runs.push({
    id: "run-chat-seed",
    goal: "契約の更新条件は？",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    ...run,
  });
}

for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`質問を送ると回答が出て、同じ会話で続けられる (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");

      await expect(page.getByRole("heading", { name: "チャット", level: 1 })).toBeVisible();
      await expect(page.locator("#chat-agent")).toContainText("資料・データ横断アシスタント");
      await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();

      const composer = page.getByRole("textbox", { name: "質問" });
      await composer.fill("今月の売上は？");
      await composer.press("Enter");
      const conversation = page.getByTestId("chat-conversation");
      await expect(conversation.getByText("「今月の売上は？」への回答です。")).toBeVisible();
      await expect(composer).toHaveValue("");
      const first = mockApi.lastRequest("POST", "/api/runs");
      expect(first?.body).toEqual({ goal: "今月の売上は？", agent_id: "default" });

      // 2 回目は同じ会話（thread_id）で送る。
      await composer.fill("先月と比べると？");
      await page.getByTestId("chat-send").click();
      await expect(conversation.getByText("「先月と比べると？」への回答です。")).toBeVisible();
      const second = mockApi.lastRequest("POST", "/api/runs");
      expect((second?.body as { thread_id?: string }).thread_id).toMatch(/^thread_/);

      // 会話の履歴（desktop は左、375px は side sheet）。
      if (viewport.width >= 1024) {
        await page.getByTestId("chat-history-toggle").click();
        await expect(page.getByTestId("chat-history").getByText("今月の売上は？")).toBeVisible();
      } else {
        await page.getByTestId("chat-history-toggle").click();
        const sheet = page.getByRole("dialog", { name: "会話の履歴" });
        await expect(sheet.getByText("今月の売上は？")).toBeVisible();
        await expect(sheet.getByText("2 往復")).toBeVisible();
        await page.keyboard.press("Escape");
      }
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`chat-${viewport.name}-${theme}.png`), fullPage: true });

      // 新しい会話は上端の行の 1 か所だけ（#889）。押すと会話を外し、すぐ書けるよう入力欄へフォーカスする。
      await page.getByRole("button", { name: "新しい会話", exact: true }).click();
      await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
      await expect(page.getByTestId("chat-conversation-title")).toHaveCount(0);
      await expect(composer).toBeFocused();
    });
  }
}

test("使ったツールは処理の経過の段で見て、承認待ちはその場で判断できる（根拠の塊は回答ができてから）", async ({ page, mockApi }) => {
  seedThread(mockApi, {
    status: "waiting_approval",
    steps: [
      {
        id: "step-1",
        run_id: "run-chat-seed",
        kind: "tool",
        status: "completed",
        tool_call: { name: "rag__rag_search", arguments: { query: "契約" } },
      },
      {
        id: "step-2",
        run_id: "run-chat-seed",
        kind: "tool",
        status: "waiting_approval",
        tool_call: { name: "nl2sql__nl2sql_query", arguments: { question: "契約件数" } },
        approval_id: "approval-chat-1",
      },
    ],
    approvals: [
      {
        id: "approval-chat-1",
        run_id: "run-chat-seed",
        step_id: "step-2",
        tool_call: { name: "nl2sql__nl2sql_query", arguments: { question: "契約件数" } },
        status: "pending",
        reason: "承認が必要です。",
        created_at: MOCK_NOW,
      },
    ],
    artifacts: [
      {
        id: "artifact-rag",
        name: "rag__rag_search:step-1",
        kind: "rag_evidence",
        content: {
          answer: "第 5 条",
          // rag_search の根拠（#1219）。回答の根拠の塊（#2328）は回答を確定するときの成果物から出す。
          evidence: [
            {
              evidence_id: "chunk-used",
              file_name: "契約書.pdf",
              locator: { section_path: ["契約書", "第5条（更新）"], page_start: 3, page_end: 3 },
              excerpt: "更新は 30 日前までに通知する。",
              used_in_answer: true,
            },
            {
              evidence_id: "chunk-sheet",
              file_name: "費目コード.xlsx",
              locator: { section_path: ["コード表"], sheet_name: "コード表", row_start: 3, row_end: 6, cell_range: "A3:C6" },
              excerpt: "コード: A03 / 名称: 交通費",
              used_in_answer: true,
            },
            {
              evidence_id: "chunk-unused",
              file_name: "別紙.pdf",
              locator: { section_path: [], page_start: null, page_end: null },
              excerpt: "回答に使っていない根拠",
              used_in_answer: false,
            },
          ],
        },
      },
    ],
  });
  await page.goto("/chat");
  await openSeedThread(page);

  const turn = page.getByTestId("chat-turn-run-chat-seed");
  await expect(turn.getByText("ツールの実行に承認が必要です")).toBeVisible();
  await expect(turn.getByText("nl2sql__nl2sql_query を実行します。", { exact: false })).toBeVisible();
  // 承認待ちのあいだは次の質問を送れない。
  await expect(page.getByTestId("chat-composer-hint")).toHaveCount(0);
  await page.getByRole("textbox", { name: "質問" }).fill("次の質問");
  // 承認待ちの間、送信のボタンは同じ位置で「停止」になる（#805）。
  await expect(page.getByTestId("chat-send")).toHaveAccessibleName("停止");
  await expect(page.getByTestId("chat-send")).toHaveAttribute("data-state", "running");

  // 回答を確定する前（承認待ち）は、回答の根拠の塊（成果物 answer_evidence）をまだ出さない（#2328）。
  await expect(turn.getByTestId("answer-citations")).toHaveCount(0);
  // 使ったツールは処理の経過のツールの段で見る（「使ったツール」の一覧を重ねて出さない。#2324）。
  await expect(turn.getByText(/使ったツール/)).toHaveCount(0);

  await turn.getByRole("button", { name: "承認して実行" }).click();
  await expect(page.getByText("承認しました")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/approvals/approval-chat-1/decision")?.body).toEqual({
    approved: true,
  });
});

// #805: 回答の作成中は、送信のボタンが同じ位置で「停止」になり、押すと Run を止めて「送信」に戻る（RAG と同じ。buttons.md §3.1）。
for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`回答の作成中は送信が停止になり、押すと Run を止める (${viewport.name}, ${theme})`, async ({ page, mockApi }, testInfo) => {
      seedThread(mockApi, { status: "running" });
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      if (viewport.width >= 1024) {
        await page.getByTestId("chat-history-toggle").click();
        await page.getByTestId("chat-history").getByRole("button", { name: /^契約の更新条件は？/ }).click();
      } else {
        await page.getByTestId("chat-history-toggle").click();
        await page.getByRole("dialog", { name: "会話の履歴" }).getByText("契約の更新条件は？").click();
      }

      const turn = page.getByTestId("chat-turn-run-chat-seed");
      await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "running");
      const button = page.getByTestId("chat-send");
      await expect(button).toHaveAccessibleName("停止");
      await expect(button).toHaveAttribute("data-state", "running");
      await expect(button).not.toHaveAttribute("aria-disabled", "true");
      await expect(page.getByTestId("chat-composer-hint")).toHaveCount(0);
      const runningComposerHeight = await page.getByTestId("chat-composer-region").evaluate((element) => element.getBoundingClientRect().height);

      // 作成中も次の質問を書ける。入力欄の Enter では送らず、停止もしない。
      const composer = page.getByRole("textbox", { name: "質問" });
      await composer.fill("次の質問");
      await composer.press("Enter");
      await expect(composer).toHaveValue("次の質問");
      expect(mockApi.lastRequest("POST", "/api/runs")).toBeUndefined();
      expect(mockApi.lastRequest("POST", "/api/runs/run-chat-seed/cancel")).toBeUndefined();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`chat-running-${viewport.name}-${theme}.png`) });

      await button.click();
      await expect.poll(() => mockApi.lastRequest("POST", "/api/runs/run-chat-seed/cancel")).toBeTruthy();
      // 同じボタンが「送信」に戻り、フォーカスはボタンに残る。会話には止めた状態が出る。
      await expect(button).toHaveAccessibleName("送信");
      await expect(button).toHaveAttribute("data-state", "idle");
      await expect(button).toBeFocused();
      await expect(turn.getByTestId("chat-cancelled")).toHaveText(
        "回答の作成を停止しました。もう一度送ると、新しく回答を作成します。"
      );
      // 処理の段階は止めた時点で終わり、「処理の経過」の一覧になる（#1147 / #2096）。
      await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "cancelled");
      await expect(turn.getByTestId("chat-progress-current")).toHaveCount(0);
      await expect(page.getByTestId("chat-composer-hint")).toHaveCount(0);
      await expect(composer).toHaveValue("次の質問");
      expect(await page.getByTestId("chat-composer-region").evaluate((element) => element.getBoundingClientRect().height)).toBe(runningComposerHeight);
      await page.screenshot({ path: testInfo.outputPath(`chat-stopped-${viewport.name}-${theme}.png`) });
    });
  }
}

// Issue 865: 入力欄の下へ実行中の補足文を追加せず、状態が変わっても入力領域の高さを保つ。
for (const viewport of VIEWPORTS) {
  for (const theme of ["light", "dark"] as const) {
    test(`送信要求・回答作成・承認待ち・完了で入力領域の高さが変わらない (${viewport.name}, ${theme})`, async ({ page, mockApi }) => {
      let createCalls = 0;
      let releaseCreate!: () => void;
      const pendingCreate = new Promise<void>((resolve) => { releaseCreate = resolve; });
      await page.route("**/api/runs", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        createCalls += 1;
        await pendingCreate;
        seedThread(mockApi, { status: "running", goal: "経費の上限は？" });
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: mockApi.state.runs[0] }) });
      });
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await useTheme(page, theme);
      await page.goto("/chat");
      const composer = page.getByRole("textbox", { name: "質問" });
      const region = page.getByTestId("chat-composer-region");
      const button = page.getByTestId("chat-send");
      await composer.fill("経費の上限は？");
      const height = await region.evaluate((element) => element.getBoundingClientRect().height);
      const expectStableComposer = async () => {
        await expect(page.getByTestId("chat-composer-hint")).toHaveCount(0);
        expect(await region.evaluate((element) => element.getBoundingClientRect().height)).toBe(height);
        await expect(composer).toBeInViewport();
        await expect(button).toBeInViewport();
      };
      await composer.press("Enter");
      await expect(button).toHaveAccessibleName("停止");
      await expectStableComposer();
      releaseCreate();
      const turn = page.getByTestId("chat-turn-run-chat-seed");
      await expect(turn.getByTestId("chat-progress")).toHaveAttribute("data-chat-progress-state", "running");
      await expectStableComposer();
      mockApi.state.runs[0].status = "waiting_approval";
      mockApi.state.runs[0].approvals = [{
        id: "approval-stable", run_id: "run-chat-seed", step_id: "step-stable",
        tool_call: { name: "nl2sql__nl2sql_query", arguments: { question: "経費の上限は？" } },
        status: "pending", reason: "承認が必要です。", created_at: MOCK_NOW,
      }];
      await expect(turn.getByRole("button", { name: "承認して実行" })).toBeVisible();
      await expectStableComposer();
      await composer.fill("次の質問");
      await composer.press("Enter");
      await expect(composer).toHaveValue("次の質問");
      expect(mockApi.lastRequest("POST", "/api/runs/run-chat-seed/cancel")).toBeUndefined();
      expect(createCalls).toBe(1);
      // 承認の応答で完了へ進む状態を再現し、承認後の再取得で画面へ反映する。
      mockApi.state.runs[0].status = "completed";
      await turn.getByRole("button", { name: "承認して実行" }).click();
      await expect(button).toHaveAccessibleName("送信");
      await expectStableComposer();
    });
  }
}

test("送信の要求中に停止を押すと、作られた Run をすぐ止める（#805）", async ({ page, mockApi }) => {
  // Run の作成の応答を遅らせ、要求中に「停止」を押す。
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") await new Promise((resolve) => setTimeout(resolve, 800));
    await route.fallback();
  });
  await page.goto("/chat");
  const composer = page.getByRole("textbox", { name: "質問" });
  await composer.fill("今月の売上は？");
  const button = page.getByTestId("chat-send");
  await button.click();
  await expect(button).toHaveAccessibleName("停止");
  // 送ったら入力欄は空になり、次の質問を書ける。
  await expect(composer).toHaveValue("");
  // Run ができる前から、送った質問と作成中の表示を出す（#907）。
  await expect(page.getByTestId("chat-pending-turn").locator('[data-status="sending"]')).toHaveText("今月の売上は？");
  await button.click();

  await expect.poll(() => mockApi.lastRequest("POST", "/api/runs/run-chat-1/cancel")).toBeTruthy();
  await expect(page.getByTestId("chat-turn-run-chat-1").getByTestId("chat-cancelled")).toBeVisible();
  // 止めても送った質問は会話に残る（#907）。
  await expect(page.getByTestId("chat-turn-run-chat-1").getByText("今月の売上は？", { exact: true })).toBeVisible();
  await expect(button).toHaveAccessibleName("送信");
});

// #907: 送れなかった質問は入力欄に戻さず、会話の欄に残して「送信できませんでした」と「再送信」を出す（#805 の入力欄に戻す動きを置き換えた）。
for (const viewport of VIEWPORTS) {
  test(`送れなかった質問は会話の欄に残し、再送信できる（#907） (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let createCalls = 0;
    await page.route("**/api/runs", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      createCalls += 1;
      if (createCalls > 1) return route.fallback();
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "実行環境に接続できません。" }),
      });
    });
    await page.goto("/chat");
    const composer = page.getByRole("textbox", { name: "質問" });
    await composer.fill("今月の売上は？");
    await composer.press("Enter");

    const pendingTurn = page.getByTestId("chat-pending-turn");
    const failed = pendingTurn.locator('[data-status="failed"]');
    await expect(failed).toContainText("今月の売上は？");
    await expect(failed).toContainText("送信できませんでした");
    const failure = page.getByTestId("chat-send-failure");
    await expect(failure.getByRole("alert")).toContainText("実行環境に接続できません。");
    await expect(composer).toHaveValue("");
    await expect(page.getByTestId("chat-send")).toHaveAccessibleName("送信");
    await expect(pendingTurn.getByTestId("chat-progress")).toHaveCount(0);
    expect(mockApi.lastRequest("POST", "/api/runs/run-chat-1/cancel")).toBeUndefined();
    await expectNoHorizontalOverflow(page);
    for (const theme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: theme });
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);
      await pendingTurn.screenshot({ path: testInfo.outputPath(`chat-failed-${viewport.name}-${theme}.png`) });
    }

    await failure.getByRole("button", { name: "再送信" }).click();
    const conversation = page.getByTestId("chat-conversation");
    await expect(conversation.getByText("「今月の売上は？」への回答です。")).toBeVisible();
    await expect(pendingTurn).toHaveCount(0);
    await expect(conversation.getByText("今月の売上は？", { exact: true })).toHaveCount(1);
    expect(createCalls).toBe(2);
  });
}

test("実行に失敗した回答は理由を出す", async ({ page, mockApi }) => {
  seedThread(mockApi, {
    status: "failed",
    events: [
      {
        id: "event-failed",
        run_id: "run-chat-seed",
        type: "runtime.failed",
        message: "モデルの呼び出しに失敗しました（BadRequestError）。",
        payload: { error_code: "runtime.model_failed" },
        created_at: MOCK_NOW,
      },
    ],
  });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/chat");
  await page.getByTestId("chat-history-toggle").click();
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText("契約の更新条件は？").click();

  const turn = page.getByTestId("chat-turn-run-chat-seed");
  // 1 文目は止まった段。用途のない旧モデル記録は未記録の表示名を使う。
  // #2342・#3226）。本文は失敗のイベントの文。
  await expect(turn.getByRole("alert")).toContainText("「モデルを呼び出す（用途の記録なし）」で処理が止まりました。");
  await expect(turn.getByText("モデルの呼び出しに失敗しました（BadRequestError）。")).toBeVisible();
  // 3 製品共通の失敗のカード: 次の操作と、開いた「詳細」（エラーコード・実行 ID。#2342）。
  const failure = turn.getByTestId("chat-failure-run-chat-seed");
  await expect(failure).toContainText("実行 ID を添えて管理者に連絡してください");
  await expect(failure.locator("details")).toHaveAttribute("open", "");
  await expect(failure.locator("details")).toContainText("runtime.model_failed");
  await expect(failure.locator("details")).toContainText("run-chat-seed");
  // 回答の本文の無い失敗の枠には見出し「回答」を出さない（#3433）。
  await expect(turn.getByTestId("chat-answer-heading")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("公開していない業務 Agent はチャットで選べない（#792）", async ({ page, mockApi }) => {
  const published = mockApi.state.agents[0] as Record<string, unknown>;
  mockApi.state.agents.push({
    ...published,
    id: "draft-792",
    name: "下書きの業務 Agent",
    versions: [],
    published_version: null,
    unpublished_changes: true,
  });
  await page.goto("/chat");
  // 閉じたボタンに虫眼鏡は無く、虫眼鏡は開いたパネルの検索欄だけ（3 製品の検索して選ぶ欄で同じ。#1771）。
  await expect(page.locator("#chat-agent svg.lucide-chevron-down")).toBeVisible();
  await expect(page.locator("#chat-agent svg.lucide-search")).toHaveCount(0);
  await page.locator("#chat-agent").click();
  await expect(page.getByRole("option", { name: new RegExp(String(published.name)) })).toBeVisible();
  await expect(page.locator("[data-searchable-select-popover] svg.lucide-search")).toHaveCount(1);
  await expect(page.locator("#chat-agent svg.lucide-search")).toHaveCount(0);
  await expect(page.getByRole("option", { name: /下書きの業務 Agent/ })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // 公開した業務 Agent が 1 つも無いときは、公開を管理者に依頼するよう案内する。
  mockApi.state.agents.forEach((agent) => {
    (agent as Record<string, unknown>).published_version = null;
  });
  await page.reload();
  await expect(page.getByText("業務 Agent の公開と権限の付与を管理者に依頼してください。", { exact: false })).toBeVisible();
});

/** 会話の履歴から種の会話を開く（desktop は左の一覧、375px（mobile-375 の project）は side sheet。#823）。 */
async function openSeedThread(page: Page) {
  const viewport = page.viewportSize();
  if (viewport && viewport.width >= 1024) {
    const toggle = page.getByTestId("chat-history-toggle");
    if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
    await page.getByTestId("chat-history").getByRole("button", { name: /^契約の更新条件は？/ }).click();
    return;
  }
  await page.getByTestId("chat-history-toggle").click();
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText("契約の更新条件は？").click();
}

// #871 / #889: RAG と同じ型。会話の履歴は既定で閉じて会話を全幅にし、上端の行の左端のアイコンだけのボタンで開閉する。
async function expectHistoryToggleAtStart(page: Page) {
  const toggle = page.getByTestId("chat-history-toggle");
  const conversation = page.getByRole("region", { name: "会話", exact: true });
  const newThread = page.getByRole("button", { name: "新しい会話", exact: true });
  await expect(toggle).toHaveAccessibleName("会話の履歴");
  // アイコンだけ（文字を出さない）。ナビの「実行履歴」と同じ History のアイコンは使わない。
  await expect(toggle).toHaveText("");
  await expect(toggle.locator("svg.lucide-history")).toHaveCount(0);
  // aria-controls の先（パネルかシート）は閉じている間も描いてある。
  const controls = await toggle.getAttribute("aria-controls");
  expect(controls).toBeTruthy();
  await expect(page.locator(`[id="${controls}"]`)).toHaveCount(1);
  const [toggleBox, conversationBox, newThreadBox] = await Promise.all([
    toggle.boundingBox(),
    conversation.boundingBox(),
    newThread.boundingBox(),
  ]);
  if (!toggleBox || !conversationBox || !newThreadBox) throw new Error("レイアウトを計測できません。");
  // 会話の欄の上端の行の左端に置き、「新しい会話」は右端。
  expect(toggleBox.x - conversationBox.x).toBeLessThanOrEqual(24);
  expect(toggleBox.y - conversationBox.y).toBeLessThanOrEqual(24);
  expect(newThreadBox.x).toBeGreaterThan(toggleBox.x + toggleBox.width);
  expect(conversationBox.x + conversationBox.width - (newThreadBox.x + newThreadBox.width)).toBeLessThanOrEqual(24);
}

test("会話の履歴は既定で閉じ、開くと会話の左に並び、開閉の状態が再読込で残る (1280px)", { tag: "@responsive" }, async ({ page, mockApi }) => {
  seedThread(mockApi, {});
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/chat");
  const toggle = page.getByTestId("chat-history-toggle");
  const history = page.getByTestId("chat-history");
  const conversation = page.getByRole("region", { name: "会話", exact: true });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle.locator("svg.lucide-panel-left-open")).toHaveCount(1);
  await expect(history).not.toBeVisible();
  await expectHistoryToggleAtStart(page);
  // 会話を選ぶまで今の会話の名前は出さない。
  await expect(page.getByTestId("chat-conversation-title")).toHaveCount(0);

  // キーボード（Enter）で開くと会話の左に並び、会話の幅が縮む。フォーカスは開閉ボタンに残る。
  const before = await conversation.boundingBox();
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(history).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle.locator("svg.lucide-panel-left-close")).toHaveCount(1);
  await expect(toggle).toBeFocused();
  await expect(page.getByRole("complementary", { name: "会話の履歴" })).toBeVisible();
  const [historyBox, opened] = await Promise.all([history.boundingBox(), conversation.boundingBox()]);
  if (!historyBox || !before || !opened) throw new Error("レイアウトを計測できません。");
  expect(before.width - opened.width).toBeGreaterThan(200);
  expect(Math.abs(historyBox.y - opened.y)).toBeLessThanOrEqual(1);
  expect(opened.x).toBeGreaterThan(historyBox.x + historyBox.width);

  // 会話を選んでもインラインのパネルは開いたまま。今の会話の名前は上端に出る。
  await history.getByRole("button", { name: /^契約の更新条件は？/ }).click();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("契約の更新条件は？");
  await expect(history).toBeVisible();

  // 開閉の状態は作業状態として残る（workspace-state.md）。
  await page.reload();
  await expect(history).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(history).not.toBeVisible();
  // 履歴を閉じていても、今の会話の名前は出したまま。
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("契約の更新条件は？");
  await page.reload();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(history).not.toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("375px では会話の履歴をシートで開き、Esc・外側・会話の選択で閉じてフォーカスを開閉ボタンへ戻す", { tag: "@responsive" }, async ({
  page,
  mockApi,
}) => {
  seedThread(mockApi, {});
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/chat");
  const toggle = page.getByTestId("chat-history-toggle");
  const sheet = page.getByRole("dialog", { name: "会話の履歴" });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(sheet).toBeHidden();
  await expectHistoryToggleAtStart(page);

  // キーボードで開く → 閉じるボタンへフォーカス → Tab は中で回る → Esc で閉じてボタンへ戻る。
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(sheet).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(sheet.getByRole("button", { name: "会話の履歴を閉じる" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(sheet.getByRole("button", { name: /^契約の更新条件は？/ })).toBeFocused();
  await expectNoHorizontalOverflow(page);
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();

  // シートの外側（scrim）を押すと閉じる。
  await toggle.click();
  await expect(sheet).toBeVisible();
  await page.getByTestId("chat-history-scrim").click({ position: { x: 360, y: 400 } });
  await expect(sheet).toBeHidden();

  // 会話を選ぶと閉じ、フォーカスは開閉ボタンへ戻り、今の会話の名前が上端に出る。
  await toggle.click();
  await sheet.getByRole("button", { name: /^契約の更新条件は？/ }).click();
  await expect(sheet).toBeHidden();
  await expect(toggle).toBeFocused();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("契約の更新条件は？");

  // モーダルのシートの開閉は残さない（再読込で画面を塞がない）。
  await toggle.click();
  await expect(sheet).toBeVisible();
  await page.reload();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(sheet).toBeHidden();
  await expectNoHorizontalOverflow(page);
});

// #907: 送った質問は、Run の作成の応答を待たずにすぐ会話の欄の末尾へ出す（楽観的な表示）。
/** Run の作成（POST /api/runs）を 1 件ずつ止めておき、`release` で mock の応答へ流す。 */
async function gateRunCreate(page: Page) {
  let allowed = 0;
  const waiters: Array<() => void> = [];
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") {
      if (allowed > 0) allowed -= 1;
      else await new Promise<void>((resolve) => waiters.push(resolve));
    }
    await route.fallback();
  });
  return () => {
    const waiter = waiters.shift();
    if (waiter) waiter();
    else allowed += 1;
  };
}

async function expectConversationScrolledToEnd(page: Page) {
  await expect
    .poll(() =>
      page
        .getByTestId("chat-conversation")
        .evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)
    )
    .toBeLessThanOrEqual(2);
}

for (const viewport of VIEWPORTS) {
  test(`送った質問は Run の作成を待たずに会話の欄へ出る（新しい会話・続きの会話。#907） (${viewport.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const release = await gateRunCreate(page);
    await page.goto("/chat");
    const empty = page.getByText("質問を入力して会話を始めます");
    await expect(empty).toBeVisible();
    const composer = page.getByRole("textbox", { name: "質問" });
    await composer.fill("今月の売上は？");
    await composer.press("Enter");

    // 新しい会話: Run の作成の応答の前に、質問と回答の作成中の表示が出る。空の状態はすぐ消える。
    const pendingTurn = page.getByTestId("chat-pending-turn");
    await expect(pendingTurn.locator('[data-status="sending"]')).toHaveText("今月の売上は？");
    // 回答の場所は「質問を送信しています」の段階（Run の作成の応答を待っている。#1147）。
    await expect(pendingTurn.getByTestId("chat-progress-current")).toContainText("質問を送信しています");
    await expect(empty).toHaveCount(0);
    await expect(page.getByRole("log", { name: "会話" })).toContainText("今月の売上は？");
    await expect(composer).toHaveValue("");
    await expect(composer).toBeFocused();
    await expect(page.getByTestId("chat-send")).toHaveAccessibleName("停止");
    // 処理中の表示は回答の場所の 1 つだけ（messaging.md §3.7）。
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
    await expectConversationScrolledToEnd(page);
    await expectNoHorizontalOverflow(page);
    for (const theme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: theme });
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);
      await page.getByRole("region", { name: "会話" }).screenshot({
        path: testInfo.outputPath(`chat-sending-${viewport.name}-${theme}.png`),
      });
    }

    // Run ができたら同じ位置の回答に置き換え、質問を二重に出さない。
    release();
    const conversation = page.getByTestId("chat-conversation");
    await expect(conversation.getByText("「今月の売上は？」への回答です。")).toBeVisible();
    await expect(pendingTurn).toHaveCount(0);
    await expect(conversation.getByText("今月の売上は？", { exact: true })).toHaveCount(1);

    // 続きの会話: 前の質問と回答の後（末尾）に出る。
    await composer.fill("先月と比べると？");
    await composer.press("Enter");
    await expect(pendingTurn.locator('[data-status="sending"]')).toHaveText("先月と比べると？");
    const text = await conversation.innerText();
    expect(text.indexOf("「今月の売上は？」への回答です。")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("「今月の売上は？」への回答です。")).toBeLessThan(text.indexOf("先月と比べると？"));
    await expect(composer).toHaveValue("");
    await expectConversationScrolledToEnd(page);
    release();
    await expect(conversation.getByText("「先月と比べると？」への回答です。")).toBeVisible();
    await expect(pendingTurn).toHaveCount(0);
  });
}

test("回答の作成中に会話の取り直しが一時的に失敗しても、会話を外さず取り直しを続ける", async ({ page, mockApi }) => {
  seedThread(mockApi, { status: "running" });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/chat");
  await openSeedThread(page);
  const conversation = page.getByTestId("chat-conversation");
  await expect(conversation.getByText("契約の更新条件は？")).toBeVisible();

  // 取り直し（1.5 秒ごと）が 1 回だけ 503 になる。
  let failures = 0;
  await page.route(`**/api/threads/${THREAD_ID}`, async (route) => {
    if (failures === 0) {
      failures += 1;
      await route.fulfill({ status: 503, json: { error_messages: ["一時的に利用できません。"] } });
      return;
    }
    await route.fallback();
  });
  await expect.poll(() => failures).toBe(1);
  // 会話はそのまま出し、新しい会話（空の案内）に戻さない。取り直しは続き、完了を出す。
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);
  await expect(conversation.getByText("契約の更新条件は？")).toBeVisible();
  await expect(page.getByTestId("chat-conversation-title")).toHaveText("契約の更新条件は？");
  mockApi.state.runs[0].status = "completed";
  mockApi.state.runs[0].artifacts = [
    { id: "answer-done", kind: "answer", name: "回答", content: { text: "更新は 1 年ごとです。" } },
  ];
  await expect(conversation.getByText("更新は 1 年ごとです。")).toBeVisible();
  await expect(page.getByTestId("chat-send")).toHaveAccessibleName("送信");
});

test("会話を開くときの読み込みに失敗したら、選んだ会話のまま理由と再試行を出す。消えた会話は新しい会話に戻す", async ({
  page,
  mockApi,
}) => {
  seedThread(mockApi, { status: "completed" });
  await page.setViewportSize({ width: 1280, height: 900 });
  let fail = true;
  await page.route(`**/api/threads/${THREAD_ID}`, async (route) => {
    if (fail) {
      await route.fulfill({ status: 500, json: { error_messages: ["会話を読み込めませんでした。"] } });
      return;
    }
    await route.fallback();
  });
  await page.goto("/chat");
  await openSeedThread(page);
  const error = page.getByTestId("chat-thread-error");
  await expect(error).toContainText("会話を読み込めませんでした。");
  await expect(page.getByText("質問を入力して会話を始めます")).toHaveCount(0);
  fail = false;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByTestId("chat-conversation").getByText("契約の更新条件は？")).toBeVisible();
  await expect(error).toHaveCount(0);

  // 会話が消えた（404）ときは、今までどおり新しい会話に戻す。
  mockApi.state.runs.length = 0;
  await page.reload();
  await expect(page.getByText("質問を入力して会話を始めます")).toBeVisible();
  await expect(page.getByTestId("chat-thread-error")).toHaveCount(0);
});

test("会話の履歴の読み込みに失敗したら「まだ会話がありません」ではなく理由と再試行を出す", async ({
  page,
  mockApi,
}) => {
  seedThread(mockApi, { status: "completed" });
  await page.setViewportSize({ width: 1280, height: 900 });
  let fail = true;
  await page.route("**/api/threads?*", async (route) => {
    if (fail) {
      await route.fulfill({ status: 500, json: { error_messages: ["会話の一覧を読み込めませんでした。"] } });
      return;
    }
    await route.fallback();
  });
  await page.goto("/chat");
  await page.getByTestId("chat-history-toggle").click();
  const history = page.getByTestId("chat-history");
  // 一覧の取得は既定の再試行（3 回）の後に失敗になる。
  const error = history.getByTestId("chat-threads-error");
  await expect(error).toContainText("会話の一覧を読み込めませんでした。", { timeout: 15_000 });
  await expect(history.getByText("まだ会話がありません")).toHaveCount(0);
  fail = false;
  await error.getByRole("button", { name: "再試行" }).click();
  await expect(history.getByRole("button", { name: /^契約の更新条件は？/ })).toBeVisible();
  await expect(error).toHaveCount(0);
});

// 送信の要求中に別の会話へ移ったら、Run の作成の応答で元の会話へ引き戻さない。
// 作られた Run は止めず、元の会話に残す（Run は会話の履歴として続く）。
test("送信の要求中に新しい会話へ移っても、作成の応答で元の会話へ戻らない", async ({ page, mockApi }) => {
  seedThread(mockApi, { status: "completed" });
  let releaseCreate!: () => void;
  const pendingCreate = new Promise<void>((resolve) => { releaseCreate = resolve; });
  let created = false;
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await pendingCreate;
    created = true;
    await route.fallback();
  });
  await page.goto("/chat");
  await openSeedThread(page);
  await expect(page.getByTestId("chat-turn-run-chat-seed")).toBeVisible();
  const composer = page.getByRole("textbox", { name: "質問" });
  await composer.fill("今月の売上は？");
  await composer.press("Enter");
  await expect(page.getByTestId("chat-pending-turn")).toContainText("今月の売上は？");

  await page.getByRole("button", { name: "新しい会話" }).click();
  await expect(page.getByTestId("chat-turn-run-chat-seed")).toHaveCount(0);
  await composer.fill("次の質問の下書き");
  releaseCreate();
  await expect.poll(() => created).toBe(true);
  await expect.poll(() => mockApi.lastRequest("POST", "/api/runs")).toBeTruthy();

  // 新しい会話のまま。元の会話の Run も、送った質問の仮の表示も出さず、下書きも残す。
  await page.waitForTimeout(500);
  await expect(page.getByTestId("chat-turn-run-chat-seed")).toHaveCount(0);
  await expect(page.getByTestId("chat-turn-run-chat-1")).toHaveCount(0);
  await expect(page.getByTestId("chat-pending-turn")).toHaveCount(0);
  await expect(composer).toHaveValue("次の質問の下書き");
  expect(mockApi.lastRequest("POST", "/api/runs/run-chat-1/cancel")).toBeUndefined();
});

for (const viewport of VIEWPORTS) {
  test(`実行の詳細の「会話を開く」から、その会話をチャットで開く（#2343） (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
    seedThread(mockApi, { owned_by_me: true });
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/runs?id=run-chat-seed");
    const openChat = page.getByTestId("run-open-chat");
    await expect(openChat).toHaveText("会話を開く");
    await openChat.click();

    // URL の会話を選んで開き、URL からは会話の指定を外す。
    await expect(page).toHaveURL(/\/chat$/);
    await expect(page.getByTestId("chat-turn-run-chat-seed")).toBeVisible();
    await expect(page.getByTestId("chat-turn-run-chat-seed")).toContainText("契約の更新条件は？");
    await expectNoHorizontalOverflow(page);
  });
}
