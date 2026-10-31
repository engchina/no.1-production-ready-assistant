import assert from "node:assert/strict";
import test from "node:test";

import {
  answerOntologyQuestion,
  browseRelationshipRows,
  groundedRelationshipRows,
} from "../src/features/nl2sql/ontology/queryPlayground.ts";
import type { OntologyGraph } from "../src/features/nl2sql/ontology/types.ts";

const graph: OntologyGraph = {
  nodes: [
    {
      id: "customer",
      kind: "business_entity",
      business_name_ja: "顧客",
      description_ja: "商品を購入する主体。",
      aliases: ["得意先", "カスタマー"],
      technical_name: "APP.CUSTOMERS",
    },
    {
      id: "order",
      kind: "business_entity",
      business_name_ja: "注文",
      aliases: ["受注"],
      technical_name: "APP.ORDERS",
    },
    {
      id: "order_item",
      kind: "business_entity",
      business_name_ja: "注文明細",
      technical_name: "APP.ORDER_ITEMS",
    },
    {
      id: "sales_total",
      kind: "metric",
      business_name_ja: "売上合計",
      description_ja: "注文金額の合計。",
    },
  ],
  edges: [
    {
      id: "edge_customer_order",
      kind: "business_relationship",
      source_node_id: "customer",
      target_node_id: "order",
      relationship_name_ja: "注文する",
      cardinality: "one_to_many",
    },
    {
      id: "edge_order_item",
      kind: "business_relationship",
      source_node_id: "order",
      target_node_id: "order_item",
      relationship_name_ja: "明細を含む",
      cardinality: "one_to_many",
    },
    {
      id: "edge_order_metric",
      kind: "maps_to",
      source_node_id: "sales_total",
      target_node_id: "order",
      relationship_name_ja: "集計対象",
    },
  ],
};

test("定義質問はエンティティ 1 件をハイライトする", () => {
  const result = answerOntologyQuestion(graph, "顧客とは何ですか?");
  assert.equal(result.stage, "entity_definition");
  assert.deepEqual(result.highlightNodeIds, ["customer"]);
  assert.ok(result.explanationJa.includes("商品を購入する主体"));
});

test("日本語 alias(得意先)でも一致する", () => {
  const result = answerOntologyQuestion(graph, "得意先について教えて");
  assert.deepEqual(result.highlightNodeIds, ["customer"]);
});

