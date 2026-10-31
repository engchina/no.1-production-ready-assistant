import assert from "node:assert/strict";
import test from "node:test";

import { querySessionPath } from "../src/features/nl2sql/ontology/api.ts";
import {
  boundedOntologyGraph,
  currentIntentForSession,
  currentIntentVersionForSession,
  currentSqlArtifactForSession,
  executionBindingForSession,
  hasGraphPatchVersionConflict,
  intentGraphToOntologyGraph,
  ontologyRelationshipRows,
  profileScopedOntologyGraph,
  querySessionState,
  sortOntologyRelationshipRows,
  sqlSemanticGraphToOntologyGraph,
  type OntologyGraph,
  type QuerySession,
  type QuestionIntentGraph,
  type SqlSemanticGraph,
} from "../src/features/nl2sql/ontology/types.ts";
import { groundSqlSemanticGraphOnOntologyGraph } from "../src/features/nl2sql/ontology/sqlGrounding.ts";

test("ontology graph is bounded to 100 nodes and reports omitted kinds and edges", () => {
  const graph: OntologyGraph = {
    nodes: Array.from({ length: 105 }, (_, index) => ({
      id: `node-${index}`,
      kind: index >= 100 ? "metric" : "business_entity",
      business_name_ja: `ノード ${index}`,
    })),
    edges: Array.from({ length: 104 }, (_, index) => ({
      id: `edge-${index}`,
      source_node_id: `node-${index}`,
      target_node_id: `node-${index + 1}`,
      relationship_name_ja: "関連",
    })),
  };

  const visible = boundedOntologyGraph(graph, 500);

  assert.equal(visible.nodes.length, 100);
  assert.equal(visible.edges.length, 99);
  assert.equal(visible.hidden_node_count, 5);
  assert.equal(visible.hidden_edge_count, 5);
  assert.equal(visible.hidden_node_kinds.metric, 5);
});

test("relationship list renders ordered composite join conditions and sorts in Japanese", () => {
  const graph: OntologyGraph = {
    nodes: [
      { id: "order", kind: "business_entity", business_name_ja: "注文" },
      { id: "customer", kind: "business_entity", business_name_ja: "顧客" },
    ],
    edges: [
      {
        id: "order-customer",
        source_node_id: "order",
        target_node_id: "customer",
        relationship_name_ja: "購入者",
        review_status: "published",
        join_conditions: [
          { source_column: "TENANT_ID", target_column: "TENANT_ID", ordinal: 1 },
          { source_column: "CUSTOMER_ID", target_column: "ID", ordinal: 2 },
        ],
      },
    ],
  };

  const rows = ontologyRelationshipRows(graph);
  assert.equal(rows[0]?.join_condition, "TENANT_ID = TENANT_ID AND CUSTOMER_ID = ID");
  assert.equal(rows[0]?.detail_kind, "join");
  assert.equal(rows[0]?.detail_text, "TENANT_ID = TENANT_ID AND CUSTOMER_ID = ID");
  assert.equal(rows[0]?.validation_status, "passed");
  assert.equal(sortOntologyRelationshipRows(rows, "target", "desc")[0]?.target_label, "顧客");
});

