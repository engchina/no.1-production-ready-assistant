import type { OntologyGraph, OntologyNodeKind } from "./types";

export const conceptKinds = ["object_type", "property", "link_type", "interface", "function", "action_type", "shared_property", "value_type", "enumeration", "metric", "business_rule", "business_event", "object_set"] as const;
export type ConceptKind = typeof conceptKinds[number];
export function nodeConceptKind(kind: OntologyNodeKind): string {return kind === "business_entity" ? "object_type" : kind === "enum_value" ? "enumeration" : kind;}
export function conceptGraph(graph: OntologyGraph, kind: string): OntologyGraph {
  if (!kind) return graph;
  const edges = graph.edges.filter(e => kind === "link_type" && (e.kind === "link_type" || e.kind === "business_relationship"));
  const ids = new Set(graph.nodes.filter(n=>nodeConceptKind(n.kind) === kind).map(n=>n.id));
  for (const edge of edges) {ids.add(edge.source_node_id);ids.add(edge.target_node_id);}
  return {...graph,nodes:graph.nodes.filter(n=>ids.has(n.id)),edges:kind === "link_type" ? edges : graph.edges.filter(e=>ids.has(e.source_node_id)&&ids.has(e.target_node_id))};
}
