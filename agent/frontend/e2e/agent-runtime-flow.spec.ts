import type { Page, Route } from "@playwright/test";

import { LOCAL_CURRENT_USER } from "./fixtures/auth";
import { expect, test } from "./fixtures/mock-api";

const now = "2026-06-28T00:00:00Z";

const agent = {
  id: "default",
  name: "資料・データ横断アシスタント",
  description: "業務調査を行う",
  instructions: "根拠を示して回答する。",
  skill_ids: ["business-rag-research"],
  enabled: true,
  source: "builtin",
  // 公開中の v1（#770）。
  versioned: true,
  versions: [
    {
      version: 1,
      name: "資料・データ横断アシスタント",
      description: "業務調査を行う",
      instructions: "根拠を示して回答する。",
      skill_ids: ["business-rag-research"],
      model_id: "",
      note: "",
      published_at: now,
      published_by: null,
    },
  ],
  published_version: 1,
  unpublished_changes: false,
  created_at: now,
  updated_at: now,
};

const skill = {
  id: "business-rag-research",
  name: "業務 RAG 調査",
  description: "根拠付き情報を検索する",
  instructions: "検索して根拠を返す",
  mcp_requirements: [
    { server_id: "rag", tool_names: ["rag_search"] },
  ],
  references: [],
  enabled: true,
  tags: ["rag"],
  source: "builtin",
  created_at: now,
  updated_at: now,
};

// 組み込み Runtime（OpenAI Agents SDK + モデルの API。#754）の状態。
const runtimeStatus = {
  id: "builtin",
  name: "組み込み Runtime",
  sdk: "openai-agents",
  sdk_version: "0.22.3",
  model_provider: "OpenAI 互換の API（Responses API）",
  model_id: "xai.grok-4",
  ready: true,
  error_code: null,
  message: null,
  models: [
    { model_id: "xai.grok-4", display_name: "Grok 4" },
    { model_id: "openai.gpt-oss-120b", display_name: "gpt-oss-120b" },
  ],
};

function api(data: unknown) {
  return JSON.stringify({ data, error_messages: [], warning_messages: [] });
}

async function installControlPlaneApi(
  page: Page,
  options?: { notReady?: boolean; auditRecords?: Record<string, unknown>[]; agentPatches?: unknown[] }
) {
  let runs: Record<string, unknown>[] = [];
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    const respond = (data: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: api(data) });

    // ローカルの全権限の利用者（#215）。spec は `page.unroute("**/api/**")` で fixture の handler も外すため、ここで返す。
    if (path === "/api/auth/me") return respond(LOCAL_CURRENT_USER);
    // DB ゲートの状態 API（#325）。この spec は DB が使える前提。
    if (path === "/api/ready/database") return respond({ status: "ok", check: "ok", detail: null });
    if (path === "/api/agents") {
      if (method === "POST") return respond(agent);
      return respond({ agents: [agent] });
    }
    if (path.startsWith("/api/agents/")) {
      if (method === "PATCH") options?.agentPatches?.push(route.request().postDataJSON());
      return respond(agent);
    }
    if (path === "/api/skills") return respond({ skills: [skill], metadata: {} });
    // 保存先（#839）。この spec は保存している構成。
    if (path === "/api/runtime/storage") {
      return respond({ backend: "oracle_checkpoint", configured_backend: "auto", persistent: true, database_configured: true, reason: null });
    }
    if (path === "/api/runtime/status") {
      return respond(
        options?.notReady
          ? {
              ...runtimeStatus,
              model_id: "",
              ready: false,
              error_code: "runtime.model_not_configured",
              message: "使うモデルが決まっていません。システム設定 > モデル で既定のテキストモデルを設定してください。",
              models: [],
            }
          : runtimeStatus
      );
    }
    if (path === "/api/runs" && method === "POST") {
      const body = route.request().postDataJSON() as { goal: string };
      const run = {
        id: "run-control-plane-e2e",
        goal: body.goal,
        agent_id: "default",
        runtime_id: "builtin",
        status: "running",
        steps: [],
        events: [
          {
            id: "event-1",
            run_id: "run-control-plane-e2e",
            type: "run.status_changed",
            message: "実行を開始しました。",
            payload: { status: "running", runtime_id: "builtin" },
            created_at: now,
          },
        ],
        approvals: [],
        artifacts: [],
        pending_tool_calls: [],
        metadata: {},
        created_at: now,
        updated_at: now,
      };
      runs = [run];
      return respond(run);
    }
    // 実行履歴の一覧はサーバー側のページングと要約、詳細は実行 1 件（#1897）。
    if (path === "/api/runs") {
      const items = runs.map((item) => ({
        id: item.id,
        goal: item.goal,
        agent_id: item.agent_id,
        runtime_id: item.runtime_id,
        status: item.status,
        created_at: item.created_at,
        updated_at: item.updated_at,
        pending_approval_count: 0,
        pending_approval: null,
        resume_pending: false,
        source: "user",
      }));
      return respond({ items, total: items.length, limit: 25, offset: 0, has_next: false, unfiltered_total: items.length });
    }
    const detail = /^\/api\/runs\/([^/]+)$/.exec(path);
    if (detail && method === "GET") {
      const found = runs.find((item) => item.id === decodeURIComponent(detail[1]));
      return found ? respond(found) : respond(null, 404);
    }
    if (path.includes("/audit")) {
      return respond({ run_id: "run-control-plane-e2e", goal: "", status: "running", records: options?.auditRecords ?? [] });
    }
    if (path.includes("/artifacts")) return respond({ artifacts: [] });
    return respond({});
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth
    )
  ).toBe(true);
}

