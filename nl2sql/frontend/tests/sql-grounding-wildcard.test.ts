import assert from "node:assert/strict";
import test from "node:test";

import { groundSqlSemanticGraphOnOntologyGraph } from "../src/features/nl2sql/ontology/sqlGrounding.ts";
import type {
  OntologyGraph,
  SqlSemanticGraph,
  SqlSemanticItem,
} from "../src/features/nl2sql/ontology/types.ts";

// 列展開（`*` / `別名.*` / `owner.表.*`）と `COUNT(*)` は列名 `*` の列の参照ではない（#1787）。
// SQL の要素は backend の parse_oracle_sql が返す形（引用の有無・scope 付き）に合わせる。

const ontologyGraph: OntologyGraph = {
  nodes: [
    {
      id: "dept-object",
      kind: "object_type",
      technical_name: "ADB_APP_USER.DEPARTMENT",
      business_name_ja: "部門",
      review_status: "approved",
      physical_mappings: [
        {
          object_ref: {
            node_id: "dept-table",
            owner: "ADB_APP_USER",
            object_name: "DEPARTMENT",
            object_type: "table",
          },
        },
      ],
    },
    {
      id: "dept-table",
      kind: "table",
      technical_name: "ADB_APP_USER.DEPARTMENT",
      business_name_ja: "部門の表",
      review_status: "approved",
      metadata: { owner: "ADB_APP_USER", object_name: "DEPARTMENT" },
    },
    {
      id: "dept-id",
      kind: "property",
      technical_name: "ADB_APP_USER.DEPARTMENT.DEPARTMENT_ID",
      business_name_ja: "部門 ID",
      review_status: "approved",
      physical_mappings: [
        {
          object_ref: { node_id: "dept-table", owner: "ADB_APP_USER", object_name: "DEPARTMENT" },
          column_refs: [{ owner: "ADB_APP_USER", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" }],
        },
      ],
    },
    {
      id: "dept-name",
      kind: "property",
      technical_name: "ADB_APP_USER.DEPARTMENT.DEPARTMENT_NAME",
      business_name_ja: "部門名",
      review_status: "approved",
      physical_mappings: [
        {
          object_ref: { node_id: "dept-table", owner: "ADB_APP_USER", object_name: "DEPARTMENT" },
          column_refs: [{ owner: "ADB_APP_USER", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" }],
        },
      ],
    },
    {
      id: "employee-table",
      kind: "table",
      technical_name: "ADB_APP_USER.EMPLOYEE",
      business_name_ja: "従業員の表",
      review_status: "approved",
      metadata: { owner: "ADB_APP_USER", object_name: "EMPLOYEE" },
    },
    {
      id: "employee-name",
      kind: "property",
      technical_name: "ADB_APP_USER.EMPLOYEE.EMPLOYEE_NAME",
      business_name_ja: "従業員名",
      review_status: "approved",
      metadata: { owner: "ADB_APP_USER", object_name: "EMPLOYEE", column_name: "EMPLOYEE_NAME" },
    },
  ],
  edges: [],
};

const departmentTable: SqlSemanticItem = {
  scope_id: "scope_1",
  owner: "ADB_APP_USER",
  name: "DEPARTMENT",
  alias: "dept",
  qualified_name: "ADB_APP_USER.DEPARTMENT",
  is_cte: false,
  source_sql: '"ADB_APP_USER"."DEPARTMENT" dept',
  owner_quoted: true,
  name_quoted: true,
};

function sqlGraph(overrides: Partial<SqlSemanticGraph>): SqlSemanticGraph {
  return {
    dialect: "oracle",
    statement_type: "SELECT",
    ctes: [],
    tables: [departmentTable],
    columns: [],
    joins: [],
    projections: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    ...overrides,
  };
}

const DEPARTMENT_NODES = ["dept-object", "dept-table", "dept-id", "dept-name"];

test("alias.* is the table's all columns, not an unmatched column named *", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: 'SELECT dept.* FROM "ADB_APP_USER"."DEPARTMENT" dept',
      columns: [
        {
          scope_id: "scope_1",
          owner: "",
          table: "dept",
          name: "*",
          clause: "select",
          expression_sql: "dept.*",
          owner_quoted: false,
          table_quoted: false,
          name_quoted: false,
        },
      ],
      projections: [
        {
          scope_id: "scope_1",
          output_name: "*",
          expression_sql: "dept.*",
          referenced_columns: ["dept.*"],
          contains_wildcard: true,
        },
      ],
    }),
    ontologyGraph
  );

  assert.equal(result.status, "matched");
  assert.deepEqual(result.unmatchedColumns, []);
  assert.deepEqual(result.unmatchedTables, []);
  // 表の接地として数え、`dept.*` を別の列の要素にしない。
  assert.deepEqual(
    result.matchedTables.map((item) => item.sql),
    ["ADB_APP_USER.DEPARTMENT"]
  );
  assert.deepEqual(result.matchedColumns, []);
  // グラフでは、その表の全列を使われた列として示す。
  for (const nodeId of DEPARTMENT_NODES) assert.ok(result.highlightNodeIds.includes(nodeId), nodeId);
  assert.ok(!result.highlightNodeIds.includes("employee-name"));
});

