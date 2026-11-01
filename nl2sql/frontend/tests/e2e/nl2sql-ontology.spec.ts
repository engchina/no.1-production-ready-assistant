import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import {
  REQUIREMENT_CLARIFIED_QUESTION,
  REQUIREMENT_PENDING_HINT,
  REQUIREMENT_QUESTION,
  REQUIREMENT_SESSION_ID,
  REQUIREMENT_TARGET_PENDING_HINT,
  TARGET_OTHER_TEXT,
  TARGET_QUESTION,
  requirementSessionData,
  type RequirementStage,
} from "./_helpers/requirement-session";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

const catalog = {
  refreshed_at: "2026-07-11T00:00:00Z",
  schema_fingerprint: "schema-fingerprint",
  tables: [
    {
      table_name: "ORDERS",
      logical_name: "受注",
      owner: "APP",
      table_type: "table",
      comment: "受注データ",
      row_count: 3,
      constraints: ["PK_ORDERS P(ID)"],
      constraint_details: [
        {
          constraint_name: "PK_ORDERS",
          constraint_type: "P",
          owner: "APP",
          table_name: "ORDERS",
          columns: ["ID"],
          referenced_columns: [],
        },
      ],
      columns: [
        {
          column_name: "ID",
          logical_name: "受注 ID",
          data_type: "NUMBER",
          nullable: false,
          comment: "受注 ID",
          sample_values: ["1"],
        },
      ],
    },
  ],
  view_dependencies: [],
};

const profile = {
  id: "default",
  name: "標準プロファイル",
  category: "販売",
  description: "受注分析",
  allowed_tables: ["ORDERS"],
  allowed_views: [],
  glossary: {},
  sql_rules: [],
  default_row_limit: 100,
  safety_policy: "select_only",
  few_shot_examples: [],
  archived: false,
};

const graphNode = {
  id: "physical-orders",
  revision_id: "revision-1",
  kind: "table",
  technical_name: "APP.ORDERS",
  business_name_ja: "受注",
  description_ja: "受注データ",
  aliases: ["ORDERS"],
  physical_mappings: [],
  provenance: { source_kind: "introspected" },
  confidence: 1,
  review_status: "approved",
  metadata: { owner: "APP", object_name: "ORDERS" },
};

const metricNode = {
  ...graphNode,
  id: "metric-order-amount",
  kind: "metric",
  technical_name: "order_amount",
  business_name_ja: "受注金額",
  description_ja: "確定した受注の金額",
  aliases: ["売上金額"],
  metadata: {},
};

const orderDateNode = {
  ...graphNode,
  id: "property-order-date",
  kind: "property",
  technical_name: "APP.ORDERS.ORDER_DATE",
  business_name_ja: "受注日",
  description_ja: "受注が確定した日付",
  aliases: ["注文日"],
  metadata: { owner: "APP", object_name: "ORDERS", column_name: "ORDER_DATE" },
};

const orderStatusNode = {
  ...graphNode,
  id: "property-order-status",
  kind: "property",
  technical_name: "APP.ORDERS.STATUS",
  business_name_ja: "受注状態",
  description_ja: "受注の業務状態",
  aliases: ["ステータス"],
  metadata: { owner: "APP", object_name: "ORDERS", column_name: "STATUS" },
};

const statusRuleNode = {
  ...graphNode,
  id: "business-rule-order-status",
  kind: "business_rule",
  technical_name: "order_status_required",
  business_name_ja: "受注状態の必須ルール",
  description_ja: "受注状態は必須です。",
  aliases: [],
  metadata: {},
  business_rule_definition: {
    rule_kind: "validation",
    statement_ja: "受注状態は必須です。",
    applies_to_node_ids: ["property-order-status"],
    expression: { operator: "not_null", property_node_id: "property-order-status" },
    severity: "violation",
    execution_mode: "shacl",
  },
};

const confirmedStatusNode = {
  ...graphNode,
  id: "enum-order-status-confirmed",
  kind: "enum_value",
  technical_name: "order_status_confirmed",
  business_name_ja: "確定済み",
  description_ja: "確定した受注状態",
  aliases: ["確定"],
  metadata: {},
  enum_value_definition: {
    code: "CONFIRMED",
    label_ja: "確定済み",
    aliases: ["確定"],
    physical_literal: "CONFIRMED",
    data_type: "string",
    property_node_id: "property-order-status",
  },
};

const customerNode = {
  ...graphNode,
  id: "physical-customers",
  kind: "table",
  technical_name: "APP.CUSTOMERS",
  business_name_ja: "顧客",
  description_ja: "顧客データ",
  aliases: ["CUSTOMERS"],
  metadata: { owner: "APP", object_name: "CUSTOMERS" },
};

const orderCustomerEdge = {
  id: "edge-order-customer",
  revision_id: "revision-1",
  kind: "business_relationship",
  source_node_id: "physical-orders",
  target_node_id: "physical-customers",
  relationship_name_ja: "受注の顧客",
  description_ja: "受注を行った顧客",
  direction: "directed",
  cardinality: "many_to_one",
  join_conditions: [],
  allowed_join_types: ["inner", "left"],
  provenance: { source_kind: "curated" },
  confidence: 1,
  review_status: "approved",
  metadata: {},
};

const revision = {
  id: "revision-1",
  version: 1,
  status: "draft",
  schema_fingerprint: "schema-fingerprint",
  etag: "revision-etag",
  created_at: "2026-07-11T00:00:00Z",
};

const intent = {
  version: 1,
  question_original: "受注件数を表示",
  question_effective: "受注件数を表示",
  profile_view_id: "profile-view-1",
  ontology_revision_id: "revision-1",
  entities: [
    {
      id: "intent-entity-1",
      ontology_node_id: "physical-orders",
      name_ja: "受注",
      role: "subject",
      physical_object_ids: ["physical-orders"],
    },
  ],
  metrics: [
    {
      id: "intent-metric-1",
      ontology_node_id: "",
      name_ja: "受注件数",
      aggregation: "COUNT",
    },
  ],
  dimensions: [],
  filters: [],
  granularity: "",
  sorts: [],
  limit: null as number | null,
  candidate_paths: [
    {
      id: "path-order-customer",
      name_ja: "受注から顧客",
      edge_ids: ["edge-order-customer"],
      node_ids: ["physical-orders", "physical-customers"],
      approved: true,
      explanation_ja: "Profile で確認済みの顧客関係",
    },
  ],
  selected_path_id: null,
  ambiguities: [],
  confidence: 0.9,
  created_at: "2026-07-11T00:00:00Z",
};

const validation = {
  id: "validation-1",
  intent_version: 1,
  sql_hash: "sql-hash",
  ontology_revision_id: "revision-1",
  is_valid: true,
  intent_coverage: 1,
  findings: [
    {
      id: "finding-1",
      code: "ONTOLOGY_THREE_WAY_VALIDATED",
      severity: "pass",
      message_ja: "質問、SQL、Profile の意味が一致しています。",
    },
  ],
  passed_count: 1,
  warning_count: 0,
  blocker_count: 0,
  validation_hash: "validation-hash",
  created_at: "2026-07-11T00:00:00Z",
};

const artifact = {
  id: "artifact-1",
  intent_version: 1,
  ontology_revision_id: "revision-1",
  sql: "SELECT COUNT(*) AS ORDER_COUNT FROM APP.ORDERS",
  sql_hash: "sql-hash",
  generation_context_hash: "context-hash",
  semantic_graph: {
    version: 1,
    sql_hash: "sql-hash",
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: "SELECT COUNT(*) AS ORDER_COUNT FROM APP.ORDERS",
    parse_complete: true,
    ctes: [],
    tables: [
      {
        id: "sql-table-1",
        scope_id: "scope-1",
        owner: "APP",
        name: "ORDERS",
        qualified_name: "APP.ORDERS",
        source_sql: "APP.ORDERS",
        is_cte: false,
      },
    ],
    columns: [],
    joins: [],
    projections: [
      {
        id: "projection-1",
        scope_id: "scope-1",
        output_name: "ORDER_COUNT",
        expression_sql: "COUNT(*) AS ORDER_COUNT",
        referenced_columns: [],
        contains_aggregate: true,
        contains_window: false,
      },
    ],
    filters: [],
    aggregates: [
      {
        id: "aggregate-1",
        scope_id: "scope-1",
        function_name: "COUNT",
        expression_sql: "COUNT(*)",
        referenced_columns: [],
      },
    ],
    groups: [],
    having: [],
    orders: [],
    limit: null as number | null,
    windows: [],
    set_operations: [],
    subqueries: [],
    lineage: [],
    parse_warnings: [],
  },
  validation_report: validation,
  created_at: "2026-07-11T00:00:00Z",
};

function session(status: string, withSql = false, confirmed = false) {
  return {
    id: "session-1",
    profile_id: "default",
    profile_view_id: "profile-view-1",
    ontology_revision_id: "revision-1",
    status,
    original_question: "受注件数を表示",
    current_intent_version: 1,
    intents: [intent],
    sql_artifacts: withSql ? [artifact] : [],
    current_sql_artifact_id: withSql ? "artifact-1" : null,
    intent_confirmed_version: withSql ? 1 : null,
    sql_confirmation: confirmed
      ? {
          artifact_id: "artifact-1",
          ontology_revision_id: "revision-1",
          intent_version: 1,
          sql_hash: "sql-hash",
          validation_hash: "validation-hash",
          generation_context_hash: "context-hash",
          confirmed_at: "2026-07-11T00:01:00Z",
        }
      : null,
    execution: null,
    proposal_ids: [],
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
    error_code: "",
    error_message_ja: "",
  };
}

