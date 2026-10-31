import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectSelectFieldValue } from "./_helpers/select-field";
import {
  REQUIREMENT_CLARIFIED_QUESTION,
  REQUIREMENT_QUESTION,
  REQUIREMENT_SESSION_ID,
  requirementSessionData,
} from "./_helpers/requirement-session";

const safety = {
  is_safe: true,
  is_select_only: true,
  row_limit_applied: 0,
  blocked_reason: "",
  warnings: [],
  referenced_tables: ["APP.INVOICES"],
  referenced_columns: ["APP.INVOICES.TOTAL_AMOUNT"],
};

const timing = {
  created_at: "2026-08-16T00:00:00.000Z",
  started_at: "2026-08-16T00:00:00.010Z",
  finished_at: "2026-08-16T00:00:00.160Z",
  elapsed_ms: 150,
  stage_timings: [
    { stage: "prepare_context", elapsed_ms: 10 },
    { stage: "generate_sql", elapsed_ms: 40 },
    { stage: "safety_check", elapsed_ms: 20 },
    { stage: "execute_sql", elapsed_ms: 50 },
    { stage: "format_results", elapsed_ms: 30 },
  ],
};

const profile = {
  id: "default",
  name: "既定プロファイル",
  category: "既定",
  description: "請求を扱うプロファイル",
  allowed_tables: ["APP.INVOICES"],
  allowed_views: [],
  glossary: { 請求金額: "INVOICES.TOTAL_AMOUNT" },
  sql_rules: ["SELECT のみ"],
  default_row_limit: 100,
  safety_policy: "select_only",
  few_shot_examples: [],
  select_ai_config: {
    profile_name: "NL2SQL_DEFAULT_PROFILE",
    region: "ap-osaka-1",
    model: "cohere.command-r-plus",
    embedding_model: "cohere.embed-v4.0",
    max_tokens: 32000,
    enforce_object_list: true,
    comments: true,
    annotations: false,
    constraints: false,
    role: "",
    additional_instructions: "",
  },
  archived: false,
  version: 1,
  etag: "profile-etag",
  updated_at: "2026-08-16T00:00:00.000Z",
};

const schemaTable = {
  table_name: "INVOICES",
  qualified_name: "APP.INVOICES",
  logical_name: "請求情報を管理するテーブル",
  owner: "APP",
  table_type: "TABLE",
  comment: "請求情報",
  row_count: 2,
  columns: [
    {
      column_name: "TOTAL_AMOUNT",
      logical_name: "請求金額",
      data_type: "NUMBER",
      nullable: false,
      comment: "税込請求金額",
      sample_values: ["1200000"],
    },
  ],
  constraints: [],
};

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

