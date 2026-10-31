import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDbObjectName,
  parseDbAdminObjectTarget,
} from "../src/features/nl2sql/dbObjectIdentity.ts";
import {
  filterUserVisibleCatalog,
  filterUserVisibleDbAdminObjectPage,
  filterUserVisibleSchemaObjectPage,
  isUserVisibleObjectName,
  isUserVisibleSchemaObject,
} from "../src/features/nl2sql/objectVisibility.ts";
import type {
  DbAdminObjectSummary,
  SchemaObjectSummary,
  SchemaTable,
} from "../src/features/nl2sql/types.ts";

const schemaTable = (name: string, tableType = "TABLE", owner = "APP"): SchemaTable => ({
  table_name: name,
  logical_name: name,
  owner,
  table_type: tableType,
  comment: "",
  columns: [],
  constraints: [],
});

const schemaObject = (
  name: string,
  objectType = "TABLE",
  owner = "APP"
): SchemaObjectSummary => ({
  owner,
  object_name: name,
  object_type: objectType,
  logical_name: name,
  comment: "",
  column_count: 0,
  last_ddl_at: "",
});

const dbAdminObject = (
  name: string,
  objectType = "table",
  owner = "APP"
): DbAdminObjectSummary => ({
  name,
  owner,
  object_type: objectType,
  row_count: null,
  comment: "",
});

test("object visibility rejects Oracle system-name markers", () => {
  assert.equal(isUserVisibleObjectName("ORDERS"), true);
  assert.equal(isUserVisibleObjectName("TD_NL2SQL_ORDERS"), true);
  assert.equal(isUserVisibleObjectName("NL2SQL_APP.ORDERS"), true);
  assert.equal(isUserVisibleObjectName("DBTOOLS$EXECUTION_HISTORY"), false);
  assert.equal(isUserVisibleObjectName("SYS#AUDIT"), false);
  assert.equal(isUserVisibleObjectName("NL2SQL_SCHEMA_OBJECTS"), false);
  assert.equal(isUserVisibleObjectName('"nl2sql_schema_objects"'), false);
  assert.equal(isUserVisibleObjectName("APP.NL2SQL_SCHEMA_OBJECTS"), false);
  assert.equal(isUserVisibleSchemaObject("APP", "ORDERS"), true);
  assert.equal(isUserVisibleSchemaObject("NL2SQL_APP", "ORDERS"), true);
  assert.equal(isUserVisibleSchemaObject("APP", "TD_NL2SQL_ORDERS"), true);
  assert.equal(isUserVisibleSchemaObject("APP", "NL2SQL_SCHEMA_OBJECTS"), false);
  assert.equal(isUserVisibleSchemaObject("RMAN$CATALOG", "RC_ARCHIVED_LOG"), false);
  assert.equal(isUserVisibleSchemaObject("SYS#CATALOG", "AUDIT_LOG"), false);
});

