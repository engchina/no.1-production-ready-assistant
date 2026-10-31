import type { OntologyGraph, OntologyJsonValue, OntologyNode } from "./types";
import {
  OBJECT_KINDS,
  columnIdentityFromNode,
  edgeEndpoint,
  jsonString,
  objectIdentityFromNode,
  objectMatches,
  objectName,
  ontologyJoinConditionText,
  type ObjectIdentity,
} from "./physicalIdentity";

export type OntologyErKeyRole = "none" | "pk" | "fk" | "pk_fk";

export interface OntologyErColumnDetail {
  id: string;
  columnName: string;
  dataType: string;
  businessNameJa: string;
  descriptionJa: string;
  keyRole: OntologyErKeyRole;
  ordinal: number | null;
}

export interface OntologyErJoinDetail {
  id: string;
  relationshipNameJa: string;
  sourceLabel: string;
  targetLabel: string;
  cardinality: string;
  joinCondition: string;
}

export interface OntologyErDetails {
  selectedNodeId: string;
  objectNodeId: string | null;
  objectName: string;
  objectType: "table" | "view" | "unknown";
  displayNameJa: string;
  columns: OntologyErColumnDetail[];
  joins: OntologyErJoinDetail[];
}

const MAPPABLE_KINDS = new Set([
  "business_entity",
  "business_event",
  "property",
  "metric",
  "business_term",
]);

function jsonNumber(value: OntologyJsonValue | undefined): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function jsonBoolean(value: OntologyJsonValue | undefined): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    return ["true", "yes", "1", "pk"].includes(value.trim().toLowerCase());
  }
  return false;
}

function nodeLabel(node: OntologyNode | undefined, fallback: string): string {
  return node?.business_name_ja || node?.technical_name || fallback;
}

function explicitPrimaryKey(node: OntologyNode): boolean {
  const metadata = node.metadata ?? {};
  const keyRole = jsonString(metadata.key_role).toLowerCase();
  return (
    jsonBoolean(metadata.primary_key) ||
    jsonBoolean(metadata.is_primary_key) ||
    jsonBoolean(metadata.pk) ||
    keyRole === "pk" ||
    keyRole.includes("primary")
  );
}

function columnRole(column: OntologyNode, fkColumnNames: Set<string>): OntologyErKeyRole {
  const identity = columnIdentityFromNode(column);
  const isPk = explicitPrimaryKey(column);
  const isFk = Boolean(identity && fkColumnNames.has(identity.columnName));
  if (isPk && isFk) return "pk_fk";
  if (isPk) return "pk";
  if (isFk) return "fk";
  return "none";
}

function objectNodeForIdentity(
  graph: OntologyGraph,
  identity: ObjectIdentity
): OntologyNode | undefined {
  return graph.nodes.find((node) => {
    if (!OBJECT_KINDS.has(node.kind)) return false;
    const nodeIdentity = objectIdentityFromNode(node);
    return Boolean(nodeIdentity && objectMatches(identity, nodeIdentity));
  });
}

function sortableOrdinal(node: OntologyNode): number | null {
  const columnRefOrdinal = node.physical_mappings?.[0]?.column_refs?.[0]?.ordinal ?? null;
  return columnRefOrdinal ?? jsonNumber(node.metadata?.ordinal);
}

export function deriveOntologyErDetails(
  graph: OntologyGraph,
  selectedNodeId: string | null | undefined
): OntologyErDetails | null {
  if (!selectedNodeId) return null;
  const selected = graph.nodes.find((node) => node.id === selectedNodeId);
  if (
    !selected ||
    (!OBJECT_KINDS.has(selected.kind) &&
      !MAPPABLE_KINDS.has(selected.kind) &&
      selected.kind !== "column")
  ) {
    return null;
  }

  const selectedObject = objectIdentityFromNode(selected);
  if (!selectedObject) return null;
  const objectNode = objectNodeForIdentity(graph, selectedObject);
  const resolvedObject = objectNode ? objectIdentityFromNode(objectNode) : selectedObject;
  if (!resolvedObject) return null;

  const fkColumnNames = new Set<string>();
  for (const edge of graph.edges) {
    for (const condition of edge.join_conditions ?? []) {
      const left = edgeEndpoint(graph, edge, condition, "left");
      if (left && objectMatches(resolvedObject, left)) fkColumnNames.add(left.columnName);
    }
  }

  const columns = graph.nodes
    .filter((node) => {
      const column = columnIdentityFromNode(node);
      return Boolean(column && objectMatches(resolvedObject, column));
    })
    .sort((left, right) => {
      const leftOrdinal = sortableOrdinal(left);
      const rightOrdinal = sortableOrdinal(right);
      if (leftOrdinal !== null && rightOrdinal !== null && leftOrdinal !== rightOrdinal) {
        return leftOrdinal - rightOrdinal;
      }
      if (leftOrdinal !== null && rightOrdinal === null) return -1;
      if (leftOrdinal === null && rightOrdinal !== null) return 1;
      const leftColumn = columnIdentityFromNode(left)?.columnName ?? "";
      const rightColumn = columnIdentityFromNode(right)?.columnName ?? "";
      return leftColumn.localeCompare(rightColumn, "en-US");
    })
    .map<OntologyErColumnDetail>((node) => {
      const metadata = node.metadata ?? {};
      const column = columnIdentityFromNode(node);
      const comment = jsonString(metadata.comment);
      return {
        id: node.id,
        columnName: column?.columnName || node.business_name_ja,
        dataType: jsonString(metadata.data_type) || "-",
        businessNameJa: node.business_name_ja,
        descriptionJa: node.description_ja || comment,
        keyRole: columnRole(node, fkColumnNames),
        ordinal: sortableOrdinal(node),
      };
    });

  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const joins = graph.edges
    .filter((edge) => (edge.join_conditions ?? []).length > 0)
    .filter((edge) =>
      (edge.join_conditions ?? []).some((condition) => {
        const left = edgeEndpoint(graph, edge, condition, "left");
        const right = edgeEndpoint(graph, edge, condition, "right");
        return Boolean(
          (left && objectMatches(resolvedObject, left)) ||
            (right && objectMatches(resolvedObject, right))
        );
      })
    )
    .sort((left, right) => left.id.localeCompare(right.id, "en-US"))
    .map<OntologyErJoinDetail>((edge) => ({
      id: edge.id,
      relationshipNameJa: edge.relationship_name_ja,
      sourceLabel: nodeLabel(nodeById.get(edge.source_node_id), edge.source_node_id),
      targetLabel: nodeLabel(nodeById.get(edge.target_node_id), edge.target_node_id),
      cardinality: edge.cardinality ?? "unknown",
      joinCondition: ontologyJoinConditionText(graph, edge) || "-",
    }));

  return {
    selectedNodeId,
    objectNodeId: objectNode?.id ?? null,
    objectName: objectName(resolvedObject),
    objectType: resolvedObject.objectType,
    displayNameJa: objectNode?.business_name_ja || selected.business_name_ja,
    columns,
    joins,
  };
}
