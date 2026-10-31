"use client";

import { useMemo, useState } from "react";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Share2 } from "lucide-react";

import { DegradedBanner } from "@/components/DegradedBanner";
import { EmptyState, ErrorState } from "@/components/StateViews";
import { Disclosure, Skeleton, TimedLoadingState } from "@production-ready/ui";
import type { KnowledgeBaseGraphData } from "@/lib/api";
import { useKnowledgeBaseGraph } from "@/lib/queries";
import { t } from "@/lib/i18n";

const NODE_STYLE = {
  background: "var(--color-surface)",
  color: "var(--color-fg)",
  border: "1px solid var(--color-border)",
  borderRadius: "var(--radius-lg)",
  fontSize: "var(--font-size-xs)",
  padding: "var(--space-1-5)",
  maxWidth: 160,
  textAlign: "center" as const,
};

function toFlow(data: KnowledgeBaseGraphData): { nodes: Node[]; edges: Edge[] } {
  const count = Math.max(1, data.nodes.length);
  const columns = Math.ceil(Math.sqrt(count));
  const nodes: Node[] = data.nodes.map((node, index) => {
    return {
      id: node.id,
      position: { x: (index % columns) * 220, y: Math.floor(index / columns) * 100 },
      data: { label: node.name || node.id },
      style: NODE_STYLE,
    };
  });
  const edges: Edge[] = data.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: edge.type === "contains" ? t("knowledgeBases.graph.contains") : edge.type ?? undefined,
    labelStyle: { fill: "var(--color-fg-muted)", fontSize: "var(--font-size-xs)" },
  }));
  return { nodes, edges };
}

/**
 * KB の関係情報(文書と章・節の見出しのつながり)を可視化する読み取り専用ビュー。
 * 展開時のみ subgraph を取得し、@xyflow で entity(node)/relationship(edge) を格子状に配置して描画する。
 */
export function KnowledgeBaseGraphView({ knowledgeBaseId }: { knowledgeBaseId: string }) {
  const [open, setOpen] = useState(false);
  const query = useKnowledgeBaseGraph(open ? knowledgeBaseId : null);
  const flow = useMemo(
    () => (query.data ? toFlow(query.data) : { nodes: [], edges: [] }),
    [query.data]
  );
  // DB が止まっていると backend は空の図と warning_messages を返す。取得できなかったことを
  // 「関係情報がまだありません」と区別して出す（空の案内は、取得できて本当に空のときだけ）。
  const warnings = query.data?.warning_messages ?? [];
  const degraded = warnings.length > 0;
  const isEmpty = query.data && (query.data.status === "empty" || query.data.nodes.length === 0);

  return (
    // 共有の Disclosure（card）。見出しの高さは 40px・タッチ端末 44px（#1135）。関係情報は開いたときだけ取得して描く。
    <Disclosure
      summary={open ? t("knowledgeBases.graph.hide") : t("knowledgeBases.graph.show")}
      icon={Share2}
      open={open}
      onOpenChange={setOpen}
    >
      {open ? (
        <div id="knowledge-base-graph" className="space-y-2">
          <p className="text-xs text-fg-muted">{t("knowledgeBases.graph.hint")}</p>
          {query.isPending ? (
            <TimedLoadingState
              label={t("knowledgeBases.graph.loading")}
              operationKey="knowledge-base-graph-load"
              testId="knowledge-base-graph-loading"
            >
              <Skeleton className="h-[25.71rem] w-full rounded-md" />
            </TimedLoadingState>
          ) : query.isError ? (
            <ErrorState message={t("knowledgeBases.graph.error")} onRetry={() => void query.refetch()} />
          ) : degraded ? (
            <DegradedBanner
              messages={warnings}
              onRetry={() => void query.refetch()}
              isRetrying={query.isFetching}
            />
          ) : isEmpty ? (
            <EmptyState
              title={t("knowledgeBases.graph.empty")}
              hint={t("knowledgeBases.graph.emptyHint")}
            />
          ) : (
            <>
              <div
                role="region"
                aria-label={t("knowledgeBases.graph.title")}
                className="h-[25.71rem] w-full overflow-hidden rounded-md border border-border bg-surface-sunken"
              >
                <ReactFlow
                  nodes={flow.nodes}
                  edges={flow.edges}
                  fitView
                  minZoom={0.02}
                  fitViewOptions={{ padding: 0.15, minZoom: 0.02 }}
                  ariaLabelConfig={{ "controls.fitView.ariaLabel": t("knowledgeBases.graph.fitView") }}
                  nodesDraggable={false}
                  nodesConnectable={false}
                  elementsSelectable={false}
                  zoomOnScroll={false}
                  preventScrolling={false}
                >
                  <Background />
                  <Controls showInteractive={false} fitViewOptions={{ padding: 0.15, minZoom: 0.02 }} />
                </ReactFlow>
              </div>
              {query.data?.truncated ? (
                <p className="text-xs text-fg-muted">{t("knowledgeBases.graph.truncated")}</p>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </Disclosure>
  );
}
