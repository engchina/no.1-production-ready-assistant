import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import type { OntologyGraph } from "../src/features/nl2sql/ontology/types.ts";

const require = createRequire(import.meta.url);
const {
  applyOntologyNodeDimensionChanges,
  applyOntologyNodePositionChanges,
  cardinalityShortLabel,
  isOntologyContainmentEdge,
  isOntologyJoinEdge,
  isOntologyMappingEdge,
  ontologyGraphForViewMode,
  ontologyGraphWithDetailVisibility,
  ontologyParallelEdgeGeometry,
  selectOntologyEdgeHandles,
} = require("../src/features/nl2sql/ontology/graphView.ts") as typeof import("../src/features/nl2sql/ontology/graphView.ts");

const graph: OntologyGraph = {
  nodes: [
    { id: "customer", kind: "business_entity", business_name_ja: "顧客" },
    { id: "orders", kind: "business_entity", business_name_ja: "注文" },
    {
      id: "orders-table",
      kind: "table",
      business_name_ja: "注文表",
      metadata: { owner: "APP", object_name: "ORDERS" },
    },
    {
      id: "orders-amount",
      kind: "column",
      business_name_ja: "注文金額",
      technical_name: "APP.ORDERS.AMOUNT",
      metadata: { owner: "APP", object_name: "ORDERS", column_name: "AMOUNT" },
    },
    { id: "outside", kind: "business_entity", business_name_ja: "社外秘" },
  ],
  edges: [
    {
      id: "customer-orders",
      source_node_id: "customer",
      target_node_id: "orders",
      relationship_name_ja: "注文する",
    },
    {
      id: "orders-maps",
      kind: "maps_to",
      source_node_id: "orders",
      target_node_id: "orders-table",
      relationship_name_ja: "物理マッピング",
    },
    {
      id: "orders-column",
      source_node_id: "orders-table",
      target_node_id: "orders-amount",
      relationship_name_ja: "列",
    },
    {
      id: "outside-edge",
      source_node_id: "outside",
      target_node_id: "customer",
      relationship_name_ja: "参照",
    },
  ],
};

test("grounding view keeps highlighted nodes, highlighted edges, and one-hop context", () => {
  const focused = ontologyGraphForViewMode(graph, "grounding", ["orders"], ["customer-orders"]);

  assert.deepEqual(
    focused.nodes.map((node) => node.id).sort(),
    ["customer", "orders", "orders-table"].sort()
  );
  assert.deepEqual(
    focused.edges.map((edge) => edge.id).sort(),
    ["customer-orders", "orders-maps"].sort()
  );
});

test("detail visibility hides columns unless explicitly expanded or forced", () => {
  const collapsed = ontologyGraphWithDetailVisibility(graph, false, new Set());
  assert.equal(collapsed.nodes.some((node) => node.id === "orders-amount"), false);

  const forced = ontologyGraphWithDetailVisibility(graph, false, new Set(["orders-amount"]));
  assert.equal(forced.nodes.some((node) => node.id === "orders-amount"), true);

  const expanded = ontologyGraphWithDetailVisibility(graph, true, new Set());
  assert.equal(expanded.nodes.some((node) => node.id === "orders-amount"), true);
});

test("physical ER view excludes business-only nodes", () => {
  const physical = ontologyGraphForViewMode(graph, "physical_er", [], []);
  assert.deepEqual(
    physical.nodes.map((node) => node.id).sort(),
    ["orders-amount", "orders-table"].sort()
  );
});

test("cardinalityShortLabel は ER 図流の短縮表記を返す(unknown は空)", () => {
  assert.equal(cardinalityShortLabel("one_to_one"), "1:1");
  assert.equal(cardinalityShortLabel("one_to_many"), "1:N");
  assert.equal(cardinalityShortLabel("many_to_one"), "N:1");
  assert.equal(cardinalityShortLabel("many_to_many"), "N:N");
  assert.equal(cardinalityShortLabel("unknown"), "");
  assert.equal(cardinalityShortLabel(undefined), "");
});

test("selectOntologyEdgeHandles は縦優勢で上下・横優勢で左右ハンドルを選ぶ", () => {
  // 業務概念(上)→ 物理表(下): 縦優勢 → bottom→top + vertical
  assert.deepEqual(selectOntologyEdgeHandles({ x: 100, y: 0 }, { x: 120, y: 300 }), {
    sourceHandle: "s-bottom",
    targetHandle: "t-top",
    orientation: "vertical",
  });
  // 下→上
  assert.deepEqual(selectOntologyEdgeHandles({ x: 0, y: 300 }, { x: 0, y: 0 }).sourceHandle, "s-top");
  // 同一レーンの右方向
  assert.deepEqual(selectOntologyEdgeHandles({ x: 0, y: 10 }, { x: 400, y: 0 }), {
    sourceHandle: "s-right",
    targetHandle: "t-left",
    orientation: "horizontal",
  });
  // 同一レーンの左方向(従来はここで自己ループ状になっていた)
  assert.equal(selectOntologyEdgeHandles({ x: 400, y: 0 }, { x: 0, y: 10 }).sourceHandle, "s-left");
});

