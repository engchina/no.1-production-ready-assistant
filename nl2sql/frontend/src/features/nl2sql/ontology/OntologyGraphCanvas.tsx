import { conceptKinds, conceptLabel, conceptGraph, nodeConceptKind } from "./unifiedConcepts";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useWorkspaceState } from "@/components/WorkspaceState";
import {
  AlertTriangle,
  BookOpen,
  Boxes,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Circle,
  Columns3,
  Database,
  Layers,
  ListOrdered,
  Maximize2,
  MessageSquare,
  Minus,
  Network,
  Plus,
  RotateCcw,
  Route,
  Search,
  ShieldCheck,
  Sigma,
  Table2,
  Tag,
  type LucideIcon,
} from "lucide-react";
import {
  Background,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  ViewportPortal,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { Button, StatusBadge, isImeComposing, fieldControlClassName } from "@production-ready/ui";
import { cn } from "@/lib/utils";
import { useValuesChanged } from "@/lib/render-sync";

import { t } from "@/lib/i18n";
import {
  layoutOntologyGraphSemanticMatrix,
  ontologyGraphObjectClusterKey,
  type GraphPoint,
  type OntologyGraphSemanticLane,
  type OntologyGraphSemanticLaneId,
} from "./graphLayout";
import { cssVar, edgeStroke, nodeFill, nodeFillVar, nodeStroke } from "./graphPalette";
import {
  applyOntologyNodeDimensionChanges,
  applyOntologyNodePositionChanges,
  cardinalityShortLabel,
  isOntologyContainmentEdge,
  isOntologyDetailNodeKind,
  isOntologyJoinEdge,
  isOntologyMappingEdge,
  ontologyGraphForViewMode,
  ontologyGraphWithDetailVisibility,
  selectOntologyEdgeHandles,
  type OntologyEdgeHandleSelection,
  type OntologyGraphViewMode,
  type OntologyNodeDimensions,
} from "./graphView";
import { ontologyNodeDisplay, ontologyNodeSearchValues } from "./nodeDisplay";
import OntologyParallelEdge from "./ParallelEdge";
import type { OntologyGraph, OntologyNode } from "./types";

interface OntologyGraphCanvasProps {
  graph: OntologyGraph;
  workspaceKey?: string;
  selectedNodeId?: string | null;
  selectedEdgeId?: string | null;
  onSelectNode?: (nodeId: string) => void;
  onSelectEdge?: (edgeId: string) => void;
  /** 決定論 NL Playground 等のハイライト。指定時は非対象を減光し、対象の枠を強調する。 */
  highlightNodeIds?: string[];
  highlightEdgeIds?: string[];
  viewMode?: OntologyGraphViewMode;
  defaultViewMode?: OntologyGraphViewMode;
  onViewModeChange?: (mode: OntologyGraphViewMode) => void;
}

const NODE_WIDTH = 220;
const NODE_HEIGHT = 76;
const GRAPH_VIEW_MODES: Array<{ id: OntologyGraphViewMode; labelKey: string; icon: LucideIcon }> = [
  { id: "grounding", labelKey: "nl2sql.ontology.graphMode.grounding", icon: Route },
  { id: "all", labelKey: "nl2sql.ontology.graphMode.all", icon: Network },
  { id: "physical_er", labelKey: "nl2sql.ontology.graphMode.physicalEr", icon: Table2 },
];
const LANE_LABEL_KEYS: Record<OntologyGraphSemanticLaneId, string> = {
  business: "nl2sql.ontology.graphLane.business",
  attribute: "nl2sql.ontology.graphLane.attribute",
  physical: "nl2sql.ontology.graphLane.physical",
  detail: "nl2sql.ontology.graphLane.detail",
};

const KIND_ICONS: Record<string, LucideIcon> = {
  schema: Database,
  table: Table2,
  view: Layers,
  column: Columns3,
  business_entity: Boxes,
  object_type: Boxes, interface: Layers, function: Sigma, action_type: Route, shared_property: Tag, value_type: BookOpen, enumeration: ListOrdered, object_set: Boxes,
  business_event: CalendarClock,
  property: Tag,
  metric: Sigma,
  business_term: BookOpen,
  business_rule: ShieldCheck,
  enum_value: ListOrdered,
  question_intent: MessageSquare,
  validation_finding: AlertTriangle,
};

const HIDDEN_HANDLE_STYLE = { visibility: "hidden" } as const;

function nodeShape(node: OntologyNode): string {
  if (node.kind === "metric") return "18px";
  if (node.kind === "validation_finding") return "3px";
  return "8px";
}

interface OntologyNodeData extends Record<string, unknown> {
  node: OntologyNode;
  highlighted: boolean;
  /** グラフ内検索の一致(接地ハイライトとは別チャネル: リングで表現)。 */
  searchMatched: boolean;
  dimmed: boolean;
  stats?: {
    columnCount: number;
    joinCount: number;
  };
}

/** smoothstep の pathOptions(stepPosition 等)を持てる Edge のローカル拡張。 */
type OntologyFlowEdge = Edge & {
  pathOptions?: { borderRadius?: number; offset?: number; stepPosition?: number };
};

/** 凡例と kind フィルタで使う表示グループ。 */
const LEGEND_GROUPS: Array<{
  id: string;
  labelKey: string;
  swatchKind: string;
  kinds: string[];
}> = [
  {
    id: "business",
    labelKey: "nl2sql.ontology.legend.business",
    swatchKind: "business_entity",
    kinds: ["business_entity", "business_event", "business_term", "business_rule"],
  },
  {
    id: "physical",
    labelKey: "nl2sql.ontology.legend.physical",
    swatchKind: "table",
    kinds: ["schema", "table", "view"],
  },
  {
    id: "attribute",
    labelKey: "nl2sql.ontology.legend.attribute",
    swatchKind: "property",
    kinds: ["property", "column", "enum_value"],
  },
  {
    id: "metric",
    labelKey: "nl2sql.ontology.legend.metric",
    swatchKind: "metric",
    kinds: ["metric"],
  },
];

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** アイコン + 業務名 + 技術名のカード。型=塗り/状態=枠のチャネル分離は graphPalette を踏襲。
 *  memo 化でドラッグ中の無関係ノード再レンダを防ぐ。 */
const OntologyNodeCard = memo(function OntologyNodeCard({
  data,
  selected,
}: NodeProps<Node<OntologyNodeData>>) {
  const { node, highlighted, searchMatched, dimmed, stats } = data;
  const Icon = KIND_ICONS[node.kind] ?? Circle;
  const display = ontologyNodeDisplay(node, { highlighted });
  const emphasizedBorder = selected || highlighted || node.validation_status === "blocked";
  const showStats =
    (node.kind === "table" || node.kind === "view") &&
    Boolean(stats && (stats.columnCount > 0 || stats.joinCount > 0));
  const hoverTitle = [
    display.primaryLabel,
    display.secondaryLabel,
    node.description_ja || node.description || "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <div
      className="grid h-full grid-cols-[auto_minmax(0,1fr)] items-center gap-2 px-3 py-2"
      data-testid={`ontology-node-card-${node.id}`}
      data-ontology-node-kind={node.kind}
      // 接地(ハイライト)は枠色と opacity で表すため、テストから検証できる印を残す。
      data-ontology-node-grounded={highlighted ? "true" : "false"}
      title={hoverTitle}
      style={{
        width: NODE_WIDTH,
        minHeight: NODE_HEIGHT,
        border: `${emphasizedBorder ? 2 : 1}px solid ${
          highlighted ? cssVar("--color-accent-fg") : nodeStroke(node, Boolean(selected))
        }`,
        borderRadius: nodeShape(node),
        background: nodeFill(node),
        color: cssVar("--color-graph-fg"),
        opacity: dimmed ? 0.35 : 1,
        // 検索一致は接地ハイライト(枠)と区別できるリングで示す
        boxShadow: searchMatched
          ? // グラフ座標系のリング幅（ズームに追従する幾何値）なので px で指定する。
            // oxlint-disable-next-line design-system/restricted-syntax
            `0 0 0 3px color-mix(in srgb, ${cssVar("--color-warning-fg")} 65%, transparent)`
          : undefined,
      }}
    >
      {/* 4 方向 × source/target の隠しハンドル。エッジ側が相対位置で接続方向を選ぶ
          (固定 Left/Right だけだと直下ノードへのエッジが自己ループ状になるため)。 */}
      <Handle id="t-left" type="target" position={Position.Left} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="t-top" type="target" position={Position.Top} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="t-right" type="target" position={Position.Right} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="t-bottom" type="target" position={Position.Bottom} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="s-left" type="source" position={Position.Left} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="s-top" type="source" position={Position.Top} style={HIDDEN_HANDLE_STYLE} />
      {/* グラフの節点の印の箱（名前と種類の 2 行のまとまりの中央。文字の 1 行目にそろえる規則の例外。#2371）。 */}
      <span
        className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
        style={{ background: "color-mix(in srgb, currentColor 12%, transparent)" }}
        data-icon-align-skip=""
      >
        <Icon size={16} aria-hidden="true" />
      </span>
      <span className="grid min-w-0 gap-0.5">
        <span
          className="min-w-0 justify-self-start"
          data-testid="ontology-node-kind-label"
        >
          <StatusBadge
            variant="info"
            icon={false}
            label={display.kindLabel}
            className="max-w-full whitespace-normal text-left"
          />
        </span>
        <span className="block truncate text-sm font-semibold leading-5">
          {display.primaryLabel}
        </span>
        {display.secondaryLabel ? (
          <span
            className="block truncate text-xs leading-4 opacity-70"
            title={display.secondaryLabel}
          >
            {display.secondaryLabel}
          </span>
        ) : null}
        {showStats ? (
          <span className="mt-0.5 flex min-w-0 flex-wrap gap-1 text-xs leading-3">
            <span className="rounded border border-current/15 px-1 py-0.5 opacity-75">
              {t("nl2sql.ontology.nodeStats.columns", { count: stats?.columnCount ?? 0 })}
            </span>
            {stats?.joinCount ? (
              <span className="rounded border border-current/15 px-1 py-0.5 opacity-75">
                {t("nl2sql.ontology.nodeStats.joins", { count: stats.joinCount })}
              </span>
            ) : null}
          </span>
        ) : null}
      </span>
      <Handle id="s-right" type="source" position={Position.Right} style={HIDDEN_HANDLE_STYLE} />
      <Handle id="s-bottom" type="source" position={Position.Bottom} style={HIDDEN_HANDLE_STYLE} />
    </div>
  );
});

// 型マップはモジュールレベル定数にする(インライン定義だと毎レンダで全要素が再マウントされる)
const NODE_TYPES = { ontology: OntologyNodeCard };
const EDGE_TYPES = { ontologyParallel: OntologyParallelEdge };

function FlowControls({
  onFitAll,
  onResetLayout,
  resetDisabled,
}: {
  onFitAll: () => void;
  onResetLayout: () => void;
  resetDisabled: boolean;
}) {
  const flow = useReactFlow();
  return (
    <div className="absolute right-3 top-3 z-10 flex gap-1 rounded-md border border-border bg-surface p-1 shadow-sm">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        aria-label={t("nl2sql.ontology.graphZoomIn")}
        tooltip={t("nl2sql.ontology.graphZoomIn")}
        onClick={() => void flow.zoomIn({ duration: 0 })} icon={Plus}>
        </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        aria-label={t("nl2sql.ontology.graphZoomOut")}
        tooltip={t("nl2sql.ontology.graphZoomOut")}
        onClick={() => void flow.zoomOut({ duration: 0 })} icon={Minus}>
        </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        aria-label={t("nl2sql.ontology.graphFit")}
        tooltip={t("nl2sql.ontology.graphFit")}
        onClick={onFitAll} icon={Maximize2}>
        </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        aria-label={t("nl2sql.ontology.graphResetLayout")}
        tooltip={t("nl2sql.ontology.graphResetLayout")}
        disabled={resetDisabled}
        onClick={onResetLayout}
        data-testid="ontology-graph-reset-layout" icon={RotateCcw}>
        </Button>
    </div>
  );
}

function LegendSwatch({ kind }: { kind: string }) {
  return (
    <span
      className="h-2.5 w-2.5 shrink-0 rounded-sm border border-current/20"
      style={{ background: cssVar(nodeFillVar(kind)) }}
      aria-hidden="true"
    />
  );
}

/**
 * 凡例: 表示中グラフに存在する種別グループのみを表示し、クリックで種別フィルタを
 * トグルする(Bloom 流)。凡例=フィルタなので role="group" + aria-pressed を付ける。
 */
function OntologyGraphLegend({
  presentGroupIds,
  disabledGroupIds,
  onToggleGroup,
}: {
  presentGroupIds: Set<string>;
  disabledGroupIds: Set<string>;
  onToggleGroup: (groupId: string) => void;
}) {
  const groups = LEGEND_GROUPS.filter((group) => presentGroupIds.has(group.id));
  if (groups.length === 0) return null;
  return (
    <div
      className="absolute bottom-3 left-3 z-10 max-w-[calc(100%-1.5rem)] rounded-md border border-border bg-surface px-2 py-1.5 shadow-sm"
      role="group"
      aria-label={t("nl2sql.ontology.legendLabel")}
      data-testid="ontology-graph-legend"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-4 text-fg-muted">
        {groups.map((group) => {
          const enabled = !disabledGroupIds.has(group.id);
          return (
            <button
              key={group.id}
              type="button"
              aria-pressed={enabled}
              title={t("nl2sql.ontology.legendToggleHint")}
              className={cn(
                "inline-flex cursor-pointer items-center gap-1 rounded px-1 py-0.5 transition-colors motion-reduce:transition-none",
                enabled ? "hover:bg-surface-hover" : "opacity-40 line-through hover:opacity-60"
              )}
              onClick={() => onToggleGroup(group.id)}
              data-testid={`ontology-graph-legend-${group.id}`}
            >
              <LegendSwatch kind={group.swatchKind} />
              {t(group.labelKey)}
            </button>
          );
        })}
        <span className="inline-flex items-center gap-1 whitespace-normal px-1">
          <span
            className="h-px w-5 shrink-0 border-t"
            style={{ borderColor: cssVar("--color-graph-line") }}
            aria-hidden="true"
          />
          {t("nl2sql.ontology.legend.mapping")}
        </span>
      </div>
    </div>
  );
}

function normalize(text: string): string {
  return text.normalize("NFKC").toLowerCase();
}

function objectNodeStats(graph: OntologyGraph): Map<string, { columnCount: number; joinCount: number }> {
  const stats = new Map<string, { columnCount: number; joinCount: number }>();
  const ensure = (key: string) => {
    const current = stats.get(key) ?? { columnCount: 0, joinCount: 0 };
    stats.set(key, current);
    return current;
  };
  for (const node of graph.nodes) {
    if (node.kind !== "column") continue;
    const objectKey = ontologyGraphObjectClusterKey(node);
    if (objectKey) ensure(objectKey).columnCount += 1;
  }
  const objectKeyByNodeId = new Map(
    graph.nodes
      .map((node) => [node.id, ontologyGraphObjectClusterKey(node)] as const)
      .filter((entry): entry is readonly [string, string] => Boolean(entry[1]))
  );
  const counted = new Set<string>();
  for (const edge of graph.edges) {
    if ((edge.join_conditions ?? []).length === 0) continue;
    for (const nodeId of [edge.source_node_id, edge.target_node_id]) {
      const objectKey = objectKeyByNodeId.get(nodeId);
      if (!objectKey) continue;
      const countKey = `${edge.id}\u0000${objectKey}`;
      if (counted.has(countKey)) continue;
      counted.add(countKey);
      ensure(objectKey).joinCount += 1;
    }
  }
  return stats;
}

function GraphModeControl({
  mode,
  onChange,
}: {
  mode: OntologyGraphViewMode;
  onChange: (mode: OntologyGraphViewMode) => void;
}) {
  return (
    <div
      className="pointer-events-auto flex min-h-[var(--control-height-lg)] min-w-0 max-w-full flex-wrap items-center gap-1 rounded-md border border-border bg-surface px-1 shadow-sm"
      role="group"
      aria-label={t("nl2sql.ontology.graphMode.label")}
      data-testid="ontology-graph-view-mode"
    >
      {GRAPH_VIEW_MODES.map((item) => {
        const Icon = item.icon;
        const selected = item.id === mode;
        return (
          <button
            key={item.id}
            type="button"
            aria-pressed={selected}
            className={cn(
              "inline-flex h-[36px] cursor-pointer items-center gap-1 rounded px-2 text-xs font-medium transition-colors motion-reduce:transition-none sm:h-[32px]",
              selected ? "bg-accent-emphasis text-fg-on-accent" : "text-fg-muted hover:bg-surface-hover hover:text-fg"
            )}
            onClick={() => onChange(item.id)}
            data-testid={`ontology-graph-mode-${item.id}`}
          >
            <Icon size={14} aria-hidden="true" />
            <span>{t(item.labelKey)}</span>
          </button>
        );
      })}
    </div>
  );
}

function GraphToolbarSearchField({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const label = t("nl2sql.ontology.graphSearch");
  return (
    <label
      className="pointer-events-auto flex h-[var(--control-height-lg)] w-full min-w-0 max-w-full items-center gap-2 rounded-md border border-border bg-surface px-3 text-sm text-fg shadow-sm transition-colors focus-within:border-focus-ring focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus-ring sm:w-72 sm:max-w-[18rem]"
      data-testid="ontology-graph-search-field"
    >
      <Search size={16} aria-hidden="true" className="shrink-0 text-fg-muted" />
      {/* 一覧の絞り込みではなく、グラフ上の一致した概念を強調・順に移動する検索（行を減らさない）。ツールバーの
          他の操作と同じ枠の中に置く専用の見た目のため SearchField にしない（UX 契約 page-archetypes.md の例外。#535）。 */}
      {/* oxlint-disable-next-line design-system/restricted-syntax */}
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          // Escape で検索をクリアする(入力が空のときはブラウザ既定に任せる)。
          // IME の変換中の Escape は変換の取り消しなので消さない（#535）。
          if (event.key === "Escape" && value && !isImeComposing(event)) {
            event.preventDefault();
            event.stopPropagation();
            onChange("");
          }
        }}
        placeholder={label}
        aria-label={label}
        className="min-w-0 flex-1 appearance-none border-0 bg-transparent p-0 text-sm leading-5 text-fg shadow-none placeholder:text-fg-muted focus:border-transparent focus:shadow-none focus-visible:shadow-none [&::-webkit-search-cancel-button]:appearance-none [&::-webkit-search-decoration]:appearance-none"
        style={{ WebkitAppearance: "none", boxShadow: "none" }}
        data-testid="ontology-graph-search"
      />
    </label>
  );
}

