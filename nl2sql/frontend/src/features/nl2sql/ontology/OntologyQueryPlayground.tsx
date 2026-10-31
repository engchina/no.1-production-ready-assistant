import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import {
  MessageSquareText,
  Network,
  Route,
  Search,
  ServerCog,
  Table2,
} from "lucide-react";

import {
  Button,
  Banner,
  DataTable,
  EmptyState,
  StatusBadge,
  ClearActionButton,
  DisclosureChevron,
  ProcessingIndicator,
  INFORMATION_LIST_ROW_CLASS,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_TABLE_FOCUS_CLASS,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  FieldActionRow,
  isImeComposing,
  TextField,
} from "@production-ready/ui";
import { ErrorState } from "@/components/StateViews";

import { t } from "@/lib/i18n";
import { DbManagementLoadingSkeleton, DbObjectManagementPanelShell, DbObjectPanelHeader } from "../components/DbObjectManagementShared";
import { SchemaSyncNotice } from "../components/SchemaRefreshFeedback";
import { useMutationSchemaSync } from "../useMutationSchemaSync";
import {
  deriveOntologyErDetails,
  type OntologyErDetails,
  type OntologyErKeyRole,
} from "./erDetails";
import { ontologyNodeDisplay } from "./nodeDisplay";
import { searchOntologyContext } from "./api";
import {
  answerOntologyQuestion,
  browseRelationshipRows,
  groundedRelationshipRows,
  type PlaygroundResult,
} from "./queryPlayground";
import { DbObjectName } from "../components/DbObjectName";
import { normalizeGroundingText } from "./groundingMatcher";
import {
  type OntologyContextSearchResult,
  type OntologyEdge,
  type OntologyGraph,
  type OntologyNode,
  type OntologyRelationshipRow,
} from "./types";
import { isOntologyDetailNodeKind, type OntologyGraphViewMode } from "./graphView";
import {
  OntologySelectionDetailsPanel,
  ontologyValidationLabel,
  ontologyValidationVariant,
} from "./OntologySelectionDetails";

const LazyOntologyGraphCanvas = lazy(() => import("./OntologyGraphCanvas"));

/**
 * 未解決の対象の案内の「DB 構造を再取得」の起点（SchemaRefreshCoordinator の start(origin)）。進行と失敗は
 * この案内の中だけに出す（#2034）。
 */
export const ONTOLOGY_SCHEMA_REFRESH_PLAYGROUND = "ontology-playground";

export interface OntologyQueryPlaygroundProps {
  graph: OntologyGraph | null;
  /** サーバ検索(実際の SQL 生成と同じ ontology-context 検索)に使う profile。 */
  profileId?: string;
  warningsJa?: string[];
  loadState?: "not_loaded" | "loading" | "error" | "ready";
  loadErrorMessage?: string;
  onRetryLoad?: () => void;
  /**
   * 起点の「オントロジーを取得」ボタンが loading（スピナー）を出しているか。出している間は、同じ取得の
   * 読込表示はスピナーを出さない（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
   */
  workspaceFetching?: boolean;
}

const STAGE_LABEL_KEYS = {
  entity_definition: "ontologyPlayground.stage.entityDefinition",
  list_all: "ontologyPlayground.stage.listAll",
  relationship: "ontologyPlayground.stage.relationship",
  property: "ontologyPlayground.stage.property",
  aggregate: "ontologyPlayground.stage.aggregate",
  no_match: "ontologyPlayground.stage.noMatch",
} as const;
const ONTOLOGY_NODE_PICKER_MAX_ITEMS = 12;

function erKeyRoleLabel(role: OntologyErKeyRole): string {
  switch (role) {
    case "pk":
      return t("ontologyPlayground.erKeyRolePk");
    case "fk":
      return t("ontologyPlayground.erKeyRoleFk");
    case "pk_fk":
      return t("ontologyPlayground.erKeyRolePkFk");
    case "none":
    default:
      return t("ontologyPlayground.erKeyRoleNone");
  }
}

function erObjectTypeLabel(type: OntologyErDetails["objectType"]): string {
  if (type === "table") return t("nl2sql.ontology.nodeKind.table");
  if (type === "view") return t("nl2sql.ontology.nodeKind.view");
  return t("nl2sql.ontology.nodeKind.unknown");
}

function stageLabel(result: PlaygroundResult): string {
  return t(STAGE_LABEL_KEYS[result.stage]);
}

type ServerSearchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; result: OntologyContextSearchResult };

