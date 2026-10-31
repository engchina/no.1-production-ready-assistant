// Ontology ノード/関係の物理識別(owner.object.column)と Join 条件表記の共有実装。
// ER 詳細(erDetails.ts)と関係一覧(types.ts)で同じ表記を使うため、ここを唯一の実装とする。
import { formatDbObjectName, formatDbObjectPart } from "../dbObjectIdentity";
import type {
  OntologyEdge,
  OntologyGraph,
  OntologyJoinCondition,
  OntologyJsonValue,
  OntologyNode,
} from "./types";

export interface ObjectIdentity {
  owner: string;
  objectName: string;
  objectType: "table" | "view" | "unknown";
  nodeId?: string;
}

export interface ColumnIdentity extends ObjectIdentity {
  columnName: string;
}

type JoinEndpoint = Partial<NonNullable<OntologyJoinCondition["left"]>>;

export const OBJECT_KINDS = new Set(["table", "view"]);

export function jsonString(value: OntologyJsonValue | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * node の metadata / 物理参照に入ったカタログ上の名前（大文字小文字を保持）を比較用に揃える。
 * `"Mixed_Case"` のような token は引用符を外す。大文字化すると大文字の同名表と同じ表として
 * 扱ってしまう（#563）。
 */
export function normalizeIdentifier(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1).replaceAll('""', '"');
  }
  return trimmed;
}

export function objectType(value: string | undefined): ObjectIdentity["objectType"] {
  const normalized = (value ?? "").trim().toLowerCase();
  if (normalized.includes("view")) return "view";
  if (normalized.includes("table")) return "table";
  return "unknown";
}

/** 表・ビューの表示名(`OWNER.OBJECT`)。組み立ては `formatDbObjectName` に委ねる。 */
export function objectName(identity: ObjectIdentity): string {
  return formatDbObjectName({ owner: identity.owner, name: identity.objectName });
}

export function splitQualifiedName(value: string | undefined): {
  owner: string;
  objectName: string;
  columnName: string;
} {
  const parts = (value ?? "")
    .split(".")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length >= 3) {
    return {
      owner: parts[parts.length - 3],
      objectName: parts[parts.length - 2],
      columnName: parts[parts.length - 1],
    };
  }
  if (parts.length === 2) return { owner: parts[0], objectName: parts[1], columnName: "" };
  return { owner: "", objectName: parts[0] ?? "", columnName: "" };
}

export function firstMappingObject(node: OntologyNode) {
  return node.physical_mappings?.[0]?.object_ref;
}

export function objectIdentityFromNode(node: OntologyNode): ObjectIdentity | null {
  const mapping = firstMappingObject(node);
  const fallback = node.physical_mapping;
  const technical = splitQualifiedName(node.technical_name);
  const metadata = node.metadata ?? {};
  const allowTechnicalFallback = OBJECT_KINDS.has(node.kind) || node.kind === "column";
  const owner = normalizeIdentifier(
    mapping?.owner ||
      jsonString(metadata.owner) ||
      fallback?.owner ||
      (allowTechnicalFallback ? technical.owner : "")
  );
  const objectNameValue =
    mapping?.object_name ||
    jsonString(metadata.object_name) ||
    fallback?.object_name ||
    (allowTechnicalFallback ? technical.objectName : "");
  const normalizedObjectName = normalizeIdentifier(objectNameValue);
  if (!normalizedObjectName) return null;
  const resolvedType = objectType(
    mapping?.object_type || jsonString(metadata.object_type) || fallback?.object_type || node.kind
  );
  return {
    owner,
    objectName: normalizedObjectName,
    objectType: resolvedType,
    nodeId: mapping?.node_id || (OBJECT_KINDS.has(node.kind) ? node.id : undefined),
  };
}