test("selectOntologyEdgeHandles は preferVertical 指定で dx 優勢でも縦を選ぶ(dy=0 は従来判定)", () => {
  // schema→表: dx が大きくても dy があれば bottom→top(貫通 bezier を出さない)
  assert.deepEqual(
    selectOntologyEdgeHandles({ x: 0, y: 0 }, { x: 600, y: 116 }, { preferVertical: true }),
    { sourceHandle: "s-bottom", targetHandle: "t-top", orientation: "vertical" }
  );
  // dy=0 のときは縦にできないので従来どおり左右ハンドル
  assert.equal(
    selectOntologyEdgeHandles({ x: 0, y: 0 }, { x: 600, y: 0 }, { preferVertical: true }).orientation,
    "horizontal"
  );
});

test("isOntologyContainmentEdge は contains / physical_contains を包含エッジと判定する", () => {
  const base = { id: "e", source_node_id: "a", target_node_id: "b", relationship_name_ja: "含む" };
  assert.equal(isOntologyContainmentEdge({ ...base, kind: "contains" }), true);
  assert.equal(isOntologyContainmentEdge({ ...base, kind: "physical_contains" }), true);
  assert.equal(isOntologyContainmentEdge({ ...base, kind: "maps_to" }), false);
  assert.equal(isOntologyContainmentEdge({ ...base }), false);
});

test("applyOntologyNodePositionChanges は position change だけを反映する", () => {
  const initial = new Map([["kept", { x: 1, y: 2 }]]);
  const applied = applyOntologyNodePositionChanges(initial, [
    { type: "position", id: "moved", position: { x: 10, y: 20 } },
    { type: "select", id: "ignored" },
    { type: "dimensions", id: "ignored-too" },
    { type: "position", id: "bad", position: { x: Number.NaN, y: 0 } },
  ]);
  assert.notEqual(applied, initial);
  assert.deepEqual(applied.get("moved"), { x: 10, y: 20 });
  assert.deepEqual(applied.get("kept"), { x: 1, y: 2 });
  assert.equal(applied.has("bad"), false);
  // 反映対象がなければ同一 Map 参照を返す(無駄な再レンダを避ける)
  const untouched = applyOntologyNodePositionChanges(initial, [{ type: "select", id: "x" }]);
  assert.equal(untouched, initial);
});

test("applyOntologyNodeDimensionChanges は実測サイズを蓄積し、同値なら同一 Map 参照を返す", () => {
  const initial = new Map([["a", { width: 220, height: 76 }]]);
  const applied = applyOntologyNodeDimensionChanges(initial, [
    { type: "dimensions", id: "b", dimensions: { width: 220, height: 92 } },
    { type: "position", id: "ignored" },
    { type: "dimensions", id: "bad", dimensions: { width: 0, height: -1 } },
  ]);
  assert.notEqual(applied, initial);
  assert.deepEqual(applied.get("b"), { width: 220, height: 92 });
  assert.equal(applied.has("bad"), false);
  // 同じ実測値の再通知では参照を変えない(measure → 反映 → 再 measure のループを断つ)
  const unchanged = applyOntologyNodeDimensionChanges(applied, [
    { type: "dimensions", id: "b", dimensions: { width: 220, height: 92 } },
  ]);
  assert.equal(unchanged, applied);
});

test("ontologyParallelEdgeGeometry は法線方向の弧とラベル座標を返す", () => {
  const straight = ontologyParallelEdgeGeometry({ x: 0, y: 0 }, { x: 100, y: 0 }, 0);
  assert.equal(straight.labelX, 50);
  assert.equal(straight.labelY, 0);
  const arced = ontologyParallelEdgeGeometry({ x: 0, y: 0 }, { x: 100, y: 0 }, 20);
  // 2 次ベジェの t=0.5 点は制御点オフセット(offset*2)の半分 = offset だけ離れる
  assert.equal(arced.labelX, 50);
  assert.equal(arced.labelY, 20);
  assert.ok(arced.path.startsWith("M 0,0 Q 50,40 100,0"));
});

test("mapping/join エッジ判定は kind と join_conditions で決まる", () => {
  assert.equal(isOntologyMappingEdge({ id: "e", source_node_id: "a", target_node_id: "b", relationship_name_ja: "対応", kind: "maps_to" }), true);
  assert.equal(isOntologyMappingEdge({ id: "e", source_node_id: "a", target_node_id: "b", relationship_name_ja: "参照", kind: "foreign_key" }), false);
  assert.equal(isOntologyJoinEdge({ id: "e", source_node_id: "a", target_node_id: "b", relationship_name_ja: "参照", kind: "foreign_key" }), true);
  assert.equal(
    isOntologyJoinEdge({
      id: "e",
      source_node_id: "a",
      target_node_id: "b",
      relationship_name_ja: "参照",
      join_conditions: [{ source_column: "X", target_column: "Y", operator: "=" }],
    }),
    true
  );
  assert.equal(isOntologyJoinEdge({ id: "e", source_node_id: "a", target_node_id: "b", relationship_name_ja: "含む", kind: "contains" }), false);
});