function sessionData(status: string, withSql = false, confirmed = false, done = false) {
  return {
    session: session(status, withSql, confirmed),
    profile_ontology_view: {
      id: "profile-view-1",
      profile_id: "default",
      ontology_revision_id: "revision-1",
      etag: "view-etag",
      node_ids: [
        "physical-orders",
        "metric-order-amount",
        "property-order-date",
        "property-order-status",
        "physical-customers",
        "business-rule-order-status",
        "enum-order-status-confirmed",
      ],
      edge_ids: ["edge-order-customer"],
      physical_objects: [
        { node_id: "physical-orders", owner: "APP", object_name: "ORDERS", object_type: "table" },
      ],
      allowed_path_ids: ["edge-order-customer"],
    },
    ontology_graph: {
      revision,
      nodes: [
        graphNode,
        metricNode,
        orderDateNode,
        orderStatusNode,
        customerNode,
        statusRuleNode,
        confirmedStatusNode,
      ],
      edges: [orderCustomerEdge],
    },
    result: done
      ? { columns: ["ORDER_COUNT"], rows: [{ ORDER_COUNT: 3 }], total: 1 }
      : null,
    performance_check: withSql
      ? { available: false, warning: "PLAN_TABLE を利用できません。" }
      : null,
  };
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

interface MockApiOptions {
  guidedStartDelayMs?: number;
  /** 回答前の確認の作成の応答（#1439）。 */
  createStage?: RequirementStage;
  /** 回答の応答。 */
  answerStage?: RequirementStage;
  /** 回答のたびに進む応答（足りなければ最後の段階のまま。answerStage より優先）。 */
  answerStages?: RequirementStage[];
  /** 回答前の確認の作成を失敗させる（HTTP status と応答の本文。#1715）。 */
  createFailure?: { status: number; body: Record<string, unknown> };
}

async function waitForMockDelay(delayMs: number) {
  if (delayMs <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function mockApi(
  page: Page,
  options: MockApiOptions = {},
) {
  const payloads: Record<string, unknown> = {};
  let answerCount = 0;
  const guidedStartDelayMs = options.guidedStartDelayMs ?? 0;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    // 共通 helper の認証 mock を優先し、catch-all で CurrentUser を空 object にしない。
    if (path === "/api/auth/me") return route.fallback();
    // profile-access 一覧(配列)は共通 helper の mock([])へ委譲する。
    if (path === "/api/security/profile-access/profiles") return route.fallback();
    if (path === "/api/nl2sql/rewrite") {
      // rewrite 既定 ON。catch-all の空 object を返すと比較カードが欠損 payload になるため echo する。
      const body = request.postDataJSON() as { question?: string };
      const original = String(body?.question ?? "");
      return fulfill(route, {
        original_question: original,
        rewritten_question: original,
        source: "deterministic",
        model: "",
        warnings: [],
      });
    }
    if (path === "/api/ready/database") {
      return fulfill(route, { status: "ok", check: "ok", detail: null });
    }
    if (path === "/api/nl2sql/persistence") {
      return fulfill(route, {
        mode: "oracle",
        ready: true,
        durable: true,
        writable: true,
        snapshot_loaded: true,
        reason_code: null,
        checked_at: "2026-07-19T00:00:00Z",
      });
    }
    if (path === "/api/schema/catalog/head") {
      return fulfill(route, {
        catalog_version: 1,
        schema_fingerprint: catalog.schema_fingerprint,
        refreshed_at: catalog.refreshed_at,
        object_count: catalog.tables.length,
        column_count: catalog.tables.reduce((total, table) => total + table.columns.length, 0),
        change_token: 1,
        etag: catalog.schema_fingerprint,
      });
    }
    if (path === "/api/schema/objects") {
      return fulfill(route, {
        items: catalog.tables.map((table) => ({
          owner: table.owner,
          object_name: table.table_name,
          object_type: table.table_type,
          logical_name: table.logical_name,
          comment: table.comment,
          row_count: table.row_count,
          column_count: table.columns.length,
          last_ddl_at: "",
        })),
        next_cursor: null,
        total: catalog.tables.length,
        catalog_version: 1,
      });
    }
    if (path.startsWith("/api/schema/objects/")) {
      return fulfill(route, {
        table: catalog.tables[0],
        dependencies: [],
        catalog_version: 1,
        etag: catalog.schema_fingerprint,
      });
    }
    if (path === "/api/schema/catalog") return fulfill(route, catalog);
    if (path === "/api/nl2sql/profiles/search") {
      return fulfill(route, {
        items: [
          {
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_tables: profile.allowed_tables,
        allowed_views: profile.allowed_views,
        glossary: profile.glossary,
        sql_rules: profile.sql_rules,
        default_row_limit: profile.default_row_limit,
        safety_policy: profile.safety_policy,
        few_shot_examples: profile.few_shot_examples,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
            version: 1,
            etag: "profile-etag",
            updated_at: "2026-07-11T00:00:00Z",
          },
        ],
        next_cursor: null,
        total: 1,
        change_token: 1,
      });
    }
    if (path === "/api/nl2sql/profiles" && request.method() === "GET") {
      return fulfill(route, [profile]);
    }
    if (path === "/api/nl2sql/profiles/default/usage-context" && request.method() === "GET") {
      return fulfill(route, {
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        allowed_tables: profile.allowed_tables,
        allowed_views: profile.allowed_views,
        archived: profile.archived,
        object_scope_version: 1,
        version: 1,
        etag: "profile-etag",
        updated_at: "2026-07-11T00:00:00Z",
      });
    }
    if (path === "/api/nl2sql/profiles/default" && request.method() === "GET") {
      return fulfill(route, profile);
    }
    if (path === "/api/nl2sql/history") return fulfill(route, { items: [] });
    if (path === "/api/nl2sql/recommend-profile") {
      return fulfill(route, {
        recommended_profile_id: "default",
        recommended_profile_name: "標準プロファイル",
        confidence: 1,
        reason: "受注",
        rewritten_question: "受注件数を表示",
        recommended_allowed_objects: { table_names: ["ORDERS"], columns: {} },
        candidates: [],
      });
    }
    if (path === "/api/nl2sql/similar-history") return fulfill(route, { items: [] });
    if (path === "/api/nl2sql/ontology/profile-recommendations") {
      await waitForMockDelay(guidedStartDelayMs);
      return fulfill(route, {
        recommendation: {
          id: "recommendation-1",
          question_hash: "a".repeat(64),
          ontology_revision_id: "revision-1",
          candidates: [
            {
              profile_id: "default",
              profile_name: "標準プロファイル",
              ontology_revision_id: "revision-1",
              score: 1,
              matched_scenarios_ja: ["受注分析"],
              matched_terms: ["受注"],
              reasons_ja: ["用語「受注」が一致しました。"],
            },
          ],
          expires_at: "2026-07-19T12:15:00Z",
        },
      });
    }
    if (path.endsWith("/ontology/profile-recommendations/recommendation-1/confirm")) {
      await waitForMockDelay(guidedStartDelayMs);
      return fulfill(route, {
        recommendation: {
          id: "recommendation-1",
          question_hash: "a".repeat(64),
          ontology_revision_id: "revision-1",
          candidates: [],
          selected_profile_id: "default",
          selected_revision_id: "revision-1",
          expires_at: "2026-07-19T12:15:00Z",
        },
        confirmation_token: "profile-confirmation-token",
      });
    }
    if (path === "/api/nl2sql/jobs" && request.method() === "POST") {
      payloads.job = request.postDataJSON();
      return fulfill(route, {
        job_id: "job-ontology-1",
        status: "running",
        created_at: "2026-07-11T00:00:00Z",
        steps: [
          { stage: "prepare_context", status: "done", elapsed_ms: 8 },
          { stage: "generate_sql", status: "running", elapsed_ms: null },
          { stage: "safety_check", status: "pending", elapsed_ms: null },
          { stage: "execute_sql", status: "pending", elapsed_ms: null },
          { stage: "format_results", status: "pending", elapsed_ms: null },
        ],
      });
    }
    if (path === "/api/nl2sql/jobs/job-ontology-1" && request.method() === "GET") {
      const question = String(
        (payloads.job as { question?: unknown } | undefined)?.question ?? "受注件数を表示"
      );
      return fulfill(route, {
        job_id: "job-ontology-1",
        status: "done",
        created_at: "2026-07-11T00:00:00Z",
        started_at: "2026-07-11T00:00:00Z",
        finished_at: "2026-07-11T00:00:01Z",
        elapsed_ms: 1000,
        error_message: null,
        warning_message: null,
        steps: [
          { stage: "prepare_context", status: "done", elapsed_ms: 8 },
          { stage: "generate_sql", status: "done", elapsed_ms: 120 },
          { stage: "safety_check", status: "done", elapsed_ms: 10 },
          { stage: "execute_sql", status: "done", elapsed_ms: 20 },
          { stage: "format_results", status: "done", elapsed_ms: 5 },
        ],
        timing: {
          created_at: "2026-07-11T00:00:00Z",
          started_at: "2026-07-11T00:00:00Z",
          finished_at: "2026-07-11T00:00:01Z",
          elapsed_ms: 1000,
          stage_timings: [],
        },
        result: {
          history_id: "hist-ontology-1",
          engine: "enterprise_ai_direct",
          engine_meta: { profile: "NL2SQL_DEFAULT_PROFILE" },
          fallback_reason: "",
          original_question: question,
          rewritten_question: question,
          generated_sql: "SELECT COUNT(*) AS ORDER_COUNT FROM APP.ORDERS",
          executable_sql: "SELECT COUNT(*) AS ORDER_COUNT FROM APP.ORDERS",
          explanation: "受注件数を集計します。",
          safety: {
            is_safe: true,
            is_select_only: true,
            row_limit_applied: 0,
            blocked_reason: "",
            warnings: [],
            referenced_tables: ["APP.ORDERS"],
            referenced_columns: [],
          },
          recommendations: ["公開済みオントロジー context を利用しました。"],
          repaired_sql: "",
          optimization_hints: [],
          results: {
            columns: ["ORDER_COUNT"],
            rows: [{ ORDER_COUNT: 3 }],
            total: 1,
          },
          timing: {
            created_at: "2026-07-11T00:00:00Z",
            started_at: "2026-07-11T00:00:00Z",
            finished_at: "2026-07-11T00:00:01Z",
            elapsed_ms: 1000,
            stage_timings: [],
          },
          interpretation: {
            available: true,
            question: {
              available: true,
              source: "deterministic",
              original_question: question,
              rewritten_question: question,
              profile_id: "default",
              profile_name: "標準プロファイル",
              profile_category: "販売",
              target_objects: ["APP.ORDERS"],
              filters: [],
              group_by: [],
              order_by: [],
              aggregations: ["COUNT"],
              row_limit: null,
              confidence: 0.9,
              warnings: [],
            },
            sql: {
              available: true,
              source: "deterministic",
              summary: "受注件数を集計します。",
              statement_type: "SELECT",
              tables: ["APP.ORDERS"],
              columns: ["ORDER_COUNT"],
              joins: [],
              filters: [],
              aggregations: ["COUNT"],
              group_by: [],
              order_by: [],
              limit: null,
              semantic_graph: artifact.semantic_graph,
              warnings: [],
            },
            ontology_graph: sessionData("awaiting_intent_confirmation").ontology_graph,
            warnings: [],
          },
          show_prompt: null,
        },
      });
    }
    if (path === "/api/nl2sql/query-sessions" && request.method() === "POST") {
      payloads.create = request.postDataJSON();
      if ((payloads.create as { clarification_mode?: string }).clarification_mode === "guided") {
        await waitForMockDelay(guidedStartDelayMs);
        if (options.createFailure) {
          payloads.createFailures = Number(payloads.createFailures ?? 0) + 1;
          return route.fulfill({ status: options.createFailure.status, json: options.createFailure.body });
        }
        return fulfill(route, requirementSessionData(options.createStage ?? "needs_answer"));
      }
      return fulfill(route, sessionData("awaiting_intent_confirmation"));
    }
    if (path.endsWith("/clarification-answers") && request.method() === "POST") {
      payloads.clarificationAnswer = request.postDataJSON();
      answerCount += 1;
      const stage =
        options.answerStages?.[answerCount - 1] ?? options.answerStages?.at(-1) ?? options.answerStage ?? "ready";
      return fulfill(route, requirementSessionData(stage, answerCount + 1));
    }
    if (path.endsWith("/apply") && request.method() === "POST") {
      const body = request.postDataJSON() as { base_version: number };
      payloads.apply = body;
      return fulfill(route, requirementSessionData("applied", body.base_version));
    }
    if (path.endsWith("/cancel") && request.method() === "POST") {
      payloads.cancel = true;
      const data = requirementSessionData("needs_answer");
      Object.assign(data.session, { status: "cancelled" });
      return fulfill(route, data);
    }
    if (path.endsWith("/intent") && request.method() === "PATCH") {
      payloads.patch = request.postDataJSON();
      return fulfill(route, sessionData("awaiting_intent_confirmation"));
    }
    if (path.endsWith("/generate-sql")) {
      payloads.generate = request.postDataJSON();
      return fulfill(route, sessionData("awaiting_sql_confirmation", true));
    }
    if (path.endsWith("/confirm-sql")) {
      payloads.confirm = request.postDataJSON();
      return fulfill(route, sessionData("awaiting_sql_confirmation", true, true));
    }
    if (path.endsWith("/execute")) {
      payloads.execute = request.postDataJSON();
      return fulfill(route, sessionData("done", true, true, true));
    }
    if (path.endsWith("/ontology-view") && request.method() === "GET") {
      const data = sessionData("awaiting_intent_confirmation");
      return fulfill(route, {
        profile_ontology_view: data.profile_ontology_view,
        ontology_graph: data.ontology_graph,
      });
    }
    if (path.endsWith("/ontology-markdown") && request.method() === "GET") {
      // draft が空だと editor でなく空状態 placeholder が出るため、編集入口検証用に本文を持たせる。
      return fulfill(route, {
        draft_markdown: "# オントロジー下書き\n\n- APP.DEPARTMENT: 部門\n",
        published_markdown: "",
        draft_revision: null,
        published_revision: null,
        draft_etag: "draft-etag-1",
        published_at: null,
      });
    }
    if (path.endsWith("/ontology-source-documents") && request.method() === "GET") {
      return fulfill(route, { source_documents: [] });
    }
    if (
      path === "/api/nl2sql/ontology/revisions/revision-1/drafts" &&
      request.method() === "POST"
    ) {
      payloads.semanticDraft = request.postDataJSON();
      const data = sessionData("awaiting_intent_confirmation").ontology_graph;
      return fulfill(route, {
        ...data,
        revision: { ...revision, id: "revision-2", version: 2, etag: "revision-2-etag" },
      });
    }
    if (path === "/api/nl2sql/db-admin/views") {
      return fulfill(route, { runtime: "deterministic", items: [], warnings: [] });
    }
    return fulfill(route, {});
  });
  return payloads;
}

async function runCurrentOntologySearch(page: Page) {
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByRole("columnheader", { name: "ORDER_COUNT" })).toBeVisible();
}