test("unqualified SELECT * grounds the FROM table and highlights only its columns", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: 'SELECT * FROM "ADB_APP_USER"."DEPARTMENT" dept',
      projections: [
        {
          scope_id: "scope_1",
          output_name: "*",
          expression_sql: "*",
          referenced_columns: [],
          contains_wildcard: true,
        },
      ],
    }),
    ontologyGraph
  );

  assert.equal(result.status, "matched");
  assert.deepEqual(result.unmatchedColumns, []);
  for (const nodeId of DEPARTMENT_NODES) assert.ok(result.highlightNodeIds.includes(nodeId), nodeId);
  assert.ok(!result.highlightNodeIds.includes("employee-name"));
});

test("owner.table.* resolves the qualifier as the table", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: "SELECT ADB_APP_USER.DEPARTMENT.* FROM ADB_APP_USER.DEPARTMENT",
      tables: [
        {
          ...departmentTable,
          alias: "",
          source_sql: "ADB_APP_USER.DEPARTMENT",
          owner_quoted: false,
          name_quoted: false,
        },
      ],
      columns: [
        {
          scope_id: "scope_1",
          owner: "ADB_APP_USER",
          table: "DEPARTMENT",
          name: "*",
          clause: "select",
          expression_sql: "ADB_APP_USER.DEPARTMENT.*",
          owner_quoted: false,
          table_quoted: false,
          name_quoted: false,
        },
      ],
      projections: [
        {
          scope_id: "scope_1",
          output_name: "*",
          expression_sql: "ADB_APP_USER.DEPARTMENT.*",
          referenced_columns: ["ADB_APP_USER.DEPARTMENT.*"],
          contains_wildcard: true,
        },
      ],
    }),
    ontologyGraph
  );

  assert.equal(result.status, "matched");
  assert.deepEqual(result.unmatchedColumns, []);
  for (const nodeId of DEPARTMENT_NODES) assert.ok(result.highlightNodeIds.includes(nodeId), nodeId);
});

test("COUNT(*) is not an unmatched column", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: 'SELECT COUNT(*) FROM "ADB_APP_USER"."DEPARTMENT" dept',
      projections: [
        {
          scope_id: "scope_1",
          output_name: "COUNT(*)",
          expression_sql: "COUNT(*)",
          referenced_columns: [],
          contains_wildcard: false,
        },
      ],
      aggregates: [
        { scope_id: "scope_1", function_name: "COUNT", expression_sql: "COUNT(*)", referenced_columns: [] },
      ],
    }),
    ontologyGraph
  );

  assert.equal(result.status, "matched");
  assert.deepEqual(result.unmatchedColumns, []);
  // COUNT(*) は全列の展開ではないので、列を使われた列として示さない。
  assert.ok(!result.highlightNodeIds.includes("dept-name"));
});

test("alias.* on a table outside the ontology is reported only as the unmatched table", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: "SELECT u.* FROM ADB_APP_USER.UNKNOWN_TABLE u",
      tables: [
        {
          scope_id: "scope_1",
          owner: "ADB_APP_USER",
          name: "UNKNOWN_TABLE",
          alias: "u",
          qualified_name: "ADB_APP_USER.UNKNOWN_TABLE",
          is_cte: false,
          source_sql: "ADB_APP_USER.UNKNOWN_TABLE u",
          owner_quoted: false,
          name_quoted: false,
        },
      ],
      columns: [
        {
          scope_id: "scope_1",
          owner: "",
          table: "u",
          name: "*",
          clause: "select",
          expression_sql: "u.*",
          owner_quoted: false,
          table_quoted: false,
          name_quoted: false,
        },
      ],
      projections: [
        {
          scope_id: "scope_1",
          output_name: "*",
          expression_sql: "u.*",
          referenced_columns: ["u.*"],
          contains_wildcard: true,
        },
      ],
    }),
    ontologyGraph
  );

  assert.equal(result.status, "unmatched");
  assert.deepEqual(result.unmatchedTables, ["ADB_APP_USER.UNKNOWN_TABLE"]);
  assert.deepEqual(result.unmatchedColumns, []);
  assert.deepEqual(result.highlightNodeIds, []);
});

test("legacy artifacts without columns[] do not report alias.* in projections as unmatched", () => {
  const result = groundSqlSemanticGraphOnOntologyGraph(
    sqlGraph({
      raw_sql: 'SELECT dept.*, dept.UNKNOWN_COLUMN FROM "ADB_APP_USER"."DEPARTMENT" dept',
      projections: [
        { expression_sql: "dept.*", referenced_columns: ["dept.*"] },
        { expression_sql: "dept.UNKNOWN_COLUMN", referenced_columns: ["dept.UNKNOWN_COLUMN"] },
      ],
    }),
    ontologyGraph
  );

  // 列展開は未接地にせず、本当に無い列だけを未接地として出す。
  assert.equal(result.status, "partial");
  assert.deepEqual(result.unmatchedColumns, ["dept.UNKNOWN_COLUMN"]);
  assert.ok(result.highlightNodeIds.includes("dept-name"));
});
