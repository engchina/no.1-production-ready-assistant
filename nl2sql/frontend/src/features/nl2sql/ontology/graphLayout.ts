// Ontology グラフの決定論レイアウト(semantic matrix)。
// 4 つの意味レーン × object クラスタ列に配置し、同じグラフは常に同じ座標になる。
import { formatDbObjectName } from "../dbObjectIdentity";
import type { OntologyGraph, OntologyJsonValue, OntologyNode, OntologyNodeKind } from "./types";

export interface GraphPoint {
  x: number;
  y: number;
}

export type OntologyGraphSemanticLaneId = "business" | "attribute" | "physical" | "detail";

export interface OntologyGraphSemanticLane {
  id: OntologyGraphSemanticLaneId;
  y: number;
  height: number;
  nodeCount: number;
}

export interface OntologyGraphSemanticLayout {
  positions: Map<string, GraphPoint>;
  lanes: OntologyGraphSemanticLane[];
  laneByNodeId: Map<string, OntologyGraphSemanticLaneId>;
  clusterByNodeId: Map<string, string>;
}

const SEMANTIC_LANES: OntologyGraphSemanticLaneId[] = [
  "business",
  "attribute",
  "physical",
  "detail",
];
const BUSINESS_KINDS = new Set<OntologyNodeKind>([
  "business_entity",
  "object_type",
  "interface",
  "function",
  "action_type",
  "object_set",
  "business_event",
  "business_term",
  "business_rule",
  "question_intent",
]);
const ATTRIBUTE_KINDS = new Set<OntologyNodeKind>([
  "property",
  "shared_property",
  "value_type",
  "enumeration",
  "metric",
  "query_plan",
  "cte",
  "sql_column",
  "sql_join",
  "sql_filter",
  "sql_aggregate",
  "sql_group",
  "sql_having",
  "sql_order",
  "sql_limit",
  "sql_window",
  "sql_artifact",
  "validation_finding",
  "execution_preview",
]);
const PHYSICAL_KINDS = new Set<OntologyNodeKind>(["schema", "table", "view", "sql_table"]);
const DETAIL_KINDS = new Set<OntologyNodeKind>(["column", "enum_value"]);

