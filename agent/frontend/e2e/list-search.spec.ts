/**
 * 一覧の絞り込み（ListToolbar + SearchField）と状態の絞り込み、エディタの「変更を破棄」（#808）。
 *
 * - 一覧の検索は入力に合わせて絞り込み、検索ボタンを置かない。IME の変換中は絞らない（page-archetypes.md #535）。
 * - 0 件は「検索に一致する〜がありません」と「検索語をクリア」。検索語は作業状態に残り、変えると 1 ページ目へ戻る。
 * - 実行履歴は状態のチップ、承認は「保留中 / 判断済み / すべて」（既定は保留中）。
 * - エディタの「変更を破棄」は常に出し、変更が無いと disabled、押すと保存済みの内容に戻す（#618）。
 */
import type { Page } from "@playwright/test";

import { expect, MOCK_NOW, test, type MockApi } from "./fixtures/mock-api";
import { chooseSelectFieldOption, withListFilters } from "./fixtures/select-field";

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

test("業務 Agent は全件を名前の自然順・同名は ID 順にしてからページングする", { tag: "@responsive" }, async ({ page, mockApi }) => {
  const base = mockApi.state.agents[0];
  const names = [
    ["number-2", "2_検証"], ["number-10", "10_検証"], ["number-20", "20_検証"], ["number-21", "21_検証"],
    ["latin-2", "Agent 2"], ["latin-10", "Agent 10"], ["kana", "あいう"],
    ...Array.from({ length: 30 }, (_, index) => index + 1).flatMap((number) => {
      const row = [`agent-${number}`, `検証 Agent ${number}`];
      if (number === 10) return [["aa-same", row[1]], row, ["zz-same", row[1]]];
      return number === 21 ? [row, ["default", row[1]]] : [row];
    }),
  ];
  const ordered = names.map(([id, name], index) => ({ ...base, id, name, enabled: index % 2 === 0 }));
  mockApi.state.agents.splice(0, mockApi.state.agents.length, ...[...ordered].reverse());
  await page.goto("/agents");
  const table = page.getByRole("table", { name: "業務 Agent 一覧" });
  const pager = page.getByTestId("agent-list-pagination");
  const rowIds = () => table.locator("tbody tr").evaluateAll((rows) => rows.map((row) => row.getAttribute("data-testid")));
  const expected = ordered.map((row) => `agent-row-${row.id}`);
  await expect.poll(rowIds).toEqual(expected.slice(0, 25));
  await expect(pager).toContainText("1–25 / 40 件");
  await pager.getByRole("button", { name: "次へ", exact: true }).click();
  await expect.poll(rowIds).toEqual(expected.slice(25));

  // 取得順が変わっても同名の順とページ境界を保つ。
  mockApi.state.agents.reverse();
  await page.reload();
  await expect.poll(rowIds).toEqual(expected.slice(25));
  await page.locator("#agent-search").fill("10_検証");
  await expect.poll(rowIds).toEqual(["agent-row-number-10"]);
  await expect(page.getByTestId("agent-list-toolbar")).toContainText("1 件（全 40 件）");
  await expect(pager).toHaveCount(0);
  await page.locator("#agent-search").fill("");
  await expect.poll(rowIds).toEqual(expected.slice(0, 25));
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: test.info().outputPath("agent-name-order.png"), fullPage: true });
});

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

function seedAgents(mockApi: MockApi, count: number) {
  const base = mockApi.state.agents[0];
  for (let index = 1; index <= count; index += 1) {
    const id = `agent-${String(index).padStart(2, "0")}`;
    mockApi.state.agents.push({ ...base, id, name: `経理 Agent ${String(index).padStart(2, "0")}`, source: "runtime" });
  }
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

/** IME の変換中の入力（compositionstart → isComposing の input）。Playwright の keyboard は IME を通さない。 */
async function composeJapanese(page: Page, selector: string, steps: string[]) {
  await page.evaluate(
    ({ selector, steps }) => {
      const input = document.querySelector<HTMLInputElement>(selector)!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      input.focus();
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "" }));
      for (const step of steps) {
        setter.call(input, step);
        input.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true, data: step }));
      }
    },
    { selector, steps }
  );
}

