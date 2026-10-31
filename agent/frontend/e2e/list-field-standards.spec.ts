/**
 * 一覧の件数・ページング（表示件数・ページ番号の入力）と、入力欄の幅・placeholder の基準（#1891 段 4）。
 *
 * - 一覧の件数はツールバーの右の `ListCount` 1 か所（「全 N 件」「N 件（全 M 件）」）。
 * - ページ送りは advanced（表示件数 10 / 25 / 50 / 100・ページ番号の入力）。ページと表示件数は作業状態に残る。
 * - 画面ごとに、横あふれ・placeholder の切れ・幅の段に当たらない欄が無いこと（共通の layout-checks）を、
 *   1280px / 1920px / 375px とライト / ダークで確かめる。欄の補足・エラーが min(20rem, 置き場所の幅) より狭く折り返さない
 *   こと、1280px で `FormGrid` の置き場所を 42〜58rem に狭めても欄が段の幅のままでセルからはみ出さないことも見る（#1891 段 5）。
 * - 一覧のツールバーの部品が同じ高さで縦の中心がそろい、狭い置き場所で積んだ欄の幅がそろうこと（#1933 段 4）。
 */
import type { Page } from "@playwright/test";

import { expect, MOCK_NOW, test, type MockApi } from "./fixtures/mock-api";
import { chooseSelectFieldOption, withListFilters } from "./fixtures/select-field";
import {
  FORM_GRID_PROBE_WIDTHS_REM,
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  findNarrowFieldTexts,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  jumpToPage,
  measureHorizontalOverflow,
  setFormGridMaxWidth,
} from "../../../platform/packages/ui/e2e/layout-checks";

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