test("relationship rows qualify join conditions and fall back to physical name or description", () => {
  const graph: OntologyGraph = {
    nodes: [
      {
        id: "department-table",
        kind: "table",
        business_name_ja: "部署情報",
        technical_name: "ADMIN.DEPARTMENT",
      },
      {
        id: "employee-table",
        kind: "table",
        business_name_ja: "従業員情報",
        technical_name: "ADMIN.EMPLOYEE",
      },
      {
        id: "created-at-column",
        kind: "column",
        business_name_ja: "レコード作成日時。",
        technical_name: "ADMIN.DEPARTMENT.CREATED_AT",
      },
      { id: "department-view", kind: "view", business_name_ja: "部署ビュー", technical_name: "ADMIN.V_DEPARTMENT" },
    ],
    edges: [
      {
        id: "fk-employee-department",
        kind: "foreign_key",
        source_node_id: "employee-table",
        target_node_id: "department-table",
        relationship_name_ja: "従業員情報 → 部署情報",
        description_ja: "Oracle 外部キー FK_EMP_DEPT",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
          },
        ],
      },
      {
        id: "contains-created-at",
        kind: "contains",
        source_node_id: "department-table",
        target_node_id: "created-at-column",
        relationship_name_ja: "含む",
      },
      {
        id: "lineage-department-view",
        kind: "lineage",
        source_node_id: "department-view",
        target_node_id: "department-table",
        relationship_name_ja: "参照元",
        description_ja: "ADMIN.V_DEPARTMENT は ADMIN.DEPARTMENT を参照",
      },
    ],
  };

  const rows = ontologyRelationshipRows(graph);
  const byId = new Map(rows.map((row) => [row.edge_id, row]));

  // Join 条件は所有者・表名まで修飾する(列名だけでは左右の表が分からないため)。
  assert.equal(byId.get("fk-employee-department")?.detail_kind, "join");
  assert.equal(
    byId.get("fk-employee-department")?.detail_text,
    "ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"
  );

  // 含む(contains)は構造上 Join 条件を持たないため、物理対応名へ縮退し空欄を出さない。
  assert.equal(byId.get("contains-created-at")?.join_condition, "");
  assert.equal(byId.get("contains-created-at")?.detail_kind, "physical");
  assert.equal(byId.get("contains-created-at")?.detail_text, "ADMIN.DEPARTMENT.CREATED_AT");

  // 説明を持つ関係(lineage)は説明を出す。
  assert.equal(byId.get("lineage-department-view")?.detail_kind, "description");
  assert.equal(
    byId.get("lineage-department-view")?.detail_text,
    "ADMIN.V_DEPARTMENT は ADMIN.DEPARTMENT を参照"
  );
});

test("relationship rows report no detail when neither join, description nor physical name exists", () => {
  const graph: OntologyGraph = {
    nodes: [
      { id: "order", kind: "business_entity", business_name_ja: "注文" },
      { id: "customer", kind: "business_entity", business_name_ja: "顧客" },
    ],
    edges: [
      {
        id: "order-customer",
        source_node_id: "order",
        target_node_id: "customer",
        relationship_name_ja: "購入者",
      },
    ],
  };

  const rows = ontologyRelationshipRows(graph);
  assert.equal(rows[0]?.detail_kind, "none");
  assert.equal(rows[0]?.detail_text, "");
  assert.equal(rows[0]?.join_condition, "");
});

test("profile ontology scope excludes otherwise approved nodes and relationships", () => {
  const graph: OntologyGraph = {
    nodes: [
      { id: "allowed", kind: "business_entity", business_name_ja: "受注", review_status: "approved" },
      { id: "outside", kind: "metric", business_name_ja: "社外秘指標", review_status: "approved" },
    ],
    edges: [
      {
        id: "outside-edge",
        source_node_id: "allowed",
        target_node_id: "outside",
        relationship_name_ja: "非公開関係",
      },
    ],
  };

  const scoped = profileScopedOntologyGraph(
    {
      id: "view-1",
      profile_id: "sales",
      ontology_revision_id: "revision-1",
      node_ids: ["allowed"],
      edge_ids: [],
      graph,
    },
    graph
  );

  assert.deepEqual(scoped.nodes.map((node) => node.id), ["allowed"]);
  assert.deepEqual(scoped.edges, []);
});

test("backend intent contract converts to a readable graph including blocker ambiguity", () => {
  const intent: QuestionIntentGraph = {
    version: 3,
    question_original: "先月の売上は？",
    question_effective: "先月の受注売上合計",
    entities: [{ id: "e1", ontology_node_id: "entity:order", name_ja: "受注", role: "target" }],
    metrics: [{ id: "m1", ontology_node_id: "metric:sales", name_ja: "売上", aggregation: "sum" }],
    dimensions: [{ id: "d1", ontology_node_id: "property:month", name_ja: "月" }],
    filters: [
      {
        id: "f1",
        property_node_id: "property:status",
        label_ja: "受注状態",
        operator: "=",
        value: "確定",
      },
    ],
    ambiguities: [
      {
        id: "a1",
        code: "time_boundary",
        message_ja: "先月の締め境界を確認してください",
        blocking: true,
        resolved: false,
      },
    ],
  };

  const graph = intentGraphToOntologyGraph(intent);

  assert.ok(graph.nodes.some((node) => node.id === "entity:order" && node.business_name_ja === "受注"));
  assert.ok(graph.nodes.some((node) => node.id === "metric:sales" && node.kind === "metric"));
  assert.ok(graph.nodes.some((node) => node.id === "a1" && node.validation_status === "blocked"));
  assert.ok(graph.edges.some((edge) => edge.relationship_name_ja === "要確認"));
});

