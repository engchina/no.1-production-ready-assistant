import { lazy, Suspense, useMemo, useState } from "react";
import { FileText, ListOrdered, Network, Play } from "lucide-react";

import {
  CopyButton,
  Button,
  Banner,
  Disclosure,
  StatusBadge,
  ContentActionBar,
  Skeleton,
  TimedLoadingState,
} from "@production-ready/ui";

import { LogicalStepsList } from "./LogicalStepsList";
import { t } from "@/lib/i18n";
import { engineLabel } from "../labels";
import { QUESTION_FILTER_LABELS, QUESTION_SLOT_LABELS } from "../questionTemplates";
import {
  groundSqlSemanticGraphOnOntologyGraph,
  isSqlSemanticGraph,
  type SqlOntologyGroundingResult,
} from "../ontology/sqlGrounding";
import type { OntologyGraph, SqlSemanticGraph } from "../ontology/types";
import { OntologySelectionDetailsPanel } from "../ontology/OntologySelectionDetails";
import type {
  GeneratedSqlPanelData,
  Nl2SqlInterpretationArtifact,
  Nl2SqlShowPromptArtifact,
} from "../types";

const LazyOntologyGraphCanvas = lazy(() => import("../ontology/OntologyGraphCanvas"));

const FILTER_LABELS = QUESTION_FILTER_LABELS;
const QUESTION_SLOT_PATTERN = /^\s*([^：:\n]{1,80})\s*[：:]\s*(.*)$/u;

function normalizeQuestionSlotLabel(value: string) {
  return value
    .normalize("NFKC")
    .trim()
    .toLowerCase()
    .replace(/[\s　（）()・/／]/gu, "");
}

const QUESTION_SLOT_LABEL_KEYS = new Map(
  QUESTION_SLOT_LABELS.map((label) => [normalizeQuestionSlotLabel(label), label])
);

function parseQuestionTemplate(question: string) {
  const slots: Record<string, string> = {};
  let currentLabel = "";
  let hasTemplate = false;
  for (const rawLine of question.split(/\r?\n/u)) {
    const match = QUESTION_SLOT_PATTERN.exec(rawLine);
    if (match) {
      const label = QUESTION_SLOT_LABEL_KEYS.get(normalizeQuestionSlotLabel(match[1]));
      if (label) {
        hasTemplate = true;
        currentLabel = label;
        slots[label] = slots[label] ? `${slots[label]}\n${match[2]}` : match[2];
        continue;
      }
    }
    if (currentLabel) {
      slots[currentLabel] = slots[currentLabel] ? `${slots[currentLabel]}\n${rawLine}` : rawLine;
    }
  }
  return {
    hasTemplate,
    slots: Object.fromEntries(Object.entries(slots).map(([label, value]) => [label, value.trim()])),
  };
}

function questionSlotValue(slots: Record<string, string>, labels: string[]) {
  const labelKeys = new Set(labels.map(normalizeQuestionSlotLabel));
  for (const [label, value] of Object.entries(slots)) {
    if (labelKeys.has(normalizeQuestionSlotLabel(label))) return value;
  }
  return undefined;
}

function GroundingStatusBadge({ result }: { result: SqlOntologyGroundingResult }) {
  if (result.status === "matched") {
    return <StatusBadge variant="success" label={t("nl2sql.interpretation.graphStatus.matched")} />;
  }
  if (result.status === "partial") {
    return <StatusBadge variant="warning" label={t("nl2sql.interpretation.graphStatus.partial")} />;
  }
  if (result.status === "unmatched") {
    return <StatusBadge variant="warning" label={t("nl2sql.interpretation.graphStatus.unmatched")} />;
  }
  return <StatusBadge variant="neutral" label={t("nl2sql.interpretation.graphStatus.unavailable")} />;
}

