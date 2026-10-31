import { useId, useMemo, type ReactNode } from "react";
import { Info } from "lucide-react";
import { StatusBadge } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { DbObjectName } from "../components/DbObjectName";
import { cardinalityShortLabel } from "./graphView";
import { ontologyNodeDisplay } from "./nodeDisplay";
import { DefinitionFields, DetailFieldList } from "./ontologyResultPresentation";
import {
  ontologyRelationshipRows,
  type OntologyCardinality,
  type OntologyEdge,
  type OntologyGraph,
  type OntologyNode,
  type OntologyValidationStatus,
} from "./types";

/**
 * グラフで選んだノード・線（関係）の詳細。ノードも線も同じ 1 つのパネルに出し、見出しを選んだものに合わせる（#1788）。
 * オントロジー構築の接地確認と、SQL 生成の結果の接地確認の両方で使う。
 */

export function ontologyValidationVariant(status: OntologyValidationStatus) {
  if (status === "passed") return "success" as const;
  if (status === "warning") return "warning" as const;
  if (status === "blocked") return "danger" as const;
  return "neutral" as const;
}

export function ontologyValidationLabel(status: OntologyValidationStatus): string {
  if (status === "passed") return t("nl2sql.ontology.nodeValidation.passed");
  if (status === "warning") return t("nl2sql.ontology.nodeValidation.warning");
  if (status === "blocked") return t("nl2sql.ontology.nodeValidation.blocked");
  return t("nl2sql.ontology.nodeValidation.unreviewed");
}

function plainDefinition(item: { metadata?: Record<string, unknown> }): Record<string, unknown> | null {
  const definition = item.metadata?.definition;
  return definition && typeof definition === "object" && !Array.isArray(definition)
    ? (definition as Record<string, unknown>)
    : null;
}

function definitionString(definition: Record<string, unknown> | null, key: string): string {
  const value = definition?.[key];
  return typeof value === "string" ? value.trim() : "";
}

// 見出し・種類のバッジ・説明の段落に出す項目は、定義の項目の並びに重ねて出さない。
const TITLE_DEFINITION_KEYS = ["name_ja", "description_ja", "kind"];

function OntologyNodeDetails({ node }: { node: OntologyNode }) {
  const display = ontologyNodeDisplay(node);
  const definition = plainDefinition(node);
  const description = node.description_ja || node.description || definitionString(definition, "description_ja");
  return (
    <div className="grid gap-2" data-testid="ontology-node-details">
      <div className="flex flex-wrap gap-2">
        <StatusBadge icon={false} variant="info" label={display.kindLabel} />
        <StatusBadge
          variant={ontologyValidationVariant(node.validation_status ?? "unreviewed")}
          label={ontologyValidationLabel(node.validation_status ?? "unreviewed")}
        />
      </div>
      <p className="break-words text-sm font-semibold text-fg">{node.business_name_ja}</p>
      {display.secondaryLabel ? (
        <p className="break-all font-mono text-xs leading-5 text-fg-muted">{display.secondaryLabel}</p>
      ) : null}
      {definition ? (
        <DefinitionFields definition={definition} hideEmpty omitKeys={TITLE_DEFINITION_KEYS} />
      ) : null}
      {description ? <p className="text-sm leading-6 text-fg-muted">{description}</p> : null}
      {node.aliases?.length ? (
        <p className="text-xs leading-5 text-fg-muted">
          {t("ontologyPlayground.inspector.aliases")}: {node.aliases.join("、")}
        </p>
      ) : null}
    </div>
  );
}

// 関係を理解するのに要る項目（結合のキー・起点・終点・多重度）は先に出すので、定義の並びからは外す。
const EDGE_SUMMARY_DEFINITION_KEYS = [
  ...TITLE_DEFINITION_KEYS,
  "join_expression_sql",
  "source",
  "target",
  "cardinality",
];

function edgeKindLabel(edge: OntologyEdge, definition: Record<string, unknown> | null): string {
  const kind = definitionString(definition, "kind") || (edge.kind ?? "").trim();
  if (kind) {
    const key = `ontologyResults.kind.${kind}` as const;
    const label = t(key);
    if (label !== key) return label;
  }
  return t("ontologyPlayground.inspector.edgeKindFallback");
}