async function mockNl2SqlWorkbenchApi(page: Page) {
  const state: {
    jobPayload: Record<string, unknown> | null;
    rewritePayload: Record<string, unknown> | null;
  } = { jobPayload: null, rewritePayload: null };
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: profile.id,
          name: profile.name,
          category: profile.category,
          description: profile.description,
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 1,
          few_shot_count: 0,
          version: 1,
          etag: profile.etag,
          updated_at: profile.updated_at,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default", (route) => fulfillJson(route, profile));
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: profile.archived,
      object_scope_version: 1,
      version: profile.version,
      etag: profile.etag,
      updated_at: profile.updated_at,
    })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-fixture",
      refreshed_at: "2026-08-16T00:00:00.000Z",
      object_count: 1,
      column_count: 1,
      change_token: 1,
      etag: "schema-etag",
    })
  );
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          owner: "APP",
          object_name: "INVOICES",
          object_type: "TABLE",
          logical_name: schemaTable.logical_name,
          comment: schemaTable.comment,
          row_count: schemaTable.row_count,
          column_count: schemaTable.columns.length,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      catalog_version: 1,
    })
  );
  await page.route("**/api/schema/objects/APP/INVOICES", (route) =>
    fulfillJson(route, {
      table: schemaTable,
      dependencies: [],
      catalog_version: 1,
      etag: "schema-etag",
    })
  );
  await page.route("**/api/nl2sql/history**", (route) =>
    fulfillJson(route, { items: [], total: 0 })
  );
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "default",
      recommended_profile_name: "既定プロファイル",
      confidence: 0.2,
      recommendation_source: "deterministic",
      reasons: [],
      recommended_allowed_objects: { table_names: ["APP.INVOICES"], columns: {} },
    })
  );
  await page.route("**/api/nl2sql/similar-history", (route) =>
    fulfillJson(route, { items: [] })
  );
  await page.route("**/api/nl2sql/rewrite", (route) => {
    state.rewritePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      original_question: "請求金額を確認したい",
      rewritten_question: "書き換え後の請求金額",
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    state.jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      job_id: "job-options-001",
      status: "pending",
      created_at: timing.created_at,
      steps: [],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-options-001", (route) =>
    fulfillJson(route, {
      job_id: "job-options-001",
      status: "done",
      created_at: timing.created_at,
      started_at: timing.started_at,
      finished_at: timing.finished_at,
      elapsed_ms: timing.elapsed_ms,
      error_message: null,
      timing,
      steps: timing.stage_timings.map((item) => ({
        stage: item.stage,
        status: "done",
        elapsed_ms: item.elapsed_ms,
      })),
      result: {
        engine: "select_ai",
        engine_meta: { runtime: "oracle", select_ai_profile: "NL2SQL_DEFAULT_PROFILE" },
        fallback_reason: "",
        original_question: "請求金額を確認したい",
        rewritten_question: "書き換え後の請求金額",
        generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        explanation: "SQL を生成しました。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["TOTAL_AMOUNT"],
          rows: [{ TOTAL_AMOUNT: 1200000 }],
          total: 1,
        },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: "請求金額を確認したい",
            rewritten_question: "書き換え後の請求金額",
            profile_id: "default",
            profile_name: "既定プロファイル",
            target_objects: ["APP.INVOICES"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: ["SUM"],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "APP.INVOICES を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["APP.INVOICES"],
            columns: ["APP.INVOICES.TOTAL_AMOUNT"],
            joins: [],
            filters: [],
            aggregations: ["SUM"],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: [
              "APP.INVOICES を参照し、SELECT 操作を行います。",
              "集計: SUM",
            ],
            logical_step_details: [
              {
                kind: "summary",
                business: "請求情報を対象に、一覧の取得・集計を行います。",
                technical: "APP.INVOICES を参照し、SELECT 操作を行います。",
              },
              { kind: "aggregation", business: "合計を計算します", technical: "集計: SUM" },
            ],
            semantic_graph: {},
            warnings: [],
          },
          warnings: [],
        },
        show_prompt: {
          available: true,
          engine: "select_ai",
          action: "showprompt",
          prompt: "Select AI prompt body\nUse APP.INVOICES only.",
          unavailable_reason: "",
          warnings: [],
        },
      },
    })
  );
  return state;
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(() => {
        const element = document.scrollingElement ?? document.documentElement;
        return element.scrollWidth - element.clientWidth;
      })
    )
    .toBeLessThanOrEqual(2);
}

const guidedRevision = {
  id: "ontology-guided-r1",
  version: 1,
  status: "published",
  schema_fingerprint: "schema-fixture",
  etag: "ontology-etag",
};

const guidedRecommendation = {
  id: "recommendation-guided-1",
  question_hash: "question-hash-guided",
  ontology_revision_id: guidedRevision.id,
  candidates: [
    {
      profile_id: "default",
      profile_name: "既定プロファイル",
      ontology_revision_id: guidedRevision.id,
      score: 0.95,
      matched_scenarios_ja: ["受注分析"],
      matched_terms: ["受注"],
      reasons_ja: ["質問が既定プロファイルの受注分析に一致します。"],
    },
  ],
  selected_profile_id: "default",
  selected_revision_id: guidedRevision.id,
  expires_at: "2026-09-06T00:10:00.000Z",
};

