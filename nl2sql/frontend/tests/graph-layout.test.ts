import assert from "node:assert/strict";
import test from "node:test";

import {
  layoutOntologyGraphSemanticMatrix,
  ontologyGraphSemanticLaneForKind,
} from "../src/features/nl2sql/ontology/graphLayout.ts";
import type { OntologyGraph } from "../src/features/nl2sql/ontology/types.ts";

test("semantic matrix レイアウトは種別を固定レーンへ配置する", () => {
  const graph: OntologyGraph = {
    nodes: [
      { id: "entity", kind: "business_entity", business_name_ja: "従業員" },
      { id: "metric", kind: "metric", business_name_ja: "人数" },
      {
        id: "table",
        kind: "table",
        business_name_ja: "従業員表",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE" },
      },
      {
        id: "column",
        kind: "column",
        business_name_ja: "従業員ID",
        technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_ID",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "EMPLOYEE_ID" },
      },
    ],
    edges: [
      {
        id: "maps",
        source_node_id: "entity",
        target_node_id: "table",
        relationship_name_ja: "物理マッピング",
      },
    ],
  };

  const layout = layoutOntologyGraphSemanticMatrix(graph);
  const lanes = new Map(layout.lanes.map((lane) => [lane.id, lane]));

  assert.equal(layout.laneByNodeId.get("entity"), "business");
  assert.equal(layout.laneByNodeId.get("metric"), "attribute");
  assert.equal(layout.laneByNodeId.get("table"), "physical");
  assert.equal(layout.laneByNodeId.get("column"), "detail");
  assert.ok((lanes.get("business")?.y ?? 0) < (lanes.get("attribute")?.y ?? 0));
  assert.ok((lanes.get("attribute")?.y ?? 0) < (lanes.get("physical")?.y ?? 0));
  assert.ok((lanes.get("physical")?.y ?? 0) < (lanes.get("detail")?.y ?? 0));
  assert.equal(ontologyGraphSemanticLaneForKind("view"), "physical");
});