test("Oracle SQL semantic graph converts AST elements and unreviewed join into graph", () => {
  const sqlGraph: SqlSemanticGraph = {
    version: 1,
    sql_hash: "sql-hash",
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: "SELECT SUM(o.amount) FROM orders o JOIN customers c ON c.id=o.customer_id GROUP BY c.name",
    ctes: [],
    tables: [{ expression: "ORDERS", alias: "o" }, { expression: "CUSTOMERS", alias: "c" }],
    columns: [{ expression: "o.amount" }, { expression: "c.name" }],
    joins: [
      {
        expression: "ORDERS → CUSTOMERS",
        condition: "c.id = o.customer_id",
        join_type: "INNER",
        reviewed_path: false,
      },
    ],
    filters: [],
    aggregates: [{ expression: "SUM(o.amount)" }],
    group_by: [],
    groups: [{ expression: "c.name" }],
    having: [],
    order_by: [],
    orders: [],
    windows: [],
    limit: 100,
  };

  const graph = sqlSemanticGraphToOntologyGraph(sqlGraph);

  assert.ok(graph.nodes.some((node) => node.kind === "sql_group" && node.business_name_ja === "c.name"));
  assert.ok(graph.nodes.some((node) => node.kind === "sql_join" && node.validation_status === "blocked"));
  assert.ok(graph.nodes.some((node) => node.kind === "sql_limit" && node.business_name_ja === "上限 100 件"));
});

test("SQL semantic graph grounds generated SQL on profile ontology graph", () => {
  const ontologyGraph: OntologyGraph = {
    nodes: [
      {
        id: "dept-business",
        kind: "business_entity",
        business_name_ja: "部署",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "dept-table",
              owner: "ADMIN",
              object_name: "DEPARTMENT",
              object_type: "table",
            },
          },
        ],
      },
      {
        id: "dept-table",
        kind: "table",
        technical_name: "ADMIN.DEPARTMENT",
        business_name_ja: "部署情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT" },
      },
      {
        id: "dept-name",
        kind: "property",
        technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
        business_name_ja: "部署名",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: { node_id: "dept-table", owner: "ADMIN", object_name: "DEPARTMENT" },
            column_refs: [{ owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" }],
          },
        ],
      },
      {
        id: "employee-table",
        kind: "table",
        technical_name: "ADMIN.EMPLOYEE",
        business_name_ja: "従業員情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE" },
      },
      {
        id: "employee-name",
        kind: "property",
        technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
        business_name_ja: "従業員氏名",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "EMPLOYEE_NAME" },
      },
    ],
    edges: [
      {
        id: "dept-employee",
        source_node_id: "dept-table",
        target_node_id: "employee-table",
        relationship_name_ja: "部署に所属する従業員",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
          },
        ],
        review_status: "approved",
      },
    ],
  };
  const sqlGraph: SqlSemanticGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql:
      'SELECT d.DEPARTMENT_NAME, e.EMPLOYEE_NAME FROM ADMIN.DEPARTMENT d JOIN ADMIN.EMPLOYEE e ON e.DEPARTMENT_ID = d.DEPARTMENT_ID',
    ctes: [],
    tables: [
      { owner: "ADMIN", name: "DEPARTMENT", alias: "d", qualified_name: "ADMIN.DEPARTMENT" },
      { qualified_name: "ADMIN.EMPLOYEE", alias: "e" },
    ],
    columns: [
      { table: "d", name: "DEPARTMENT_NAME", clause: "select", expression_sql: "d.DEPARTMENT_NAME" },
      { expression_sql: "e.EMPLOYEE_NAME", clause: "select", referenced_columns: ["e.EMPLOYEE_NAME"] },
      // backend の columns[] は where 句の列も含む(filters[] は同じ列の再掲)。
      { table: "e", name: "UNKNOWN_COLUMN", clause: "where", expression_sql: '"e"."UNKNOWN_COLUMN"' },
    ],
    joins: [
      {
        left_source: "ADMIN.DEPARTMENT d",
        right_source: "ADMIN.EMPLOYEE e",
        condition_sql: "e.DEPARTMENT_ID = d.DEPARTMENT_ID",
      },
    ],
    filters: [{ expression_sql: "e.UNKNOWN_COLUMN IS NOT NULL", referenced_columns: ["e.UNKNOWN_COLUMN"] }],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
  };

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.equal(result.status, "partial");
  assert.ok(result.highlightNodeIds.includes("dept-business"));
  assert.ok(result.highlightNodeIds.includes("dept-table"));
  assert.ok(result.highlightNodeIds.includes("dept-name"));
  assert.ok(result.highlightNodeIds.includes("employee-name"));
  assert.ok(result.highlightEdgeIds.includes("dept-employee"));
  assert.deepEqual(result.unmatchedTables, []);
  assert.deepEqual(result.unmatchedJoins, []);
  assert.deepEqual(result.unmatchedColumns, ['"e"."UNKNOWN_COLUMN"']);
});