/** 質問文(正規化後)の一致スパンを下線表示する。 */
function QuestionMatchedSpans({
  question,
  result,
}: {
  question: string;
  result: PlaygroundResult;
}) {
  const normalized = useMemo(() => normalizeGroundingText(question), [question]);
  const spans = useMemo(() => {
    const raw = result.candidates
      .filter((candidate) => candidate.span && candidate.score >= 0.65)
      .map((candidate) => candidate.span!)
      .sort((a, b) => a.start - b.start || b.end - a.end);
    const merged: Array<{ start: number; end: number }> = [];
    for (const span of raw) {
      const last = merged[merged.length - 1];
      if (last && span.start < last.end) {
        last.end = Math.max(last.end, span.end);
      } else {
        merged.push({ ...span });
      }
    }
    return merged;
  }, [result]);
  if (!normalized || spans.length === 0) return null;
  const parts: Array<{ text: string; matched: boolean }> = [];
  let cursor = 0;
  for (const span of spans) {
    if (span.start > cursor) parts.push({ text: normalized.slice(cursor, span.start), matched: false });
    parts.push({ text: normalized.slice(span.start, span.end), matched: true });
    cursor = span.end;
  }
  if (cursor < normalized.length) parts.push({ text: normalized.slice(cursor), matched: false });
  return (
    <p className="text-sm leading-6 text-fg-muted" data-testid="ontology-playground-matched-spans">
      {parts.map((part, index) =>
        part.matched ? (
          <mark
            key={index}
            data-surface-tint="accent"
            className="rounded bg-accent-muted px-0.5 font-medium text-fg underline decoration-accent-fg decoration-2 underline-offset-2"
          >
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        )
      )}
    </p>
  );
}

interface GroundingComparison {
  both: string[];
  clientOnly: string[];
  serverOnly: string[];
}

/**
 * サーバ検索(SQL 生成と同じ ontology-context 検索)の結果パネル。
 * 即時判定との一致/不一致を 3 バケットのチップで示し、不一致時は
 * エイリアス追加などの改善アクションへ誘導する。
 */