async function endComposition(page: Page, selector: string, committed: string) {
  await page.evaluate(
    ({ selector, committed }) => {
      const input = document.querySelector<HTMLInputElement>(selector)!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, committed);
      input.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true, data: committed }));
      input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: committed }));
    },
    { selector, committed }
  );
}

for (const viewport of VIEWPORTS) {
  test.describe(`一覧の検索と絞り込み (${viewport.name})`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("業務 Agent の一覧は入力で絞り込み、0 件で「検索語をクリア」、検索語を残して 1 ページ目へ戻す", async ({
      page,
      mockApi,
    }, testInfo) => {
      seedAgents(mockApi, 29);
      await page.goto("/agents");
      const table = page.getByRole("table", { name: "業務 Agent 一覧" });
      const toolbar = page.getByTestId("agent-list-toolbar");
      await expect(toolbar).toContainText("全 32 件");
      // 検索ボタンは置かない（入力に合わせて絞り込む）。
      await expect(toolbar.getByRole("button", { name: /^検索$|絞り込み$/ })).toHaveCount(0);

      // 2 ページ目へ移ってから検索すると、1 ページ目へ戻る。
      const pagination = page.getByTestId("agent-list-pagination");
      await pagination.getByRole("button", { name: "次へ" }).click();
      await expect(pagination).toContainText("2 / 2 ページ");

      const search = page.locator("#agent-search");
      await search.fill("経理 Agent 1");
      await expect(toolbar).toContainText("12 件（全 32 件）");
      await expect(table.getByRole("link", { name: /経理 Agent 10/ })).toBeVisible();
      // 1 ページ目へ戻り、1 ページに収まるのでページ送りは出さない（12 件は最も少ない表示件数の段の 10 件より多いので、範囲と表示件数は出す）。
      await expect(pagination).toContainText("1–12 / 12 件");
      await expect(pagination.getByRole("button", { name: "次へ" })).toHaveCount(0);

      // 再読込しても検索語が残る（作業状態）。
      await page.reload();
      await expect(page.locator("#agent-search")).toHaveValue("経理 Agent 1");
      await expect(page.getByTestId("agent-list-toolbar")).toContainText("12 件（全 32 件）");

      // 0 件は「検索に一致する業務 Agent がありません」と「検索語をクリア」。
      await page.locator("#agent-search").fill("存在しない名前");
      await expect(page.getByText("検索に一致する業務 Agent がありません")).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`agents-no-match-${viewport.name}.png`), fullPage: true });
      await page.getByRole("button", { name: "検索語をクリア" }).last().click();
      await expect(page.getByTestId("agent-list-toolbar")).toContainText("全 32 件");
      await expect(page.locator("#agent-search")).toHaveValue("");
    });

    test("IME の変換中は絞り込まず、確定した語で絞り込む", async ({ page, mockApi }) => {
      seedAgents(mockApi, 3);
      await page.goto("/agents");
      const toolbar = page.getByTestId("agent-list-toolbar");
      await expect(toolbar).toContainText("全 6 件");
      await composeJapanese(page, "#agent-search", ["け", "けい", "けいり"]);
      await page.waitForTimeout(500);
      await expect(toolbar).toContainText("全 6 件");
      await endComposition(page, "#agent-search", "経理");
      await expect(toolbar).toContainText("3 件（全 6 件）");
    });

    test("実行履歴は状態の FilterSelect で絞り込み、承認は既定で保留中だけを出す", async ({ page, mockApi }, testInfo) => {
      seedRun(mockApi, "run-done", "completed", [approval("approval-done", "run-done", "approved")]);
      seedRun(mockApi, "run-failed", "failed");
      seedRun(mockApi, "run-waiting", "waiting_approval", [approval("approval-pending", "run-waiting", "pending")]);
      await page.goto("/runs");
      const table = page.getByRole("table", { name: "実行履歴" });
      // 状態は共通の FilterSelect（「状態: すべて」。#1933・#1955）。
      const toolbar = page.getByTestId("run-list-toolbar");
      await withListFilters(toolbar, (scope) => chooseSelectFieldOption(scope.getByTestId("run-list-toolbar-status"), "failed"));
      await expect(table.getByTestId("run-row-run-failed")).toBeVisible();
      await expect(table.getByTestId("run-row-run-done")).toHaveCount(0);
      // 絞り込んだ一覧の対象名から全幅の詳細を開く。
      await page.locator('a[data-run-id="run-failed"]').click();
      await expect(page.getByRole("region", { name: "実行の詳細" })).toContainText("run-failed");
      await page.screenshot({ path: testInfo.outputPath(`runs-filter-${viewport.name}.png`), fullPage: true });

      await page.getByRole("button", { name: "一覧へ戻る", exact: true }).click();
      await page.locator("#run-search").fill("該当なし");
      await expect(page.getByText("条件に一致する実行がありません")).toBeVisible();
      await page.getByRole("button", { name: "絞り込みをクリア" }).click();
      await expect(table.getByTestId("run-row-run-done")).toBeVisible();
      await withListFilters(toolbar, async (scope) => {
        await expect(scope.getByTestId("run-list-toolbar-status")).toHaveAttribute("data-value", "all");
      });
      await expectNoHorizontalOverflow(page);

      await page.goto("/approvals");
      const approvalChips = page.getByRole("group", { name: "判断の状態で絞り込み" });
      await expect(approvalChips.getByRole("button", { name: "保留中" })).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator('a[data-approval-id="approval-pending"]')).toBeVisible();
      await expect(page.locator('a[data-approval-id="approval-done"]')).toHaveCount(0);
      await approvalChips.getByRole("button", { name: "判断済み" }).click();
      await expect(page.locator('a[data-approval-id="approval-done"]')).toBeVisible();
      await page.locator('a[data-approval-id="approval-done"]').click();
      await expect(page.getByRole("region", { name: "承認の詳細" }).getByText("承認済み", { exact: true })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath(`approvals-filter-${viewport.name}.png`), fullPage: true });
      await expectNoHorizontalOverflow(page);
    });

    test("実行の作成の失敗は操作の行の直下に出す", async ({ page }) => {
      let posts = 0;
      await page.route("**/api/runs", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        posts += 1;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "モデルに接続できません。" }) });
      });
      await page.goto("/runs?id=new");
      const submit = page.getByTestId("run-create-submit");
      await submit.click();
      const actions = page.getByRole("group", { name: "実行の作成の操作" }).or(page.getByLabel("実行の作成の操作"));
      await expect(actions.first()).toContainText("実行を作成できませんでした。");
      await expect(actions.first()).toContainText("モデルに接続できません。");
      // 目標の Ctrl+Enter でも実行を作成する。
      await page.locator("#run-goal").focus();
      await page.keyboard.press("Control+Enter");
      await expect.poll(() => posts).toBe(2);
    });
  });
}