test("star join grounds each SQL join on the relationship its ON clause actually links", () => {
  const objectNode = (id: string, owner: string, objectName: string, label: string) => ({
    id,
    kind: "table" as const,
    technical_name: `${owner}.${objectName}`,
    business_name_ja: label,
    review_status: "approved" as const,
    physical_mappings: [
      { object_ref: { node_id: id, owner, object_name: objectName, object_type: "table" as const } },
    ],
  });
  const ontologyGraph: OntologyGraph = {
    nodes: [
      objectNode("dept-table", "ADMIN", "DEPARTMENT", "部署情報"),
      objectNode("employee-table", "ADMIN", "EMPLOYEE", "従業員情報"),
      objectNode("project-table", "ADMIN", "PROJECT", "プロジェクト情報"),
    ],
    edges: [
      {
        id: "dept-employee",
        kind: "foreign_key",
        source_node_id: "employee-table",
        target_node_id: "dept-table",
        relationship_name_ja: "従業員情報 → 部署情報",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
        review_status: "approved",
      },
      {
        id: "dept-project",
        kind: "foreign_key",
        source_node_id: "project-table",
        target_node_id: "dept-table",
        relationship_name_ja: "プロジェクト情報 → 部署情報",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "PROJECT", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
        review_status: "approved",
      },
    ],
  };
  const sqlGraph: SqlSemanticGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    ctes: [],
    tables: [
      { owner: "ADMIN", name: "DEPARTMENT", alias: "D", qualified_name: "ADMIN.DEPARTMENT" },
      { owner: "ADMIN", name: "EMPLOYEE", alias: "E", qualified_name: "ADMIN.EMPLOYEE" },
      { owner: "ADMIN", name: "PROJECT", alias: "P", qualified_name: "ADMIN.PROJECT" },
    ],
    columns: [],
    joins: [
      {
        left_source: "ADMIN.DEPARTMENT D",
        right_source: "ADMIN.EMPLOYEE E",
        condition_sql: '"E"."DEPARTMENT_ID" = "D"."DEPARTMENT_ID"',
      },
      {
        // 既存 artifact は誤った位置ベースの左端点(EMPLOYEE)を持つ。
        left_source: "ADMIN.EMPLOYEE E",
        right_source: "ADMIN.PROJECT P",
        condition_sql: '"P"."DEPARTMENT_ID" = "D"."DEPARTMENT_ID"',
      },
    ],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
  };

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.deepEqual(result.unmatchedJoins, []);
  assert.equal(result.matchedJoins.length, 2);
  assert.deepEqual(
    result.matchedJoins.map((join) => join.ontologyEdgeIds).flat().sort(),
    ["dept-employee", "dept-project"]
  );
  assert.ok(result.highlightEdgeIds.includes("dept-project"));
  assert.ok(result.highlightNodeIds.includes("project-table"));
  assert.equal(result.status, "matched");
});

test("join grounding falls back to table endpoints when the ON clause has no qualified columns", () => {
  const ontologyGraph: OntologyGraph = {
    nodes: [
      {
        id: "dept-table",
        kind: "table",
        technical_name: "ADMIN.DEPARTMENT",
        business_name_ja: "部署情報",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "dept-table",
              owner: "ADMIN",
              object_name: "DEPARTMENT",
              object_type: "table",
            },
          },
        ],
      },
      {
        id: "employee-table",
        kind: "table",
        technical_name: "ADMIN.EMPLOYEE",
        business_name_ja: "従業員情報",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "employee-table",
              owner: "ADMIN",
              object_name: "EMPLOYEE",
              object_type: "table",
            },
          },
        ],
      },
    ],
    edges: [
      {
        id: "dept-employee",
        kind: "foreign_key",
        source_node_id: "dept-table",
        target_node_id: "employee-table",
        relationship_name_ja: "部署と従業員の Join",
        review_status: "approved",
      },
    ],
  };
  const sqlGraph: SqlSemanticGraph = {
    dialect: "oracle",
    ctes: [],
    tables: [
      { owner: "ADMIN", name: "DEPARTMENT", alias: "D", qualified_name: "ADMIN.DEPARTMENT" },
      { owner: "ADMIN", name: "EMPLOYEE", alias: "E", qualified_name: "ADMIN.EMPLOYEE" },
    ],
    columns: [],
    joins: [
      {
        left_source: "ADMIN.DEPARTMENT D",
        right_source: "ADMIN.EMPLOYEE E",
        condition_sql: "",
        using_columns: ["DEPARTMENT_ID"],
      },
    ],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
  };

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.deepEqual(result.unmatchedJoins, []);
  assert.deepEqual(result.matchedJoins[0]?.ontologyEdgeIds, ["dept-employee"]);
});