function ServerSearchResultPanel({
  result,
  comparison,
  graph,
  onSelectNode,
}: {
  result: OntologyContextSearchResult;
  comparison: GroundingComparison | null;
  graph: OntologyGraph | null;
  onSelectNode: (nodeId: string) => void;
}) {
  const graphNodeIds = useMemo(
    () => new Set((graph?.nodes ?? []).map((node) => node.id)),
    [graph]
  );
  return (
    <section
      className="grid gap-3 rounded-md border border-border bg-surface p-3"
      aria-label={t("ontologyPlayground.serverSearch.title")}
      data-testid="ontology-playground-server-result"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <ServerCog size={16} className="text-accent-fg" aria-hidden="true" />
          {t("ontologyPlayground.serverSearch.title")}
        </h3>
        <StatusBadge
          icon={false}
          variant="neutral"
          label={t("ontologyPlayground.serverSearch.hitCount", {
            count: result.hits.length,
          })}
        />
      </div>
      {comparison ? (
        <div className="grid gap-2" data-testid="ontology-playground-grounding-comparison">
          <div className="flex flex-wrap gap-2">
            <StatusBadge
              variant="success"
              label={t("ontologyPlayground.serverSearch.compareBoth", {
                count: comparison.both.length,
              })}
            />
            <StatusBadge
              variant="warning"
              label={t("ontologyPlayground.serverSearch.compareClientOnly", {
                count: comparison.clientOnly.length,
              })}
            />
            <StatusBadge
              variant="info"
              label={t("ontologyPlayground.serverSearch.compareServerOnly", {
                count: comparison.serverOnly.length,
              })}
            />
          </div>
          {comparison.serverOnly.length > 0 ? (
            <p className="text-xs leading-5 text-fg-muted">
              {t("ontologyPlayground.serverSearch.compareHint")}
            </p>
          ) : null}
        </div>
      ) : null}
      {result.hits.length === 0 ? (
        <Banner severity="info">{t("ontologyPlayground.serverSearch.empty")}</Banner>
      ) : (
        <ol
          // 関係一覧(ontology-inspector-relationship-list)と同じ標準: 最大高さ + 縦スクロール。
          // ヘッダーと比較バッジは常時見えるよう、スクロールはヒット一覧だけに限定する。
          className="grid max-h-80 gap-1.5 overflow-y-auto overscroll-contain pr-1"
          data-testid="ontology-playground-server-hits"
        >
          {result.hits.map((hit, index) => {
            const display = ontologyNodeDisplay(hit.node);
            const scorePercent = Math.round(hit.score * 100);
            const inGraph = graphNodeIds.has(hit.node.id);
            return (
              <li key={hit.node.id}>
                <button
                  type="button"
                  className="grid w-full cursor-pointer gap-1 rounded-md border border-border bg-surface-sunken px-3 py-2 text-left transition-colors hover:border-accent-emphasis motion-reduce:transition-none disabled:cursor-default"
                  onClick={() => inGraph && onSelectNode(hit.node.id)}
                  disabled={!inGraph}
                  data-testid={`ontology-server-hit-${hit.node.id}`}
                >
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold tabular-nums text-fg-muted">
                      {index + 1}.
                    </span>
                    <span className="min-w-0 flex-1 break-words text-sm font-medium text-fg">
                      {hit.node.business_name_ja}
                    </span>
                    <span className="text-xs text-fg-muted">{display.kindLabel}</span>
                    {hit.inference_source !== "asserted" ? (
                      <StatusBadge
                        icon={false}
                        variant="info"
                        label={t("ontologyPlayground.serverSearch.inferred")}
                      />
                    ) : null}
                  </span>
                  <span className="flex items-center gap-2">
                    <span
                      data-surface-tint="neutral"
                      className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-hover"
                      aria-hidden="true"
                    >
                      <span
                        className="block h-full rounded-full bg-accent-emphasis"
                        style={{ width: `${scorePercent}%` }}
                      />
                    </span>
                    <span className="text-xs tabular-nums text-fg-muted">
                      {t("ontologyPlayground.serverSearch.score")} {scorePercent}%
                    </span>
                  </span>
                  {hit.matched_terms.length > 0 ? (
                    <span className="text-xs leading-5 text-fg-muted">
                      {t("ontologyPlayground.serverSearch.matchedTerms")}:{" "}
                      {hit.matched_terms.join("、")}
                    </span>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function relationshipDetailLabel(kind: OntologyRelationshipRow["detail_kind"]): string {
  if (kind === "join") return t("ontologyPlayground.inspector.detailJoin");
  if (kind === "physical") return t("ontologyPlayground.inspector.detailPhysical");
  return t("ontologyPlayground.inspector.detailNote");
}

function RelationshipCard({
  row,
  selected,
  onSelect,
}: {
  row: OntologyRelationshipRow;
  selected: boolean;
  onSelect?: (edgeId: string) => void;
}) {
  return (
    <button
      type="button"
      className={`grid w-full cursor-pointer gap-2 rounded-md border bg-surface-sunken px-3 py-2 text-left transition-colors motion-reduce:transition-none ${
        selected ? "border-accent-emphasis ring-2 ring-accent-emphasis" : "border-border hover:border-accent-emphasis"
      }`}
      onClick={() => onSelect?.(row.edge_id)}
      data-testid={`ontology-inspector-relationship-${row.edge_id}`}
    >
      <span className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 break-words text-sm font-semibold text-fg">
          {row.relationship_label}
        </span>
        <StatusBadge variant={ontologyValidationVariant(row.validation_status)} label={ontologyValidationLabel(row.validation_status)} />
      </span>
      <span className="text-xs leading-5 text-fg-muted">
        {row.source_label} → {row.target_label}
      </span>
      {row.detail_kind === "none" ? null : (
        <span className="grid gap-0.5">
          <span className="text-xs leading-4 text-fg-muted">{relationshipDetailLabel(row.detail_kind)}</span>
          {row.detail_kind === "join" ? (
            <code
              className="break-all rounded bg-surface px-2 py-1 font-mono text-xs leading-5 text-fg"
              data-testid="ontology-inspector-relationship-detail"
            >
              {row.detail_text}
            </code>
          ) : row.detail_kind === "physical" ? (
            <DbObjectName
              value={row.detail_text}
              size="xs"
              className="leading-5"
              data-testid="ontology-inspector-relationship-detail"
            />
          ) : (
            <span
              className="break-all text-xs leading-5 text-fg"
              data-testid="ontology-inspector-relationship-detail"
            >
              {row.detail_text}
            </span>
          )}
        </span>
      )}
    </button>
  );
}

function OntologyGroundingPathPanel({
  graph,
  result,
  rows,
}: {
  graph: OntologyGraph;
  result: PlaygroundResult | null;
  rows: OntologyRelationshipRow[];
}) {
  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes]);
  const highlightedNodes = useMemo(
    () => (result?.highlightNodeIds ?? []).map((nodeId) => nodeById.get(nodeId)).filter(Boolean) as OntologyNode[],
    [nodeById, result]
  );
  return (
    <section className="grid gap-3 rounded-md border border-border bg-surface p-3" data-testid="ontology-grounding-path-panel">
      <div className="flex items-center gap-2">
        <Route size={16} className="text-accent-fg" aria-hidden="true" />
        <h3 className="text-sm font-semibold text-fg">
          {t("ontologyPlayground.inspector.groundingPath")}
        </h3>
      </div>
      {!result ? (
        <p className="text-sm leading-6 text-fg-muted">{t("ontologyPlayground.inspector.groundingEmpty")}</p>
      ) : highlightedNodes.length === 0 && rows.length === 0 ? (
        <Banner severity="info">{t("ontologyPlayground.inspector.groundingNoMatch")}</Banner>
      ) : (
        <div className="grid gap-3">
          <div className="flex flex-wrap gap-2">
            <StatusBadge icon={false} variant="info" label={stageLabel(result)} />
            <StatusBadge
              icon={false}
              variant="neutral"
              label={t("ontologyPlayground.inspector.groundingCount", {
                nodes: highlightedNodes.length,
                edges: rows.length,
              })}
            />
          </div>
          {highlightedNodes.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {highlightedNodes.map((node) => {
                const display = ontologyNodeDisplay(node);
                return (
                  <span
                    key={node.id}
                    className="rounded-md border border-border bg-surface-sunken px-2 py-1 text-xs leading-5 text-fg"
                    title={display.ariaLabel}
                  >
                    {display.kindLabel}: {display.primaryLabel}
                  </span>
                );
              })}
            </div>
          ) : null}
          {rows.length > 0 ? (
            <div className="grid gap-2">
              {rows.slice(0, 5).map((row) => (
                <div key={row.edge_id} className="rounded-md border border-border bg-surface-sunken px-3 py-2">
                  <p className="text-sm font-semibold text-fg">{row.relationship_label}</p>
                  <p className="mt-1 text-xs leading-5 text-fg-muted">
                    {row.source_label} → {row.target_label}
                  </p>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

function OntologyDetailsPanel({
  graph,
  node,
  edge,
  onSelectNode,
}: {
  graph: OntologyGraph;
  node: OntologyNode | null;
  edge: OntologyEdge | null;
  onSelectNode: (nodeId: string) => void;
}) {
  const selectableNodes = useMemo(
    () =>
      graph.nodes
        .filter((item) => !isOntologyDetailNodeKind(item.kind))
        .slice(0, ONTOLOGY_NODE_PICKER_MAX_ITEMS),
    [graph.nodes]
  );
  return (
    <OntologySelectionDetailsPanel
      graph={graph}
      node={node}
      edge={edge}
      emptyContent={
        <div className="grid gap-3">
          <p className="text-sm leading-6 text-fg-muted">{t("ontologyPlayground.inspector.nodeEmpty")}</p>
          <div className="grid gap-2" data-testid="ontology-inspector-node-picker">
            <p className="text-xs font-semibold text-fg-muted">
              {t("ontologyPlayground.inspector.nodePicker")}
            </p>
            <div
              className={`grid min-w-0 content-start gap-1.5 pr-1 ${INFORMATION_LIST_SCROLL_CLASS} ${INFORMATION_TABLE_FOCUS_CLASS}`}
              role="region"
              aria-label={t("ontologyPlayground.inspector.nodePicker")}
              tabIndex={0}
              data-testid="ontology-inspector-node-picker-scroll-region"
            >
              {selectableNodes.map((item) => {
                const display = ontologyNodeDisplay(item);
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={`grid min-w-0 cursor-pointer gap-0.5 rounded-md border border-border bg-surface-sunken px-3 py-2 text-left transition-colors hover:border-accent-emphasis motion-reduce:transition-none ${INFORMATION_LIST_ROW_CLASS}`}
                    onClick={() => onSelectNode(item.id)}
                    data-testid={`ontology-inspector-node-${item.id}`}
                  >
                    <span className="text-xs font-semibold text-fg-muted">{display.kindLabel}</span>
                    <span className="break-words text-sm font-medium text-fg">
                      {display.primaryLabel}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      }
    />
  );
}

function OntologyRelationshipListPanel({
  rows,
  selectedEdgeId,
  onSelectEdge,
}: {
  rows: OntologyRelationshipRow[];
  selectedEdgeId: string | null;
  onSelectEdge: (edgeId: string) => void;
}) {
  return (
    <section className="grid gap-3 rounded-md border border-border bg-surface p-3" data-testid="ontology-inspector-relationships">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Network size={16} className="text-accent-fg" aria-hidden="true" />
          {t("ontologyPlayground.inspector.relationships")}
        </h3>
        <StatusBadge icon={false} variant="neutral" label={t("ontologyPlayground.inspector.relationshipCount", { count: rows.length })} />
      </div>
      {rows.length === 0 ? (
        <Banner severity="info">{t("ontologyPlayground.inspector.relationshipsEmpty")}</Banner>
      ) : (
        <div className="grid max-h-80 gap-2 overflow-y-auto pr-1" data-testid="ontology-inspector-relationship-list">
          {rows.map((row) => (
            <RelationshipCard
              key={row.edge_id}
              row={row}
              selected={row.edge_id === selectedEdgeId}
              onSelect={onSelectEdge}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function OntologyErDetailsPanel({ details }: { details: OntologyErDetails }) {
  return (
    <section
      className="grid gap-4 rounded-md border border-border bg-surface p-3"
      aria-labelledby="ontology-er-details-title"
      data-testid="ontology-er-details-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="ontology-er-details-title"
            className="flex items-center gap-2 text-sm font-semibold text-fg"
          >
            <Table2 size={16} className="text-accent-fg" aria-hidden="true" />
            {t("ontologyPlayground.erDetailsTitle")}
          </h3>
          <p className="mt-1 leading-5">
            <DbObjectName value={details.objectName} size="xs" data-testid="ontology-er-detail-object-name" />
          </p>
        </div>
        <div className="flex flex-wrap gap-2" aria-label={t("ontologyPlayground.erSummary")}>
          <StatusBadge icon={false} variant="neutral" label={erObjectTypeLabel(details.objectType)} />
          <StatusBadge
            icon={false}
            variant="info"
            label={t("ontologyPlayground.erColumnCount", { count: details.columns.length })}
          />
        </div>
      </div>

      {details.columns.length > 0 ? (
        <DataTable
          columns={[
            {
              key: "columnName",
              header: t("ontologyPlayground.erColumnName"),
              headerClassName: "w-[24%]",
              className: "break-all py-3 font-mono leading-5",
              render: (column) => column.columnName,
            },
            {
              key: "dataType",
              header: t("ontologyPlayground.erDataType"),
              headerClassName: "w-[16%]",
              className: "break-all py-3 font-mono leading-5 text-fg-muted",
              render: (column) => column.dataType,
            },
            {
              key: "keyRole",
              header: t("ontologyPlayground.erKeyRole"),
              headerClassName: "w-[12%]",
              className: "py-3",
              render: (column) => erKeyRoleLabel(column.keyRole),
            },
            {
              key: "businessNameJa",
              header: t("ontologyPlayground.erBusinessName"),
              headerClassName: "w-[22%]",
              className: "break-words py-3",
              render: (column) => column.businessNameJa,
            },
            {
              key: "descriptionJa",
              header: t("ontologyPlayground.erDescription"),
              headerClassName: "w-[26%]",
              className: "break-words py-3 text-fg-muted",
              render: (column) => column.descriptionJa || "-",
            },
          ]}
          rows={details.columns}
          getRowKey={(column) => column.id}
          rowProps={(column) => ({ className: INFORMATION_TABLE_ROW_CLASS, "data-testid": `ontology-er-column-${column.id}` })}
          tableClassName="w-full min-w-[48rem] table-fixed"
          scrollTestId="ontology-er-columns"
          stickyHeader
          visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
        />
      ) : (
        <Banner severity="info">{t("ontologyPlayground.erColumnsEmpty")}</Banner>
      )}

      <div className="grid gap-2" data-testid="ontology-er-joins">
        <h4 className="text-xs font-semibold text-fg-muted">
          {t("ontologyPlayground.erJoinConditions")}
        </h4>
        {details.joins.length > 0 ? (
          <div className="grid gap-2">
            {details.joins.map((join) => (
              <div
                key={join.id}
                className="grid gap-1 rounded-md border border-border bg-surface-sunken px-3 py-2"
                data-testid={`ontology-er-join-${join.id}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-fg">
                    {join.relationshipNameJa}
                  </span>
                  <span className="text-xs text-fg-muted">{join.cardinality}</span>
                </div>
                <p className="text-xs leading-5 text-fg-muted">
                  {join.sourceLabel} → {join.targetLabel}
                </p>
                <code data-surface-tint="neutral" className="break-all rounded bg-surface-hover px-2 py-1 font-mono text-xs leading-5 text-fg">
                  {join.joinCondition}
                </code>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm leading-6 text-fg-muted">{t("ontologyPlayground.erJoinsEmpty")}</p>
        )}
      </div>
    </section>
  );
}

/**
 * 決定論 NL Query Playground(LLM 不要)。質問がオントロジーの
 * どのエンティティ/関係に接地するかをグラフ上でハイライトする。
 */
export function OntologyQueryPlayground({
  graph,
  profileId = "",
  warningsJa = [],
  loadState = "ready",
  loadErrorMessage = "",
  onRetryLoad,
  workspaceFetching = false,
}: OntologyQueryPlaygroundProps) {
  // 公開オントロジーに解決できない対象の案内。「DB 構造を再取得」の進行は同じ案内の中のボタンの直下に出し、
  // 再取得が終わっても同じ警告が残るときは、再取得を求めずに残りの対処だけを出す（messaging §10.2、#2034）。
  const unresolvedKey = warningsJa.join("\n");
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: ONTOLOGY_SCHEMA_REFRESH_PLAYGROUND,
    required: unresolvedKey
      ? {
          key: unresolvedKey,
          notice: {
            severity: "warning",
            title: t("profiles.ontology.unresolvedTitle"),
            message: "",
            errorCode: "",
          },
        }
      : null,
  });
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [graphViewMode, setGraphViewMode] = useState<OntologyGraphViewMode>("all");
  const [mobileGraphOpen, setMobileGraphOpen] = useState(false);
  const [serverSearch, setServerSearch] = useState<ServerSearchState>({ status: "idle" });
  // 古いサーバ検索応答が新しい状態を上書きしないための世代カウンタ
  const serverSearchSeqRef = useRef(0);

  const hasGraph = Boolean(graph && graph.nodes.length > 0);
  const graphRevisionId = graph?.revision?.id ?? graph?.revision_id ?? "";

  // revision が変わったレンダーで結果と選択を消す（effect で setState しない）。古い検索応答は effect で捨てる。
  const revisionChanged = useValuesChanged([graphRevisionId]);
  if (revisionChanged) {
    setResult(null);
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    setServerSearch({ status: "idle" });
  }
  useEffect(() => {
    serverSearchSeqRef.current += 1;
  }, [graphRevisionId]);

  // グラフ・一覧で選んだノードと線（関係）は、同じ詳細のパネルに出すので片方だけを選ぶ（#1788）。
  // 質問の接地の直後だけは、最初に一致したノードと関係を両方選び、パネルはノードを出す。
  const selectNode = (nodeId: string | null) => {
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
  };
  const selectEdge = (edgeId: string | null) => {
    setSelectedEdgeId(edgeId);
    setSelectedNodeId(null);
  };

  const resetGroundingState = ({ clearQuestion = false }: { clearQuestion?: boolean } = {}) => {
    if (clearQuestion) setQuestion("");
    setResult(null);
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    setGraphViewMode("all");
    serverSearchSeqRef.current += 1;
    setServerSearch({ status: "idle" });
  };

  const handleQuestionChange = (value: string) => {
    setQuestion(value);
    if (!value.trim()) resetGroundingState();
  };

  const runQuestion = () => {
    const normalizedQuestion = question.trim();
    if (!graph || !normalizedQuestion) {
      resetGroundingState();
      return;
    }
    const nextResult = answerOntologyQuestion(graph, normalizedQuestion);
    setResult(nextResult);
    setGraphViewMode("grounding");
    setSelectedNodeId(nextResult.highlightNodeIds[0] ?? null);
    setSelectedEdgeId(nextResult.highlightEdgeIds[0] ?? null);
    // 質問が変わったら前のサーバ検索結果は無効
    serverSearchSeqRef.current += 1;
    setServerSearch({ status: "idle" });
  };

  const canServerSearch = Boolean(profileId && graphRevisionId && question.trim());
  const runServerSearch = async () => {
    const normalizedQuestion = question.trim();
    if (!graph || !canServerSearch || !normalizedQuestion) return;
    // 即時判定を未実行ならまず実行して比較の土台を揃える
    if (!result) runQuestion();
    const seq = ++serverSearchSeqRef.current;
    setServerSearch({ status: "loading" });
    try {
      const searchResult = await searchOntologyContext(profileId, {
        question: normalizedQuestion,
        ontologyRevisionId: graphRevisionId,
      });
      if (serverSearchSeqRef.current !== seq) return;
      setServerSearch({ status: "success", result: searchResult });
      setGraphViewMode("grounding");
    } catch (err) {
      if (serverSearchSeqRef.current !== seq) return;
      setServerSearch({
        status: "error",
        message:
          err instanceof Error && err.message
            ? err.message
            : t("ontologyPlayground.serverSearch.error"),
      });
    }
  };

  const hasResettableGroundingState = Boolean(
    question.length > 0 ||
      result ||
      selectedNodeId ||
      selectedEdgeId ||
      graphViewMode !== "all" ||
      serverSearch.status !== "idle"
  );

  const serverHitNodeIds = useMemo(
    () =>
      serverSearch.status === "success"
        ? serverSearch.result.hits.map((hit) => hit.node.id)
        : [],
    [serverSearch]
  );
  const serverEdgeIds = useMemo(
    () =>
      serverSearch.status === "success"
        ? serverSearch.result.edges.map((edge) => edge.id)
        : [],
    [serverSearch]
  );
  // グラフの強調は即時判定とサーバ検索の合成(比較チップでどちら由来かを示す)
  const highlightNodeIds = useMemo(
    () => [...new Set([...(result?.highlightNodeIds ?? []), ...serverHitNodeIds])],
    [result, serverHitNodeIds]
  );
  const highlightEdgeIds = useMemo(
    () => [...new Set([...(result?.highlightEdgeIds ?? []), ...serverEdgeIds])],
    [result, serverEdgeIds]
  );
  const groundingComparison = useMemo(() => {
    if (!result || serverSearch.status !== "success") return null;
    const clientIds = new Set(result.highlightNodeIds);
    const serverIds = new Set(serverHitNodeIds);
    return {
      both: [...clientIds].filter((id) => serverIds.has(id)),
      clientOnly: [...clientIds].filter((id) => !serverIds.has(id)),
      serverOnly: [...serverIds].filter((id) => !clientIds.has(id)),
    };
  }, [result, serverSearch, serverHitNodeIds]);
  const selectedNode = useMemo(
    () => graph?.nodes.find((node) => node.id === selectedNodeId) ?? null,
    [graph, selectedNodeId]
  );
  const selectedEdge = useMemo(
    () => graph?.edges.find((edge) => edge.id === selectedEdgeId) ?? null,
    [graph, selectedEdgeId]
  );
  const groundedRows = useMemo(
    () => (graph ? groundedRelationshipRows(graph, result) : []),
    [graph, result]
  );
  const browseRows = useMemo(
    () => (graph ? browseRelationshipRows(graph, groundedRows) : []),
    [graph, groundedRows]
  );
  const erDetails = useMemo(
    () => (graph ? deriveOntologyErDetails(graph, selectedNodeId) : null),
    [graph, selectedNodeId]
  );

  return (
    <DbObjectManagementPanelShell
      id="ontology-query-playground-panel"
      role="region"
      ariaLabel={t("ontologyPlayground.title")}
      idPrefix="ontology-query-playground"
    >
      <DbObjectPanelHeader
        icon={MessageSquareText}
        title={t("ontologyPlayground.title")}
        description={t("ontologyPlayground.description")}
      />
      {warningsJa.length > 0 ? (
        schemaSync.requiredResolved ? (
          <div data-testid="profile-ontology-unresolved">
            <Banner severity="warning" title={t("profiles.ontology.unresolvedTitle")}>
              <div className="min-w-0 space-y-2">
                <p>{t("profiles.ontology.unresolvedAfterRefreshHint")}</p>
                <ul className="grid gap-1 pl-4">
              {warningsJa.map((warning) => (
                <li key={warning} className="list-disc break-words">
                  {warning}
                </li>
              ))}
            </ul>
              </div>
            </Banner>
          </div>
        ) : (
          <SchemaSyncNotice
            sync={schemaSync}
            testId="profile-ontology-unresolved"
            processingTestId="ontology-playground-schema-refresh-processing"
          >
            <ul className="grid gap-1 pl-4">
              {warningsJa.map((warning) => (
                <li key={warning} className="list-disc break-words">
                  {warning}
                </li>
              ))}
            </ul>
          </SchemaSyncNotice>
        )
      ) : null}
      {loadState === "not_loaded" ? (
        <EmptyState
          title={t("ontologyBuild.workspace.notLoadedTitle")}
          hint={t("ontologyBuild.workspace.notLoadedHint")}
        />
      ) : loadState === "loading" ? (
        <DbManagementLoadingSkeleton
          idPrefix="ontology-view"
          ariaLabel={t("ontologyBuild.workspace.ontologyLoading")}
          variant="detail"
          testId="ontology-view-loading"
          activityIcon={workspaceFetching ? "none" : "spinner"}
        />
      ) : loadState === "error" ? (
        <ErrorState
          message={loadErrorMessage || t("ontologyBuild.workspace.error")}
          onRetry={onRetryLoad}
        />
      ) : !hasGraph ? (
        <EmptyState
          title={t("ontologyPlayground.emptyTitle")}
          hint={t("ontologyPlayground.emptyHint")}
          action={
            <p className="text-xs font-medium leading-5 text-fg-muted">
              {t("ontologyPlayground.emptyFlow")}
            </p>
          }
        />
      ) : (
        <div className="grid gap-3">
          <form
            className="space-y-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              runQuestion();
            }}
          >
            {/* 主な問い合わせの行なので、質問欄と操作をすべて lg（40px、タッチ端末は 44px）にそろえる（#613）。 */}
            <FieldActionRow
              actions={
                <>
                  <Button
                    type="submit"
                    variant="primary"
                    size="lg"
                    className="whitespace-nowrap"
                    disabled={!question.trim()}
                    data-testid="ontology-playground-run" icon={Search}>
                    <span>{t("ontologyPlayground.run")}</span>
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    size="lg"
                    className="whitespace-nowrap"
                    disabled={!canServerSearch || serverSearch.status === "loading"}
                    loading={serverSearch.status === "loading"}
                    onClick={() => void runServerSearch()}
                    title={t("ontologyPlayground.serverSearch.hint")}
                    data-testid="ontology-playground-server-search" icon={ServerCog}>
                    <span>{t("ontologyPlayground.serverSearch.run")}</span>
                  </Button>
                  <ClearActionButton
                    size="lg"
                    disabled={!hasResettableGroundingState}
                    ariaLabel={t("ontologyPlayground.clearAriaLabel")}
                    onClick={() => resetGroundingState({ clearQuestion: true })}
                    dataTestId="ontology-playground-clear"
                    label={t("ontologyPlayground.clear")}
                  />
                </>
              }
            >
              {/* 質問が空では実行できない（disabled={!question.trim()}）ので必須として示す（#531）。 */}
              <TextField
                id="ontology-playground-question"
                label={t("ontologyPlayground.questionLabel")}
                required
                size="lg"
                // 質問は文章なので full（下の操作と端をそろえる。#1891）。
                width="full"
                value={question}
                onValueChange={handleQuestionChange}
                onKeyDown={(event) => {
                  // IME の変換を確定する Enter でフォームを送信しない（重い問い合わせは明示的に実行する。#535）。
                  if (event.key === "Enter" && isImeComposing(event)) event.preventDefault();
                }}
                placeholder={t("ontologyPlayground.questionPlaceholder")}
                data-testid="ontology-playground-question"
              />
            </FieldActionRow>
          </form>
          {serverSearch.status === "loading" ? (
            // 質問の embedding とベクトル検索を行うため数秒かかる。スピナーはボタンの loading が担う。
            <ProcessingIndicator
              active
              label={t("ontologyPlayground.serverSearch.progress")}
              operationKey="ontology-server-search"
              placement="action"
              activityIcon="none"
              testId="ontology-playground-server-search-processing"
            />
          ) : null}
          {!result ? (
            <div
              data-surface-tint="neutral"
              className="rounded-md border border-border bg-surface-hover px-3 py-2"
              data-testid="ontology-playground-ready-state"
            >
              <p className="text-sm leading-6 text-fg">
                {t("ontologyPlayground.readyHint")}
              </p>
            </div>
          ) : null}
          {result ? (
            <div
              className="grid gap-1 rounded-md border border-border bg-surface p-3"
              aria-live="polite"
              data-testid="ontology-playground-result"
            >
              <p className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
                {t(STAGE_LABEL_KEYS[result.stage])}
              </p>
              <p className="text-sm leading-6 text-fg">{result.explanationJa}</p>
              <QuestionMatchedSpans question={question} result={result} />
              {result.suggestionsJa.length > 0 ? (
                <p className="text-sm text-fg-muted">
                  {t("ontologyPlayground.suggestions")}: {result.suggestionsJa.join("、")}
                </p>
              ) : null}
            </div>
          ) : null}
          {serverSearch.status === "error" ? (
            <Banner severity="danger">{serverSearch.message}</Banner>
          ) : null}
          {serverSearch.status === "success" ? (
            <ServerSearchResultPanel
              result={serverSearch.result}
              comparison={groundingComparison}
              graph={graph}
              onSelectNode={selectNode}
            />
          ) : null}
          {graph ? (
            <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(22rem,26rem)] xl:items-start">
              <section className="order-2 grid gap-2 xl:order-1" aria-label={t("ontologyPlayground.graphSection")}>
                <div className="flex items-center justify-between gap-2 xl:hidden">
                  <h3 className="text-sm font-semibold text-fg">
                    {t("ontologyPlayground.graphSection")}
                  </h3>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={() => setMobileGraphOpen((current) => !current)}
                    aria-expanded={mobileGraphOpen}
                    aria-controls="ontology-playground-graph-region"
                  >
                    <DisclosureChevron expanded={mobileGraphOpen} size={16} />
                    <span>
                      {mobileGraphOpen
                        ? t("ontologyPlayground.graphCollapse")
                        : t("ontologyPlayground.graphExpand")}
                    </span>
                  </Button>
                </div>
                <div
                  id="ontology-playground-graph-region"
                  className={mobileGraphOpen ? "block" : "hidden xl:block"}
                  data-testid="ontology-playground-graph-region"
                >
                  <Suspense
                    fallback={
                      <DbManagementLoadingSkeleton
                        idPrefix="ontology-query-playground-graph"
                        ariaLabel={t("nl2sql.ontology.loading")}
                        variant="compact"
                      />
                    }
                  >
                    <LazyOntologyGraphCanvas
                      graph={graph}
                      workspaceKey={`playground:${profileId}`}
                      selectedNodeId={selectedNodeId}
                      selectedEdgeId={selectedEdgeId}
                      onSelectNode={selectNode}
                      onSelectEdge={selectEdge}
                      highlightNodeIds={highlightNodeIds}
                      highlightEdgeIds={highlightEdgeIds}
                      viewMode={graphViewMode}
                      onViewModeChange={setGraphViewMode}
                    />
                  </Suspense>
                </div>
              </section>
              <aside
                className="order-1 grid gap-3 xl:order-2"
                aria-label={t("ontologyPlayground.inspector.title")}
                data-testid="ontology-playground-inspector"
              >
                <OntologyGroundingPathPanel graph={graph} result={result} rows={groundedRows} />
                <OntologyDetailsPanel
                  graph={graph}
                  node={selectedNode}
                  edge={selectedEdge}
                  onSelectNode={selectNode}
                />
                {erDetails ? <OntologyErDetailsPanel details={erDetails} /> : null}
                <OntologyRelationshipListPanel
                  rows={browseRows}
                  selectedEdgeId={selectedEdgeId}
                  onSelectEdge={selectEdge}
                />
              </aside>
            </div>
          ) : null}
        </div>
      )}
    </DbObjectManagementPanelShell>
  );
}