test("unified execute button runs SQL and renders execution artifacts", async ({ page }) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  await page.goto("/query");

  await expect(page.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "質問を解釈" })).toHaveCount(0);
  await expect(page.getByText("履歴 0 件")).toHaveCount(0);
  const executionOptionsDisclosure = page.getByRole("button", { name: /実行オプション/ });
  const executionOptionsChevron = executionOptionsDisclosure.locator('svg[data-state]');
  await expect(executionOptionsDisclosure).toHaveAttribute("aria-expanded", "false");
  await expect(executionOptionsChevron).toHaveAttribute("data-state", "collapsed");
  await expect(executionOptionsChevron).toHaveClass(/rotate-90/);
  await expect(page.getByLabel("公開版オントロジーを使う")).toBeHidden();
  await executionOptionsDisclosure.focus();
  await page.keyboard.press("Enter");
  await expect(executionOptionsDisclosure).toHaveAttribute("aria-expanded", "true");
  await expect(executionOptionsChevron).toHaveAttribute("data-state", "expanded");
  await expect(executionOptionsChevron).toHaveClass(/rotate-0/);
  const glossaryOption = page.getByLabel("用語・同義語を使う");
  // 用語・同義語は既定 off。ON にしたときだけ「条件あり」バッジが出る。
  await expect(glossaryOption).not.toBeChecked();
  await expect(page.getByLabel("Schema を使う")).toHaveCount(0);
  await expect(page.getByLabel("公開版オントロジーを使う")).toBeChecked();
  await expect(page.getByLabel("処理手順を表示")).toBeChecked();
  // Show Prompt は追加の Select AI 呼び出しを伴うため既定 off。
  const showPromptOption = page.getByLabel("Show Prompt を表示");
  await expect(showPromptOption).not.toBeChecked();
  await expect(executionOptionsDisclosure).not.toContainText("条件あり");
  await glossaryOption.check();
  await expect(executionOptionsDisclosure).toContainText("条件あり");
  await glossaryOption.uncheck();
  await expect(executionOptionsDisclosure).not.toContainText("条件あり");
  // 以降の rewrite / Show Prompt 呼び出し検証のため ON にする。
  await glossaryOption.check();
  await showPromptOption.check();
  await expect(page.getByTestId("nl2sql-execution-options")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect.poll(() => api.rewritePayload).not.toBeNull();
  expect(api.rewritePayload).toEqual({
    question: "請求金額を確認したい",
    profile_id: "default",
    use_glossary: true,
  });
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({
    question: "請求金額を確認したい",
    use_glossary: true,
    use_ontology_context: true,
    include_interpretation: true,
    include_show_prompt: true,
  });
  await expect(page.getByRole("textbox", { name: "生成 SQL" })).toHaveValue(
    /SELECT TOTAL_AMOUNT FROM INVOICES/
  );
  await expect(page.getByTestId("nl2sql-interpretation-panel")).toHaveCount(0);
  // 処理手順パネルは semantic graph が無くても logical_steps だけで表示できる。
  const logicalStepsPanel = page.getByTestId("nl2sql-logical-steps-panel");
  await expect(logicalStepsPanel).toBeVisible();
  await expect(logicalStepsPanel).toContainText("SQL の処理手順");
  // 各手順は業務者向けの説明を主、技術詳細(SQL 断片)を副として併記する。
  await expect(logicalStepsPanel).toContainText("請求情報を対象に、一覧の取得・集計を行います。");
  await expect(logicalStepsPanel).toContainText("合計を計算します");
  await expect(logicalStepsPanel).toContainText("集計: SUM");
  await expect(logicalStepsPanel).not.toContainText("先頭 100 件だけ取り出します");
  await expect(logicalStepsPanel).not.toContainText("件数制限: 上位100件");
  await expect(logicalStepsPanel.locator("ol > li")).toHaveCount(2);
  await expect(logicalStepsPanel.getByText("技術詳細").first()).toBeAttached();
  await expect(page.getByText("入力と生成 SQL の対応")).toHaveCount(0);
  await expect(page.getByText("入力テンプレート")).toHaveCount(0);
  await expect(page.getByText("生成 SQL の意味")).toHaveCount(0);
  // summary 文は旧「生成 SQL の意味」パネルではなく処理手順パネルの手順 1 としてのみ現れる。
  await expect(page.getByText("APP.INVOICES を参照し、SELECT 操作を行います。")).toHaveCount(1);
  await expect(page.getByText("1200000")).toBeVisible();

  const showPromptPanel = page.getByTestId("nl2sql-show-prompt-panel");
  const showPromptSummary = showPromptPanel.locator("summary");
  // 共有の Disclosure（#397）の Chevron。開閉の状態は data-state と回転で示す。
  const showPromptChevron = showPromptSummary.locator("svg[data-state]");
  const showPromptBody = showPromptPanel.getByText("Select AI prompt body");
  await expect(showPromptPanel).toBeVisible();
  await expect(showPromptBody).toBeHidden();
  await expect(showPromptChevron).toHaveAttribute("data-state", "collapsed");
  await expect(showPromptChevron).toHaveClass(/rotate-90/);

  await showPromptSummary.click();
  await expect(showPromptBody).toBeVisible();
  await expect(showPromptChevron).toHaveAttribute("data-state", "expanded");
  await expect(showPromptChevron).toHaveClass(/rotate-0/);

  await showPromptSummary.click();
  await expect(showPromptBody).toBeHidden();
  await expect(showPromptChevron).toHaveAttribute("data-state", "collapsed");
  await expect(showPromptChevron).toHaveClass(/rotate-90/);

  await showPromptSummary.focus();
  await page.keyboard.press("Enter");
  await expect(showPromptBody).toBeVisible();
  await expect(showPromptChevron).toHaveAttribute("data-state", "expanded");
  await expect(showPromptChevron).toHaveClass(/rotate-0/);

  await showPromptSummary.focus();
  await page.keyboard.press("Space");
  await expect(showPromptBody).toBeHidden();
  await expect(showPromptChevron).toHaveAttribute("data-state", "collapsed");
  await expect(showPromptChevron).toHaveClass(/rotate-90/);

  const ontologyOption = page.getByLabel("公開版オントロジーを使う");
  await ontologyOption.focus();
  await expect(ontologyOption).toBeFocused();
  await page.keyboard.press("Space");
  await expect(ontologyOption).not.toBeChecked();
  api.jobPayload = null;
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({
    use_ontology_context: false,
  });
  await expect(executionOptionsDisclosure).toContainText("条件あり");
  const resetButton = page.getByRole("button", { name: "新しい質問を開始", exact: true });
  await expect(resetButton).toBeEnabled();
  await resetButton.click();
  await page.getByRole("alertdialog", { name: "未保存の入力を破棄しますか？" }).getByRole("button", { name: "新しい質問を開始" }).click();
  await expect(executionOptionsDisclosure).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByLabel("公開版オントロジーを使う")).toBeHidden();
  await executionOptionsDisclosure.click();
  await expect(page.getByLabel("公開版オントロジーを使う")).toBeChecked();
  // リセットで用語・同義語は既定の off に戻る。
  await expect(page.getByLabel("用語・同義語を使う")).not.toBeChecked();
  await expect(page.getByLabel("Schema を使う")).toHaveCount(0);
  await expect(page.getByLabel("処理手順を表示")).toBeChecked();
  await expect(page.getByLabel("Show Prompt を表示")).not.toBeChecked();
  await expectNoHorizontalOverflow(page);
});