test("SQL を生成して実行すると公開済みオントロジー context を使って結果と解釈を表示する", async ({ page }, testInfo) => {
  const payloads = await mockApi(page);
  await page.goto("/query");

  await page.locator("#nl2sql-question-input").fill("受注件数を表示");
  await runCurrentOntologySearch(page);

  await expect(page.getByRole("columnheader", { name: "ORDER_COUNT" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "3" })).toBeVisible();
  await expect(page.getByTestId("nl2sql-interpretation-panel")).toBeVisible();
  await expect(page.getByTestId("nl2sql-sql-grounding-panel")).toBeVisible();
  await expect(page.getByText("入力と生成 SQL の対応")).toHaveCount(0);
  await expect(page.getByText("入力テンプレート")).toHaveCount(0);
  await expect(page.getByText("生成 SQL の意味")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("ontology-query.png"), fullPage: true });
  expect(payloads.job).toMatchObject({
    question: "受注件数を表示",
    profile_id: "default",
    use_ontology_context: true,
    include_interpretation: true,
  });
});

test("SQL の生成と実行は明示操作後だけ現在の質問とオントロジー利用設定を送信する", async ({ page }, testInfo) => {
  const payloads = await mockApi(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/query");

  await page.locator("#nl2sql-question-input").fill("受注件数を表示");
  expect(payloads.job).toBeUndefined();
  await page.locator("#nl2sql-question-input").fill("確定した受注の月別売上金額");
  await runCurrentOntologySearch(page);
  expect(payloads.job).toMatchObject({
    question: "確定した受注の月別売上金額",
    profile_id: "default",
    use_ontology_context: true,
    include_interpretation: true,
  });

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await page.screenshot({ path: testInfo.outputPath("ontology-intent-editor.png"), fullPage: true });
});

async function startRequirementCheck(page: Page, question = REQUIREMENT_QUESTION) {
  await page.locator("#nl2sql-question-input").fill(question);
  await page.getByRole("button", { name: "回答前の確認", exact: true }).click();
  const panel = page.getByTestId("nl2sql-guided-clarification");
  await expect(panel).toBeVisible();
  return panel;
}

async function expectNoPageOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
}