// 単一表 SELECT で未修飾列を出す実 SQL 相当の fixture。
// SELECT "EMPLOYEE_ID","DEPARTMENT_ID","SALARY" FROM "ADMIN"."EMPLOYEE" "EMP"
// backend の parse_oracle_sql は columns[] を table:"" (未修飾) で返し、
// projections[] に同じ列を再掲する。
function unqualifiedSingleTableFixture() {
  const objectNode = (id: string, owner: string, objectName: string, label: string) => ({
    id,
    kind: "table" as const,
    technical_name: `${owner}.${objectName}`,
    business_name_ja: label,
    review_status: "approved" as const,
    metadata: { owner, object_name: objectName },
  });
  const columnNode = (
    id: string,
    owner: string,
    objectName: string,
    columnName: string,
    label: string
  ) => ({
    id,
    kind: "column" as const,
    technical_name: `${owner}.${objectName}.${columnName}`,
    business_name_ja: label,
    review_status: "approved" as const,
    metadata: { owner, object_name: objectName, column_name: columnName },
  });
  const ontologyGraph: OntologyGraph = {
    nodes: [
      {
        id: "employee-business",
        kind: "business_entity",
        business_name_ja: "従業員",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "employee-table",
              owner: "ADMIN",
              object_name: "EMPLOYEE",
              object_type: "table",
            },
          },
        ],
      },
      objectNode("employee-table", "ADMIN", "EMPLOYEE", "従業員情報"),
      objectNode("dept-table", "ADMIN", "DEPARTMENT", "部署情報"),
      objectNode("project-table", "ADMIN", "PROJECT", "プロジェクト情報"),
      columnNode("employee-id", "ADMIN", "EMPLOYEE", "EMPLOYEE_ID", "従業員ID"),
      columnNode("employee-dept-id", "ADMIN", "EMPLOYEE", "DEPARTMENT_ID", "所属部署ID"),
      columnNode("employee-salary", "ADMIN", "EMPLOYEE", "SALARY", "給与"),
      // 同名列を持つ無関係な表。未修飾列の横断一致で誤接地しやすい。
      columnNode("dept-dept-id", "ADMIN", "DEPARTMENT", "DEPARTMENT_ID", "部署ID"),
      columnNode("project-dept-id", "ADMIN", "PROJECT", "DEPARTMENT_ID", "部門ID"),
    ],
    edges: [],
  };
  const columns = [
    { scope_id: "scope_1", table: "", name: "EMPLOYEE_ID", clause: "select", expression_sql: '"EMPLOYEE_ID"' },
    { scope_id: "scope_1", table: "", name: "DEPARTMENT_ID", clause: "select", expression_sql: '"DEPARTMENT_ID"' },
    { scope_id: "scope_1", table: "", name: "SALARY", clause: "select", expression_sql: '"SALARY"' },
  ];
  const sqlGraph: SqlSemanticGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    ctes: [],
    tables: [
      {
        scope_id: "scope_1",
        owner: "ADMIN",
        name: "EMPLOYEE",
        alias: "EMP",
        qualified_name: "ADMIN.EMPLOYEE",
      },
    ],
    columns,
    projections: columns.map((column) => ({
      scope_id: "scope_1",
      output_name: column.name,
      expression_sql: `${column.expression_sql} AS "${column.name}"`,
      referenced_columns: [column.name],
    })),
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
  };
  return { ontologyGraph, sqlGraph };
}