test("回答前の確認は「この内容で SQL を生成して実行」で、構造化した要件を SQL の生成と実行の job に渡す (#1761)", async ({ page }) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  const calls: Record<string, Record<string, unknown> | null> = {
    recommendationPayload: null,
    confirmationPayload: null,
    createPayload: null,
    answerPayload: null,
    applyPayload: null,
  };

  await page.route("**/api/nl2sql/ontology/profile-recommendations", (route) => {
    calls.recommendationPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, { recommendation: guidedRecommendation });
  });
  await page.route(
    "**/api/nl2sql/ontology/profile-recommendations/recommendation-guided-1/confirm",
    (route) => {
      calls.confirmationPayload = route.request().postDataJSON() as Record<string, unknown>;
      return fulfillJson(route, {
        recommendation: guidedRecommendation,
        confirmation_token: "profile-confirmation-token",
      });
    }
  );
  await page.route("**/api/nl2sql/query-sessions", (route) => {
    calls.createPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, requirementSessionData("needs_answer"));
  });
  await page.route(`**/api/nl2sql/query-sessions/${REQUIREMENT_SESSION_ID}/clarification-answers`, (route) => {
    calls.answerPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, requirementSessionData("ready", 2));
  });
  await page.route(`**/api/nl2sql/query-sessions/${REQUIREMENT_SESSION_ID}/apply`, (route) => {
    calls.applyPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, requirementSessionData("applied", 2));
  });

  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await page.locator("#nl2sql-question-input").fill(REQUIREMENT_QUESTION);
  const startButton = page.getByRole("button", { name: "回答前の確認", exact: true });
  await expect(startButton).toBeEnabled();
  await startButton.click();

  const panel = page.getByTestId("nl2sql-guided-clarification");
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  await panel.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await panel.getByRole("button", { name: "この内容で回答" }).click();

  await expect(
    panel.locator('[data-status-variant="success"]').filter({ hasText: "確認完了" })
  ).toBeVisible();
  await expect(panel.getByTestId("nl2sql-clarification-skeleton")).toContainText("FROM APP.ORDERS");
  // 押すまで要件を生成に渡さない（#1761）。
  expect(api.jobPayload).toBeNull();
  // チャットと同じ文言・同じ操作で、確定した要件で SQL を生成して実行する（押し直させない。#1761）。
  await panel.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();

  const questionInput = page.locator("#nl2sql-question-input");
  await expect(panel).toHaveCount(0);
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({
    question: REQUIREMENT_QUESTION,
    requirement_session_id: REQUIREMENT_SESSION_ID,
  });
  // 利用者が入力した質問は書き換えない（#1727）。
  await expect(questionInput).toHaveValue(REQUIREMENT_QUESTION);
  // 確かめた質問の文は質問の欄でなく、要件の表示に出す。
  await page.getByTestId("nl2sql-requirement-question").click();
  await expect(page.getByTestId("nl2sql-requirement-active")).toContainText(REQUIREMENT_CLARIFIED_QUESTION);
  await expectNoHorizontalOverflow(page);

  expect(calls.recommendationPayload).toMatchObject({ question: REQUIREMENT_QUESTION, limit: 3 });
  expect(calls.confirmationPayload).toMatchObject({
    selected_profile_id: "default",
    selected_revision_id: guidedRevision.id,
  });
  expect(calls.createPayload).toMatchObject({
    question: REQUIREMENT_QUESTION,
    profile_id: "default",
    engine: "select_ai",
    profile_confirmation_token: "profile-confirmation-token",
    clarification_mode: "guided",
  });
  expect(calls.answerPayload).toEqual({
    base_version: 1,
    item_id: "item-period",
    selected_candidate_ids: ["candidate-fy-april"],
    free_text: "",
  });
  expect(calls.applyPayload).toEqual({ base_version: 2 });
});