{
  // desktop（1280px）と mobile-375 の 2 つの project で動く。
  test("回答前の確認は SQL の要件を欄ごとに見せ、1 問ずつ確かめて SQL を生成して実行する", async ({ page }, testInfo) => {
    const viewport = { name: testInfo.project.name };
    const payloads = await mockApi(page);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/query");

    // 補足の説明は常設せず、ボタンの横の info アイコンを押すと出す（#901）。
    const clarificationDescription = page.getByTestId("nl2sql-clarification-description");
    await expect(clarificationDescription).toBeHidden();
    await page.getByRole("button", { name: "回答前の確認の説明", exact: true }).click();
    await expect(clarificationDescription).toHaveText(
      "質問と、データベースの Schema・業務プロファイル・用語集・オントロジーを合わせて、SQL を作るのに足りない情報・明確でない情報を 1 つずつ確認します。表のデータは読みません。"
    );
    await page.locator("#nl2sql-question-input").click();

    const panel = await startRequirementCheck(page);
    // 値のある部分だけを 1 行ずつの一覧にし、値の無い部分は「指定なし」の 1 行にまとめる（#1718）。
    // 要確認があるので一覧は開いている。
    const requirements = panel.getByTestId("nl2sql-clarification-requirements");
    await expect(requirements).toHaveAttribute("data-needs-answer", "true");
    await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText("要確認 1 件");
    const rows = requirements.getByTestId("nl2sql-requirement-item");
    await expect(rows).toHaveCount(6);
    // 表・列は物理名（論理名は括弧）で示す。AI の推測は一覧に出し、1 つずつは聞かない。
    const selectRow = rows.and(page.locator('[data-item-key="select_region_total"]'));
    await expect(selectRow.getByText("CUSTOMERS.REGION（地域）、ORDERS.AMOUNT（受注金額）")).toBeVisible();
    await expect(selectRow.getByText("AI の推測")).toBeVisible();
    await expect(rows.and(page.locator('[data-item-key="from_tables"]'))).toContainText("APP.ORDERS（受注）、APP.CUSTOMERS（顧客）");
    await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toHaveText(/指定なし: 絞り込み・サブクエリ・その他/);
    // 要確認の行は質問を出す。今の質問は回答欄で答える（候補を 2 か所に出さない）。
    const periodRow = rows.and(page.locator('[data-item-key="period_this_fiscal_year"]'));
    await expect(periodRow).toHaveAttribute("data-item-kind", "needs_answer");
    await expect(periodRow.getByText("要確認", { exact: true })).toBeVisible();
    await expect(periodRow.getByText("「今年度」は、どの期間ですか？")).toBeVisible();
    await expect(periodRow.getByRole("button")).toHaveCount(0);
    await expect(panel.getByText("確認 1 回目（上限 4 回）")).toBeVisible();

    // 質問は 1 問ずつ（不足・あいまいだけ）。相対的な期間は具体的な日付の範囲で選ぶ。
    const question = panel.getByTestId("nl2sql-clarification-question");
    await expect(question).toHaveAttribute("data-item-key", "period_this_fiscal_year");
    await expect(question.getByText("確認事項 1 / 1")).toBeVisible();
    const heading = question.getByRole("heading", { name: "「今年度」は、どの期間ですか？" });
    await expect(heading).toBeFocused();
    await expect(question).toContainText("基準日は実行日（2026-10-11）");
    await expect(question.getByRole("radio")).toHaveCount(3);
    await expect(panel.getByRole("button", { name: "この内容で SQL を生成して実行" })).toHaveCount(0);
    const answerButton = question.getByRole("button", { name: "この内容で回答" });
    await expect(answerButton).toBeDisabled();
    // 回答前の確認のカード（#1839）: 確定・スキップ（確認せずに SQL を生成）。「確認をやめる」は無い（#2311）。
    await expect(answerButton.locator("..").getByRole("button")).toHaveText([
      "この内容で回答",
      "確認せずに SQL を生成",
    ]);
    await page.screenshot({ path: testInfo.outputPath(`requirement-question-${viewport.name}.png`), fullPage: true });
    await panel.screenshot({ path: testInfo.outputPath(`requirement-panel-question-${viewport.name}.png`) });
    await expectNoPageOverflow(page);

    await question.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
    await answerButton.click();

    // すべて答えたら、まとめ（書き直した質問・SQL の骨組み）を見せてから反映する。
    await expect(panel.locator('[data-status-variant="success"]').filter({ hasText: "確認完了" })).toBeVisible();
    await expect(panel.getByText("確認 2 回目（上限 4 回）")).toBeVisible();
    const summary = panel.getByTestId("nl2sql-clarification-summary");
    await expect(summary.getByTestId("nl2sql-clarified-question")).toHaveText(REQUIREMENT_CLARIFIED_QUESTION);
    const skeleton = summary.getByTestId("nl2sql-clarification-skeleton");
    await expect(skeleton).toContainText("FROM APP.ORDERS");
    await expect(skeleton).toContainText("JOIN APP.CUSTOMERS ON ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID");
    await expect(skeleton).toContainText("WHERE ORDERS.ORDER_DATE >= DATE '2026-04-01'");
    await expect(summary).toContainText("実行はせず、生成する SQL そのものでもありません");
    await expect(panel.getByTestId("nl2sql-clarification-question")).toHaveCount(0);
    // 確認のカード（チャットと同じ部品・同じ文言。#1761）: 直す内容の欄と、確定・スキップの操作。
    // 選び直す対象が無い（選択肢の無い）カードなので「その他」と言わない（#3366）。
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    await expect(confirm.getByRole("textbox", { name: "直す内容", exact: true })).toBeVisible();
    await expect(confirm.getByRole("textbox", { name: "その他（自由入力）" })).toHaveCount(0);
    await expect(confirm.getByRole("button", { name: "直しを伝える" })).toBeDisabled();
    await expect(confirm.getByRole("button")).toHaveText([
      "この内容で SQL を生成して実行",
      "直しを伝える",
      "確認せずに SQL を生成",
    ]);
    await page.screenshot({ path: testInfo.outputPath(`requirement-summary-${viewport.name}.png`), fullPage: true });
    await panel.screenshot({ path: testInfo.outputPath(`requirement-panel-summary-${viewport.name}.png`) });
    await expectNoPageOverflow(page);
    // 押すまで要件を生成に渡さない。
    expect(payloads.job).toBeUndefined();

    // 確定した要件で SQL を生成して実行する（「SQL を生成して実行」を押し直させない。#1761）。
    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    const questionInput = page.locator("#nl2sql-question-input");
    await expect(panel).toHaveCount(0);
    await expect(page.getByRole("columnheader", { name: "ORDER_COUNT" })).toBeVisible();
    // 質問は利用者の入力のまま送る（確かめた質問の文はサーバーが session から生成に使う。#1727）。
    expect(payloads.job).toMatchObject({
      question: REQUIREMENT_QUESTION,
      requirement_session_id: REQUIREMENT_SESSION_ID,
    });
    // 回答して確定しても、利用者が入力した質問は書き換えない（#1727）。確かめた質問の文は要件の表示に出す。
    await expect(questionInput).toHaveValue(REQUIREMENT_QUESTION);
    const active = page.getByTestId("nl2sql-requirement-active");
    await expect(active).toContainText("回答前の確認の要件を SQL の生成に渡します");
    await page.getByTestId("nl2sql-requirement-question").click();
    await expect(active).toContainText(REQUIREMENT_CLARIFIED_QUESTION);
    await expectNoPageOverflow(page);
    expect(payloads.create).toMatchObject({
      question: REQUIREMENT_QUESTION,
      profile_id: "default",
      clarification_mode: "guided",
    });
    expect(payloads.clarificationAnswer).toEqual({
      base_version: 1,
      item_id: "item-period",
      selected_candidate_ids: ["candidate-fy-april"],
      free_text: "",
    });
    expect(payloads.apply).toEqual({ base_version: 2 });
    // 反映した session は閉じる（中止は送らない）。
    expect(payloads.cancel).toBeUndefined();
    // 質問の文を書き換えたら、要件は渡さない。
    await questionInput.fill(`${REQUIREMENT_QUESTION} 追記`);
    await expect(page.getByTestId("nl2sql-requirement-dropped")).toBeVisible();
    await runCurrentOntologySearch(page);
    expect((payloads.job as Record<string, unknown>).requirement_session_id).toBeUndefined();
    await expectNoPageOverflow(page);
  });
}