test("unqualified columns ground only on the tables in the SQL FROM scope", () => {
  const { ontologyGraph, sqlGraph } = unqualifiedSingleTableFixture();

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.equal(result.status, "matched");
  for (const nodeId of ["employee-table", "employee-business", "employee-id", "employee-dept-id", "employee-salary"]) {
    assert.ok(result.highlightNodeIds.includes(nodeId), `expected highlight: ${nodeId}`);
  }
  // 同名 DEPARTMENT_ID を持つだけの無関係な表・列は接地しない。
  for (const nodeId of ["dept-table", "dept-dept-id", "project-table", "project-dept-id"]) {
    assert.ok(!result.highlightNodeIds.includes(nodeId), `unexpected highlight: ${nodeId}`);
  }
});

test("columns[] is the single source so projections[] does not double count grounded columns", () => {
  const { ontologyGraph, sqlGraph } = unqualifiedSingleTableFixture();

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.equal(result.matchedTables.length, 1);
  assert.equal(result.matchedColumns.length, 3);
  assert.equal(result.matchedJoins.length, 0);
  assert.deepEqual(result.unmatchedColumns, []);
});

test("unqualified columns absent from the FROM scope are reported as unmatched", () => {
  const { ontologyGraph, sqlGraph } = unqualifiedSingleTableFixture();
  sqlGraph.columns = [
    ...sqlGraph.columns,
    // DEPARTMENT にしか無い列を未修飾で参照している = EMPLOYEE には接地できない。
    { scope_id: "scope_1", table: "", name: "DEPARTMENT_NAME", clause: "select", expression_sql: '"DEPARTMENT_NAME"' },
  ];
  // 実 backend と同じく projections にも同じ列が再掲される(出力別名 = 参照列なので
  // 「計算された別名」ではなく、未接地の抑止対象にならないことを確認する)。
  sqlGraph.projections = [
    ...(sqlGraph.projections ?? []),
    {
      scope_id: "scope_1",
      output_name: "DEPARTMENT_NAME",
      expression_sql: '"DEPARTMENT_NAME" AS "DEPARTMENT_NAME"',
      referenced_columns: ["DEPARTMENT_NAME"],
    },
  ];
  ontologyGraph.nodes.push({
    id: "dept-name",
    kind: "column",
    technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
    business_name_ja: "部署名",
    review_status: "approved",
    metadata: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" },
  });

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.equal(result.status, "partial");
  assert.deepEqual(result.unmatchedColumns, ['"DEPARTMENT_NAME"']);
  assert.ok(!result.highlightNodeIds.includes("dept-name"));
});

test("ORDER BY on a SELECT output alias is not reported as an unmatched column", () => {
  const { ontologyGraph, sqlGraph } = unqualifiedSingleTableFixture();
  // COUNT(*) AS "CNT" … ORDER BY "CNT" 相当。CNT は物理列ではないので未接地にしない。
  sqlGraph.columns = [
    ...sqlGraph.columns,
    { scope_id: "scope_1", table: "", name: "CNT", clause: "order", expression_sql: '"CNT"' },
  ];
  sqlGraph.projections = [
    ...(sqlGraph.projections ?? []),
    {
      scope_id: "scope_1",
      output_name: "CNT",
      expression_sql: 'COUNT(*) AS "CNT"',
      referenced_columns: [],
    },
  ];

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.deepEqual(result.unmatchedColumns, []);
  assert.equal(result.status, "matched");
});

test("query session adapters restore current version and hash-bound execution request", () => {
  const intentV1: QuestionIntentGraph = {
    version: 1,
    entities: [],
    metrics: [],
    dimensions: [],
    filters: [],
  };
  const intentV2: QuestionIntentGraph = {
    ...intentV1,
    version: 2,
    question_effective: "確定受注の売上",
  };
  const semanticGraph: SqlSemanticGraph = {
    dialect: "oracle",
    ctes: [],
    tables: [],
    columns: [],
    joins: [],
    filters: [],
    aggregates: [],
    group_by: [],
    having: [],
    order_by: [],
    windows: [],
  };
  const session: QuerySession = {
    id: "session-1",
    profile_id: "finance",
    ontology_revision_id: "ontology-r7",
    status: "awaiting_sql_confirmation",
    current_intent_version: 2,
    intents: [intentV1, intentV2],
    current_sql_artifact_id: "artifact-2",
    sql_artifacts: [
      {
        id: "artifact-2",
        intent_version: 2,
        ontology_revision_id: "ontology-r7",
        sql: "SELECT 1 FROM DUAL",
        sql_hash: "sql-hash-2",
        generation_context_hash: "context-hash-2",
        semantic_graph: semanticGraph,
        validation_report: {
          id: "validation-2",
          is_valid: true,
          findings: [],
          intent_coverage: 1,
          validation_hash: "validation-hash-2",
        },
      },
    ],
  };

  assert.equal(querySessionState(session), "awaiting_sql_confirmation");
  assert.equal(currentIntentVersionForSession(session), 2);
  assert.equal(currentIntentForSession(session)?.question_effective, "確定受注の売上");
  assert.equal(currentSqlArtifactForSession(session)?.id, "artifact-2");
  assert.deepEqual(executionBindingForSession(session), {
    session_id: "session-1",
    artifact_id: "artifact-2",
    ontology_revision_id: "ontology-r7",
    intent_version: 2,
    sql_hash: "sql-hash-2",
    validation_hash: "validation-hash-2",
    generation_context_hash: "context-hash-2",
  });
});