function jsonString(value: OntologyJsonValue | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

function jsonNumber(value: OntologyJsonValue | undefined): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeIdentifier(value: string | undefined): string {
  return (value ?? "").trim();
}

function splitQualifiedName(value: string | undefined): {
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

function stableClusterId(prefix: string, value: string): string {
  const normalized = value
    .trim()
    .toLocaleUpperCase("en-US")
    .replace(/[^A-Z0-9_.:-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${prefix}:${normalized || "UNKNOWN"}`;
}

function firstObjectMapping(node: OntologyNode) {
  return node.physical_mappings?.[0]?.object_ref;
}

export function ontologyGraphSemanticLaneForKind(
  kind: OntologyNodeKind
): OntologyGraphSemanticLaneId {
  if (DETAIL_KINDS.has(kind)) return "detail";
  if (PHYSICAL_KINDS.has(kind)) return "physical";
  if (ATTRIBUTE_KINDS.has(kind)) return "attribute";
  if (BUSINESS_KINDS.has(kind)) return "business";
  return "business";
}

export function ontologyGraphObjectClusterKey(node: OntologyNode): string | null {
  const mapping = firstObjectMapping(node);
  const fallback = node.physical_mapping;
  const metadata = node.metadata ?? {};
  if (metadata.definition && !mapping && !fallback) {
    return node.kind === "object_type" ? stableClusterId("concept", node.id) : null;
  }
  const technical = splitQualifiedName(node.technical_name);
  const owner = normalizeIdentifier(
    mapping?.owner || jsonString(metadata.owner) || fallback?.owner || technical.owner
  );
  const objectName = normalizeIdentifier(
    mapping?.object_name ||
      jsonString(metadata.object_name) ||
      fallback?.object_name ||
      technical.objectName
  );
  if (!objectName) return null;
  // カタログ上の名前を引用規則の表記にしてから ID 化する。大文字化だけでは `SALES."Mixed_Case"` と
  // 大文字の同名表 `SALES.MIXED_CASE` が同じ cluster になる（#563）。
  return stableClusterId("object", formatDbObjectName({ owner, name: objectName }));
}

function enumParentClusterKey(node: OntologyNode, clusterByNodeId: Map<string, string>): string | null {
  const parentId = typeof node.metadata?.derived_from_definition_id === "string"
    ? node.metadata.derived_from_definition_id : node.enum_value_definition?.property_node_id;
  return parentId ? clusterByNodeId.get(parentId) ?? null : null;
}

function nodeOrdinal(node: OntologyNode): number | null {
  const columnOrdinal = node.physical_mappings?.[0]?.column_refs?.[0]?.ordinal ?? null;
  return columnOrdinal ?? jsonNumber(node.metadata?.ordinal);
}

function sortNodesForSemanticCell(left: OntologyNode, right: OntologyNode): number {
  if (left.kind === "object_type" && right.kind !== "object_type") return -1;
  if (right.kind === "object_type" && left.kind !== "object_type") return 1;
  const leftOrdinal = nodeOrdinal(left);
  const rightOrdinal = nodeOrdinal(right);
  if (leftOrdinal !== null && rightOrdinal !== null && leftOrdinal !== rightOrdinal) {
    return leftOrdinal - rightOrdinal;
  }
  if (leftOrdinal !== null && rightOrdinal === null) return -1;
  if (leftOrdinal === null && rightOrdinal !== null) return 1;
  const kindCompare = left.kind.localeCompare(right.kind, "en-US");
  if (kindCompare !== 0) return kindCompare;
  return (left.business_name_ja || left.technical_name || left.id).localeCompare(
    right.business_name_ja || right.technical_name || right.id,
    "ja"
  );
}

export function layoutOntologyGraphSemanticMatrix(
  graph: OntologyGraph,
  options: {
    nodeWidth?: number;
    nodeHeight?: number;
    nodeGap?: number;
    clusterGap?: number;
    laneGap?: number;
    lanePaddingY?: number;
    left?: number;
    top?: number;
    /** physical レーン内で schema 行と表・ビュー行の間に確保するエッジ専用の余白帯。 */
    schemaRowGap?: number;
  } = {}
): OntologyGraphSemanticLayout {
  const positions = new Map<string, GraphPoint>();
  const laneByNodeId = new Map<string, OntologyGraphSemanticLaneId>();
  const clusterByNodeId = new Map<string, string>();
  if (graph.nodes.length === 0) {
    return { positions, lanes: [], laneByNodeId, clusterByNodeId };
  }

  const nodeWidth = options.nodeWidth ?? 200;
  const nodeHeight = options.nodeHeight ?? 64;
  const nodeGap = options.nodeGap ?? 14;
  const clusterGap = options.clusterGap ?? 72;
  const laneGap = options.laneGap ?? 44;
  const lanePaddingY = options.lanePaddingY ?? 22;
  const left = options.left ?? 220;
  const top = options.top ?? 34;
  const schemaRowGap = options.schemaRowGap ?? 40;
  // schema は physical レーン内の専用上段行に置き、schema→表エッジが同一行の
  // 表ノードを真横に貫通しないようにする(下段との間はエッジ専用の余白帯)。
  const schemaNodeIds = new Set(
    graph.nodes.filter((node) => node.kind === "schema").map((node) => node.id)
  );
  const hasSchemaRow = schemaNodeIds.size > 0;
  const physicalRowOffset = hasSchemaRow ? nodeHeight + schemaRowGap : 0;
  const incomingObjectCluster = new Map(
    graph.nodes
      .map((node) => [node.id, ontologyGraphObjectClusterKey(node)] as const)
      .filter((entry): entry is readonly [string, string] => Boolean(entry[1]))
  );

  for (const node of graph.nodes) {
    laneByNodeId.set(node.id, ontologyGraphSemanticLaneForKind(node.kind));
    const directCluster = incomingObjectCluster.get(node.id);
    if (directCluster) clusterByNodeId.set(node.id, directCluster);
  }

  let grew = true;
  while (grew) {
    grew = false;
  for (const node of [...graph.nodes].sort((a,b)=>a.id.localeCompare(b.id))) {
    if (clusterByNodeId.has(node.id)) continue;
    const parentCluster = enumParentClusterKey(node, clusterByNodeId);
    if (parentCluster) {
      clusterByNodeId.set(node.id, parentCluster);
      grew = true;
      continue;
    }
    const connectedCluster = graph.edges
      .flatMap((edge) => {
        if (edge.source_node_id === node.id) return [edge.target_node_id];
        if (edge.target_node_id === node.id) return [edge.source_node_id];
        return [];
      })
      .sort()
      .map((nodeId) => clusterByNodeId.get(nodeId) ?? incomingObjectCluster.get(nodeId) ?? null)
      .find((cluster): cluster is string => Boolean(cluster));
    if (connectedCluster) {clusterByNodeId.set(node.id, connectedCluster); grew = true;}
  }
  }
  for (const node of graph.nodes) {
    if (!clusterByNodeId.has(node.id)) clusterByNodeId.set(node.id, stableClusterId(ontologyGraphSemanticLaneForKind(node.kind), node.id));
  }

  const clusterOrder = new Map<string, number>();
  graph.nodes.forEach((node, index) => {
    const cluster = clusterByNodeId.get(node.id) ?? stableClusterId("node", node.id);
    clusterOrder.set(cluster, Math.min(clusterOrder.get(cluster) ?? index, index));
  });
  const initialClusters = [...clusterOrder]
    .sort((leftEntry, rightEntry) => {
      const indexCompare = leftEntry[1] - rightEntry[1];
      return indexCompare || leftEntry[0].localeCompare(rightEntry[0], "en-US");
    })
    .map(([cluster]) => cluster);
  // バリセンタ 1 パス: 隣接クラスタの初期順位との平均で列順を安定ソートし、
  // 横方向の長距離エッジと交差を減らす(初期順位・クラスタ ID タイブレークで決定論)。
  const initialIndex = new Map(initialClusters.map((cluster, index) => [cluster, index]));
  const neighborOrder = new Map<string, { sum: number; count: number }>();
  const addNeighbor = (cluster: string, neighborRank: number) => {
    const entry = neighborOrder.get(cluster) ?? { sum: 0, count: 0 };
    entry.sum += neighborRank;
    entry.count += 1;
    neighborOrder.set(cluster, entry);
  };
  for (const edge of graph.edges) {
    const sourceCluster = clusterByNodeId.get(edge.source_node_id);
    const targetCluster = clusterByNodeId.get(edge.target_node_id);
    if (!sourceCluster || !targetCluster || sourceCluster === targetCluster) continue;
    addNeighbor(sourceCluster, initialIndex.get(targetCluster) ?? 0);
    addNeighbor(targetCluster, initialIndex.get(sourceCluster) ?? 0);
  }
  const barycenter = (cluster: string): number => {
    const own = initialIndex.get(cluster) ?? 0;
    const neighbors = neighborOrder.get(cluster);
    if (!neighbors || neighbors.count === 0) return own;
    return (own + neighbors.sum / neighbors.count) / 2;
  };
  const clusters = [...initialClusters].sort((leftCluster, rightCluster) => {
    const barycenterCompare = barycenter(leftCluster) - barycenter(rightCluster);
    if (barycenterCompare !== 0) return barycenterCompare;
    const indexCompare =
      (initialIndex.get(leftCluster) ?? 0) - (initialIndex.get(rightCluster) ?? 0);
    return indexCompare || leftCluster.localeCompare(rightCluster, "en-US");
  });
  const cells = new Map<string, OntologyNode[]>();
  for (const node of graph.nodes) {
    const lane = laneByNodeId.get(node.id) ?? "business";
    const cluster = clusterByNodeId.get(node.id) ?? stableClusterId("node", node.id);
    const key = `${lane}\u0000${cluster}`;
    const cell = cells.get(key) ?? [];
    cell.push(node);
    cells.set(key, cell);
  }
  for (const cell of cells.values()) {
    cell.sort(sortNodesForSemanticCell);
  }
  // 型付き定義が多い object は同一クラスタ内で折り返し、長い一列を避ける。
  const cellColumns = (cell: OntologyNode[]) => cell.length > 3 && cell.every(node => node.metadata?.definition)
    ? Math.min(3, Math.ceil(cell.length / 3)) : 1;
  const clusterX = new Map<string, number>();
  let nextX = left;
  for (const cluster of clusters) {
    clusterX.set(cluster, nextX);
    const columns = Math.max(1, ...SEMANTIC_LANES.map(lane => cellColumns(cells.get(`${lane}\u0000${cluster}`) ?? [])));
    nextX += columns * nodeWidth + (columns - 1) * nodeGap + clusterGap;
  }

  // physical レーンが schema 行を持つときは、非 schema の最大行数を別勘定で高さに使う
  let maxPhysicalObjectRows = 0;
  if (hasSchemaRow) {
    for (const [cellKey, cellNodes] of cells) {
      if (!cellKey.startsWith("physical")) continue;
      maxPhysicalObjectRows = Math.max(
        maxPhysicalObjectRows,
        cellNodes.filter((node) => !schemaNodeIds.has(node.id)).length
      );
    }
  }
  const laneHeights = new Map<OntologyGraphSemanticLaneId, number>();
  for (const lane of SEMANTIC_LANES) {
    if (lane === "physical" && hasSchemaRow) {
      // schema 行(上段) + 余白帯 + 表・ビュー行(下段)で高さを積む
      const rowsHeight =
        maxPhysicalObjectRows > 0
          ? physicalRowOffset +
            maxPhysicalObjectRows * nodeHeight +
            (maxPhysicalObjectRows - 1) * nodeGap
          : nodeHeight;
      laneHeights.set(lane, lanePaddingY * 2 + rowsHeight);
      continue;
    }
    let maxRows = 1;
    for (const cluster of clusters) {
      const cell = cells.get(`${lane}\u0000${cluster}`) ?? [];
      maxRows = Math.max(maxRows, Math.ceil(cell.length / cellColumns(cell)));
    }
    laneHeights.set(
      lane,
      lanePaddingY * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * nodeGap
    );
  }

  const lanes: OntologyGraphSemanticLane[] = [];
  let y = top;
  for (const lane of SEMANTIC_LANES) {
    const nodeCount = graph.nodes.filter((node) => laneByNodeId.get(node.id) === lane).length;
    // 空レーンは縦領域もラベルも作らない(グラフの無駄な余白と誤解を防ぐ)
    if (nodeCount === 0) continue;
    const height = laneHeights.get(lane) ?? nodeHeight;
    lanes.push({ id: lane, y, height, nodeCount });
    y += height + laneGap;
  }

  for (const [cellKey, cellNodes] of cells) {
    const [lane, cluster] = cellKey.split("\u0000") as [OntologyGraphSemanticLaneId, string];
    const laneMeta = lanes.find((item) => item.id === lane);
    const x = clusterX.get(cluster) ?? left;
    const baseY = (laneMeta?.y ?? top) + lanePaddingY;
    if (lane === "physical" && hasSchemaRow) {
      // schema は常に上段行、表・ビューは余白帯を挟んだ下段行に積む
      let objectRowIndex = 0;
      for (const node of cellNodes) {
        if (schemaNodeIds.has(node.id)) {
          positions.set(node.id, { x, y: baseY });
          continue;
        }
        positions.set(node.id, {
          x,
          y: baseY + physicalRowOffset + objectRowIndex * (nodeHeight + nodeGap),
        });
        objectRowIndex += 1;
      }
      continue;
    }
    cellNodes.forEach((node, index) => {
      positions.set(node.id, {
        x: x + (index % cellColumns(cellNodes)) * (nodeWidth + nodeGap),
        y: baseY + Math.floor(index / cellColumns(cellNodes)) * (nodeHeight + nodeGap),
      });
    });
  }

  return { positions, lanes, laneByNodeId, clusterByNodeId };
}