test("回答前の確認の開始中は実処理に合わせて案内を切り替え経過時間を表示する", async ({ page }, testInfo) => {
  await mockApi(page, { guidedStartDelayMs: 1_500 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/query");

  const panel = await startRequirementCheck(page);
  const progress = panel.getByTestId("nl2sql-guided-start-progress");
  const timer = panel.getByTestId("nl2sql-guided-start-progress-timer");
  await expect(progress).toBeVisible();
  await expect(progress).toHaveAttribute("data-processing-placement", "panel");
  await expect(progress).toHaveAttribute("aria-busy", "true");
  const visibleProgressLabel = progress.locator("[data-processing-label]");
  await expect(visibleProgressLabel).toHaveText("質問に合う業務プロファイルを確認しています");
  await expect(timer).toContainText("経過時間");
  await expect(timer).toHaveAttribute("role", "timer");
  await expect(timer).toHaveAttribute("aria-live", "off");
  // 共有 Spinner(svg.animate-spin)。reduced motion でも回転を止めない(#440)。
  const spinner = progress.locator("svg.animate-spin");
  await expect(spinner).toHaveCount(1);
  await expect(spinner).toHaveCSS("width", "16px");
  await expect(spinner).toHaveCSS("animation-name", "spin");

  await expect(timer).toContainText(/00:0[1-9]/);
  await expect(visibleProgressLabel).toHaveText("利用する業務プロファイルを確定しています");
  await expect(visibleProgressLabel).toHaveText("SQL の要件の下書きを作っています");
  await expect(timer).toContainText(/00:0[2-9]/);
  await page.screenshot({
    path: testInfo.outputPath("guided-clarification-progress.png"),
    fullPage: true,
  });

  await expect(panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  await expect(progress).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("回答前の確認は開始処理の途中でも、経過の行の「確認せずに SQL を生成」で待たずに生成へ進む", async ({ page }, testInfo) => {
  const payloads = await mockApi(page, { guidedStartDelayMs: 1_500 });
  await page.goto("/query");

  const questionInput = page.locator("#nl2sql-question-input");
  const panel = await startRequirementCheck(page);
  const progress = panel.getByTestId("nl2sql-guided-start-progress");
  await expect(progress).toBeVisible();
  // 確認を終える操作は「確認せずに SQL を生成」だけ（「確認をやめる」は無い。#2311）。
  await expect(panel.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  const skipButton = progress.getByRole("button", { name: "確認せずに SQL を生成" });
  await expect(skipButton).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath("guided-clarification-interruptible-start.png"),
    fullPage: true,
  });
  await skipButton.click();

  await expect(panel).toHaveCount(0);
  await expect(questionInput).toHaveValue(REQUIREMENT_QUESTION);
  await expect.poll(() => payloads.job).toMatchObject({ question: REQUIREMENT_QUESTION });
  expect(payloads.job).not.toHaveProperty("requirement_session_id");
  expect(payloads.clarificationAnswer).toBeUndefined();
});

test("回答前の確認は AI の推測を確認済みの一覧から直せる", async ({ page }, testInfo) => {
  const payloads = await mockApi(page, { answerStage: "corrected" });
  await page.goto("/query");
  const panel = await startRequirementCheck(page);

  const selectRow = panel.locator('[data-testid="nl2sql-requirement-item"][data-item-key="select_region_total"]');
  await selectRow.getByRole("button", { name: "地域と受注金額の合計を直す" }).click();
  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question).toHaveAttribute("data-item-key", "select_region_total");
  await expect(question.getByText("直す項目: 地域と受注金額の合計")).toBeVisible();
  await expect(question.getByText("今の内容: CUSTOMERS.REGION と SUM(ORDERS.AMOUNT)")).toBeVisible();
  await expect(question.getByRole("button", { name: "直すのをやめる" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("requirement-correct.png"), fullPage: true });

  // やめると、もとの質問（期間）に戻る。
  await question.getByRole("button", { name: "直すのをやめる" }).click();
  await expect(question).toHaveAttribute("data-item-key", "period_this_fiscal_year");

  await selectRow.getByRole("button", { name: "地域と受注金額の合計を直す" }).click();
  // 候補の無い要件を直す欄は「直す内容」（「その他」と言わない。#3366）。
  await question.getByRole("textbox", { name: "直す内容", exact: true }).fill("CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)");
  await question.getByRole("button", { name: "直した内容で更新" }).click();
  expect(payloads.clarificationAnswer).toEqual({
    base_version: 1,
    item_id: "item-select",
    selected_candidate_ids: [],
    free_text: "CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)",
  });
  // 答えた項目は「確定」（あなたの回答）。要確認が無くなったので一覧は畳み、要約の 1 行にする（#1718）。
  const requirements = panel.getByTestId("nl2sql-clarification-requirements");
  await expect(requirements).toHaveAttribute("data-needs-answer", "false");
  const toggle = requirements.getByTestId("nl2sql-clarification-requirements-toggle");
  await expect(toggle).toContainText("確認の必要なし");
  await expect(toggle).toContainText("表示列 CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)");
  await toggle.click();
  await expect(selectRow).toHaveAttribute("data-item-status", "answered");
  await expect(selectRow).toContainText("CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)");
  await expect(selectRow.getByText("確定", { exact: true })).toBeVisible();
  await expect(selectRow.getByText("あなたの回答")).toBeVisible();
});

test("回答前の確認は複数選択の質問を Checkbox で答える", async ({ page }) => {
  const payloads = await mockApi(page, { createStage: "multi_select" });
  await page.goto("/query");
  const panel = await startRequirementCheck(page);

  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("heading", { name: "どの受注状態（ORDERS.STATUS）に絞りますか？" })).toBeVisible();
  await expect(question.getByRole("checkbox")).toHaveCount(2);
  await question.getByRole("checkbox", { name: /出荷済み/ }).check();
  await question.getByRole("checkbox", { name: /取消/ }).check();
  await expect(question.getByText("根拠: 列のコメント").first()).toBeVisible();
  await question.getByRole("button", { name: "この内容で回答" }).click();
  expect(payloads.clarificationAnswer).toMatchObject({
    item_id: "item-status",
    selected_candidate_ids: ["candidate-shipped", "candidate-cancelled"],
  });
});

test("回答前の確認は再検査の上限で残った項目を示し、残りを AI に任せて SQL を生成できる", async ({ page }, testInfo) => {
  const payloads = await mockApi(page, { answerStage: "limit" });
  await page.goto("/query");
  const panel = await startRequirementCheck(page);
  const question = panel.getByTestId("nl2sql-clarification-question");
  await question.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();

  await expect(panel.getByText("AI の再検査は上限（3 回）に達しました。残った確認の項目: 金額の税", { exact: false })).toBeVisible();
  await expect(question).toHaveAttribute("data-item-key", "other_currency");
  // 残りに答えるか、SELECT と FROM が明確なので残りを AI に任せて SQL を生成するかを選べる（チャットと同じ文言。#1761）。
  const applyRemaining = question.getByRole("button", { name: "確認せずに SQL を生成" });
  await expect(applyRemaining).toBeEnabled();
  await expect(panel.getByTestId("nl2sql-clarification-skeleton")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("requirement-limit.png"), fullPage: true });
  await applyRemaining.click();
  await expect(panel).toHaveCount(0);
  expect(payloads.apply).toEqual({ base_version: 2 });
  await expect.poll(() => payloads.job).toMatchObject({ requirement_session_id: REQUIREMENT_SESSION_ID });
});

test("回答前の確認は SELECT と FROM が明確になるまで反映させない", async ({ page }, testInfo) => {
  await mockApi(page, { createStage: "minimum" });
  await page.goto("/query");
  const panel = await startRequirementCheck(page, "いい感じに出して");

  await expect(
    panel.getByText("SELECT（何を表示するか）と FROM（どの表から）が明確になるまで、SQL の生成に進めません。")
  ).toBeVisible();
  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("checkbox", { name: /APP\.ORDERS（受注）/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: "この内容で SQL を生成して実行" })).toHaveCount(0);
  // 回答前の確認のスキップ（#1839）は、要件を反映せず確認を使わずに作る（残りを AI の推測で反映しない）。
  await expect(question.getByRole("button", { name: "確認せずに SQL を生成" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("requirement-minimum.png"), fullPage: true });
});

// #1718: SQL の要件（AI の理解）は、値のある部分だけのコンパクトな一覧と「指定なし」の 1 行。状態は
// 確定 / AI の推測 / 要確認 の 3 つで、要確認が無ければ畳んで要約の 1 行にする。チャットと同じ部品。
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

async function reduceMotion(page: Page) {
  await page.emulateMedia({ reducedMotion: "reduce" });
}

test(`回答前の確認は select * の要件を 2 行と「指定なし」の 1 行で示し、畳んだ要約にする`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  const viewport = testInfo.project.name;
  const payloads = await mockApi(page, {
    createStage: "select_all_table",
    answerStages: ["select_all", "select_all_ordered"],
  });
  await page.goto("/query");
  await reduceMotion(page);
  // `select * from employee` 相当の質問（SQL の文そのものは回答前の確認を飛ばすので、自然文で聞く。#1715）。
  const panel = await startRequirementCheck(page, "社員をすべて表示");
  // 確認が要らなければ聞かずに反映する（#1715）。ここでは表が 2 つあるので、表だけを聞く。
  const tableQuestion = panel.getByTestId("nl2sql-clarification-question");
  await tableQuestion.getByRole("radio", { name: /ADMIN\.EMPLOYEE（社員）/ }).check();
  await tableQuestion.getByRole("button", { name: "この内容で回答" }).click();

  const requirements = panel.getByTestId("nl2sql-clarification-requirements");
  await expect(requirements).toHaveAttribute("data-needs-answer", "false");
  const toggle = requirements.getByTestId("nl2sql-clarification-requirements-toggle");
  await expect(toggle).toContainText("確認の必要なし");
  await expect(toggle).toContainText("使用テーブル ADMIN.EMPLOYEE ／ 表示列 ADMIN.EMPLOYEE.*");
  await expect(requirements.getByTestId("nl2sql-requirement-item").first()).toBeHidden();
  await expectNoPageOverflow(page);
  await screenshotWhole(page, panel, testInfo.outputPath(`select-all-collapsed-${viewport}-light.png`));

  await toggle.click();
  const rows = requirements.getByTestId("nl2sql-requirement-item");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("使用テーブル");
  await expect(rows.nth(0)).toContainText("ADMIN.EMPLOYEE");
  await expect(rows.nth(1)).toContainText("表示列");
  await expect(rows.nth(1)).toContainText("ADMIN.EMPLOYEE.*");
  await expect(rows.getByText("確定", { exact: true })).toHaveCount(2);
  await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toContainText(
    "指定なし: 結合・期間・絞り込み・集計・並び順・サブクエリ・その他"
  );
  // 値の無い部分に「AI の推測」「未確認」を出さない。期間の列（HIRE_DATE）も値のように出さない。
  await expect(requirements.getByText("AI の推測", { exact: true })).toHaveCount(0);
  await expect(requirements.getByText("未確認")).toHaveCount(0);
  await expect(requirements.getByText(/HIRE_DATE/)).toHaveCount(0);
  await expectNoPageOverflow(page);
  await screenshotWhole(page, panel, testInfo.outputPath(`select-all-open-${viewport}-light.png`));

  // 「条件を追加」で、値の無い部分（並び順）に条件を足す。
  const addToggle = requirements.getByTestId("nl2sql-requirement-add-toggle");
  await expect(addToggle).toHaveAttribute("aria-expanded", "false");
  await addToggle.click();
  await expect(addToggle).toHaveAttribute("aria-expanded", "true");
  const addGroup = requirements.getByRole("group", { name: "追加する部分" });
  await expect(addGroup.getByRole("button")).toHaveText([
    "結合",
    "期間",
    "絞り込み",
    "集計",
    "並び順",
    "件数上限",
    "サブクエリ",
    "その他",
  ]);
  await addGroup.getByRole("button", { name: "並び順の条件を追加" }).click();
  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question.getByText("追加する条件: 並び順")).toBeVisible();
  await expect(question.getByRole("heading", { name: "並び順の条件を入力してください。" })).toBeFocused();
  // 条件を足す欄は「追加する条件」（「その他」と言わない。#3366）。
  await question.getByRole("textbox", { name: "追加する条件", exact: true }).fill("EMPLOYEE.HIRE_DATE の新しい順");
  await question.getByRole("button", { name: "この条件を追加" }).click();
  expect(payloads.clarificationAnswer).toEqual({
    base_version: 2,
    item_id: "add:order_by",
    selected_candidate_ids: [],
    free_text: "EMPLOYEE.HIRE_DATE の新しい順",
  });
  await expect(panel.getByTestId("nl2sql-clarification-question")).toHaveCount(0);
  // 開いた一覧はそのまま。足した部分は確定（あなたの回答）の行になり、「指定なし」から外れる。
  const added = requirements.locator('[data-testid="nl2sql-requirement-item"][data-item-key="order_none"]');
  await expect(added).toContainText("EMPLOYEE.HIRE_DATE の新しい順");
  await expect(added).toHaveAttribute("data-item-status", "answered");
  await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toContainText(
    "指定なし: 結合・期間・絞り込み・集計・サブクエリ・その他"
  );
  await screenshotWhole(page, panel, testInfo.outputPath(`select-all-added-${viewport}-light.png`));
});