test("同じ物理名に業務概念と物理表がある場合は業務概念を優先する", () => {
  const result = answerOntologyQuestion(
    {
      nodes: [
        {
          id: "employee-business",
          kind: "business_entity",
          business_name_ja: "従業員",
          technical_name: "ADMIN.EMPLOYEE",
          aliases: ["社員"],
        },
        {
          id: "employee-table",
          kind: "table",
          business_name_ja: "従業員情報",
          technical_name: "ADMIN.EMPLOYEE",
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
      ],
    },
    "ADMIN.EMPLOYEE とは?"
  );

  assert.equal(result.stage, "entity_definition");
  assert.deepEqual(result.highlightNodeIds, ["employee-business"]);
});

test("業務概念がない場合は物理表・ビューに fallback する", () => {
  const result = answerOntologyQuestion(
    {
      nodes: [
        {
          id: "project-table",
          kind: "table",
          business_name_ja: "PROJECT",
          technical_name: "ADMIN.PROJECT",
        },
      ],
      edges: [],
    },
    "ADMIN.PROJECT とは?"
  );

  assert.equal(result.stage, "entity_definition");
  assert.deepEqual(result.highlightNodeIds, ["project-table"]);
});

test("一覧質問は list_all になる", () => {
  const result = answerOntologyQuestion(graph, "注文の一覧を見せて");
  assert.equal(result.stage, "list_all");
  assert.deepEqual(result.highlightNodeIds, ["order"]);
});

test("2 エンティティは直接辺をハイライトする", () => {
  const result = answerOntologyQuestion(graph, "顧客と注文の関係は?");
  assert.equal(result.stage, "relationship");
  assert.ok(result.highlightNodeIds.includes("customer"));
  assert.ok(result.highlightNodeIds.includes("order"));
  assert.deepEqual(result.highlightEdgeIds, ["edge_customer_order"]);
  assert.ok(result.explanationJa.includes("注文する"));
});

test("直接辺がない 2 エンティティは 1-hop 経由で結ぶ", () => {
  const result = answerOntologyQuestion(graph, "顧客と注文明細のつながりは?");
  assert.equal(result.stage, "relationship");
  assert.deepEqual([...result.highlightNodeIds].sort(), ["customer", "order", "order_item"]);
  assert.deepEqual(
    [...result.highlightEdgeIds].sort(),
    ["edge_customer_order", "edge_order_item"]
  );
});

test("最長一致で「注文明細」を「注文」より優先する", () => {
  const result = answerOntologyQuestion(graph, "注文明細とは?");
  assert.deepEqual(result.highlightNodeIds, ["order_item"]);
});

test("属性質問は指標ノードと接続辺をハイライトする", () => {
  const result = answerOntologyQuestion(graph, "注文の売上合計は?");
  assert.equal(result.stage, "property");
  assert.deepEqual(result.highlightNodeIds, ["order", "sales_total"]);
  assert.deepEqual(result.highlightEdgeIds, ["edge_order_metric"]);
});

test("一致しない質問は no_match と候補を返す", () => {
  const result = answerOntologyQuestion(graph, "天気はどうですか?");
  assert.equal(result.stage, "no_match");
  assert.deepEqual(result.highlightNodeIds, []);
  assert.ok(result.suggestionsJa.includes("顧客"));
});

test("空質問は no_match", () => {
  assert.equal(answerOntologyQuestion(graph, "  ").stage, "no_match");
});

test("no_match の接地パス行は空(無関係な関係を混ぜない)", () => {
  const result = answerOntologyQuestion(graph, "天気はどうですか?");
  const rows = groundedRelationshipRows(graph, result);
  assert.deepEqual(rows, []);
});

test("接地一致時の接地パス行はハイライト辺のみ", () => {
  const result = answerOntologyQuestion(graph, "顧客と注文の関係は?");
  const rows = groundedRelationshipRows(graph, result);
  assert.deepEqual(
    rows.map((row) => row.edge_id),
    ["edge_customer_order"]
  );
});

test("関係一覧は接地なしなら全件へフォールバックする", () => {
  const grounded = groundedRelationshipRows(graph, answerOntologyQuestion(graph, "天気は?"));
  const rows = browseRelationshipRows(graph, grounded);
  assert.ok(rows.length >= 3);
});

// --- 実データ相当(物理ノードのみの ER ミラー)での接地回帰 -----------------------------
const physicalGraph: OntologyGraph = {
  nodes: [
    {
      id: "t-employee",
      kind: "table",
      business_name_ja: "従業員情報",
      technical_name: "ADMIN.EMPLOYEE",
      aliases: ["EMPLOYEE"],
    },
    {
      id: "t-department",
      kind: "table",
      business_name_ja: "部署情報",
      technical_name: "ADMIN.DEPARTMENT",
    },
    {
      id: "c-salary",
      kind: "column",
      business_name_ja: "給与",
      technical_name: "ADMIN.EMPLOYEE.SALARY",
    },
    {
      id: "c-dept-id",
      kind: "column",
      business_name_ja: "所属部署ID(外部キー)",
      technical_name: "ADMIN.EMPLOYEE.DEPARTMENT_ID",
    },
  ],
  edges: [
    {
      id: "e-emp-salary",
      kind: "contains",
      source_node_id: "t-employee",
      target_node_id: "c-salary",
      relationship_name_ja: "含む",
    },
    {
      id: "e-emp-dept-id",
      kind: "contains",
      source_node_id: "t-employee",
      target_node_id: "c-dept-id",
      relationship_name_ja: "含む",
    },
    {
      id: "e-emp-dept",
      kind: "foreign_key",
      source_node_id: "t-employee",
      target_node_id: "t-department",
      relationship_name_ja: "従業員情報 → 部署情報",
      cardinality: "many_to_one",
      join_conditions: [
        { source_column: "DEPARTMENT_ID", target_column: "DEPARTMENT_ID", operator: "=" },
      ],
    },
  ],
};

test("「部署ごとの平均給与を教えて」が部署情報・給与・従業員情報へ接地する", () => {
  const result = answerOntologyQuestion(physicalGraph, "部署ごとの平均給与を教えて");
  assert.equal(result.stage, "aggregate");
  assert.ok(result.highlightNodeIds.includes("t-department"));
  assert.ok(result.highlightNodeIds.includes("c-salary"));
  assert.ok(result.highlightNodeIds.includes("t-employee"));
  assert.ok(result.highlightEdgeIds.includes("e-emp-dept"));
  assert.ok(result.highlightEdgeIds.includes("e-emp-salary"));
});

test("「給与とは」が属性単独で親エンティティへ接地する", () => {
  const result = answerOntologyQuestion(physicalGraph, "給与とは");
  assert.equal(result.stage, "property");
  assert.deepEqual([...result.highlightNodeIds].sort(), ["c-salary", "t-employee"]);
  assert.deepEqual(result.highlightEdgeIds, ["e-emp-salary"]);
});

test("「従業員情報と部署情報の関係は?」は relationship のまま", () => {
  const result = answerOntologyQuestion(physicalGraph, "従業員情報と部署情報の関係は?");
  assert.equal(result.stage, "relationship");
  assert.ok(result.highlightEdgeIds.includes("e-emp-dept"));
});

test("部分一致「部署」で 部署情報(表)が列より優先される", () => {
  const result = answerOntologyQuestion(physicalGraph, "部署の一覧");
  assert.equal(result.stage, "list_all");
  assert.deepEqual(result.highlightNodeIds, ["t-department"]);
});

test("候補はスコア降順で matchedText を持つ(下線表示用)", () => {
  const result = answerOntologyQuestion(physicalGraph, "部署ごとの平均給与を教えて");
  assert.ok(result.candidates.length >= 2);
  const scores = result.candidates.map((candidate) => candidate.score);
  assert.deepEqual(scores, [...scores].sort((a, b) => b - a));
  const salary = result.candidates.find((candidate) => candidate.node.id === "c-salary");
  assert.equal(salary?.matchedText, "給与");
  assert.ok((salary?.span?.start ?? -1) >= 0);
});