test(`業務 Agent のエディタの「変更を破棄」と段組み`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/agents?id=default");
  await expect(page.getByRole("heading", { name: "資料・データ横断アシスタント", level: 1 })).toBeVisible();
  const discard = page.getByRole("button", { name: "変更を破棄" });
  await expect(discard).toBeDisabled();
  const name = page.locator("#default-agent-name");
  const original = await name.inputValue();
  await name.fill("名前を変えた Agent");
  await expect(discard).toBeEnabled();
  await discard.click();
  await expect(name).toHaveValue(original);
  await expect(discard).toBeDisabled();

  // 欄は値の種類の幅（#1891）: 名前は md（20rem = 280px）、説明は全幅でも FormGrid の 60rem（840px）で止まる。
  const nameBox = await name.boundingBox();
  const descriptionBox = await page.locator("#default-agent-description").boundingBox();
  expect(nameBox && Math.abs(nameBox.width - 280) < 2).toBe(true);
  expect(descriptionBox && descriptionBox.width <= 841).toBe(true);
  expect(descriptionBox && nameBox && Math.abs(descriptionBox.x - nameBox.x) < 2).toBe(true);
  // スキルは ListPicker（検索・選択中だけ表示）。
  await expect(page.getByRole("listbox", { name: "業務 Agent に割り当てるスキル" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`agent-editor.png`), fullPage: true });
});