test(`回答前の確認の要確認の行は質問と候補を出し、候補を押して答えられる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  const viewport = testInfo.project.name;
  const payloads = await mockApi(page, {
    createStage: "date_column",
    answerStages: ["date_column_basis_answered", "date_column_answered"],
  });
  await page.goto("/query");
  await reduceMotion(page);
  const panel = await startRequirementCheck(page, "直近 1 年の社員");

  const requirements = panel.getByTestId("nl2sql-clarification-requirements");
  await expect(requirements).toHaveAttribute("data-needs-answer", "true");
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText("要確認 2 件");
  // 今の質問（日付の列）は回答欄で答える。一覧の行には質問だけを出し、候補を 2 か所に出さない。
  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question).toHaveAttribute("data-item-key", "period_date_column");
  const columnRow = requirements.locator('[data-testid="nl2sql-requirement-item"][data-item-key="period_date_column"]');
  await expect(columnRow).toHaveAttribute("data-current", "true");
  await expect(columnRow.getByText("「直近 1 年」は、どの日付の列で絞りますか？")).toBeVisible();
  await expect(columnRow.getByText("この質問は、いま回答欄で確認しています。")).toBeVisible();
  await expect(columnRow.getByRole("button")).toHaveCount(0);
  // ほかの要確認の行は、質問と候補を出し、候補を押して答えられる。
  const basisRow = requirements.locator('[data-testid="nl2sql-requirement-item"][data-item-key="period_reference_basis"]');
  await expect(basisRow).toHaveAttribute("data-item-kind", "needs_answer");
  await expect(basisRow.getByText("「直近 1 年」は、いつから数えますか？")).toBeVisible();
  await expect(basisRow.getByText("要確認", { exact: true })).toBeVisible();
  const candidates = basisRow.getByRole("group", { name: "期間の基準日の候補" });
  await expect(candidates.getByRole("button")).toHaveText(["実行日（2026-10-11）から", "データの最新日から"]);
  await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toContainText(
    "指定なし: 結合・絞り込み・集計・並び順・サブクエリ・その他"
  );
  await expectNoPageOverflow(page);
  await screenshotWhole(page, panel, testInfo.outputPath(`date-column-needs-answer-${viewport}-light.png`));

  await candidates.getByRole("button", { name: "「実行日（2026-10-11）から」と答える" }).click();
  expect(payloads.clarificationAnswer).toEqual({
    base_version: 1,
    item_id: "item-e-basis",
    selected_candidate_ids: ["candidate-basis-execution"],
    free_text: "",
  });
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText("要確認 1 件");
  await expect(basisRow).toHaveAttribute("data-item-status", "answered");

  await question.getByRole("radio", { name: /EMPLOYEE\.HIRE_DATE/ }).check();
  await question.getByRole("button", { name: "この内容で回答" }).click();
  // 答えたら要確認が無くなり、畳んで要約の 1 行にする。
  await expect(requirements).toHaveAttribute("data-needs-answer", "false");
  await expect(requirements.getByTestId("nl2sql-clarification-requirements-toggle")).toContainText(
    "期間 EMPLOYEE.HIRE_DATE（入社日）"
  );
  await expect(panel.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeVisible();
  await expectNoPageOverflow(page);
  await screenshotWhole(page, panel, testInfo.outputPath(`date-column-answered-${viewport}-light.png`));
});

test(`回答前の確認はすべて確定した要件を畳み、開くと確定の一覧を見せる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  const viewport = testInfo.project.name;
  await mockApi(page, { createStage: "all_confirmed" });
  await page.goto("/query");
  await reduceMotion(page);
  const panel = await startRequirementCheck(page);

  const requirements = panel.getByTestId("nl2sql-clarification-requirements");
  const toggle = requirements.getByTestId("nl2sql-clarification-requirements-toggle");
  await expect(toggle).toContainText("確認の必要なし");
  await screenshotWhole(page, panel, testInfo.outputPath(`all-confirmed-collapsed-${viewport}-light.png`));
  await toggle.click();
  const rows = requirements.getByTestId("nl2sql-requirement-item");
  await expect(rows).toHaveCount(6);
  await expect(rows.getByText("確定", { exact: true })).toHaveCount(6);
  await expect(requirements.getByTestId("nl2sql-requirement-unspecified")).toContainText(
    "指定なし: 絞り込み・サブクエリ・その他"
  );
  await expectNoPageOverflow(page);
  await screenshotWhole(page, panel, testInfo.outputPath(`all-confirmed-open-${viewport}-light.png`));
});

// 375px ではサイドナビが畳まれるため、ナビのリンクと同じ client-side の遷移を history で起こす。
async function navigateInApp(page: Page, path: string) {
  await page.evaluate((target) => {
    window.history.pushState({}, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

test("回答前の確認は別の画面へ移って戻ると同じ確認を続けられ、確認中は質問を変えられない", async ({ page }) => {
  const payloads = await mockApi(page);
  await page.goto("/query");

  const questionInput = page.locator("#nl2sql-question-input");
  const panel = await startRequirementCheck(page);
  const heading = panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" });
  await expect(heading).toBeVisible();
  // 確認は開始時の質問で進むため、確認中は質問の入力と「新しい質問を開始」を使えない。
  await expect(questionInput).toBeDisabled();
  await expect(page.getByRole("button", { name: "新しい質問を開始" })).toBeDisabled();

  // SQL 生成の画面は keep-alive。別の画面へ移っても確認は中止せず、戻ると続きから答えられる。
  await navigateInApp(page, "/chat");
  await expect(panel).toBeHidden();
  await navigateInApp(page, "/query");
  await expect(heading).toBeVisible();
  await expect(questionInput).toHaveValue(REQUIREMENT_QUESTION);
  expect(payloads.cancel).toBeUndefined();
});

test("回答前の確認は作成の途中で確認せずに進んでも、作成された確認をサーバーで中止する", async ({ page }) => {
  const payloads = await mockApi(page, { guidedStartDelayMs: 1_000 });
  await page.goto("/query");

  const panel = await startRequirementCheck(page);
  // 作成の request がサーバーへ届いた後（応答の前）に「確認せずに SQL を生成」で進む。
  await expect.poll(() => payloads.create, { timeout: 10_000 }).toBeDefined();
  await panel.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => payloads.cancel, { timeout: 10_000 }).toBe(true);
});

test("回答前の確認の途中で再読込するとサーバーの確認を中止する", async ({ page }) => {
  const payloads = await mockApi(page);
  // 再読込の途中（pagehide）の keepalive の request は、Playwright の request の event にも route にも
  // 現れない（Playwright 1.64 の Chromium では毎回。以前も route に届かないことがあった。#1667）。
  // 画面が中止を送ったことは、fetch の呼び出しを再読込をまたいで残る sessionStorage に記録して確かめる。
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/query-sessions/")) {
        const method = init?.method ?? (input instanceof Request ? input.method : "GET");
        const calls = JSON.parse(sessionStorage.getItem("e2e-query-session-fetches") ?? "[]") as unknown[];
        calls.push({ url, method, keepalive: init?.keepalive === true });
        sessionStorage.setItem("e2e-query-session-fetches", JSON.stringify(calls));
      }
      return originalFetch(input, init);
    };
  });
  await page.goto("/query");

  const panel = await startRequirementCheck(page);
  await expect(panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  expect(payloads.cancel).toBeUndefined();

  await page.reload();
  const cancelCalls = await page.evaluate(
    (sessionId) =>
      (JSON.parse(sessionStorage.getItem("e2e-query-session-fetches") ?? "[]") as {
        url: string;
        method: string;
        keepalive: boolean;
      }[]).filter((call) => call.url.endsWith(`/query-sessions/${sessionId}/cancel`)),
    REQUIREMENT_SESSION_ID
  );
  expect(cancelCalls).toEqual([expect.objectContaining({ method: "POST", keepalive: true })]);
});

test("回答前の確認は期限切れで終わった確認の質問を出さず、理由と閉じるだけを出す", async ({ page }, testInfo) => {
  const payloads = await mockApi(page);
  const expiredMessage =
    "一定時間操作がなかったため、この確認は終了しました。閉じてから、もう一度始めてください。";
  // mockApi より後に登録した route が優先される。
  await page.route("**/api/nl2sql/query-sessions/*/clarification-answers", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "QUERY_SESSION_EXPIRED", message_ja: expiredMessage } }),
    })
  );
  await page.route(/\/api\/nl2sql\/query-sessions\/[^/]+$/, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const data = requirementSessionData("needs_answer");
    Object.assign(data.session, {
      status: "cancelled",
      error_code: "QUERY_SESSION_EXPIRED",
      error_message_ja: expiredMessage,
    });
    await fulfill(route, data);
  });
  await page.goto("/query");

  const panel = await startRequirementCheck(page);
  await panel.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await panel.getByRole("button", { name: "この内容で回答" }).click();

  await expect(panel.getByText(expiredMessage)).toHaveCount(1);
  await expect(panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "この内容で回答" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "この内容で SQL を生成して実行" })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("guided-clarification-expired.png"), fullPage: true });

  // 終了済みの確認は、中止を送らずに「確認せずに SQL を生成」で質問のまま生成へ進む。
  await expect(panel.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  await panel.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => payloads.job).toMatchObject({ question: REQUIREMENT_QUESTION });
  expect(payloads.cancel).toBeUndefined();
});

// 回答前の確認の LLM の失敗の応答（backend の problem と error_details。2026-10-13 の xai.grok-4.3 の HTTP 500 の形。#1715）。
const REQUIREMENT_LLM_FAILURE = {
  status: 503,
  body: {
    data: null,
    error_messages: [
      "回答前の確認でモデルの API がサーバーのエラーを返しました（HTTP 500、2 回試行）。サービス側の一時的な障害の可能性があります。",
    ],
    error_code: "REQUIREMENT_AI_FAILED",
    problem: {
      type: "urn:nl2sql:problem:REQUIREMENT_AI_FAILED",
      title: "Service Unavailable",
      status: 503,
      detail: "回答前の確認でモデルの API がサーバーのエラーを返しました（HTTP 500、2 回試行）。",
      code: "REQUIREMENT_AI_FAILED",
      request_id: "req-requirement-1",
      retryable: true,
      field_errors: [],
    },
    error_details: {
      kind: "unavailable",
      kind_label_ja: "サービスの一時的な失敗（5xx）",
      error_code: "ENTERPRISE_AI_UNAVAILABLE",
      http_status: 500,
      model_id: "xai.grok-4.3",
      elapsed_ms: 28350,
      attempts: 2,
      upstream_request_id: "opc-req-500",
      provider_message: "Internal server error (internal_error)",
    },
  },
};