test("glossary option off skips rewrite and sends the original question", async ({ page }) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  await page.goto("/query");

  await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.rewritePayload).toBeNull();
  expect(api.jobPayload).toMatchObject({
    question: "請求金額を確認したい",
    use_glossary: false,
  });
  await expect(page.getByText("生成に使用される質問")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("editing the question clears the previous rewrite card", async ({ page }) => {
  await mockNl2SqlWorkbenchApi(page);
  await page.goto("/query");

  await page.getByRole("button", { name: /実行オプション/ }).click();
  await page.getByLabel("用語・同義語を使う").check();
  const questionInput = page.locator("#nl2sql-question-input");
  await questionInput.fill("請求金額を確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const rewriteCard = page.getByTestId("nl2sql-rewrite-card");
  await expect(rewriteCard.getByText("生成に使用される質問")).toBeVisible();
  await expect(rewriteCard.getByText("書き換え後の請求金額")).toBeVisible();

  await questionInput.fill("社員の一覧");

  await expect(page.getByTestId("nl2sql-rewrite-card")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "適用" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("select ai overrides show inactive notice for other engines and role can collapse", async ({
  page,
}) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  await page.goto("/query");

  const overridesDisclosure = page.getByRole("button", { name: /今回だけの生成条件/ });
  await overridesDisclosure.click();
  await page.getByLabel("今回の追加条件").fill("最新月だけを対象にする。");
  const roleDisclosure = page.getByRole("button", { name: /ロールを上書き/ });
  await roleDisclosure.click();
  await page.getByLabel("アシスタントロール").fill("財務 SQL アシスタントとして回答する。");
  await expect(roleDisclosure).toHaveAttribute("aria-expanded", "true");

  await roleDisclosure.click();
  await expect(roleDisclosure).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByLabel("アシスタントロール")).toBeHidden();

  await page.getByRole("button", { name: /Enterprise AI Direct/ }).click();
  const executionOptionsDisclosure = page.getByRole("button", { name: /実行オプション/ });
  await expect(executionOptionsDisclosure).toContainText("条件あり");
  await executionOptionsDisclosure.click();
  await expect(page.getByText("今回だけの生成条件は Select AI 実行時のみ適用されます。")).toBeVisible();
  await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({
    engine: "enterprise_ai_direct",
    select_ai_overrides: null,
  });

  api.jobPayload = null;
  await page.getByRole("button", { name: /^Select AI 質問に表や項目/ }).click();
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({
    engine: "select_ai",
    select_ai_overrides: {
      role: "財務 SQL アシスタントとして回答する。",
      additional_instructions: "最新月だけを対象にする。",
    },
  });
  await expectNoHorizontalOverflow(page);
});

test("実行エンジンの初期の選択は業務プロファイルの既定の生成方式 (#1524)", async ({ page }) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  // 後から登録した route が先に当たる。
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      default_engine: "select_ai_agent",
      archived: profile.archived,
      object_scope_version: 1,
      version: profile.version,
      etag: profile.etag,
      updated_at: profile.updated_at,
    })
  );
  await page.goto("/query");
  await expect(page.getByRole("button", { name: /^Select AI Agent/ })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({ engine: "select_ai_agent" });
});

