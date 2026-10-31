import assert from "node:assert/strict";
import test from "node:test";
import { conceptGraph, conceptKinds } from "../src/features/nl2sql/ontology/conceptModel.ts";
import { ontologyGraphForViewMode } from "../src/features/nl2sql/ontology/graphView.ts";
import { matchQuestionToNodes } from "../src/features/nl2sql/ontology/groundingMatcher.ts";
import { layoutOntologyGraphSemanticMatrix } from "../src/features/nl2sql/ontology/graphLayout.ts";
import type { OntologyGraph } from "../src/features/nl2sql/ontology/types.ts";

const graph: OntologyGraph = {
  nodes: conceptKinds.filter(kind => kind !== "link_type").map(kind => ({
    id: kind, kind, business_name_ja: `${kind} の定義`, technical_name: `API.${kind}`,
    aliases: [`別名_${kind}`], metadata: { definition: { api_name: `API.${kind}` } },
  })),
  edges: [
    { id: "link", kind: "link_type", source_node_id: "object_type", target_node_id: "object_type", relationship_name_ja: "関連定義" },
    ...conceptKinds.filter(kind => !["link_type", "object_type"].includes(kind)).map(kind => ({
      id: `ref-${kind}`, kind: "uses" as const, source_node_id: kind, target_node_id: "object_type", relationship_name_ja: "対象",
    })),
  ],
};

test("all thirteen filters keep canonical identity; link type remains a selectable edge", () => {
  assert.equal(conceptKinds.length, 13);
  for (const kind of conceptKinds) {
    const filtered = conceptGraph(graph, kind);
    if (kind === "link_type") {
      assert.deepEqual(filtered.edges.map(edge => edge.id), ["link"]);
      assert.equal(filtered.nodes[0].id, "object_type");
    } else assert.deepEqual(filtered.nodes.map(node => node.id), [kind]);
  }
  assert.equal(conceptGraph(graph, ""), graph);
});

test("every node concept can be grounded by API name and alias", () => {
  for (const node of graph.nodes) {
    for (const question of [node.technical_name!, node.aliases![0]]) {
      assert.ok(matchQuestionToNodes(graph, question).candidates.some(c => c.node.id === node.id), question);
    }
  }
});

test("reference concepts stay near their object in a stable non-overlapping layout", () => {
  const layout = layoutOntologyGraphSemanticMatrix(graph);
  const reversed = layoutOntologyGraphSemanticMatrix({ nodes: [...graph.nodes].reverse(), edges: [...graph.edges].reverse() });
  assert.equal(new Set(layout.clusterByNodeId.values()).size, 1);
  assert.equal(new Set([...layout.positions.values()].map(p => `${p.x}:${p.y}`)).size, graph.nodes.length);
  for (const node of graph.nodes) assert.deepEqual(layout.positions.get(node.id), reversed.positions.get(node.id));
});

test("enumeration filter retains derived members and membership edges", () => {
  const expanded: OntologyGraph = {
    nodes: [...graph.nodes, { id: "member", kind: "enum_value", business_name_ja: "有効", metadata: { derived_from_definition_id: "enumeration" } }],
    edges: [...graph.edges, { id: "membership", kind: "has_value", source_node_id: "enumeration", target_node_id: "member", relationship_name_ja: "列挙メンバー" }],
  };
  assert.ok(!ontologyGraphForViewMode(expanded, "physical_er", undefined, undefined).nodes.some(n => n.id === "member"));
  const filtered = conceptGraph(expanded, "enumeration");
  assert.deepEqual(filtered.nodes.map(n => n.id), ["enumeration", "member"]);
  assert.deepEqual(filtered.edges.map(e => e.id), ["membership"]);
  const layout = layoutOntologyGraphSemanticMatrix(expanded);
  assert.equal(layout.clusterByNodeId.get("member"), layout.clusterByNodeId.get("enumeration"));
});