// #1761: 確認の質問が 0 件でも自動では反映せず、確認のカード（対象の選び直し・その他（自由入力）・
// 「この内容で SQL を生成して実行」・「確認せずに SQL を生成」）で利用者が確かめてから SQL を生成する。チャットと
// 同じ部品・同じ文言。質問の欄はどの操作でも書き換えない（#1727）。
test.describe("回答前の確認の確認のカード (#1761)", { tag: "@responsive" }, () => {
  test("質問 0 件でも確認のカードを出し、押すまで要件を渡さず、押すと SQL を生成して実行する", async ({ page }, testInfo) => {
    const payloads = await mockApi(page, { createStage: "target_choice" });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    await expect(confirm).toBeVisible();
    await expect(panel.getByTestId("nl2sql-clarification-question")).toHaveCount(0);
    // AI が推測した表と、選び直せる候補（業務プロファイルのほかの表）。
    const target = confirm.getByRole("group", { name: "使用テーブルはこの表でよいですか？" });
    await expect(target.getByRole("radio")).toHaveCount(2);
    await expect(target.getByRole("radio", { name: /ADMIN\.V_EMP_DEPT/ })).toBeChecked();
    await expect(confirm.getByRole("textbox", { name: "その他（自由入力）" })).toBeVisible();
    await expect(confirm.getByRole("button")).toHaveText([
      "この内容で SQL を生成して実行",
      "直しを伝える",
      "確認せずに SQL を生成",
    ]);
    await expect(confirm.getByRole("button", { name: "直しを伝える" })).toBeDisabled();
    await expect(panel.getByTestId("nl2sql-clarification-skeleton")).toContainText("FROM ADMIN.V_EMP_DEPT");
    await expectNoPageOverflow(page);
    await screenshotWhole(page, panel, testInfo.outputPath(`sql-confirm-card-${testInfo.project.name}.png`));
    // 押すまで要件を生成に渡さない（自動の反映をしない）。質問の欄も書き換えない。
    expect(payloads.apply).toBeUndefined();
    expect(payloads.job).toBeUndefined();
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(TARGET_QUESTION);

    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    await expect(panel).toHaveCount(0);
    expect(payloads.apply).toEqual({ base_version: 1 });
    await expect.poll(() => payloads.job).toMatchObject({
      question: TARGET_QUESTION,
      requirement_session_id: REQUIREMENT_SESSION_ID,
    });
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(TARGET_QUESTION);
    await expect(page.getByTestId("nl2sql-requirement-active")).toBeVisible();
    expect(payloads.clarificationAnswer).toBeUndefined();
  });

  test("「確認せずに SQL を生成」は要件を渡さず、質問の欄を変えずに質問のまま生成する", async ({ page }) => {
    const payloads = await mockApi(page, { createStage: "target_choice" });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    await confirm.getByRole("button", { name: "確認せずに SQL を生成" }).click();
    await expect(panel).toHaveCount(0);
    await expect.poll(() => payloads.cancel).toBe(true);
    expect(payloads.apply).toBeUndefined();
    await expect.poll(() => payloads.job).toMatchObject({ question: TARGET_QUESTION });
    expect(payloads.job).not.toHaveProperty("requirement_session_id");
    await expect(page.getByTestId("nl2sql-requirement-active")).toHaveCount(0);
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(TARGET_QUESTION);
  });

  test("対象の表を選び直すと、回答として再検査に回し、直した要件を確かめてから生成する", async ({ page }) => {
    const payloads = await mockApi(page, { createStage: "target_choice", answerStages: ["target_chosen"] });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    await confirm.getByRole("radio", { name: /ADMIN\.V_DEPT_PROJECT/ }).check();
    // 直しを入力している間は、直す前の要件で生成しない（「この内容で回答」で送る）。
    await expect(confirm.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeDisabled();
    await confirm.getByRole("button", { name: "直しを伝える" }).click();
    await expect(panel.getByTestId("nl2sql-clarification-skeleton")).toContainText("FROM ADMIN.V_DEPT_PROJECT");
    expect(payloads.clarificationAnswer).toEqual({
      base_version: 1,
      item_id: "item-view-from",
      selected_candidate_ids: ["candidate-v-dept-project"],
      free_text: "",
    });
    // 答えた表は選び直しの候補を出さない。生成はまだ。
    await expect(confirm.getByRole("radio")).toHaveCount(0);
    expect(payloads.job).toBeUndefined();
    await confirm.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
    await expect.poll(() => payloads.job).toMatchObject({ requirement_session_id: REQUIREMENT_SESSION_ID });
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(TARGET_QUESTION);
  });

  test("「その他（自由入力）」で AI の解釈を直せる", async ({ page }, testInfo) => {
    const payloads = await mockApi(page, { createStage: "target_choice", answerStages: ["other_corrected"] });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    await confirm.getByRole("textbox", { name: "その他（自由入力）" }).fill(TARGET_OTHER_TEXT);
    await expect(confirm.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeDisabled();
    await confirm.getByRole("button", { name: "直しを伝える" }).click();
    await expect(panel.getByTestId("nl2sql-clarified-question")).toHaveText(
      "ADMIN.V_EMP_DEPT の部署ごとの社員数を表示してください。"
    );
    expect(payloads.clarificationAnswer).toEqual({
      base_version: 1,
      item_id: "add:other",
      selected_candidate_ids: [],
      free_text: TARGET_OTHER_TEXT,
    });
    await expect(panel.getByText("確認 2 回目（上限 4 回）")).toBeVisible();
    // 送った後は欄を空にし、直した要件で生成できる。
    await expect(confirm.getByRole("textbox", { name: "その他（自由入力）" })).toHaveValue("");
    await expect(confirm.getByRole("button", { name: "この内容で SQL を生成して実行" })).toBeEnabled();
    await expectNoPageOverflow(page);
    await screenshotWhole(page, panel, testInfo.outputPath(`sql-confirm-other-${testInfo.project.name}.png`));
    expect(payloads.job).toBeUndefined();
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(TARGET_QUESTION);
  });

  // #1772: 自由入力に文字がある間だけ、生成のボタンが押せない理由と次の操作を入力欄の補足に出す。
  test("自由入力の間だけ「この内容で回答」を促す説明を出し、生成のボタンの説明につなぐ (#1772)", async ({ page }, testInfo) => {
    const payloads = await mockApi(page, { createStage: "target_choice" });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
    const freeText = confirm.getByRole("textbox", { name: "その他（自由入力）" });
    const generate = confirm.getByRole("button", { name: "この内容で SQL を生成して実行" });
    const hint = confirm.getByTestId("nl2sql-clarification-card-blocked");
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);

    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toHaveText(REQUIREMENT_PENDING_HINT);
    await expect(generate).toBeDisabled();
    await expect(generate).toHaveAccessibleDescription(REQUIREMENT_PENDING_HINT);
    // ボタンの文言は変えない。説明は入力欄の直下（ボタンの行より上）。
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
    await expectNoPageOverflow(page);
    await reduceMotion(page);
    await screenshotWhole(
      page,
      confirm,
      testInfo.outputPath(`sql-confirm-pending-hint-${testInfo.project.name}-light.png`)
    );

    // 空白だけは直しにならないので説明を出さず、空にすると消えて押せる。
    await freeText.fill("   ");
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toBeVisible();
    await freeText.fill("");
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);
    expect(payloads.clarificationAnswer).toBeUndefined();
    expect(payloads.job).toBeUndefined();
  });

  // #1776: 別の表を選んだだけ（未送信）の間も、#1772 と同じ位置・同じ部品で押せない理由を出す。
  test("表を選び直した間だけ「この内容で回答」を促す説明を出し、元の表に戻すと消える (#1776)", async ({ page }, testInfo) => {
    const payloads = await mockApi(page, { createStage: "target_choice" });
    await page.goto("/query");
    const panel = await startRequirementCheck(page, TARGET_QUESTION);
    const confirm = panel.getByTestId("nl2sql-clarification-confirm");
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
    // 説明は #1772 と同じ位置（入力欄の直下・ボタンの行より上）。
    const hintBox = await hint.boundingBox();
    const textBox = await freeText.boundingBox();
    const buttonBox = await generate.boundingBox();
    expect(hintBox && textBox && buttonBox).toBeTruthy();
    // 押せない理由は操作の行の直下（#1839 の共通のカード）。入力欄より下、確定のボタンより下。
    expect(hintBox!.y).toBeGreaterThanOrEqual(textBox!.y + textBox!.height);
    expect(hintBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height);
    await expectNoPageOverflow(page);
    await reduceMotion(page);
    await screenshotWhole(
      page,
      confirm,
      testInfo.outputPath(`sql-confirm-target-hint-${testInfo.project.name}-light.png`)
    );

    // 元の表に戻すと直しは無くなり、説明は消えて押せる。
    await original.check();
    await expect(hint).toHaveCount(0);
    await expect(generate).toBeEnabled();
    await expect(generate).not.toHaveAttribute("aria-describedby", /.+/u);
    // 選び直した後に自由入力を書くと（表の選択は外れる）、自由入力の説明に替わる。
    await other.check();
    await expect(hint).toHaveText(REQUIREMENT_TARGET_PENDING_HINT);
    await freeText.fill(TARGET_OTHER_TEXT);
    await expect(hint).toHaveText(REQUIREMENT_PENDING_HINT);
    await expect(generate).toHaveAccessibleDescription(REQUIREMENT_PENDING_HINT);
    await freeText.fill("");
    await expect(hint).toHaveCount(0);
    await expect(original).toBeChecked();
    await expect(generate).toBeEnabled();
    expect(payloads.clarificationAnswer).toBeUndefined();
    expect(payloads.job).toBeUndefined();
  });
});

