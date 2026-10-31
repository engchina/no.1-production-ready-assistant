import type { GraphPoint } from "./graphLayout";
import type { OntologyCardinality, OntologyGraph } from "./types";

export type OntologyGraphViewMode = "grounding" | "all" | "physical_er";

/** 業務⇄物理の「対応」を表すエッジ(点線で描く。FK join の実線と区別する)。 */
const MAPPING_EDGE_KINDS = new Set(["maps_to", "physical_mapping"]);

export function isOntologyMappingEdge(edge: OntologyGraph["edges"][number]): boolean {
  return MAPPING_EDGE_KINDS.has((edge.kind ?? "").trim().toLocaleLowerCase());
}

/** join を持つ関係エッジ(カーディナリティを常時表示する対象)。 */
export function isOntologyJoinEdge(edge: OntologyGraph["edges"][number]): boolean {
  const kind = (edge.kind ?? "").trim().toLocaleLowerCase();
  return kind === "foreign_key" || (edge.join_conditions ?? []).length > 0;
}

/** 包含(schema→表、表→列 等)を表すエッジ。レーン内でも縦ルーティングさせる対象。 */
const CONTAINMENT_EDGE_KINDS = new Set(["contains", "physical_contains"]);

export function isOntologyContainmentEdge(edge: OntologyGraph["edges"][number]): boolean {
  return CONTAINMENT_EDGE_KINDS.has((edge.kind ?? "").trim().toLocaleLowerCase());
}

/** ER 図流の常時表示用カーディナリティ短縮ラベル(unknown/未設定は空)。 */
export function cardinalityShortLabel(cardinality: OntologyCardinality | undefined): string {
  switch (cardinality) {
    case "one_to_one":
      return "1:1";
    case "one_to_many":
      return "1:N";
    case "many_to_one":
      return "N:1";
    case "many_to_many":
      return "N:N";
    default:
      return "";
  }
}

export interface OntologyEdgeHandleSelection {
  sourceHandle: string;
  targetHandle: string;
  orientation: "horizontal" | "vertical";
}

/**
 * ノード中心の相対位置からエッジの接続ハンドルを選ぶ。
 * 縦優勢(レーン間: 業務概念→物理表 等)は上下ハンドル、横優勢は左右ハンドルを使い、
 * 固定 Left/Right だけのときに起きる自己ループ状の曲線を防ぐ。
 */
export function selectOntologyEdgeHandles(
  source: { x: number; y: number },
  target: { x: number; y: number },
  options: { preferVertical?: boolean } = {}
): OntologyEdgeHandleSelection {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  // preferVertical: 同一レーン内の別行(schema→表 等)は dx 優勢でも上下で結び、
  // 途中のノードを真横に貫通する bezier を避ける(dy=0 のときは従来判定)。
  if (Math.abs(dy) > Math.abs(dx) || (options.preferVertical && dy !== 0)) {
    return dy >= 0
      ? { sourceHandle: "s-bottom", targetHandle: "t-top", orientation: "vertical" }
      : { sourceHandle: "s-top", targetHandle: "t-bottom", orientation: "vertical" };
  }
  return dx >= 0
    ? { sourceHandle: "s-right", targetHandle: "t-left", orientation: "horizontal" }
    : { sourceHandle: "s-left", targetHandle: "t-right", orientation: "horizontal" };
}

const DETAIL_NODE_KINDS = new Set(["column", "enum_value"]);
const PHYSICAL_ER_NODE_KINDS = new Set(["schema", "table", "view", "column", "enum_value"]);
const GROUNDING_CONTEXT_EDGE_KINDS = new Set([
  "maps_to",
  "physical_mapping",
  "physical_contains",
  "contains",
  "column",
  "foreign_key",
  "link_type",
  "uses",
  "is_a",
  "domain",
  "range",
  "governs",
  "has_value",
]);

export function isOntologyDetailNodeKind(kind: string): boolean {
  return DETAIL_NODE_KINDS.has(kind);
}

function filterGraphByNodeIds(graph: OntologyGraph, nodeIds: Set<string>): OntologyGraph {
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => nodeIds.has(node.id)),
    edges: graph.edges.filter(
      (edge) => nodeIds.has(edge.source_node_id) && nodeIds.has(edge.target_node_id)
    ),
  };
}

/** 物理マッピング/包含(表→列 等)を表すエッジか。属性→親エンティティの逆引きにも使う。 */
export function isGroundingContextEdge(edge: OntologyGraph["edges"][number]): boolean {
  const kind = (edge.kind ?? "").trim().toLocaleLowerCase();
  if (GROUNDING_CONTEXT_EDGE_KINDS.has(kind)) return true;
  if ((edge.join_conditions ?? []).length > 0) return true;
  const label = edge.relationship_name_ja.trim();
  return label.includes("物理マッピング") || label === "列" || label.includes("Join");
}