test("MCP 接続の API キーは SecretField（保存済み / 未設定・表示の切り替え）", async ({ page }) => {
  await page.goto("/settings/mcp-connections?id=new");
  await page.locator("#mcp-server-auth-mode").click();
  await page.getByRole("option", { name: "API キー" }).click();
  const field = page.locator("#mcp-server-api-key");
  await expect(field).toHaveAttribute("type", "password");
  await expect(page.getByText("未設定", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "API キーを表示" }).click();
  await expect(field).toHaveAttribute("type", "text");
  const discard = page.getByRole("button", { name: "変更を破棄" });
  await field.fill("secret-value");
  await expect(discard).toBeEnabled();
  await discard.click();
  await expect(page.locator("#mcp-server-auth-mode")).toContainText("なし");
});


test("/agents の主一覧は表示領域に合わせて伸び、小画面は標準密度を保つ", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  const mobile = testInfo.project.name === "mobile-375";
  if (!mobile) await page.setViewportSize({ width: 1440, height: 1300 });
  const rows = mockApi.state.agents;
  const base = rows[0];
  rows.splice(0, rows.length, ...Array.from({ length: 35 }, (_, index) => ({
    ...structuredClone(base), id: `height-${index}`, name: `検証 ${index}`,
  })));
  await page.goto("/agents");
  const scroll = page.getByTestId("agents-scroll-region");
  await expect(scroll.locator("tbody tr")).toHaveCount(25);
  const delta = () => scroll.evaluate((node) => {
    const count = window.matchMedia("(min-width: 768px)").matches ? 8 : 5;
    return node.getBoundingClientRect().bottom - node.querySelectorAll("tbody tr")[count - 1].getBoundingClientRect().bottom;
  });
  if (mobile) await expect.poll(async () => Math.abs(await delta())).toBeLessThanOrEqual(3);
  else await expect.poll(delta).toBeGreaterThan(100);
  const metrics = await scroll.evaluate((node) => ({ height: node.clientHeight, content: node.scrollHeight }));
  expect(metrics.content).toBeGreaterThan(metrics.height);
  await expect(scroll).toHaveAttribute("role", "region");
  await page.screenshot({ path: testInfo.outputPath("agents-height.png"), fullPage: true });
});


test("/skills の主一覧は表示領域に合わせて伸び、小画面は標準密度を保つ", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  const mobile = testInfo.project.name === "mobile-375";
  if (!mobile) await page.setViewportSize({ width: 1440, height: 1300 });
  const rows = mockApi.state.skills;
  const base = rows[0];
  rows.splice(0, rows.length, ...Array.from({ length: 35 }, (_, index) => ({
    ...structuredClone(base), id: `height-${index}`, name: `検証 ${index}`,
  })));
  await page.goto("/skills");
  const scroll = page.getByTestId("skills-scroll-region");
  await expect(scroll.locator("tbody tr")).toHaveCount(25);
  const delta = () => scroll.evaluate((node) => {
    const count = window.matchMedia("(min-width: 768px)").matches ? 8 : 5;
    return node.getBoundingClientRect().bottom - node.querySelectorAll("tbody tr")[count - 1].getBoundingClientRect().bottom;
  });
  if (mobile) await expect.poll(async () => Math.abs(await delta())).toBeLessThanOrEqual(3);
  else await expect.poll(delta).toBeGreaterThan(100);
  const metrics = await scroll.evaluate((node) => ({ height: node.clientHeight, content: node.scrollHeight }));
  expect(metrics.content).toBeGreaterThan(metrics.height);
  await expect(scroll).toHaveAttribute("role", "region");
  await page.screenshot({ path: testInfo.outputPath("skills-height.png"), fullPage: true });
});