test("semantic matrix は同じ物理 object の業務概念・表・列を同一クラスタに揃える", () => {
  const graph: OntologyGraph = {
    nodes: [
      {
        id: "employee-business",
        kind: "business_entity",
        business_name_ja: "従業員",
        physical_mappings: [
          {
            object_ref: {
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
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", object_type: "TABLE" },
      },
      {
        id: "department-table",
        kind: "table",
        business_name_ja: "部署情報",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT", object_type: "TABLE" },
      },
      {
        id: "employee-id",
        kind: "column",
        business_name_ja: "従業員ID",
        technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_ID",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "EMPLOYEE_ID", ordinal: 1 },
      },
      {
        id: "employee-name",
        kind: "column",
        business_name_ja: "従業員氏名",
        technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "EMPLOYEE_NAME", ordinal: 2 },
      },
    ],
    edges: [],
  };

  const first = layoutOntologyGraphSemanticMatrix(graph);
  const second = layoutOntologyGraphSemanticMatrix(graph);
  assert.deepEqual([...first.positions.entries()], [...second.positions.entries()]);
  assert.equal(
    first.clusterByNodeId.get("employee-business"),
    first.clusterByNodeId.get("employee-table")
  );
  assert.equal(first.clusterByNodeId.get("employee-id"), first.clusterByNodeId.get("employee-table"));
  assert.equal(first.clusterByNodeId.get("employee-name"), first.clusterByNodeId.get("employee-table"));
  assert.notEqual(
    first.clusterByNodeId.get("department-table"),
    first.clusterByNodeId.get("employee-table")
  );
  assert.equal(first.positions.get("employee-id")?.x, first.positions.get("employee-table")?.x);
  assert.equal(first.positions.get("employee-name")?.x, first.positions.get("employee-table")?.x);
  assert.ok((first.positions.get("employee-name")?.y ?? 0) > (first.positions.get("employee-id")?.y ?? 0));
});

test("semantic matrix は schema を physical レーンの専用上段行に置き、表との間に余白帯を確保する", () => {
  const tables = ["DEPARTMENT", "EMPLOYEE", "PROJECT"].map((name) => ({
    id: `table-${name.toLowerCase()}`,
    kind: "table" as const,
    business_name_ja: `${name}表`,
    metadata: { owner: "ADMIN", object_name: name, object_type: "TABLE" },
  }));
  const graph: OntologyGraph = {
    nodes: [
      {
        id: "schema-admin",
        kind: "schema",
        business_name_ja: "ADMIN スキーマ",
        technical_name: "ADMIN",
        metadata: { owner: "ADMIN" },
      },
      ...tables,
    ],
    edges: tables.map((table) => ({
      id: `contains-${table.id}`,
      kind: "contains",
      source_node_id: "schema-admin",
      target_node_id: table.id,
      relationship_name_ja: "含む",
    })),
  };

  const nodeHeight = 64;
  const schemaRowGap = 40;
  const first = layoutOntologyGraphSemanticMatrix(graph);
  const second = layoutOntologyGraphSemanticMatrix(graph);
  // schema 入りでも決定論(同一入力 → 同一座標)
  assert.deepEqual([...first.positions.entries()], [...second.positions.entries()]);

  const schemaY = first.positions.get("schema-admin")?.y ?? Number.NaN;
  for (const table of tables) {
    const tableY = first.positions.get(table.id)?.y ?? Number.NaN;
    // schema は常に上段行、表は余白帯(schemaRowGap)を挟んだ下段行
    assert.ok(schemaY < tableY, `${table.id} は schema より下に配置される`);
    assert.ok(
      tableY - schemaY >= nodeHeight + schemaRowGap,
      `${table.id} と schema の間にエッジ専用の余白帯がある`
    );
  }

  // schema 行の分だけ physical レーンの高さが増える
  const withoutSchema = layoutOntologyGraphSemanticMatrix({ nodes: tables, edges: [] });
  const physicalWith = first.lanes.find((lane) => lane.id === "physical");
  const physicalWithout = withoutSchema.lanes.find((lane) => lane.id === "physical");
  assert.ok(
    (physicalWith?.height ?? 0) >= (physicalWithout?.height ?? 0) + nodeHeight + schemaRowGap
  );
});

test("semantic matrix はバリセンタ 1 パスで隣接クラスタを近づける(決定論維持)", () => {
  const makeTable = (name: string) => ({
    id: `table-${name.toLowerCase()}`,
    kind: "table" as const,
    business_name_ja: `${name}表`,
    metadata: { owner: "ADMIN", object_name: name, object_type: "TABLE" },
  });
  const graph: OntologyGraph = {
    nodes: [makeTable("ALPHA"), makeTable("BRAVO"), makeTable("CHARLIE"), makeTable("DELTA")],
    edges: [
      {
        id: "fk-delta-alpha",
        kind: "foreign_key",
        source_node_id: "table-delta",
        target_node_id: "table-alpha",
        relationship_name_ja: "参照",
      },
    ],
  };
  const layout = layoutOntologyGraphSemanticMatrix(graph);
  // ALPHA と結ばれる DELTA は、無関係な CHARLIE より左(ALPHA 側)へ寄る
  assert.ok(
    (layout.positions.get("table-delta")?.x ?? 0) < (layout.positions.get("table-charlie")?.x ?? 0)
  );
  // エッジのないクラスタ同士(BRAVO / CHARLIE)は初出順の相対関係を維持する
  assert.ok(
    (layout.positions.get("table-bravo")?.x ?? 0) < (layout.positions.get("table-charlie")?.x ?? 0)
  );
});

test("semantic matrix はノードのないレーンを生成しない(空レーンラベル抑止)", () => {
  const graph: OntologyGraph = {
    nodes: [
      {
        id: "only-table",
        kind: "table",
        business_name_ja: "従業員情報",
        technical_name: "ADMIN.EMPLOYEE",
      },
    ],
    edges: [],
  };
  const layout = layoutOntologyGraphSemanticMatrix(graph);
  assert.deepEqual(
    layout.lanes.map((lane) => lane.id),
    ["physical"]
  );
  assert.ok(layout.lanes.every((lane) => lane.nodeCount > 0));
});