export function ontologyGraphForViewMode(
  graph: OntologyGraph,
  mode: OntologyGraphViewMode,
  highlightNodeIds: string[] | undefined,
  highlightEdgeIds: string[] | undefined
): OntologyGraph {
  if (mode === "physical_er") {
    return filterGraphByNodeIds(
      graph,
      new Set(
        graph.nodes.filter((node) => PHYSICAL_ER_NODE_KINDS.has(node.kind) && !node.metadata?.derived_from_definition_id).map((node) => node.id)
      )
    );
  }
  const hasGrounding =
    mode === "grounding" &&
    ((highlightNodeIds?.length ?? 0) > 0 || (highlightEdgeIds?.length ?? 0) > 0);
  if (!hasGrounding) return graph;

  const keep = new Set(highlightNodeIds ?? []);
  const highlightedEdges = new Set(highlightEdgeIds ?? []);
  for (const edge of graph.edges) {
    if (!highlightedEdges.has(edge.id)) continue;
    keep.add(edge.source_node_id);
    keep.add(edge.target_node_id);
  }
  // 接地ノードから文脈エッジ 1 ホップ分だけ隣接ノードを足す。SQL に登場しない隣接表
  // (FK 先など)も「接地はしていない減光ノード」として残し、周辺に何があるかを示す仕様。
  const contextAnchor = new Set(keep);
  for (const edge of graph.edges) {
    if (!contextAnchor.has(edge.source_node_id) && !contextAnchor.has(edge.target_node_id)) continue;
    if (!isGroundingContextEdge(edge)) continue;
    keep.add(edge.source_node_id);
    keep.add(edge.target_node_id);
  }
  return filterGraphByNodeIds(graph, keep);
}

/**
 * 同一ノードペア間の並行(horizontal)エッジ用の 2 次ベジェ経路。
 * 同じ y の bezier/smoothstep は直線に潰れて完全に重なるため、進行方向の
 * 単位法線に沿って制御点をずらした弧で分離する。ラベルは曲線の t=0.5 点。
 */
export function ontologyParallelEdgeGeometry(
  source: GraphPoint,
  target: GraphPoint,
  offset: number
): { path: string; labelX: number; labelY: number } {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const length = Math.hypot(dx, dy) || 1;
  // 2 次ベジェの t=0.5 での実オフセットは制御点オフセットの半分なので 2 倍しておく
  const controlX = (source.x + target.x) / 2 + (-dy / length) * offset * 2;
  const controlY = (source.y + target.y) / 2 + (dx / length) * offset * 2;
  return {
    path: `M ${source.x},${source.y} Q ${controlX},${controlY} ${target.x},${target.y}`,
    labelX: 0.25 * source.x + 0.5 * controlX + 0.25 * target.x,
    labelY: 0.25 * source.y + 0.5 * controlY + 0.25 * target.y,
  };
}

/**
 * ノードドラッグの position change だけを座標上書き Map へ反映する
 * (select/dimensions/remove 等は無視。変更が無ければ同一 Map 参照を返す)。
 * controlled flow でこの反映を怠るとドラッグがスナップバックする。
 */
export function applyOntologyNodePositionChanges(
  overrides: Map<string, GraphPoint>,
  changes: ReadonlyArray<{ type: string; id?: string; position?: GraphPoint }>
): Map<string, GraphPoint> {
  let next: Map<string, GraphPoint> | null = null;
  for (const change of changes) {
    if (change.type !== "position" || !change.id || !change.position) continue;
    if (!Number.isFinite(change.position.x) || !Number.isFinite(change.position.y)) continue;
    next ??= new Map(overrides);
    next.set(change.id, { x: change.position.x, y: change.position.y });
  }
  return next ?? overrides;
}

export interface OntologyNodeDimensions {
  width: number;
  height: number;
}

/**
 * React Flow が通知する dimensions change を実測サイズ Map へ反映する。
 * この実測値をノードへ `measured` として返さないと、配列再生成(hover 等)のたびに
 * React Flow が handleBounds をリセットし、全エッジが 1 フレーム消えて点滅する。
 * 値が同じなら同一 Map 参照を返し、measure → 反映 → 再 measure のループを断つ。
 */
export function applyOntologyNodeDimensionChanges(
  dimensions: Map<string, OntologyNodeDimensions>,
  changes: ReadonlyArray<{ type: string; id?: string; dimensions?: OntologyNodeDimensions }>
): Map<string, OntologyNodeDimensions> {
  let next: Map<string, OntologyNodeDimensions> | null = null;
  for (const change of changes) {
    if (change.type !== "dimensions" || !change.id || !change.dimensions) continue;
    const { width, height } = change.dimensions;
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
      continue;
    }
    const current = (next ?? dimensions).get(change.id);
    if (current && current.width === width && current.height === height) continue;
    next ??= new Map(dimensions);
    next.set(change.id, { width, height });
  }
  return next ?? dimensions;
}

export function ontologyGraphWithDetailVisibility(
  graph: OntologyGraph,
  showDetails: boolean,
  forcedNodeIds: Set<string>
): OntologyGraph {
  if (showDetails) return graph;
  return filterGraphByNodeIds(
    graph,
    new Set(
      graph.nodes
        .filter((node) => !isOntologyDetailNodeKind(node.kind) || forcedNodeIds.has(node.id))
        .map((node) => node.id)
    )
  );
}