function OntologyEdgeDetails({ graph, edge }: { graph: OntologyGraph; edge: OntologyEdge }) {
  const row = useMemo(() => ontologyRelationshipRows({ ...graph, edges: [edge] })[0], [graph, edge]);
  const definition = plainDefinition(edge);
  const joinKey = definitionString(definition, "join_expression_sql") || row?.join_condition || "";
  const cardinality = cardinalityShortLabel(
    edge.cardinality ?? (definitionString(definition, "cardinality") as OntologyCardinality)
  );
  const description = (edge.description_ja ?? "").trim() || definitionString(definition, "description_ja");
  const validationStatus = row?.validation_status ?? edge.validation_status ?? "unreviewed";
  const summary: { key: string; label: string; value: ReactNode }[] = [];
  if (joinKey) {
    summary.push({
      key: "join",
      label: t("ontologyPlayground.inspector.detailJoin"),
      value: (
        <code className="block break-all rounded bg-surface-sunken px-2 py-1 font-mono text-xs leading-5 text-fg">
          {joinKey}
        </code>
      ),
    });
  }
  summary.push(
    { key: "source", label: t("ontologyPlayground.inspector.edgeSource"), value: row?.source_label ?? edge.source_node_id },
    { key: "target", label: t("ontologyPlayground.inspector.edgeTarget"), value: row?.target_label ?? edge.target_node_id }
  );
  if (cardinality) {
    summary.push({ key: "cardinality", label: t("ontologyResults.field.cardinality"), value: cardinality });
  }
  if (!joinKey && row?.detail_kind === "physical") {
    summary.push({
      key: "physical",
      label: t("ontologyPlayground.inspector.detailPhysical"),
      value: <DbObjectName value={row.detail_text} size="xs" className="leading-5" />,
    });
  }
  return (
    <div className="grid gap-2" data-testid="ontology-edge-details">
      <div className="flex flex-wrap gap-2">
        <StatusBadge icon={false} variant="info" label={edgeKindLabel(edge, definition)} />
        <StatusBadge variant={ontologyValidationVariant(validationStatus)} label={ontologyValidationLabel(validationStatus)} />
      </div>
      <p className="break-words text-sm font-semibold text-fg">{edge.relationship_name_ja}</p>
      <DetailFieldList items={summary} />
      {definition ? (
        <DefinitionFields definition={definition} hideEmpty omitKeys={EDGE_SUMMARY_DEFINITION_KEYS} />
      ) : null}
      {description ? <p className="text-sm leading-6 text-fg-muted">{description}</p> : null}
    </div>
  );
}

/**
 * 選んだノード・線の詳細のパネル。何も選んでいないときは `emptyContent` を出す。
 * ノードと線が両方選ばれているとき（質問の接地の直後）はノードを出す。クリックで選ぶ側は、片方を選んだら他方を外す。
 */
export function OntologySelectionDetailsPanel({
  graph,
  node,
  edge,
  emptyContent,
  testId = "ontology-selection-details-panel",
}: {
  graph: OntologyGraph;
  node: OntologyNode | null;
  edge: OntologyEdge | null;
  emptyContent?: ReactNode;
  testId?: string;
}) {
  const titleId = useId();
  const title = node
    ? t("ontologyPlayground.inspector.nodeDetails")
    : edge
      ? t("ontologyPlayground.inspector.edgeDetails")
      : t("ontologyPlayground.inspector.details");
  return (
    <section
      className="grid gap-3 rounded-md border border-border bg-surface p-3"
      aria-labelledby={titleId}
      data-testid={testId}
      data-selection-kind={node ? "node" : edge ? "edge" : "none"}
    >
      <div className="flex items-center gap-2">
        <Info size={16} className="text-accent-fg" aria-hidden="true" />
        <h3 id={titleId} className="text-sm font-semibold text-fg">
          {title}
        </h3>
      </div>
      {node ? (
        <OntologyNodeDetails node={node} />
      ) : edge ? (
        <OntologyEdgeDetails graph={graph} edge={edge} />
      ) : (
        emptyContent
      )}
    </section>
  );
}