function GroundingMatchRows({
  title,
  values,
}: {
  title: string;
  values: Array<{ sql: string; ontologyLabels: string[] }>;
}) {
  return (
    <div className="grid gap-1">
      <p className="text-xs font-semibold text-fg-muted">{title}</p>
      {values.length === 0 ? (
        <p className="text-xs leading-5 text-fg-muted">-</p>
      ) : (
        <ul className="grid gap-1 text-xs leading-5">
          {values.slice(0, 5).map((value) => (
            <li key={`${title}:${value.sql}`} className="min-w-0 [overflow-wrap:anywhere]">
              <span className="font-mono text-fg">{value.sql}</span>
              <span className="text-fg-muted"> → {value.ontologyLabels.join("、") || "-"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UnmatchedList({ values }: { values: string[] }) {
  if (values.length === 0) return null;
  return (
    <ul className="mt-1 grid gap-1 text-xs leading-5 text-warning-fg">
      {values.slice(0, 6).map((value) => (
        <li key={value} className="min-w-0 font-mono [overflow-wrap:anywhere]">
          {value}
        </li>
      ))}
    </ul>
  );
}

function SqlOntologyGroundingPanel({
  sqlGraph,
  profileId,
  ontologyGraph,
  notPublished = false,
}: {
  sqlGraph: SqlSemanticGraph | null;
  profileId: string;
  ontologyGraph?: OntologyGraph | null;
  /** 業務プロファイルに公開されたオントロジーが無く、接地確認をしていない(失敗ではない。#1168)。 */
  notPublished?: boolean;
}) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  // ノードと線（関係）は同じ詳細のパネルに出すので、片方を選んだら他方を外す（#1788）。
  const selectNode = (nodeId: string | null) => {
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
  };
  const selectEdge = (edgeId: string | null) => {
    setSelectedEdgeId(edgeId);
    setSelectedNodeId(null);
  };
  const selectedNode = ontologyGraph?.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const selectedEdge = ontologyGraph?.edges.find((edge) => edge.id === selectedEdgeId) ?? null;
  const grounding = useMemo(
    () => groundSqlSemanticGraphOnOntologyGraph(sqlGraph, ontologyGraph ?? null),
    [ontologyGraph, sqlGraph]
  );
  const unmatchedValues = [
    ...grounding.unmatchedTables,
    ...grounding.unmatchedColumns,
    ...grounding.unmatchedJoins,
  ];

  if (!sqlGraph) return null;

  return (
    <section
      className="grid gap-3 rounded-md border border-border bg-surface p-3"
      aria-labelledby="nl2sql-sql-grounding-title"
      data-testid="nl2sql-sql-grounding-panel"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        {/* 見出しが折り返しても 1 行目の中心にそろえる（#2371）。 */}
        <div className="flex min-w-0 items-start gap-2 text-sm">
          <span className="pr-icon-line text-fg">
            <Network size={16} aria-hidden="true" />
          </span>
          <h4 id="nl2sql-sql-grounding-title" className="text-sm font-semibold text-fg">
            {t("nl2sql.interpretation.graphTitle")}
          </h4>
        </div>
        {notPublished ? (
          <StatusBadge
            variant="neutral"
            label={t("nl2sql.interpretation.graphStatus.notPublished")}
          />
        ) : (
          <div className="flex flex-wrap gap-2">
            <GroundingStatusBadge result={grounding} />
            <StatusBadge
              icon={false}
              variant="neutral"
              label={t("nl2sql.interpretation.graphMatchedCount", {
                count:
                  grounding.matchedTables.length +
                  grounding.matchedColumns.length +
                  grounding.matchedJoins.length,
              })}
            />
          </div>
        )}
      </div>

      {notPublished ? (
        <Banner severity="info">
          {t("nl2sql.interpretation.graphNotPublished")}
        </Banner>
      ) : !profileId ? (
        <Banner severity="info">{t("nl2sql.interpretation.graphNoProfile")}</Banner>
      ) : !ontologyGraph ? (
        <Banner severity="warning">{t("nl2sql.interpretation.graphLoadFailed")}</Banner>
      ) : ontologyGraph.nodes.length === 0 ? (
        <Banner severity="info">{t("nl2sql.interpretation.graphEmpty")}</Banner>
      ) : (
        <>
          {grounding.status === "unmatched" ? (
            <Banner severity="warning">{t("nl2sql.interpretation.graphNoMatch")}</Banner>
          ) : unmatchedValues.length > 0 ? (
            <Banner severity="warning" title={t("nl2sql.interpretation.graphPartialTitle")}>
              {t("nl2sql.interpretation.graphPartial")}
              <UnmatchedList values={unmatchedValues} />
            </Banner>
          ) : null}
          {/* 選んだノード・線の詳細は、オントロジー構築の接地確認と同じく、広い幅ではグラフの右、狭い幅ではグラフの上に出す（#1788）。 */}
          <div className="@container min-w-0">
            <div className="grid gap-3 @4xl:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)] @4xl:items-start">
              <div className="order-2 min-w-0 overflow-hidden @4xl:order-1" data-testid="nl2sql-sql-grounding-graph">
                <Suspense
                  fallback={
                    // 中央寄せの文字だけにせず、ほかの読み込み中と同じく先頭の全幅の行とグラフの形の Skeleton（#2510）。
                    <TimedLoadingState
                      label={t("nl2sql.interpretation.graphLoading")}
                      operationKey="sql-grounding-graph"
                      className="h-80 grid-rows-[auto_minmax(0,1fr)]"
                      testId="nl2sql-sql-grounding-graph-loading"
                    >
                      <Skeleton className="h-full w-full" />
                    </TimedLoadingState>
                  }
                >
                  <LazyOntologyGraphCanvas
                    graph={ontologyGraph}
                    selectedNodeId={selectedNodeId}
                    selectedEdgeId={selectedEdgeId}
                    onSelectNode={selectNode}
                    onSelectEdge={selectEdge}
                    highlightNodeIds={grounding.highlightNodeIds}
                    highlightEdgeIds={grounding.highlightEdgeIds}
                    defaultViewMode="grounding"
                  />
                </Suspense>
              </div>
              <div className="order-1 min-w-0 @4xl:order-2">
                <OntologySelectionDetailsPanel
                  graph={ontologyGraph}
                  node={selectedNode}
                  edge={selectedEdge}
                  testId="nl2sql-sql-grounding-details"
                  emptyContent={
                    <p className="text-sm leading-6 text-fg-muted">
                      {t("ontologyPlayground.inspector.nodeEmpty")}
                    </p>
                  }
                />
              </div>
            </div>
          </div>
          <div
            className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3 sm:grid-cols-3"
            aria-label={t("nl2sql.interpretation.graphListAria")}
            data-testid="nl2sql-sql-grounding-list"
          >
            <GroundingMatchRows
              title={t("nl2sql.interpretation.graphMatchedTables")}
              values={grounding.matchedTables}
            />
            <GroundingMatchRows
              title={t("nl2sql.interpretation.graphMatchedColumns")}
              values={grounding.matchedColumns}
            />
            <GroundingMatchRows
              title={t("nl2sql.interpretation.graphMatchedJoins")}
              values={grounding.matchedJoins}
            />
          </div>
        </>
      )}
    </section>
  );
}

function InterpretationArtifactPanel({
  artifact,
  profileId,
}: {
  artifact?: Nl2SqlInterpretationArtifact | null;
  profileId?: string;
}) {
  const sqlGraph = isSqlSemanticGraph(artifact?.sql.semantic_graph)
    ? artifact.sql.semantic_graph
    : null;
  const effectiveProfileId = profileId || artifact?.question.profile_id || "";

  if (!artifact) return null;
  if (!artifact.available) {
    return (
      <Banner severity="info" title={t("nl2sql.interpretation.graphTitle")}>
        {artifact.warnings.join(" ") || t("nl2sql.interpretation.unavailable")}
      </Banner>
    );
  }
  const template = parseQuestionTemplate(artifact.question.original_question);
  const inputFilter = questionSlotValue(template.slots, FILTER_LABELS);
  const inputFilterSlotExists = inputFilter !== undefined;
  const inputFilterEmpty = template.hasTemplate && inputFilterSlotExists && !inputFilter.trim();
  const sqlFilters = artifact.sql.filters.filter(Boolean);
  const hasFilterMismatch = inputFilterEmpty && sqlFilters.length > 0;
  // 「Ontology を使う」OFF(backend echo)のときは接地確認を出さない。未指定の旧データは互換で表示。
  const groundingEnabled = artifact.ontology_grounding_enabled !== false;

  if (!hasFilterMismatch && !(groundingEnabled && sqlGraph)) return null;

  return (
    <div
      className="grid gap-3"
      aria-label={t("nl2sql.interpretation.graphTitle")}
      data-testid="nl2sql-interpretation-panel"
    >
      {hasFilterMismatch && (
        <Banner severity="danger" title={t("nl2sql.interpretation.mismatchTitle")}>
          {t("nl2sql.interpretation.emptyFilterMismatch")}
        </Banner>
      )}
      {groundingEnabled && (
        <SqlOntologyGroundingPanel
          sqlGraph={sqlGraph}
          profileId={effectiveProfileId}
          ontologyGraph={artifact.ontology_graph ?? null}
          notPublished={artifact.ontology_grounding_skip_reason === "no_published_ontology"}
        />
      )}
    </div>
  );
}

/**
 * 生成 SQL の処理手順(決定論 logical_steps)を番号付きで表示する独立パネル。
 * 「処理手順を表示」OFF のときは backend が steps を空にするため描画されない。
 */
function SqlLogicalStepsPanel({
  artifact,
}: {
  artifact?: Nl2SqlInterpretationArtifact | null;
}) {
  const available = artifact?.available ?? false;
  const steps = available ? (artifact?.sql.logical_steps ?? []) : [];
  const stepDetails = available ? (artifact?.sql.logical_step_details ?? []) : [];
  if (steps.length === 0 && stepDetails.length === 0) return null;

  return (
    <section
      className="grid gap-3 rounded-md border border-border bg-surface p-3"
      aria-labelledby="nl2sql-logical-steps-title"
      data-testid="nl2sql-logical-steps-panel"
    >
      <div className="flex min-w-0 items-center gap-2">
        <ListOrdered size={16} className="shrink-0 text-fg" aria-hidden="true" />
        <h4 id="nl2sql-logical-steps-title" className="text-sm font-semibold text-fg">
          {t("nl2sql.logicalSteps.title")}
        </h4>
      </div>
      <LogicalStepsList
        steps={stepDetails}
        fallbackSteps={steps}
        listAriaLabel={t("nl2sql.logicalSteps.listAria")}
      />
    </section>
  );
}

function ShowPromptArtifactPanel({
  artifact,
}: {
  artifact?: Nl2SqlShowPromptArtifact | null;
}) {
  if (!artifact) return null;
  if (!artifact.available) {
    return (
      <Banner severity="info" title={t("nl2sql.showPrompt.title")}>
        {artifact.unavailable_reason || artifact.warnings.join(" ") || t("nl2sql.showPrompt.unavailable")}
      </Banner>
    );
  }

  return (
    <Disclosure
      surface="sunken"
      icon={FileText}
      summary={t("nl2sql.showPrompt.title")}
      data-testid="nl2sql-show-prompt-panel"
    >
      {/* スクロール領域はキーボードでも操作できるよう focus 可能にする(WCAG 2.1.1)。 */}
      <pre data-surface="code"
        tabIndex={0}
        role="region"
        aria-label={t("nl2sql.showPrompt.title")}
        className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface p-3 text-xs leading-5 text-fg"
      >
        <code>{artifact.prompt}</code>
      </pre>
    </Disclosure>
  );
}

/**
 * 生成 SQL のサマリ部（安全/エンジンバッジ・コピー/実行ボタン・SQL コード・説明文）。
 * タイムライン `generate_sql` ステップ内に埋め込めるよう Card に包まない。
 * `onExecute` が渡されたときだけ補助的な実行ボタンを出す。
 */
export function GeneratedSqlSummary({
  result,
  profileId = "",
  executeLoading = false,
  onExecute,
}: {
  result: GeneratedSqlPanelData;
  profileId?: string;
  executeLoading?: boolean;
  onExecute?: () => void;
}) {
  const displayedSql = result.executable_sql || result.generated_sql;

  return (
    <div className="min-w-0 space-y-3">
      <ContentActionBar
        ariaLabel={t("nl2sql.sql.copy")}
        leading={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              variant={result.safety.is_safe ? "success" : "danger"}
              label={result.safety.is_safe ? t("nl2sql.safety.safe") : t("nl2sql.safety.blocked")}
            />
            <span data-surface-tint="neutral" className="rounded-md bg-surface-hover px-2 py-1 text-xs text-fg">
              {engineLabel(result.engine)}
            </span>
            {result.fallback_reason && (
              <span
                data-surface-tint="warning"
                className="rounded-md bg-warning-subtle px-2 py-1 text-xs text-warning-fg"
                title={result.fallback_reason}
                aria-label={t("nl2sql.result.fallbackWithReason", {
                  reason: result.fallback_reason,
                })}
              >
                {t("nl2sql.result.fallback")}
              </span>
            )}
          </div>
        }
        testId="generated-sql-content-actions"
      >
        {onExecute && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            loading={executeLoading}
            disabled={!result.safety.is_safe}
            onClick={onExecute} icon={Play}>
            <span>{t("nl2sql.action.executePreview")}</span>
          </Button>
        )}
        <CopyButton
          size="sm"
          text={displayedSql}
          label={t("nl2sql.sql.copy")}
          copiedMessage={t("nl2sql.sql.copied")}
          failedMessage={t("common.action.copyFailed")}
        />
      </ContentActionBar>
      {/* スクロール領域はキーボードでも操作できるよう focus 可能にする(WCAG 2.1.1)。 */}
      <pre data-surface="code"
        tabIndex={0}
        role="region"
        aria-label={t("nl2sql.sql.region")}
        className="max-h-72 overflow-auto rounded-md border border-border bg-surface p-4 text-sm leading-6 text-fg"
      >
        <code>{displayedSql}</code>
      </pre>
      {result.fallback_reason && (
        <Banner severity="warning" title={t("nl2sql.result.fallbackTitle")}>
          <span className="[overflow-wrap:anywhere]">{result.fallback_reason}</span>
        </Banner>
      )}
      <p className="text-sm leading-6 text-fg">{result.explanation}</p>
      <InterpretationArtifactPanel artifact={result.interpretation} profileId={profileId} />
      <SqlLogicalStepsPanel artifact={result.interpretation} />
      <ShowPromptArtifactPanel artifact={result.show_prompt} />
    </div>
  );
}
