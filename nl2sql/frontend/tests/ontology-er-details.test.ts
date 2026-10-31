import assert from "node:assert/strict";
import test from "node:test";

import { deriveOntologyErDetails } from "../src/features/nl2sql/ontology/erDetails.ts";
import type { OntologyGraph } from "../src/features/nl2sql/ontology/types.ts";

const graph: OntologyGraph = {
  nodes: [
    {
      id: "employee-business",
      kind: "business_entity",
      business_name_ja: "従業員",
      technical_name: "ADMIN.EMPLOYEE",
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
    {
      id: "employee-table",
      kind: "table",
      business_name_ja: "従業員情報",
      technical_name: "ADMIN.EMPLOYEE",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        object_type: "TABLE",
      },
    },
    {
      id: "department-table",
      kind: "table",
      business_name_ja: "部署情報",
      technical_name: "ADMIN.DEPARTMENT",
      metadata: {
        owner: "ADMIN",
        object_name: "DEPARTMENT",
        object_type: "TABLE",
      },
    },
    {
      id: "employee-name",
      kind: "column",
      business_name_ja: "従業員氏名",
      technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
      description_ja: "従業員の氏名。",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "EMPLOYEE_NAME",
        data_type: "VARCHAR2",
        ordinal: 3,
      },
    },
    {
      id: "employee-id",
      kind: "column",
      business_name_ja: "従業員ID",
      technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_ID",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "EMPLOYEE_ID",
        data_type: "NUMBER",
        ordinal: 1,
        primary_key: true,
      },
    },
    {
      id: "employee-department-id",
      kind: "column",
      business_name_ja: "所属部署ID",
      technical_name: "ADMIN.EMPLOYEE.DEPARTMENT_ID",
      metadata: {
        owner: "ADMIN",
        object_name: "EMPLOYEE",
        column_name: "DEPARTMENT_ID",
        data_type: "NUMBER",
        ordinal: 2,
      },
    },
    {
      id: "department-id",
      kind: "column",
      business_name_ja: "部署ID",
      technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_ID",
      metadata: {
        owner: "ADMIN",
        object_name: "DEPARTMENT",
        column_name: "DEPARTMENT_ID",
        data_type: "NUMBER",
        ordinal: 1,
        primary_key: true,
      },
    },
  ],
  edges: [
    {
      id: "employee-maps-to",
      kind: "maps_to",
      source_node_id: "employee-business",
      target_node_id: "employee-table",
      relationship_name_ja: "物理マッピング",
    },
    {
      id: "employee-department-fk",
      kind: "foreign_key",
      source_node_id: "employee-table",
      target_node_id: "department-table",
      relationship_name_ja: "所属部署を参照",
      cardinality: "many_to_one",
      join_conditions: [
        {
          left: {
            owner: "ADMIN",
            object_name: "EMPLOYEE",
            column_name: "DEPARTMENT_ID",
          },
          right: {
            owner: "ADMIN",
            object_name: "DEPARTMENT",
            column_name: "DEPARTMENT_ID",
          },
          operator: "=",
          ordinal: 1,
        },
      ],
    },
  ],
};

test("mapped business entity derives ER columns, key roles, and joins", () => {
  const details = deriveOntologyErDetails(graph, "employee-business");
  assert.ok(details);
  assert.equal(details.objectName, "ADMIN.EMPLOYEE");
  assert.equal(details.objectNodeId, "employee-table");
  assert.deepEqual(
    details.columns.map((column) => column.columnName),
    ["EMPLOYEE_ID", "DEPARTMENT_ID", "EMPLOYEE_NAME"]
  );
  assert.deepEqual(
    details.columns.map((column) => column.keyRole),
    ["pk", "fk", "none"]
  );
  assert.equal(details.columns[2].dataType, "VARCHAR2");
  assert.equal(details.columns[2].descriptionJa, "従業員の氏名。");
  assert.equal(details.joins.length, 1);
  assert.equal(details.joins[0].relationshipNameJa, "所属部署を参照");
  assert.equal(
    details.joins[0].joinCondition,
    "ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"
  );
});

test("physical table selection derives the same ER detail target", () => {
  const details = deriveOntologyErDetails(graph, "employee-table");
  assert.ok(details);
  assert.equal(details.objectName, "ADMIN.EMPLOYEE");
  assert.equal(details.columns[1].keyRole, "fk");
});

test("unmapped node returns no ER details", () => {
  assert.equal(
    deriveOntologyErDetails(
      {
        nodes: [
          {
            id: "metric",
            kind: "metric",
            business_name_ja: "売上合計",
            technical_name: "SUM(AMOUNT)",
          },
        ],
        edges: [],
      },
      "metric"
    ),
    null
  );
});