export function columnIdentityFromNode(node: OntologyNode): ColumnIdentity | null {
  if (node.kind !== "column") return null;
  const object = objectIdentityFromNode(node);
  if (!object) return null;
  const columnRef = node.physical_mappings?.[0]?.column_refs?.[0];
  const technical = splitQualifiedName(node.technical_name);
  const metadata = node.metadata ?? {};
  const columnName = normalizeIdentifier(
    columnRef?.column_name ||
      jsonString(metadata.column_name) ||
      node.physical_mapping?.column_name ||
      technical.columnName
  );
  if (!columnName) return null;
  return { ...object, columnName };
}

export function objectMatches(left: ObjectIdentity, right: ObjectIdentity): boolean {
  if (left.nodeId && right.nodeId && left.nodeId === right.nodeId) return true;
  return left.owner === right.owner && left.objectName === right.objectName;
}

function endpointIdentity(endpoint: JoinEndpoint | undefined): ObjectIdentity | null {
  const objectNameValue = normalizeIdentifier(endpoint?.object_name);
  if (!objectNameValue) return null;
  return {
    owner: normalizeIdentifier(endpoint?.owner),
    objectName: objectNameValue,
    objectType: "unknown",
  };
}

function endpointColumn(endpoint: JoinEndpoint | undefined): string {
  return normalizeIdentifier(endpoint?.column_name);
}

export function edgeEndpoint(
  graph: OntologyGraph,
  edge: OntologyEdge,
  condition: OntologyJoinCondition,
  side: "left" | "right"
): ColumnIdentity | null {
  const explicit = side === "left" ? condition.left : condition.right;
  const explicitObject = endpointIdentity(explicit);
  const explicitColumn = endpointColumn(explicit);
  if (explicitObject && explicitColumn) return { ...explicitObject, columnName: explicitColumn };

  const nodeId = side === "left" ? edge.source_node_id : edge.target_node_id;
  const fallbackNode = graph.nodes.find((node) => node.id === nodeId);
  const fallbackObject = fallbackNode ? objectIdentityFromNode(fallbackNode) : null;
  const fallbackColumn = normalizeIdentifier(
    side === "left" ? condition.source_column : condition.target_column
  );
  if (fallbackObject && fallbackColumn) return { ...fallbackObject, columnName: fallbackColumn };
  return null;
}

export function endpointLabel(endpoint: ColumnIdentity | null): string {
  if (!endpoint) return "?";
  const qualified = objectName(endpoint);
  const column = formatDbObjectPart(endpoint.columnName);
  return qualified ? `${qualified}.${column}` : column;
}

/** 物理修飾できないときは列名のみ、それも無ければ "?" へ縮退する。 */
function joinEndpointText(
  graph: OntologyGraph,
  edge: OntologyEdge,
  condition: OntologyJoinCondition,
  side: "left" | "right"
): string {
  const resolved = edgeEndpoint(graph, edge, condition, side);
  if (resolved) return endpointLabel(resolved);
  const bare =
    side === "left"
      ? condition.source_column ?? condition.left?.column_name
      : condition.target_column ?? condition.right?.column_name;
  return (bare ?? "").trim() || "?";
}

/** Join 条件を `OWNER.OBJECT.COLUMN = OWNER.OBJECT.COLUMN` 形式で表記する。条件が無ければ空文字。 */
export function ontologyJoinConditionText(graph: OntologyGraph, edge: OntologyEdge): string {
  return (edge.join_conditions ?? [])
    .slice()
    .sort((left, right) => (left.ordinal ?? 0) - (right.ordinal ?? 0))
    .map((condition) => {
      const left = joinEndpointText(graph, edge, condition, "left");
      const right = joinEndpointText(graph, edge, condition, "right");
      return `${left} ${condition.operator ?? "="} ${right}`;
    })
    .join(" AND ");
}

/** ノードの物理対応名(列なら OWNER.OBJECT.COLUMN、表・ビューなら OWNER.OBJECT)。無ければ空文字。 */
export function ontologyPhysicalNodeLabel(node: OntologyNode | undefined): string {
  if (!node) return "";
  const column = columnIdentityFromNode(node);
  if (column) return endpointLabel(column);
  const object = objectIdentityFromNode(node);
  return object ? objectName(object) : "";
}
