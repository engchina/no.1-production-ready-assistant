import type { OntologyEdge, OntologyNode } from "./types";

/**
 * ontology グラフの配色を「型チャネル（塗り）」と「状態チャネル（枠/線）」に分離した単一の正。
 * React Flow 版（JS style）と SVG 版の両レンダラが同一マッピングを参照し、
 * 色は共有トークンの CSS 変数（`--color-graph-*` / semantic）を使うのでダークテーマにも追従する。
 * 状態は色だけでなく形状・線幅・破線でも冗長符号化する（color-not-only）。
 */

/** CSS 変数を `var(...)` 参照へ。 */
export const cssVar = (name: string) => `var(${name})`;

/** 型チャネル: ノード背景の塗り（CSS 変数名）。 */
export function nodeFillVar(kind: string): string {
  if (kind === "business_entity" || kind === "business_event") return "--color-graph-entity";
  if (kind === "metric") return "--color-graph-metric";
  if (kind.startsWith("sql_")) return "--color-graph-sql";
  if (kind === "business_term" || kind === "property") return "--color-graph-term";
  return "--color-graph-default";
}

/** 状態チャネル: 検証状態→枠/線色（CSS 変数名, semantic）。型の塗りとは別チャネル。 */
export function statusStrokeVar(status: string | null | undefined): string {
  if (status === "blocked") return "--color-danger-fg";
  if (status === "warning") return "--color-warning-fg";
  if (status === "passed") return "--color-success-fg";
  return "--color-graph-line"; // unreviewed / 未検証 / 既定
}

/** ノードの塗り色（型）。 */
export function nodeFill(node: OntologyNode): string {
  return cssVar(nodeFillVar(node.kind));
}

/** ノードの枠色。選択時は primary、それ以外は検証状態。 */
export function nodeStroke(node: OntologyNode, selected: boolean): string {
  return selected ? cssVar("--color-accent-fg") : cssVar(statusStrokeVar(node.validation_status));
}

/** エッジの線色（検証状態）。選択は色ではなく線幅で表す。 */
export function edgeStroke(edge: OntologyEdge): string {
  return cssVar(statusStrokeVar(edge.validation_status));
}