test("execution options keep ontology toggle usable at mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await mockNl2SqlWorkbenchApi(page);
  await page.goto("/query");

  const options = page.getByTestId("nl2sql-execution-options");
  const executionOptionsDisclosure = page.getByRole("button", { name: /実行オプション/ });
  const ontologyOption = page.getByLabel("公開版オントロジーを使う");
  await expect(options).toBeVisible();
  await expect(executionOptionsDisclosure).toHaveAttribute("aria-expanded", "false");
  await expect(ontologyOption).toBeHidden();
  await executionOptionsDisclosure.focus();
  await page.keyboard.press("Space");
  await expect(executionOptionsDisclosure).toHaveAttribute("aria-expanded", "true");
  await expect(ontologyOption).toBeVisible();
  await expect(ontologyOption).toBeChecked();
  await ontologyOption.focus();
  await page.keyboard.press("Space");
  await expect(ontologyOption).not.toBeChecked();
  await expectNoHorizontalOverflow(page);
});

test("用語置換が起きないときは書き換えカードを表示せず、入力そのままで実行する", async ({
  page,
}) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  // 後から登録した route が優先される。無変換（= 入力と同一）の応答を返す。
  const question =
    'SELECT "e"."EMPLOYEE_ID","e"."DEPARTMENT_ID" FROM "ADMIN"."EMPLOYEE" "e"';
  await page.route("**/api/nl2sql/rewrite", (route) =>
    fulfillJson(route, {
      original_question: question,
      rewritten_question: question,
      source: "deterministic",
      model: "",
      warnings: [],
    })
  );
  await page.goto("/query");

  // 用語・同義語は既定 off のため、書き換え経路を通すには明示的に ON にする。
  await page.getByRole("button", { name: /実行オプション/ }).click();
  await page.getByLabel("用語・同義語を使う").check();

  await page.locator("#nl2sql-question-input").fill(question);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect.poll(() => api.jobPayload).not.toBeNull();
  // 「先頭100件」のような件数表現を足さず、入力そのままが job へ渡る。
  expect(api.jobPayload).toMatchObject({ question, use_glossary: true });
  await expect(page.getByText("生成に使用される質問")).toHaveCount(0);
  await expect(page.getByText("変更前の質問")).toHaveCount(0);
});