test("patch conflict and query-session path helpers are deterministic", () => {
  assert.equal(hasGraphPatchVersionConflict(2, 3), true);
  assert.equal(hasGraphPatchVersionConflict(3, 3), false);
  assert.equal(querySessionPath(), "/api/nl2sql/query-sessions");
  assert.equal(
    querySessionPath("session/with spaces", "generate-sql"),
    "/api/nl2sql/query-sessions/session%2Fwith%20spaces/generate-sql"
  );
  assert.equal(
    querySessionPath("guided-1", "clarification-answers"),
    "/api/nl2sql/query-sessions/guided-1/clarification-answers"
  );
  assert.equal(
    querySessionPath("guided-1", "cancel"),
    "/api/nl2sql/query-sessions/guided-1/cancel"
  );
});

test("SQL 接地の表ラベルは backend が current schema で補った所有者付き名前を使う (#556)", () => {
  const ontologyGraph: OntologyGraph = {
    profile_id: "p",
    // 一致しない表だけの graph（未一致ラベルを確かめる）
    nodes: [{ id: "other", kind: "table", business_name_ja: "その他", technical_name: "ADMIN.OTHER" }],
    edges: [],
  } as unknown as OntologyGraph;
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: "SELECT * FROM INVOICES JOIN HR.EMPLOYEES ON 1 = 1",
    ctes: [],
    tables: [
      {
        owner: "",
        name: "INVOICES",
        qualified_name: "INVOICES",
        resolved_owner: "APP",
        resolved_qualified_name: "APP.INVOICES",
      },
      // 旧 backend（resolved_* なし）は SQL に書かれた名前のまま
      { owner: "HR", name: "EMPLOYEES", qualified_name: "HR.EMPLOYEES" },
    ],
    columns: [],
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
  } as unknown as SqlSemanticGraph;

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph);

  assert.deepEqual(result.unmatchedTables, ["APP.INVOICES", "HR.EMPLOYEES"]);
});

function quotedSameNameOntologyGraph(): OntologyGraph {
  const table = (id: string, objectName: string, label: string) => ({
    id,
    kind: "table" as const,
    technical_name: objectName === objectName.toUpperCase() ? `SALES.${objectName}` : `SALES."${objectName}"`,
    business_name_ja: label,
    review_status: "approved" as const,
    metadata: { owner: "SALES", object_name: objectName },
  });
  const column = (id: string, objectName: string, columnName: string, label: string) => ({
    id,
    kind: "column" as const,
    technical_name: `SALES.${objectName}.${columnName}`,
    business_name_ja: label,
    review_status: "approved" as const,
    metadata: { owner: "SALES", object_name: objectName, column_name: columnName },
  });
  return {
    nodes: [
      table("upper-table", "MIXED_CASE", "大文字の表"),
      table("quoted-table", "Mixed_Case", "引用名の表"),
      column("upper-amount", "MIXED_CASE", "AMOUNT", "大文字の金額"),
      column("upper-id", "MIXED_CASE", "ID", "大文字の ID"),
      column("quoted-amount", "Mixed_Case", "Amount", "引用名の金額"),
      column("quoted-upper-amount", "Mixed_Case", "AMOUNT", "引用名の表の大文字列"),
      column("quoted-parent", "Mixed_Case", "PARENT_ID", "親 ID"),
    ],
    edges: [
      {
        id: "quoted-parent-fk",
        kind: "foreign_key",
        source_node_id: "quoted-table",
        target_node_id: "upper-table",
        relationship_name_ja: "親の表",
        join_conditions: [
          {
            left: { owner: "SALES", object_name: "Mixed_Case", column_name: "PARENT_ID" },
            right: { owner: "SALES", object_name: "MIXED_CASE", column_name: "ID" },
          },
        ],
        review_status: "approved",
      },
    ],
  } as unknown as OntologyGraph;
}