test("catalog and schema pages remove system objects before updating counts", () => {
  const catalog = filterUserVisibleCatalog({
    refreshed_at: "2026-07-22T00:00:00.000Z",
    tables: [
      schemaTable("ORDERS"),
      schemaTable("TD_NL2SQL_ORDERS"),
      schemaTable("ORDERS", "TABLE", "NL2SQL_APP"),
      schemaTable("NL2SQL_SCHEMA_OBJECTS"),
      schemaTable("VECTOR_IDX$VECTAB"),
      schemaTable("RC_ARCHIVED_LOG", "VIEW", "RMAN$CATALOG"),
    ],
    view_dependencies: [
      {
        owner: "APP",
        view_name: "ORDER_VIEW",
        referenced_owner: "APP",
        referenced_name: "ORDERS",
      },
      {
        owner: "APP",
        view_name: "ORDER_VIEW",
        referenced_owner: "APP",
        referenced_name: "SYS#AUDIT",
      },
      {
        owner: "APP",
        view_name: "ORDER_VIEW",
        referenced_owner: "APP",
        referenced_name: "NL2SQL_SCHEMA_OBJECTS",
      },
      {
        owner: "APP",
        view_name: "ORDER_VIEW",
        referenced_owner: "RMAN$CATALOG",
        referenced_name: "RC_ARCHIVED_LOG",
      },
    ],
  });
  const page = filterUserVisibleSchemaObjectPage({
    items: [
      schemaObject("ORDERS"),
      schemaObject("TD_NL2SQL_ORDERS"),
      schemaObject("ORDERS", "TABLE", "NL2SQL_APP"),
      schemaObject("NL2SQL_SCHEMA_OBJECTS"),
      schemaObject("VECTOR_IDX$VECTAB"),
      schemaObject("SYS#AUDIT", "VIEW"),
      schemaObject("RC_ARCHIVED_LOG", "VIEW", "RMAN$CATALOG"),
    ],
    next_cursor: null,
    total: 7,
    table_count: 5,
    view_count: 2,
    catalog_version: 1,
  });

  assert.deepEqual(
    catalog.tables.map((table) => `${table.owner}.${table.table_name}`),
    ["APP.ORDERS", "APP.TD_NL2SQL_ORDERS", "NL2SQL_APP.ORDERS"]
  );
  assert.deepEqual(catalog.view_dependencies?.map((item) => item.referenced_name), ["ORDERS"]);
  assert.deepEqual(
    page.items.map((item) => `${item.owner}.${item.object_name}`),
    ["APP.ORDERS", "APP.TD_NL2SQL_ORDERS", "NL2SQL_APP.ORDERS"]
  );
  assert.equal(page.total, 3);
  assert.equal(page.table_count, 3);
  assert.equal(page.view_count, 0);
});

test("DB admin pages remove dollar and hash objects defensively", () => {
  const page = filterUserVisibleDbAdminObjectPage({
    runtime: "oracle",
    owner: "APP",
    items: [
      dbAdminObject("ORDERS"),
      dbAdminObject("TD_NL2SQL_ORDERS"),
      dbAdminObject("ORDERS", "table", "NL2SQL_APP"),
      dbAdminObject("NL2SQL_SCHEMA_OBJECTS"),
      dbAdminObject("DBTOOLS$EXECUTION_HISTORY"),
      dbAdminObject("SYS#AUDIT", "view"),
      dbAdminObject("RC_ARCHIVED_LOG", "view", "RMAN$CATALOG"),
    ],
    total: 7,
    table_count: 5,
    view_count: 2,
    next_cursor: null,
    refreshed_at: "2026-07-22T00:00:00.000Z",
    catalog_version: 1,
    warnings: [],
  });

  assert.deepEqual(
    page.items.map((item) => `${item.owner}.${item.name}`),
    ["APP.ORDERS", "APP.TD_NL2SQL_ORDERS", "NL2SQL_APP.ORDERS"]
  );
  assert.equal(page.total, 3);
  assert.equal(page.table_count, 3);
  assert.equal(page.view_count, 0);
});

test("DB admin object identity preserves quoted and non-ASCII identifiers", () => {
  assert.equal(parseDbAdminObjectTarget("app.orders").qualifiedName, "APP.ORDERS");
  assert.equal(formatDbObjectName({ name: "lower", owner: "APP" }), 'APP."lower"');
  assert.equal(
    formatDbObjectName({ name: "ignored", qualified_name: 'APP."売上"' }),
    'APP."売上"'
  );
  assert.deepEqual(parseDbAdminObjectTarget('APP."lower"'), {
    owner: "APP",
    name: '"lower"',
    qualifiedName: 'APP."lower"',
  });
  assert.deepEqual(parseDbAdminObjectTarget('"Mixed Owner"."売上.2026"'), {
    owner: '"Mixed Owner"',
    name: '"売上.2026"',
    qualifiedName: '"Mixed Owner"."売上.2026"',
  });
  assert.throws(() => parseDbAdminObjectTarget('APP."BROKEN'), /Oracle 識別子が不正です。/u);
});