function LaneOverlays({ lanes }: { lanes: OntologyGraphSemanticLane[] }) {
  return (
    <ViewportPortal>
      {lanes.map((lane) => (
        <div
          key={lane.id}
          className="pointer-events-none absolute flex items-start"
          data-testid={`ontology-graph-lane-${lane.id}`}
          style={{
            // React Flow のビューポート座標（ズームに追従する幾何値）なので px で指定する。
            // oxlint-disable-next-line design-system/restricted-syntax
            transform: `translate(18px, ${lane.y + 2}px)`,
            height: lane.height,
            width: 176,
          }}
        >
          <span data-ontology-lane-label className="rounded-md border border-border bg-surface px-2 py-1 text-xs font-semibold leading-4 text-fg-muted shadow-sm">
            {t(LANE_LABEL_KEYS[lane.id])}
          </span>
        </div>
      ))}
    </ViewportPortal>
  );
}

function OntologyFlow({
  graph,
  workspaceKey = "canvas",
  selectedNodeId,
  selectedEdgeId,
  onSelectNode,
  onSelectEdge,
  highlightNodeIds,
  highlightEdgeIds,
  viewMode,
  defaultViewMode,
  onViewModeChange,
}: OntologyGraphCanvasProps) {
  const flow = useReactFlow();
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [canvasVisible, setCanvasVisible] = useState(false);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const observer = new ResizeObserver(([entry]) => {
      setCanvasVisible(entry.contentRect.width > 0 && entry.contentRect.height > 0);
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);
  const stateKey = `ontology-graph:${workspaceKey}:${graph.revision?.id ?? graph.revision_id ?? "current"}`;
  const [search, setSearch] = useWorkspaceState(`${stateKey}:search`, "");
  const [conceptKind, setConceptKind] = useWorkspaceState(`${stateKey}:concept`, "");
  const [searchCursor, setSearchCursor] = useState(0);
  const [showDetails, setShowDetails] = useState(false);
  const [disabledLegendGroups, setDisabledLegendGroups] = useState<Set<string>>(new Set());
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  // ドラッグによる座標上書き。レイアウトは決定論のまま、差分だけを保持する
  const [positionOverrides, setPositionOverrides] = useState<Map<string, GraphPoint>>(
    () => new Map()
  );
  // React Flow が実測したノードサイズ。`measured` として返さないと配列再生成のたびに
  // handleBounds がリセットされ、全エッジが 1 フレーム消えて点滅する(controlled flow の契約)
  const [measuredById, setMeasuredById] = useState<Map<string, OntologyNodeDimensions>>(
    () => new Map()
  );
  const [internalViewMode, setInternalViewMode] = useState<OntologyGraphViewMode>(
    defaultViewMode ?? "all"
  );
  const currentViewMode = viewMode ?? internalViewMode;
  const changeViewMode = (nextMode: OntologyGraphViewMode) => {
    setInternalViewMode(nextMode);
    onViewModeChange?.(nextMode);
  };

  const externalHighlight =
    (highlightNodeIds?.length ?? 0) > 0 || (highlightEdgeIds?.length ?? 0) > 0;
  const query = normalize(search.trim());

  // view-mode 適用後・詳細フィルタ前のグラフ。検索は非表示の詳細ノードも対象にする。
  const scopedGraph = useMemo<OntologyGraph>(
    () => conceptGraph(ontologyGraphForViewMode(graph, currentViewMode, highlightNodeIds, highlightEdgeIds), currentViewMode === "physical_er" ? "" : conceptKind),
    [graph, currentViewMode, highlightNodeIds, highlightEdgeIds, conceptKind]
  );
  // トグルに出す件数は view-mode 適用後を数える(接地パスで実際に出る列数と一致させる)。
  const detailCount = useMemo(
    () => scopedGraph.nodes.filter((node) => isOntologyDetailNodeKind(node.kind)).length,
    [scopedGraph]
  );
  const searchMatchedNodeIds = useMemo(() => {
    if (!query) return [] as string[];
    return scopedGraph.nodes
      .filter((node) =>
        ontologyNodeSearchValues(node).some((name) => normalize(name).includes(query))
      )
      .map((node) => node.id);
  }, [query, scopedGraph]);

  const disabledKinds = useMemo(() => {
    const kinds = new Set<string>();
    for (const group of LEGEND_GROUPS) {
      if (!disabledLegendGroups.has(group.id)) continue;
      for (const kind of group.kinds) kinds.add(kind);
    }
    return kinds;
  }, [disabledLegendGroups]);

  const visibleGraph = useMemo<OntologyGraph>(() => {
    // 検索一致した詳細ノードは強制表示する(検索が「見えないものに当たって無反応」にならない)
    const forcedDetails = new Set<string>([
      ...(highlightNodeIds ?? []),
      ...searchMatchedNodeIds,
    ]);
    if (selectedNodeId) forcedDetails.add(selectedNodeId);
    const withDetails = ontologyGraphWithDetailVisibility(
      scopedGraph,
      showDetails || detailCount === 0,
      forcedDetails
    );
    if (disabledKinds.size === 0) return withDetails;
    const keep = new Set(
      withDetails.nodes.filter((node) => !disabledKinds.has(node.kind)).map((node) => node.id)
    );
    return {
      ...withDetails,
      nodes: withDetails.nodes.filter((node) => keep.has(node.id)),
      edges: withDetails.edges.filter(
        (edge) => keep.has(edge.source_node_id) && keep.has(edge.target_node_id)
      ),
    };
  }, [
    scopedGraph,
    highlightNodeIds,
    searchMatchedNodeIds,
    selectedNodeId,
    showDetails,
    detailCount,
    disabledKinds,
  ]);

  const semanticLayout = useMemo(
    () =>
      layoutOntologyGraphSemanticMatrix(visibleGraph, {
        nodeWidth: NODE_WIDTH,
        nodeHeight: NODE_HEIGHT,
      }),
    [visibleGraph]
  );
  // 決定論レイアウトへドラッグ上書きをマージした実効座標(表示中ノードのみ適用)
  const effectivePositions = useMemo(() => {
    if (positionOverrides.size === 0) return semanticLayout.positions;
    const merged = new Map(semanticLayout.positions);
    for (const [nodeId, position] of positionOverrides) {
      if (merged.has(nodeId)) merged.set(nodeId, position);
    }
    return merged;
  }, [semanticLayout.positions, positionOverrides]);
  const statsByObject = useMemo(() => objectNodeStats(graph), [graph]);

  const onNodesChange = useCallback((changes: NodeChange<Node<OntologyNodeData>>[]) => {
    setPositionOverrides((current) => applyOntologyNodePositionChanges(current, changes));
    setMeasuredById((current) => applyOntologyNodeDimensionChanges(current, changes));
  }, []);
  // レイアウトの意味が変わるビューモード切替をまたいだ上書きの持ち越しは混乱の元なので破棄
  const lastViewModeRef = useRef(currentViewMode);
  useEffect(() => {
    if (lastViewModeRef.current === currentViewMode) return;
    lastViewModeRef.current = currentViewMode;
    setPositionOverrides(new Map());
  }, [currentViewMode]);
  const fitAll = useCallback((duration = 0) => {
    const canvas = canvasRef.current;
    const visibleNodes = flow.getNodes();
    if (!canvas || visibleNodes.length === 0) return;
    const bounds = flow.getNodesBounds(visibleNodes);
    let left = bounds.x, top = bounds.y;
    let right = bounds.x + bounds.width, bottom = bounds.y + bounds.height;
    // ViewportPortal のラベルは fitView の対象外。実測範囲を graph 座標に戻して加える。
    for (const label of canvas.querySelectorAll("[data-ontology-lane-label]")) {
      const rect = label.getBoundingClientRect();
      const start = flow.screenToFlowPosition({ x: rect.left, y: rect.top }, { snapToGrid: false });
      const end = flow.screenToFlowPosition({ x: rect.right, y: rect.bottom }, { snapToGrid: false });
      left = Math.min(left, start.x);
      top = Math.min(top, start.y);
      right = Math.max(right, end.x);
      bottom = Math.max(bottom, end.y);
    }
    void flow.fitBounds({ x: left, y: top, width: right - left, height: bottom - top }, { padding: 0.18, duration });
  }, [flow]);
  const resetLayout = () => {
    setPositionOverrides(new Map());
    // 上書きクリアの再レンダ後にフィットする(即時だと旧座標でフィットしてしまう)
    window.setTimeout(() => {
      fitAll(prefersReducedMotion() ? 0 : 300);
    }, 80);
  };

  // ホバー中は隣接ノード・エッジを残して他を減光する(接地/検索の強調中は無効)
  const hoverNeighborIds = useMemo(() => {
    if (!hoveredNodeId) return null;
    const neighbors = new Set([hoveredNodeId]);
    for (const edge of visibleGraph.edges) {
      if (edge.source_node_id === hoveredNodeId) neighbors.add(edge.target_node_id);
      else if (edge.target_node_id === hoveredNodeId) neighbors.add(edge.source_node_id);
    }
    return neighbors;
  }, [hoveredNodeId, visibleGraph.edges]);

  // 強調は 2 チャネルを合成する: 接地ハイライト(枠・塗り)と検索一致(リング)。
  // 以前は接地表示中に検索が無反応だったため、両者を独立チャネルとして常に効かせる。
  const emphasis = useMemo(() => {
    const highlightSet = new Set(highlightNodeIds ?? []);
    const highlightEdgeSet = new Set(highlightEdgeIds ?? []);
    const visibleIds = new Set(visibleGraph.nodes.map((node) => node.id));
    const searchSet = new Set(searchMatchedNodeIds.filter((id) => visibleIds.has(id)));
    const searchEdgeSet = new Set(
      query
        ? visibleGraph.edges
            .filter(
              (edge) =>
                (searchSet.has(edge.source_node_id) && searchSet.has(edge.target_node_id)) ||
                normalize(edge.relationship_name_ja).includes(query)
            )
            .map((edge) => edge.id)
        : []
    );
    return {
      active: externalHighlight || Boolean(query),
      highlightNodes: highlightSet,
      highlightEdges: highlightEdgeSet,
      searchNodes: searchSet,
      searchEdges: searchEdgeSet,
    };
  }, [
    externalHighlight,
    highlightNodeIds,
    highlightEdgeIds,
    query,
    searchMatchedNodeIds,
    visibleGraph,
  ]);
  // ホバー減光は接地・検索の強調が非アクティブのときだけ効かせる第 3 の一時チャネル
  const hoverDimActive = Boolean(hoverNeighborIds) && !emphasis.active;

  // 検索一致のジャンプナビゲーション(レイアウト位置順)
  const orderedSearchMatches = useMemo(() => {
    return searchMatchedNodeIds
      .filter((id) => effectivePositions.has(id))
      .sort((a, b) => {
        const pa = effectivePositions.get(a)!;
        const pb = effectivePositions.get(b)!;
        return pa.y - pb.y || pa.x - pb.x;
      });
  }, [searchMatchedNodeIds, effectivePositions]);
  // 検索語が変わったらジャンプ位置を先頭へ戻す（render 中に同期する）。
  if (useValuesChanged([query])) setSearchCursor(0);
  const jumpToSearchMatch = (direction: 1 | -1) => {
    if (orderedSearchMatches.length === 0) return;
    const next =
      (searchCursor + direction + orderedSearchMatches.length) % orderedSearchMatches.length;
    setSearchCursor(next);
    const nodeId = orderedSearchMatches[next];
    const position = effectivePositions.get(nodeId);
    if (!position) return;
    onSelectNode?.(nodeId);
    void flow.setCenter(position.x + NODE_WIDTH / 2, position.y + NODE_HEIGHT / 2, {
      zoom: Math.max(flow.getZoom(), 0.9),
      duration: prefersReducedMotion() ? 0 : 250,
    });
  };

  // 可視ノード集合・接地ハイライトが変わったら自動リフィットする
  // (React Flow の fitView prop は初期化時のみのため、モード切替や接地実行の
  //  結果が画面外に残る問題への対処)。
  const visibleSignature = useMemo(
    () => visibleGraph.nodes.map((node) => node.id).sort().join("|"),
    [visibleGraph.nodes]
  );
  const highlightSignature = useMemo(
    () => [...(highlightNodeIds ?? [])].sort().join("|"),
    [highlightNodeIds]
  );
  const layoutPositionsRef = useRef(effectivePositions);
  const highlightNodeIdsRef = useRef(highlightNodeIds);
  // 最新の配置とハイライトを commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => {
    layoutPositionsRef.current = effectivePositions;
    highlightNodeIdsRef.current = highlightNodeIds;
  });
  useEffect(() => {
    if (!canvasVisible) return;
    // React Flow が新しいノード集合を測り終えるのを待ってからフィットする
    const timer = window.setTimeout(() => {
      const highlightTargets = (highlightNodeIdsRef.current ?? []).filter((id) =>
        layoutPositionsRef.current.has(id)
      );
      if (highlightTargets.length > 0) {
        void flow.fitView({
          padding: 0.18,
          duration: prefersReducedMotion() ? 0 : 300,
          nodes: highlightTargets.map((id) => ({ id })),
        });
      } else {
        fitAll(prefersReducedMotion() ? 0 : 300);
      }
    }, 80);
    return () => window.clearTimeout(timer);
  }, [visibleSignature, highlightSignature, flow, canvasVisible, fitAll]);

  // インスペクタ等の外部選択で対象が画面外のときだけ、そのノードへセンタリングする
  // (fitView での全体リセットはしない。ズームは現状維持ベース)。
  useEffect(() => {
    if (!selectedNodeId || !canvasVisible) return;
    const position = layoutPositionsRef.current.get(selectedNodeId);
    const container = canvasRef.current;
    if (!position || !container) return;
    const { x, y, zoom } = flow.getViewport();
    const centerX = (position.x + NODE_WIDTH / 2) * zoom + x;
    const centerY = (position.y + NODE_HEIGHT / 2) * zoom + y;
    const rect = container.getBoundingClientRect();
    const margin = 24;
    const visible =
      centerX >= margin &&
      centerX <= rect.width - margin &&
      centerY >= margin &&
      centerY <= rect.height - margin;
    if (visible) return;
    void flow.setCenter(position.x + NODE_WIDTH / 2, position.y + NODE_HEIGHT / 2, {
      zoom: Math.max(flow.getZoom(), 0.8),
      duration: prefersReducedMotion() ? 0 : 250,
    });
  }, [selectedNodeId, flow, canvasVisible]);

  // スクリーンリーダー向けの選択通知文(aria-live)。選択解除時は空にする
  const selectedNodeAnnouncement = useMemo(() => {
    if (!selectedNodeId) return "";
    const node = graph.nodes.find((item) => item.id === selectedNodeId);
    if (!node) return "";
    return t("nl2sql.ontology.graphSelectionAnnouncement", {
      label: ontologyNodeDisplay(node, { highlighted: false }).ariaLabel,
    });
  }, [selectedNodeId, graph.nodes]);

  const presentLegendGroupIds = useMemo(() => {
    const present = new Set<string>();
    for (const node of scopedGraph.nodes) {
      for (const group of LEGEND_GROUPS) {
        if (group.kinds.includes(node.kind)) present.add(group.id);
      }
    }
    return present;
  }, [scopedGraph.nodes]);
  const toggleLegendGroup = (groupId: string) => {
    setDisabledLegendGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  };

  const nodes = useMemo<Node<OntologyNodeData>[]>(
    () =>
      visibleGraph.nodes.map((node) => {
        const highlighted = emphasis.highlightNodes.has(node.id);
        const searchMatched = emphasis.searchNodes.has(node.id);
        const hoverDimmed = hoverDimActive && !hoverNeighborIds!.has(node.id);
        const measured = measuredById.get(node.id);
        return {
          id: node.id,
          type: "ontology",
          position: effectivePositions.get(node.id) ?? { x: 0, y: 0 },
          // 実測サイズを返して handleBounds を保持させる(無いと再生成のたびにエッジが点滅)
          ...(measured ? { measured } : {}),
          data: {
            node,
            highlighted,
            searchMatched,
            dimmed: (emphasis.active && !highlighted && !searchMatched) || hoverDimmed,
            stats: statsByObject.get(ontologyGraphObjectClusterKey(node) ?? ""),
          },
          ariaLabel: ontologyNodeDisplay(node, { highlighted }).ariaLabel,
          selected: node.id === selectedNodeId,
          style: { width: NODE_WIDTH, minHeight: NODE_HEIGHT, padding: 0, border: "none" },
        };
      }),
    [
      visibleGraph.nodes,
      effectivePositions,
      selectedNodeId,
      emphasis,
      statsByObject,
      hoverDimActive,
      hoverNeighborIds,
      measuredById,
    ]
  );

  const edges = useMemo<OntologyFlowEdge[]>(() => {
    const kindById = new Map(visibleGraph.nodes.map((node) => [node.id, node.kind]));
    // 第 1 パス: 実効座標からハンドル方向を決める。schema 絡み・包含・同一レーン別行は
    // 縦優先にし、同じ行のノードを真横に貫通する bezier を出さない。
    const prepared = visibleGraph.edges.map((edge) => {
      const sourcePosition = effectivePositions.get(edge.source_node_id);
      const targetPosition = effectivePositions.get(edge.target_node_id);
      let handles: OntologyEdgeHandleSelection | null = null;
      if (sourcePosition && targetPosition) {
        const sameLane =
          semanticLayout.laneByNodeId.get(edge.source_node_id) ===
          semanticLayout.laneByNodeId.get(edge.target_node_id);
        const preferVertical =
          kindById.get(edge.source_node_id) === "schema" ||
          kindById.get(edge.target_node_id) === "schema" ||
          isOntologyContainmentEdge(edge) ||
          (sameLane && Math.abs(targetPosition.y - sourcePosition.y) >= NODE_HEIGHT);
        handles = selectOntologyEdgeHandles(sourcePosition, targetPosition, { preferVertical });
      }
      return { edge, targetPosition, handles };
    });

    // 縦エッジは source ごとに水平セグメント高さ(stepPosition)を扇状に散らし、
    // schema→表のような 1:N の線・ラベルが同じ高さで重ならないようにする
    const verticalBySource = new Map<string, typeof prepared>();
    for (const item of prepared) {
      if (item.handles?.orientation !== "vertical") continue;
      const group = verticalBySource.get(item.edge.source_node_id) ?? [];
      group.push(item);
      verticalBySource.set(item.edge.source_node_id, group);
    }
    const fanStepByEdgeId = new Map<string, number>();
    for (const group of verticalBySource.values()) {
      const sorted = [...group].sort(
        (a, b) =>
          (a.targetPosition?.x ?? 0) - (b.targetPosition?.x ?? 0) ||
          a.edge.id.localeCompare(b.edge.id, "en-US")
      );
      sorted.forEach((item, index) => {
        fanStepByEdgeId.set(
          item.edge.id,
          sorted.length === 1 ? 0.5 : 0.3 + (0.4 * index) / (sorted.length - 1)
        );
      });
    }

    // 同一ノードペア間の並行エッジ(向き無視)を数え、経路とラベルの完全重なりを防ぐ
    const pairKeyOf = (edge: OntologyGraph["edges"][number]) =>
      [edge.source_node_id, edge.target_node_id].sort().join("::");
    const pairCounts = new Map<string, number>();
    for (const item of prepared) {
      const key = pairKeyOf(item.edge);
      pairCounts.set(key, (pairCounts.get(key) ?? 0) + 1);
    }
    const pairSeen = new Map<string, number>();

    return prepared.map(({ edge, handles }) => {
      const highlighted =
        emphasis.highlightEdges.has(edge.id) || emphasis.searchEdges.has(edge.id);
      const selected = edge.id === selectedEdgeId;
      const showFullLabel = highlighted || selected || edge.id === hoveredEdgeId;
      // ER 図流: join を持つ関係は hover を待たずカーディナリティを常時表示する
      const persistentLabel = isOntologyJoinEdge(edge)
        ? cardinalityShortLabel(edge.cardinality)
        : "";
      const mappingEdge = isOntologyMappingEdge(edge);
      const pairKey = pairKeyOf(edge);
      const parallelCount = pairCounts.get(pairKey) ?? 1;
      const parallelIndex = pairSeen.get(pairKey) ?? 0;
      pairSeen.set(pairKey, parallelIndex + 1);
      const centeredParallel = parallelIndex - (parallelCount - 1) / 2;

      let edgeType: string | undefined;
      let pathOptions: OntologyFlowEdge["pathOptions"];
      let edgeData: OntologyFlowEdge["data"];
      if (handles?.orientation === "vertical") {
        // レーン間・schema→表(縦)は直角の smoothstep で ER 図らしく描く
        edgeType = "smoothstep";
        const fanStep = fanStepByEdgeId.get(edge.id) ?? 0.5;
        pathOptions = {
          borderRadius: 8,
          offset: 12 + 8 * parallelIndex,
          stepPosition: Math.min(
            0.85,
            Math.max(0.15, fanStep + (parallelCount > 1 ? centeredParallel * 0.16 : 0))
          ),
        };
      } else if (handles && parallelCount > 1) {
        // 同 y の並行エッジは直線に潰れるため、法線オフセット付きの弧で分離する
        edgeType = "ontologyParallel";
        edgeData = { parallelOffset: centeredParallel * 20 };
      }
      const hoverDimmed =
        hoverDimActive &&
        edge.source_node_id !== hoveredNodeId &&
        edge.target_node_id !== hoveredNodeId;
      return {
        id: edge.id,
        source: edge.source_node_id,
        target: edge.target_node_id,
        ...(handles
          ? { sourceHandle: handles.sourceHandle, targetHandle: handles.targetHandle }
          : {}),
        ...(edgeType ? { type: edgeType } : {}),
        ...(pathOptions ? { pathOptions } : {}),
        ...(edgeData ? { data: edgeData } : {}),
        label: showFullLabel ? edge.relationship_name_ja : persistentLabel || undefined,
        ariaLabel: `${edge.relationship_name_ja}${
          emphasis.highlightEdges.has(edge.id) ? t("nl2sql.ontology.edgeGroundedSuffix") : ""
        }`,
        markerEnd: { type: MarkerType.ArrowClosed, color: cssVar("--color-graph-line") },
        // hover/選択中のエッジラベルは他エッジより前面に出す
        ...(showFullLabel ? { zIndex: 1000 } : {}),
        style: {
          stroke: highlighted || selected ? cssVar("--color-accent-fg") : edgeStroke(edge),
          strokeWidth: highlighted || selected
            ? 2.5
            : edge.validation_status === "blocked"
              ? 2
              : mappingEdge
                ? 1
                : 1.25,
          // 「対応」(maps_to)は点線で FK join の実線と区別。proposed の点線は従来どおり優先
          strokeDasharray:
            edge.review_status === "proposed" ? "5 4" : mappingEdge ? "4 3" : undefined,
          opacity: emphasis.active && !highlighted ? 0.3 : hoverDimmed ? 0.35 : 1,
        },
        labelStyle: { fill: cssVar("--color-fg-muted"), fontSize: cssVar("--font-size-xs"), fontWeight: 600 },
        labelBgStyle: { fill: cssVar("--color-surface"), fillOpacity: 0.92 },
        labelBgPadding: [6, 3] as [number, number],
        labelBgBorderRadius: 4,
      };
    });
  }, [
    visibleGraph,
    emphasis,
    hoveredEdgeId,
    selectedEdgeId,
    effectivePositions,
    semanticLayout.laneByNodeId,
    hoverDimActive,
    hoveredNodeId,
  ]);

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-2">
      {/* ツールバーはキャンバス外(上部)に置き、フィット時にノードと重ならないようにする。
          高さはツールバーの全部品を lg（40px、タッチ端末は 44px）にそろえる（画面幅ではなく入力方式で決める。#613）。 */}
      <div className="flex flex-wrap items-end gap-2" data-testid="ontology-graph-toolbar">
        {/* 種類の選択は「共有プロパティ（Shared Property）」が入る md の段（#1891。以前は手書きの 18rem）。 */}
        <label className="grid min-w-0 max-w-full gap-1 text-xs text-fg-muted max-sm:w-full">{t("markdownOntology.kindFilter")}
          {/* oxlint-disable-next-line design-system/restricted-syntax -- #631: 主な概念 / 補助の概念の <optgroup> と、グラフに無い種類を選べない選択肢（<option disabled>）で示すため、SelectField で表せない */}
          <select aria-label={t("markdownOntology.kindFilter")} className={fieldControlClassName({ size: "lg", width: "md", className: "min-w-0" })} value={conceptKind} onChange={e=>setConceptKind(e.target.value)}>
            <option value="">{t("markdownOntology.allConcepts")}</option>
            {[conceptKinds.slice(0,6),conceptKinds.slice(6)].map((kinds,i)=><optgroup key={i} label={t(i?"markdownOntology.auxConcepts":"markdownOntology.mainConcepts")}>
              {kinds.map(kind=>{const exists=kind === "link_type" ? graph.edges.some(e=>e.kind === "link_type" || e.kind === "business_relationship") : graph.nodes.some(n=>nodeConceptKind(n.kind) === kind); return <option key={kind} value={kind} disabled={!exists}>{conceptLabel(kind)}{exists?"":` · ${t("markdownOntology.noConcept")}`}</option>;})}
            </optgroup>)}
          </select>
        </label>
        <GraphModeControl mode={currentViewMode} onChange={changeViewMode} />
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1">
          <GraphToolbarSearchField value={search} onChange={setSearch} />
          {query ? (
            <div
              className="flex h-[var(--control-height-lg)] items-center gap-0.5 rounded-md border border-border bg-surface px-1 shadow-sm"
              data-testid="ontology-graph-search-nav"
            >
              <span className="px-1 text-xs tabular-nums text-fg-muted" aria-live="polite">
                {orderedSearchMatches.length === 0
                  ? t("nl2sql.ontology.graphSearchNoMatch")
                  : `${searchCursor + 1} / ${orderedSearchMatches.length}`}
              </span>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label={t("nl2sql.ontology.graphSearchPrev")}
                tooltip={t("nl2sql.ontology.graphSearchPrev")}
                disabled={orderedSearchMatches.length === 0}
                onClick={() => jumpToSearchMatch(-1)}
                data-testid="ontology-graph-search-prev" icon={ChevronLeft}>
                </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label={t("nl2sql.ontology.graphSearchNext")}
                tooltip={t("nl2sql.ontology.graphSearchNext")}
                disabled={orderedSearchMatches.length === 0}
                onClick={() => jumpToSearchMatch(1)}
                data-testid="ontology-graph-search-next" icon={ChevronRight}>
                </Button>
            </div>
          ) : null}
        </div>
        {query && scopedGraph.edges.filter(e=>[e.relationship_name_ja,e.description_ja,String(e.metadata?.api_name ?? "")].some(value=>normalize(value ?? "").includes(query))).map(edge=><Button type="button" key={edge.id} size="sm" variant="secondary" onClick={()=>onSelectEdge?.(edge.id)}>{edge.relationship_name_ja}</Button>)}
        {detailCount > 0 ? (
          <label
            className="flex h-[var(--control-height-lg)] min-w-0 max-w-full cursor-pointer items-center gap-2 rounded-md border border-border bg-surface px-3 text-xs text-fg shadow-sm transition-colors focus-within:border-focus-ring"
            data-testid="ontology-graph-details-toggle-field"
          >
            <input
              type="checkbox"
              className="h-4 w-4 accent-accent-emphasis"
              checked={showDetails}
              onChange={(event) => setShowDetails(event.currentTarget.checked)}
              data-testid="ontology-graph-details-toggle"
            />
            <span>{t("nl2sql.ontology.graphShowDetails", { count: detailCount })}</span>
          </label>
        ) : null}
      </div>
      <div
        ref={canvasRef}
        data-testid="ontology-graph-canvas"
        className="relative h-[56rem] min-h-80 overflow-hidden rounded-md border border-border bg-surface-sunken"
      >
      {canvasVisible && <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.05}
        maxZoom={1.8}
        // 初期レイアウトは決定論(semantic matrix)。ドラッグ差分だけを positionOverrides に
        // 反映する controlled flow(内部選択は無効のまま。選択は onNodeClick + selected prop)。
        nodesDraggable
        onNodesChange={onNodesChange}
        // 既定 1px だと微小な手ぶれがドラッグ扱いになりクリック選択が不発になる
        nodeDragThreshold={4}
        nodesConnectable={false}
        elementsSelectable={false}
        nodesFocusable
        edgesFocusable
        onlyRenderVisibleElements={visibleGraph.nodes.length > 150}
        onNodeClick={(_event, node) => onSelectNode?.(node.id)}
        onNodeMouseEnter={(_event, node) => setHoveredNodeId(node.id)}
        onNodeMouseLeave={() => setHoveredNodeId(null)}
        onEdgeClick={(_event, edge) => onSelectEdge?.(edge.id)}
        onEdgeMouseEnter={(_event, edge) => setHoveredEdgeId(edge.id)}
        onEdgeMouseLeave={() => setHoveredEdgeId(null)}
        proOptions={{ hideAttribution: true }}
      >
        <Background color={cssVar("--color-border")} gap={20} size={1} />
        <LaneOverlays lanes={semanticLayout.lanes} />
        {visibleGraph.nodes.length > 12 ? (
          // 小規模グラフでは全体が一目で見えるため出さない(白い矩形ノイズを避ける)
          <MiniMap
            pannable
            zoomable
            position="bottom-right"
            aria-label={t("nl2sql.ontology.graphMinimap")}
            style={{ width: 140, height: 90 }}
            bgColor={cssVar("--color-surface")}
            maskColor="color-mix(in srgb, var(--color-border) 45%, transparent)"
            // 種別(型チャネル)の塗りをミニマップにも反映し、縮小表示でも構造が読めるようにする
            nodeColor={(node) => {
              const data = node.data as OntologyNodeData | undefined;
              return data?.node ? nodeFill(data.node) : cssVar("--color-graph-line");
            }}
            nodeStrokeColor={cssVar("--color-graph-line")}
          />
        ) : null}
        <FlowControls onFitAll={() => fitAll()} onResetLayout={resetLayout} resetDisabled={positionOverrides.size === 0} />
      </ReactFlow>}
      <OntologyGraphLegend
        presentGroupIds={presentLegendGroupIds}
        disabledGroupIds={disabledLegendGroups}
        onToggleGroup={toggleLegendGroup}
      />
      </div>
      {/* スクリーンリーダー向け: 選択されたノードを読み上げる(視覚は selected 枠で表現済み) */}
      <span
        className="sr-only"
        aria-live="polite"
        data-testid="ontology-graph-selection-announcement"
      >
        {selectedNodeAnnouncement}
      </span>
    </div>
  );
}

export default function OntologyGraphCanvas(props: OntologyGraphCanvasProps) {
  return (
    <ReactFlowProvider>
      <OntologyFlow {...props} />
    </ReactFlowProvider>
  );
}