test.describe("AI Agent Control Plane", () => {
  test("業務 Agent は Skill とモデルを選び、実行先（Binding）の管理は無い", async ({ page }) => {
    const agentPatches: unknown[] = [];
    await installControlPlaneApi(page, { agentPatches });
    await page.goto("/agents");

    await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();
    // 一覧はモデル（空は既定のテキストモデル）を示す（#754）。
    await expect(page.getByRole("table", { name: "業務 Agent 一覧" }).getByText("既定のテキストモデル")).toBeVisible();
    await page.getByRole("link", { name: "資料・データ横断アシスタント default", exact: true }).click();
    await expect(page).toHaveURL(/\/agents\?id=default$/);
    await expect(page.getByRole("heading", { name: "資料・データ横断アシスタント", level: 1 })).toBeVisible();
    await expect(page.getByText("業務 RAG 調査").first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "実行先", exact: true })).toHaveCount(0);
    await expect(page.getByText("利用可能ツール")).toHaveCount(0);

    // モデルは「既定のテキストモデル（…）」か、登録モデルから選ぶ。
    const model = page.getByRole("combobox", { name: "モデル" });
    await expect(model).toContainText("既定のテキストモデル（xai.grok-4）");
    await model.click();
    await page.getByRole("option", { name: "gpt-oss-120b" }).click();
    await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("Agent を保存しました")).toBeVisible();
    expect(agentPatches.at(-1)).toMatchObject({ model_id: "openai.gpt-oss-120b" });

    await page.keyboard.press("Tab");
    await expectNoHorizontalOverflow(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await expect(model).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test("Run は Agent とゴールだけで作成し、組み込み Runtime が実行する", async ({ page }) => {
    await installControlPlaneApi(page);
    await page.goto("/runs?id=new");
    await expect(page.getByLabel("実行先 Binding")).toHaveCount(0);
    const goal = "契約情報を確認する";
    await page.locator("#run-goal").fill(goal);
    await page.getByRole("button", { name: "実行を作成" }).click();

    await expect(page.getByText("実行を作成しました", { exact: true })).toBeVisible();
    await expect(page.getByText(goal).first()).toBeVisible();
    // 実行環境とイベントの記録は処理の経過の中の「処理の詳細」に畳む（#1955 / #2325）。
    const technical = page.getByTestId("run-technical-section");
    await page.getByTestId("run-process-details").click();
    await expect(technical.locator("[data-process-identifier]")).toHaveCount(3);
    await expect(technical.locator("[data-run-used-models]")).toBeVisible();
    await expect(technical.locator('[data-run-fact="runtime"]')).toHaveCount(0);
    await expect(technical.getByTestId("run-event-log")).toHaveCount(0);
    // 実行中の Run は、サーバーの開始時刻からの経過時間を出す（#376）。
    const progress = page.getByTestId("run-progress");
    await expect(progress).toContainText("実行しています");
    await expect(progress).toHaveAttribute("data-processing-placement", "job");
    await expect(progress.getByRole("timer")).toHaveAccessibleName(/^経過時間 /);
  });

  test("モデルが未設定なら、Run の作成の前に理由を知らせる", async ({ page }) => {
    await installControlPlaneApi(page, { notReady: true });
    await page.goto("/runs?id=new");
    // 警告（warning の Banner。アイコン付き）で、見出しと理由を出す。
    await expect(page.getByText("業務 Agent を実行できません")).toBeVisible();
    await expect(page.getByText(/既定のテキストモデルを設定してください。/)).toBeVisible();
  });

  for (const viewport of [
    { name: "desktop", width: 1280, height: 800 },
    { name: "mobile", width: 375, height: 812 },
  ]) {
    test(`Runtime 画面は組み込み Runtime の状態と使うモデルを出す (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await installControlPlaneApi(page);
      await page.goto("/runtimes");

      await expect(page.getByRole("heading", { name: "実行環境", level: 1 })).toBeVisible();
      const card = page.getByTestId("builtin-runtime-card");
      await expect(card.getByRole("heading", { name: "組み込みの実行環境" })).toBeVisible();
      await expect(card.locator("[data-status-variant]", { hasText: "実行できます" }).locator("svg")).toHaveCount(1);
      await expect(card).toContainText("openai-agents 0.22.3");
      await expect(card).toContainText("xai.grok-4");
      await expect(card).toContainText("Grok 4、gpt-oss-120b");
      // 外部 Runtime の操作（Pull・起動・停止・ログ）は無い（#754）。
      await expect(page.getByRole("button", { name: "Pull" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "起動", exact: true })).toHaveCount(0);
      await expectNoHorizontalOverflow(page);
    });

    test(`モデルが未設定なら Runtime 画面で理由とモデルの設定への導線を出す (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await installControlPlaneApi(page, { notReady: true });
      await page.goto("/runtimes");
      const card = page.getByTestId("builtin-runtime-card");
      await expect(card.locator("[data-status-variant]", { hasText: "設定が必要" })).toBeVisible();
      await expect(card.getByText("業務 Agent を実行できません")).toBeVisible();
      await expect(card.getByText(/既定のテキストモデルを設定してください。/)).toBeVisible();
      await card.getByTestId("builtin-runtime-open-model-settings").click();
      await expect(page).toHaveURL(/\/settings\/model$/);
      await expectNoHorizontalOverflow(page);
    });
  }

  // #2403: 実行の詳細の監査ログは 1 行 = 1 記録（共通の RunEventLog）。失敗の行は最初から開き、要約とエラーコードを出す。
  test("監査ログのツールのエラーは行を開いて要約とエラーコードを出す", async ({ page }) => {
    await installControlPlaneApi(page, {
      auditRecords: [
        {
          step_id: "step-1",
          tool_name: "rag__rag_search",
          status: "failed",
          error: "RAG に接続できませんでした。",
          error_code: "mcp.http_error",
          guardrail_warnings: [],
          artifact_ids: [],
          audit_metadata: {},
        },
      ],
    });
    await page.goto("/runs?id=new");
    await page.locator("#run-goal").fill("契約情報を確認する");
    await page.getByRole("button", { name: "実行を作成" }).click();

    await page.getByTestId("run-audit-section").locator("summary").click();
    const row = page.getByTestId("run-audit-log-event-tool-step-1");
    await expect(row).toContainText("rag__rag_search");
    await expect(row.getByTestId("run-audit-log-event-tool-step-1-toggle")).toHaveAttribute("aria-expanded", "true");
    const details = row.getByTestId("run-audit-log-event-tool-step-1-details");
    await expect(details.getByText("RAG に接続できませんでした。")).toBeVisible();
    await expect(details).toContainText("エラーコード");
    await expect(details).toContainText("mcp.http_error");
    await page.setViewportSize({ width: 375, height: 812 });
    await expect(details).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
});