test("回答前の確認は SQL の文の質問でも押せて、押すと確認を始める (#1727)", async ({ page }) => {
  const payloads = await mockApi(page);
  await page.goto("/query");
  const start = page.getByRole("button", { name: "回答前の確認", exact: true });
  // 押せないのは未入力のときだけ（理由の案内を出す）。
  await expect(start).toBeDisabled();
  await expect(start).toHaveAttribute("aria-describedby", "nl2sql-guided-query-required");
  await page.locator("#nl2sql-question-input").fill("select * from orders;");
  // SQL の文でも押せる（利用者の明示の操作。「回答前の確認は要りません」の案内は出さない）。
  await expect(start).toBeEnabled();
  await expect(start).not.toHaveAttribute("aria-describedby", /.+/u);
  await expect(page.getByText("回答前の確認は要りません")).toHaveCount(0);
  await start.click();
  await expect(page.getByTestId("nl2sql-guided-clarification")).toBeVisible();
  await expect
    .poll(() => payloads.create, { timeout: 10_000 })
    .toMatchObject({ question: "select * from orders;", clarification_mode: "guided" });
});

test("回答前の確認の検査の失敗は理由と詳細を出し、確認せずに SQL を生成できる (#1715)", async ({ page }, testInfo) => {
  const payloads = await mockApi(page, { createFailure: REQUIREMENT_LLM_FAILURE });
  await page.goto("/query");
  const panel = await startRequirementCheck(page);
  const error = panel.getByTestId("nl2sql-guided-clarification-error");
  // チャットと同じ部品（理由・「詳細」・もう一度確認・確認せずに SQL を生成・中止）。
  await expect(error).toContainText("サーバーのエラーを返しました（HTTP 500、2 回試行）");
  await expect(error).toContainText("もう一度確認するか、確認せずに SQL を生成してください。");
  await expect(error).toContainText("xai.grok-4.3");
  await expect(error).toContainText("opc-req-500");
  await expect(error).toContainText("Internal server error (internal_error)");
  await expect(error).toContainText("req-requirement-1");
  await expect(error).toContainText("28.4 秒");
  await expectNoPageOverflow(page);
  await error.screenshot({ path: testInfo.outputPath("guided-clarification-failure.png") });

  await error.getByRole("button", { name: "もう一度確認" }).click();
  await expect.poll(() => payloads.createFailures).toBe(2);
  await expect(error).toBeVisible();

  await error.getByRole("button", { name: "確認せずに SQL を生成" }).click();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => payloads.job).toMatchObject({ question: REQUIREMENT_QUESTION });
  expect(payloads.job).not.toHaveProperty("requirement_session_id");
});

test("回答前の確認のカードは「確認をやめる」を持たず、「確認せずに SQL を生成」で元の質問のまま生成する", async ({ page }) => {
  const payloads = await mockApi(page);
  await page.goto("/query");

  const questionInput = page.locator("#nl2sql-question-input");
  const panel = await startRequirementCheck(page);
  const question = panel.getByTestId("nl2sql-clarification-question");
  await expect(question.getByRole("button", { name: "確認をやめる" })).toHaveCount(0);
  await question.getByRole("button", { name: "確認せずに SQL を生成" }).click();

  await expect(panel).toHaveCount(0);
  await expect(questionInput).toHaveValue(REQUIREMENT_QUESTION);
  await expect.poll(() => payloads.cancel).toBe(true);
  await expect.poll(() => payloads.job).toMatchObject({ question: REQUIREMENT_QUESTION });
  expect(payloads.clarificationAnswer).toBeUndefined();
});

for (const storageBlocked of [false, true]) {
test(`ALLの要件確認はProfileと質問を引き継ぐ（保存不可=${storageBlocked}）`, async ({ page }, testInfo) => {
  const payloads = await mockApi(page);
  const allProfile = {
    ...profile,
    id: "all",
    name: "ALL",
    category: "ALL",
    description: "全業務プロファイルから候補を選択します。",
  };
  await page.route("**/api/nl2sql/profiles/search**", async (route) => {
    return fulfill(route, {
      items: [allProfile, profile].map((item) => ({
        ...item,
        allowed_table_count: item.allowed_tables.length,
        allowed_view_count: item.allowed_views.length,
        glossary_count: Object.keys(item.glossary).length,
        few_shot_count: item.few_shot_examples.length,
        version: 1,
        etag: `${item.id}-etag`,
        updated_at: "2026-07-11T00:00:00Z",
      })),
      next_cursor: null,
      total: 2,
      change_token: 1,
    });
  });
  await page.route("**/api/nl2sql/profiles/all/usage-context", async (route) => {
    return fulfill(route, {
      ...allProfile,
      object_scope_version: 1,
      version: 1,
      etag: "all-etag",
      updated_at: "2026-07-11T00:00:00Z",
    });
  });
  await page.route("**/api/nl2sql/ontology/profile-recommendations", async (route) => {
    return fulfill(route, {
      recommendation: {
        id: "recommendation-all",
        question_hash: "b".repeat(64),
        ontology_revision_id: "revision-1",
        candidates: [
          {
            profile_id: "default",
            profile_name: "標準プロファイル",
            ontology_revision_id: "revision-1",
            score: 0.94,
            matched_scenarios_ja: ["受注分析"],
            matched_terms: ["受注"],
            reasons_ja: ["受注分析の業務定義に一致しました。"],
          },
          {
            profile_id: "all",
            profile_name: "ALL",
            ontology_revision_id: "revision-1",
            score: 0.52,
            matched_scenarios_ja: [],
            matched_terms: [],
            reasons_ja: ["全体として利用できます。"],
          },
        ],
        expires_at: "2026-07-19T12:15:00Z",
      },
    });
  });
  await page.route("**/ontology/profile-recommendations/recommendation-all/confirm", async (route) => {
    const confirmation = route.request().postDataJSON();
    payloads.profileConfirmation = confirmation;
    return fulfill(route, {
      recommendation: {
        id: "recommendation-all",
        question_hash: "b".repeat(64),
        ontology_revision_id: "revision-1",
        candidates: [],
        selected_profile_id: "default",
        selected_revision_id: "revision-1",
        expires_at: "2026-07-19T12:15:00Z",
      },
      confirmation_token: "profile-confirmation-token",
    });
  });

  await page.goto("/query");
  await chooseSelectFieldOption(page.locator("#nl2sql-profile-select"), "all");
  const panel = await startRequirementCheck(page);
  await expect(panel.getByRole("group", { name: "どの業務プロファイルで確認しますか？" })).toBeVisible();
  await panel.getByRole("radio", { name: /標準プロファイル/ }).check();
  await panel.getByRole("button", { name: "この業務プロファイルで開始" }).click();
  await expect(panel.getByRole("heading", { name: "「今年度」は、どの期間ですか？" })).toBeVisible();
  expect(payloads.profileConfirmation).toMatchObject({ selected_profile_id: "default" });
  expect(payloads.create).toMatchObject({ profile_id: "default", clarification_mode: "guided" });
  await panel.getByRole("radio", { name: /2026-04-01〜2027-03-31/ }).check();
  await panel.getByRole("button", { name: "この内容で回答" }).click();
  if (storageBlocked) {
    await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (this === window.sessionStorage) throw new DOMException("unavailable", "QuotaExceededError");
        return original.call(this, key, value);
      };
    });
  }
  await panel.getByRole("button", { name: "この内容で SQL を生成して実行" }).click();
  if (storageBlocked) {
    await expect(panel).toBeVisible();
    await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "all");
    await expect(page.locator("#nl2sql-question-input")).toHaveValue(REQUIREMENT_QUESTION);
    expect(payloads.job).toBeUndefined();
    await page.screenshot({ path: testInfo.outputPath("guided-profile-storage-error.png"), fullPage: true });
    return;
  }
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  // 確定した業務プロファイルに切り替えてから、確定した要件で SQL を生成して実行する（#1761）。
  await expect(page.getByRole("columnheader", { name: "ORDER_COUNT" })).toBeVisible();
  expect(payloads.job).toMatchObject({
    profile_id: "default",
    question: REQUIREMENT_QUESTION,
    requirement_session_id: REQUIREMENT_SESSION_ID,
  });
  // 業務プロファイルが変わっても、質問の欄は利用者の入力のまま（#1727）。
  await expect(page.locator("#nl2sql-question-input")).toHaveValue(REQUIREMENT_QUESTION);
  // Profile ごとの草稿を維持し、確認した Profile の質問を再読込でも復元する。
  await chooseSelectFieldOption(page.locator("#nl2sql-profile-select"), "all");
  await expect(page.locator("#nl2sql-question-input")).toHaveValue(REQUIREMENT_QUESTION);
  await chooseSelectFieldOption(page.locator("#nl2sql-profile-select"), "default");
  await expect(page.locator("#nl2sql-question-input")).toHaveValue(REQUIREMENT_QUESTION);
  await page.reload();
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  await expect(page.locator("#nl2sql-question-input")).toHaveValue(REQUIREMENT_QUESTION);
  await page.screenshot({ path: testInfo.outputPath("guided-profile-applied.png"), fullPage: true });
});
}

test("対象オブジェクトは業務プロファイル、構築と Markdown 下書きは専用の単一ページに分離される", async ({ page }, testInfo) => {
  await mockApi(page);

  // 業務プロファイル編集: 対象オブジェクト一覧は常時表示、オントロジー構築は非表示。
  await page.goto("/profiles");
  await page.getByTestId("profile-management-grid").locator("tbody tr").first().locator("td").nth(1).click();
  await expect(page.getByTestId("profile-allowed-table-list")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);

  // 旧 tab URL は正規化され、構築と Markdown 下書きが同じ専用ページに表示される。
  await page.goto("/ontology-build?profile=default&tab=model");
  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);
  await expect(page.getByText("Inspector", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("ontology-build.png"), fullPage: true });

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

test("旧モデル編集 UI は表示せず Markdown 下書きを唯一の編集入口にする", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default&tab=model");
  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);

  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Markdown オントロジー下書き" })).toBeVisible();
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toBeVisible();
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);
  await expect(page.getByLabel("Inspector の編集対象")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Draft を保存", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "意味定義を Draft に保存" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "質問のオントロジー接地確認" })).toBeVisible();
});