function seedAgents(mockApi: MockApi, count: number) {
  const base = mockApi.state.agents[0];
  for (let index = 1; index <= count; index += 1) {
    const id = `agent-${String(index).padStart(2, "0")}`;
    mockApi.state.agents.push({ ...base, id, name: `検証 Agent ${String(index).padStart(2, "0")}`, source: "runtime" });
  }
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

/** 調べる画面（一覧と、作成・編集の画面）。`ready` は内容が出たことの目印。 */
const SCREENS: ReadonlyArray<{ name: string; path: string; ready: (page: Page) => ReturnType<Page["getByTestId"]> }> = [
  { name: "agents", path: "/agents", ready: (page) => page.getByTestId("agent-list-toolbar") },
  { name: "agent-editor", path: "/agents?id=default", ready: (page) => page.getByTestId("agent-clarification") },
  { name: "agent-new", path: "/agents?id=new", ready: (page) => page.getByTestId("agent-clarification") },
  { name: "runs", path: "/runs", ready: (page) => page.getByTestId("run-list-toolbar") },
  { name: "run-new", path: "/runs?id=new", ready: (page) => page.locator("#run-goal") },
  { name: "approvals", path: "/approvals", ready: (page) => page.getByTestId("approval-list-toolbar") },
  { name: "automations", path: "/automations", ready: (page) => page.getByRole("heading", { level: 1 }) },
  { name: "automation-new", path: "/automations?id=new", ready: (page) => page.getByTestId("automation-schedule") },
  { name: "skills", path: "/skills", ready: (page) => page.getByTestId("skill-list-toolbar") },
  { name: "skill-new", path: "/skills?id=new", ready: (page) => page.locator("#skill-mcp-requirements") },
  { name: "tools", path: "/tools", ready: (page) => page.getByTestId("tool-list-toolbar") },
  { name: "audit", path: "/audit", ready: (page) => page.getByTestId("audit-list-toolbar") },
  { name: "feedback", path: "/feedback", ready: (page) => page.getByTestId("signals-filters") },
  { name: "usage", path: "/usage", ready: (page) => page.locator("#usage-period") },
  { name: "evaluation", path: "/evaluation", ready: (page) => page.getByRole("heading", { level: 1 }) },
  { name: "mcp-connections", path: "/settings/mcp-connections", ready: (page) => page.getByTestId("mcp-connection-list-toolbar") },
  { name: "mcp-connection-new", path: "/settings/mcp-connections?id=new", ready: (page) => page.getByTestId("mcp-trust-annotations") },
  { name: "tool-policy", path: "/settings/tool-policy", ready: (page) => page.getByTestId("tool-policy-toolbar") },
  // platform の共通の画面（@production-ready/system-settings）。
  { name: "users", path: "/settings/security/users", ready: (page) => page.getByTestId("security-users-toolbar") },
  { name: "roles", path: "/settings/security/roles", ready: (page) => page.getByTestId("security-roles-toolbar") },
  { name: "permissions", path: "/settings/security/permissions", ready: (page) => page.getByTestId("security-permissions-toolbar") },
  { name: "account-api-keys", path: "/account?tab=api-keys", ready: (page) => page.locator("#api-key-name") },
  // ローカルの認証ではパスワードの欄の代わりに案内を出す。
  {
    name: "account-password",
    path: "/account?tab=password",
    ready: (page) => page.locator('[data-testid="account-password-form"], [data-testid="account-password-local"]'),
  },
  { name: "system-tables", path: "/settings/system-tables", ready: (page) => page.getByRole("heading", { level: 1 }) },
  { name: "oci", path: "/settings/oci", ready: (page) => page.locator("#oci-config-profile") },
  { name: "upload-storage", path: "/settings/upload-storage", ready: (page) => page.locator("#upload-storage-local") },
  { name: "model", path: "/settings/model", ready: (page) => page.locator("#genai-embedding-model") },
  { name: "database", path: "/settings/database", ready: (page) => page.locator("#oracle-user") },
];

const LAYOUTS = [
  { name: "desktop 1280px light", width: 1280, height: 800, theme: "light" },
  { name: "desktop 1920px dark", width: 1920, height: 1080, theme: "dark" },
  { name: "desktop 1920px light", width: 1920, height: 1080, theme: "light" },
  { name: "mobile 375px light", width: 375, height: 812, theme: "light" },
  { name: "mobile 375px dark", width: 375, height: 812, theme: "dark" },
] as const;

for (const layout of LAYOUTS) {
  test(`一覧と入力欄の基準: 横あふれ・placeholder の切れ・幅の段 (${layout.name})`, async ({ page, mockApi }, testInfo) => {
    // 30 画面ほどを 1 つの test で回す（題名の「(desktop」「(mobile」で mobile-375 の project では二重に実行しない）。
    test.setTimeout(240_000);
    seedAgents(mockApi, 52);
    seedRuns(mockApi, 53);
    seedAuditRecords(mockApi, 54);
    await useTheme(page, layout.theme);
    await page.setViewportSize({ width: layout.width, height: layout.height });
    const problems: string[] = [];
    for (const screen of SCREENS) {
      await page.goto(screen.path);
      await expect(screen.ready(page)).toBeVisible();
      const overflow = await measureHorizontalOverflow(page);
      if (overflow > 1) problems.push(`${screen.name}: 横あふれ ${overflow}px`);
      for (const clipped of await findClippedPlaceholders(page)) {
        problems.push(`${screen.name}: placeholder の切れ ${clipped.name}「${clipped.placeholder}」${clipped.textWidth}px > ${clipped.availableWidth}px`);
      }
      for (const text of await findNarrowFieldTexts(page)) {
        problems.push(`${screen.name}: 狭く折り返す補足 ${text.name} ${text.width}px < ${text.expectedMin}px`);
      }
      for (const row of await findToolbarMisalignment(page)) {
        problems.push(`${screen.name}: ツールバー ${row.toolbar} の行 ${row.row} の高さの差 ${row.heightDelta}px・中心の差 ${row.centerDelta}px`);
      }
      for (const field of await findToolbarFieldWidthMismatch(page)) {
        problems.push(`${screen.name}: ツールバー ${field.toolbar} の欄 ${field.name} ${field.width}px（期待 ${field.expected}px）`);
      }
      // 幅の段は sm（640px）以上だけで効く（375px はどの段も全幅）。
      if (layout.width >= 640) {
        for (const field of await findFieldsOffWidthScale(page)) {
          problems.push(`${screen.name}: 幅の段に当たらない欄 ${field.name} ${field.width}px（置き場所 ${field.containerWidth}px）${field.reason ?? ""}`);
        }
      }
      // FormGrid の置き場所が 42〜58rem（2 列の境 57.5rem の前後）でも、欄が段の幅のままでセルからはみ出さない（#1891 段 5）。
      if (layout.width === 1280 && (await page.locator("[data-form-grid]").count()) > 0) {
        for (const rem of FORM_GRID_PROBE_WIDTHS_REM) {
          await setFormGridMaxWidth(page, rem);
          for (const field of await findFieldsOffWidthScale(page)) {
            problems.push(`${screen.name} FormGrid ${rem}rem: 幅の段に当たらない欄 ${field.name} ${field.width}px（置き場所 ${field.containerWidth}px）${field.reason ?? ""}`);
          }
          for (const text of await findNarrowFieldTexts(page)) {
            problems.push(`${screen.name} FormGrid ${rem}rem: 狭く折り返す補足 ${text.name} ${text.width}px < ${text.expectedMin}px`);
          }
          for (const clipped of await findClippedPlaceholders(page)) {
            problems.push(`${screen.name} FormGrid ${rem}rem: placeholder の切れ ${clipped.name}「${clipped.placeholder}」`);
          }
        }
        await setFormGridMaxWidth(page, null);
      }
      if (layout.width !== 1920 || layout.theme === "light") {
        await page.screenshot({ path: testInfo.outputPath(`${screen.name}-${layout.name.replaceAll(" ", "-")}.png`), fullPage: true });
      }
    }
    expect(problems).toEqual([]);
  });
}

test.describe("一覧の件数と表示件数（#1891）", () => {
  test("業務 Agent: 件数はツールバーの 1 か所、表示件数を 100 件にすると 1 ページに収まり、再読込でも残る", async ({ page, mockApi }) => {
    seedAgents(mockApi, 52);
    await page.goto("/agents");
    const toolbar = page.getByTestId("agent-list-toolbar");
    await expect(toolbar.locator("[data-list-count]")).toHaveText("全 55 件");
    const pagination = page.getByTestId("agent-list-pagination");
    await expect(pagination).toContainText("1–25 / 55 件");
    await jumpToPage(pagination, 3);
    await expect(pagination).toContainText("51–55 / 55 件");

    await pagination.getByRole("combobox", { name: "表示件数" }).click();
    await page.getByRole("option", { name: "100 件" }).click();
    // 表示件数を変えると 1 ページ目へ戻り、1 ページに収まるのでページ送りのボタンは出さない（範囲と表示件数だけ）。
    await expect(pagination).toContainText("1–55 / 55 件");
    await expect(pagination.getByRole("button", { name: "次へ" })).toHaveCount(0);
    await expect(page.getByRole("table", { name: "業務 Agent 一覧" }).locator("tbody tr")).toHaveCount(55);

    await page.reload();
    await expect(page.getByTestId("agent-list-pagination")).toContainText("1–55 / 55 件");

    // 絞り込み中は「N 件（全 M 件）」。
    await page.locator("#agent-search").fill("検証 Agent 1");
    await expect(toolbar.locator("[data-list-count]")).toHaveText(/^\d+ 件（全 55 件）$/);
  });

  test("実行履歴: 表示件数は API の limit になり、状態の絞り込み中は「N 件（全 M 件）」", async ({ page, mockApi }) => {
    seedRuns(mockApi, 53);
    await page.goto("/runs");
    const toolbar = page.getByTestId("run-list-toolbar");
    // 期間の既定（過去 7 日。#3386）で絞っているので、最初から「N 件（全 M 件）」。
    await expect(toolbar.locator("[data-list-count]")).toHaveText(/^\d+ 件（全 \d+ 件）$/);
    const pagination = page.getByTestId("run-history-pagination");
    await pagination.getByRole("combobox", { name: "表示件数" }).click();
    await page.getByRole("option", { name: "50 件" }).click();
    await expect.poll(() => mockApi.lastRequest("GET", "/api/runs")?.searchParams.get("limit")).toBe("50");
    await expect.poll(() => mockApi.lastRequest("GET", "/api/runs")?.searchParams.get("offset")).toBe("0");

    await withListFilters(toolbar, (scope) => chooseSelectFieldOption(scope.getByTestId("run-list-toolbar-status"), "succeeded"));
    await expect(toolbar.locator("[data-list-count]")).toHaveText(/^\d+ 件（全 \d+ 件）$/);
  });

  test("監査ログ: 1 ページの件数の入力は無く、ページ送りの表示件数で limit を変える", async ({ page, mockApi }) => {
    seedAuditRecords(mockApi, 54);
    await page.goto("/audit");
    await expect(page.locator("#audit-limit")).toHaveCount(0);
    await expect(page.getByTestId("audit-list-toolbar").locator("[data-list-count]")).toHaveText("全 54 件");
    const pagination = page.getByTestId("audit-pagination");
    await pagination.getByRole("combobox", { name: "表示件数" }).click();
    await page.getByRole("option", { name: "50 件" }).click();
    await expect.poll(() => mockApi.lastRequest("GET", "/api/audit/tool-calls")?.searchParams.get("limit")).toBe("50");
    await expect(pagination).toContainText("1–50 / 54 件");
    await page.reload();
    await expect(page.getByTestId("audit-pagination")).toContainText("1–50 / 54 件");
  });
});
