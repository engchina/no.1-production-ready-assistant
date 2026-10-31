import { BaseEdge, type EdgeProps } from "@xyflow/react";

import { ontologyParallelEdgeGeometry } from "./graphView";

/**
 * 同一ノードペア間に複数ある horizontal エッジ用のカスタムエッジ。
 * 既定 bezier は同じ y だと直線に潰れて完全に重なるため、`data.parallelOffset`
 * に応じた弧(2 次ベジェ)で分離する。ロジックは graphView の純関数に寄せる。
 */
export default function OntologyParallelEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  label,
  labelStyle,
  labelShowBg,
  labelBgStyle,
  labelBgPadding,
  labelBgBorderRadius,
  interactionWidth,
  data,
}: EdgeProps) {
  const offset = typeof data?.parallelOffset === "number" ? data.parallelOffset : 0;
  const { path, labelX, labelY } = ontologyParallelEdgeGeometry(
    { x: sourceX, y: sourceY },
    { x: targetX, y: targetY },
    offset
  );
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={markerEnd}
      style={style}
      label={label}
      labelX={labelX}
      labelY={labelY}
      labelStyle={labelStyle}
      labelShowBg={labelShowBg}
      labelBgStyle={labelBgStyle}
      labelBgPadding={labelBgPadding}
      labelBgBorderRadius={labelBgBorderRadius}
      interactionWidth={interactionWidth}
    />
  );
}