test("Select AI Credential が無いと Select AI の生成の前に事前条件を示して実行を止め、Enterprise AI は止めない (#1688)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlWorkbenchApi(page);
  const readiness = { exists: false };
  await page.route("**/api/nl2sql/select-ai/credential-readiness", (route) =>
    fulfillJson(route, {
      credential_required: true,
      credential_name: "NL2SQL_OCI_CRED",
      credential_exists: readiness.exists,
    })
  );
  await page.goto("/query");
  await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
  await page.getByRole("button", { name: /^Select AI(?! Agent)/ }).click();

  const precondition = page.getByTestId("nl2sql-credential-precondition");
  const run = page.getByRole("button", { name: "SQL を生成して実行" });
  await expect(precondition).toContainText("Select AI Credential が作成されていません");
  await expect(precondition).toContainText(
    "Select AI・Select AI Agent での SQL の生成には Select AI Credential（NL2SQL_OCI_CRED）が必要です。作成するまでSQL を生成して実行できません。"
  );
  await expect(precondition.getByRole("link", { name: "Select AI Credential を開く" })).toHaveAttribute(
    "href",
    /\/settings\/select-ai-credential$/u
  );
  await expect(run).toBeDisabled();
  await expect(run).toHaveAttribute("aria-describedby", "nl2sql-credential-precondition");
  // 案内は実行の操作の行の直前に、同じ幅で出す。
  const [noticeBox, runBox] = await Promise.all([precondition.boundingBox(), run.boundingBox()]);
  expect(noticeBox!.y + noticeBox!.height).toBeLessThanOrEqual(runBox!.y);
  await expectNoHorizontalOverflow(page);
  await precondition.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("query-credential-missing.png") });

  // Select AI Agent も止める。OCI Enterprise AI の直接の生成は Credential を使わないので止めない。
  await page.getByRole("button", { name: /^Select AI Agent/ }).click();
  await expect(precondition).toBeVisible();
  await page.getByRole("button", { name: /^Enterprise AI Direct/ }).click();
  await expect(precondition).toHaveCount(0);
  await expect(run).toBeEnabled();

  // Credential を作った後に「再試行」すると、Select AI でも実行できる。
  await page.getByRole("button", { name: /^Select AI(?! Agent)/ }).click();
  readiness.exists = true;
  await precondition.getByRole("button", { name: "再試行" }).click();
  await expect(precondition).toHaveCount(0);
  await run.click();
  await expect.poll(() => api.jobPayload).not.toBeNull();
  expect(api.jobPayload).toMatchObject({ engine: "select_ai" });
});