function emptySqlGraph(overrides: Partial<SqlSemanticGraph>): SqlSemanticGraph {
  return {
    dialect: "oracle",
    statement_type: "SELECT",
    ctes: [],
    tables: [],
    columns: [],
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    ...overrides,
  } as SqlSemanticGraph;
}

test("SQL 接地は引用名の表・列を大文字の同名表・同名列と区別する (#573)", () => {
  const sqlGraph = emptySqlGraph({
    raw_sql: 'SELECT m."Amount" FROM SALES."Mixed_Case" m',
    tables: [
      {
        scope_id: "scope_root",
        owner: "SALES",
        name: "Mixed_Case",
        alias: "m",
        qualified_name: "SALES.Mixed_Case",
        source_sql: 'SALES."Mixed_Case" m',
        owner_quoted: false,
        name_quoted: true,
      },
    ],
    columns: [
      {
        scope_id: "scope_root",
        table: "m",
        name: "Amount",
        clause: "select",
        expression_sql: 'm."Amount"',
        owner_quoted: false,
        table_quoted: false,
        name_quoted: true,
      },
    ],
  });

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, quotedSameNameOntologyGraph());

  assert.equal(result.status, "matched");
  assert.deepEqual(result.matchedTables.map((item) => item.ontologyNodeIds), [["quoted-table"]]);
  assert.deepEqual(result.matchedColumns.map((item) => item.ontologyNodeIds), [["quoted-amount", "quoted-table"]]);
  assert.ok(!result.highlightNodeIds.includes("upper-table"));
  assert.ok(!result.highlightNodeIds.includes("upper-amount"));
  assert.ok(!result.highlightNodeIds.includes("quoted-upper-amount"));
});

test("引用しない同名表の SQL は大文字の表だけに接地し、引用名の表を強調しない (#573)", () => {
  const sqlGraph = emptySqlGraph({
    raw_sql: "SELECT mixed_case.amount FROM sales.mixed_case",
    tables: [
      {
        owner: "sales",
        name: "mixed_case",
        qualified_name: "sales.mixed_case",
        source_sql: "sales.mixed_case",
        owner_quoted: false,
        name_quoted: false,
      },
    ],
    columns: [
      {
        table: "mixed_case",
        name: "amount",
        clause: "select",
        expression_sql: "mixed_case.amount",
        table_quoted: false,
        name_quoted: false,
      },
    ],
  });

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, quotedSameNameOntologyGraph());

  assert.deepEqual(result.matchedTables.map((item) => item.ontologyNodeIds), [["upper-table"]]);
  assert.deepEqual(result.matchedColumns.map((item) => item.ontologyNodeIds), [["upper-amount", "upper-table"]]);
  assert.ok(!result.highlightNodeIds.includes("quoted-table"));
});

test("引用情報を持たない旧 artifact と Join 条件も SQL の表記の引用符で区別する (#573)", () => {
  const sqlGraph = emptySqlGraph({
    raw_sql:
      'SELECT m."Amount" FROM SALES."Mixed_Case" m JOIN SALES.MIXED_CASE u ON m.PARENT_ID = u.ID',
    tables: [
      { name: "Mixed_Case", owner: "SALES", alias: "m", source_sql: 'SALES."Mixed_Case" m' },
      { name: "MIXED_CASE", owner: "SALES", alias: "u", source_sql: "SALES.MIXED_CASE u" },
    ],
    projections: [{ expression_sql: 'm."Amount"', referenced_columns: ['m."Amount"'] }],
    columns: [],
    joins: [
      {
        left_source: 'SALES."Mixed_Case" m',
        right_source: "SALES.MIXED_CASE u",
        condition_sql: "m.PARENT_ID = u.ID",
        referenced_columns: ["m.PARENT_ID", "u.ID"],
      },
    ],
  });

  const result = groundSqlSemanticGraphOnOntologyGraph(sqlGraph, quotedSameNameOntologyGraph());

  assert.deepEqual(result.matchedTables.map((item) => item.ontologyNodeIds), [["quoted-table"], ["upper-table"]]);
  assert.deepEqual(result.matchedColumns.map((item) => item.ontologyNodeIds), [["quoted-amount", "quoted-table"]]);
  assert.deepEqual(result.matchedJoins.map((item) => item.ontologyEdgeIds), [["quoted-parent-fk"]]);
  assert.deepEqual(result.unmatchedColumns, []);
});
