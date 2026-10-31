import { useMemo } from "react";
import { ReactFlow, Background, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { t } from "@/lib/i18n";

/** Canonical artifact をそのまま可視化し、別の業務 graph を作らない。 */
export function ProfileOntologyGraph({ artifact }: { artifact: string }) {
  const graph = useMemo(() => {
    try {
      const parsed = JSON.parse(artifact) as { nodes: { id: string; name_ja: string; kind: string }[]; edges: Edge[] };
      const nodes: Node[] = parsed.nodes.map((item, index) => ({ id: item.id, position: { x: (index % 3) * 230, y: Math.floor(index / 3) * 130 }, data: { label: `${item.name_ja}\n${t(`ontologyResults.kind.${item.kind}` as Parameters<typeof t>[0])}` }, style: { width: 200, whiteSpace: "pre-wrap", background: "var(--color-canvas)", color: "var(--color-fg)", borderColor: "var(--color-border)" } }));
      return { nodes, edges: parsed.edges };
    } catch { return { nodes: [], edges: [] }; }
  }, [artifact]);
  return <div className="h-80 min-w-0 overflow-hidden rounded border border-border" role="region" aria-label={t("ontologyWorkspace.graph")}><ReactFlow nodes={graph.nodes} edges={graph.edges} nodesDraggable={false} nodesConnectable={false} fitView minZoom={0.1} maxZoom={2} ariaLabelConfig={{ "node.a11yDescription.default": t("ontologyWorkspace.graphKeyboard"), "edge.a11yDescription.default": t("ontologyWorkspace.graphKeyboard") }}><Background /></ReactFlow></div>;
}
