"use client";

import {
  Check,
  Clock3,
  Download,
  FileSearch,
  FileText,
  GitBranch,
  ListTree,
  LocateFixed,
  Pencil,
  RotateCcw,
  Route,
  Save,
  Send,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, useId, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { chunksOnPage, extractionOnPage } from "@/lib/document-inspector-page";

import { CategorySelectFields } from "./CategorySelectFields";
import { DocumentPreview } from "./DocumentPreview";
import { LanguagePackDocumentPanel } from "./LanguagePackDocumentPanel";
import { DocumentRecipeManager } from "./DocumentRecipeManager";
import { DocumentVersionEditor } from "./DocumentVersionEditor";
import { DocumentExtraction, DocumentRawText } from "./DocumentExtraction";
import {
  DocumentChunksJsonDownload,
  DocumentExtractionExportPanel,
} from "./DocumentExtractionExportPanel";
import { ExtractedText, IndexBadge, InfoChip } from "./extraction-bits";
import {
  type ChunkPreviewForm,
  type IngestionParserDisplay,
  type IngestionProgressSummary,
  resolveExtractStageProgress,
  resolveSegmentStrip,
  segmentStatusLabelKey,
  segmentsWithProgress,
  type ProgressUnit,
  type SegmentStripView,
  chunkPreviewForm,
  chunkPreviewValidationError,
  classificationPeriodIsValid,
  ingestConflictBannerIsStale,
  isIndexedTransition,
  phaseForDocumentStatus,
  phaseLabelKey,
  phaseRetryLabelKey,
  phaseRunningMessageKey,
  phaseStartedMessageKey,
  recipeIngestionActive,
  resolveDocumentActionPlan,
  resolveIngestionParserDisplay,
  resolveIngestionProgressSummary,
  resolvePhaseRows,
  resolveStatusMessageSlot,
  selectDocumentRecipe,
  shouldShowProcessingWatchBanner,
  shouldStopLocalProcessingWatch,
} from "./DocumentWorkspace.logic";
import {
  normalizeIngestionErrorMessage,
  resolveDocumentFailureView,
  resolveIngestionErrorDisplayPlan,
  splitErrorCode,
} from "./ingestion-error-display";
import { useRecipeOperationError } from "./recipe-operation-error";
import { IngestionSegmentStrip } from "./IngestionSegmentStrip";
import { ReviewTextEditor } from "./ReviewTextEditor";
import { KnowledgeBaseScopePicker } from "@/components/knowledge-bases/KnowledgeBaseScopePicker";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Banner,
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CopyButton,
  Disclosure,
  FieldError,
  FormGrid,
  FormStatus,
  INFORMATION_LIST_SCROLL_CLASS,
  ListSkeleton,
  ProcessingIndicator,
  SelectField,
  type SelectFieldOption,
  Skeleton,
  TabPanel,
  Tabs,
  TextField,
  TimedLoadingState,
  apiErrorMessage,
  surfaceTintFor,
} from "@production-ready/ui";
import { EmptyState, ErrorState } from "@/components/StateViews";
import {
  api,
  ApiError,
  type DocumentElement,
  type ChunkingStrategyName,
  type DocumentChunkPreviewResponse,
  type DocumentChunkView,
  type DocumentClassification,
  type DocumentRecipeStep,
  type DocumentRecipeStepStatus,
  type DocumentReviewEditsRequest,
  type ExtractionTable,
  type ExtractionTableCell,
  type IngestionJob,
  type IngestionJobProgress,
  type IngestionJobPhase,
  type IngestionSegment,
  type KnowledgeBaseRef,
  type SourceProfile,
} from "@/lib/api";
import {
  CHUNK_OVERLAP_MAX_CHARS,
  CHUNK_SIZE_MAX_CHARS,
  CHUNK_SIZE_MIN_CHARS,
  SMALL_TO_BIG_PARAMS,
  chunkSizeLabelKey,
  smallToBigFellBack,
  chunkingStrategyPreset,
  isSemanticBoundaryStrategy,
  overlapLabelKey,
} from "@/lib/chunking";
import { parseStructuredExtraction, type SourceDerivationView } from "@/lib/extraction";
import {
  documentWorkspaceShouldRefresh,
  ingestionJobIsActive,
  initialLoadError,
  useDocument,
  useDocumentChunkSets,
  useDocumentRecipeChunks,
  useDocumentRecipeExtractionExport,
  useDocumentRecipes,
  useDocumentIngestionJobs,
  useDocumentIngestionSegments,
  useDocumentKnowledgeBases,
  useApproveDocumentRecipe,
  useEnqueueDocumentRecipeJob,
  useIngestionJob,
  useModelSettings,
  usePreviewDocumentRecipeChunks,
  useReplaceDocumentKnowledgeBases,
  useRetryFailedDocumentIngestionSegments,
  useSaveDocumentClassification,
  useSaveDocumentRecipeReviewEdits,
} from "@/lib/queries";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { toast } from "@/lib/toast";
import { t, type I18nKey } from "@/lib/i18n";
import { recipeQueueWait } from "@/lib/ingestion-queue";
import { useCustomLeaveGuard, useLeaveGuard } from "@/lib/leave-guard";
import { formatBytes, formatDateTime, formatNumber, parseApiDateTime } from "@/lib/format";
import {
  nearestVerticalScrollContainer,
  revealWithinScrollContainer,
  scrollFocusedControlIntoView,
} from "@/lib/focus-scroll";
import { useValuesChanged } from "@/lib/render-sync";
import { useNowMs } from "@/lib/use-now-ms";
import {
  type BboxCoordinateMode,
  type BboxOverlayUnit,
  type BboxPageSize,
  bboxCoordinateModeFromMetadata,
  bboxFromMetadata,
  bboxPageRotationFromMetadata,
  bboxPageSizeFromMetadata,
  bboxUnitFromMetadata,
  buildPreviewHighlights,
  displayRegionsFromMetadata,
  withBboxPageRotation,
} from "@production-ready/ui";
import {
  isSameParserBackend,
  parserBackendLabel,
  parserProfileKey,
  unsupportedReasonLabel,
  visibleSourceWarningKeys,
} from "@/lib/source-profile-labels";
import {
  findTableCellTarget,
  tableCellKey,
  type TableCellFocusTarget,
} from "@/lib/table-cell-focus";
import { cn } from "@/lib/utils";
import {
  excelWarningLabel,
  isHeaderLowConfidenceWarning,
  lowConfidenceHeaderSheets,
} from "@/lib/excel-warnings";

const DOCUMENT_WORKSPACE_REFETCH_INTERVAL_MS = 4000;

/** 承認待ちゲート案内(状態メッセージ単一スロット)の文言キー。 */
const GATE_MESSAGE_KEYS = {
  PREPROCESSED: "flow.preprocessed.description",
  REVIEW: "flow.review.description",
  CHUNKED: "flow.chunked.description",
} as const;

function emptyReviewEdits(): DocumentReviewEditsRequest {
  return { element_edits: [], table_cell_edits: [] };
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function workspaceElementKey(element: DocumentElement): string {
  return element.element_id || `el-${String(element.order).padStart(4, "0")}`;
}

function integerSearchParam(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

function numberSearchParam(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function bboxSearchParam(value: string | null): number[] | null {
  return bboxFromMetadata(value ? { bbox: value } : null);
}

function bboxModeSearchParam(value: string | null): BboxCoordinateMode | null {
  return value === "xyxy" || value === "xywh" ? value : null;
}

function bboxUnitSearchParam(value: string | null): BboxOverlayUnit | null {
  return value === "ratio" || value === "percent" || value === "absolute" ? value : null;
}

function pageSizeSearchParams(
  width: string | null,
  height: string | null,
  rotation: string | null
): BboxPageSize | null {
  const parsedWidth = numberSearchParam(width);
  const parsedHeight = numberSearchParam(height);
  return withBboxPageRotation(
    parsedWidth && parsedHeight ? { width: parsedWidth, height: parsedHeight } : null,
    integerSearchParam(rotation)
  );
}

function findTableCellByKey(
  tables: ExtractionTable[],
  key: string | null
): TableCellFocusTarget | null {
  if (!key) return null;
  for (const table of tables) {
    for (const cell of table.cells) {
      const candidateKey = tableCellKey(table.table_id, cell);
      if (candidateKey === key) return { key, table, cell };
    }
  }
  return null;
}

type WorkspaceFocusRequest = {
  key: string;
  target: "chunk" | "element" | "table_cell";
};

type UrlFallbackFocus = {
  key: string;
  page: number | null;
  bbox: number[] | null;
  bboxMode: BboxCoordinateMode | null;
  bboxUnit: BboxOverlayUnit | null;
  pageSize: BboxPageSize | null;
};

const CHUNK_PREVIEW_STRATEGIES: SelectFieldOption<ChunkingStrategyName>[] = [
  "structure_aware",
  "recursive_character",
  "small_to_big",
  "markdown_heading",
  "page_level",
  "fixed_size",
  "fixed_delimiter",
].map((value) => ({
  value: value as ChunkingStrategyName,
  label: t(`settings.chunking.strategy.${value}` as I18nKey),
}));

/** 文書プレビュー作業領域：原本プレビュー｜本文・構造化要素＋取込アクション。 */
export function DocumentWorkspace({
  documentId,
  watchProcessing = false,
  initialSourceProfile = null,
  showTitle = true,
}: {
  documentId: string;
  watchProcessing?: boolean;
  initialSourceProfile?: SourceProfile | null;
  /**
   * カードの見出し（ファイル名と状態）を出すか。文書詳細（`/documents/:id`）はページの `PageHeader` が
   * ファイル名・状態を持つので出さない（#581）。アップロード直後の画面では出す。
   */
  showTitle?: boolean;
}) {
  const query = useDocument(documentId);
  const [searchParams, setSearchParams] = useSearchParams();
  const recipesQuery = useDocumentRecipes(documentId);
  const requestedRecipeId = searchParams.get("recipe");
  const selectedRecipe = selectDocumentRecipe(recipesQuery.data, requestedRecipeId);
  const selectedRecipeId = selectedRecipe?.recipe_id ?? null;
  const hasSelectedRecipeExtraction = Boolean(selectedRecipe?.active_extraction_recipe_id);
  useEffect(() => {
    if (!selectedRecipeId || requestedRecipeId === selectedRecipeId) return;
    const next = new URLSearchParams(searchParams);
    next.set("recipe", selectedRecipeId);
    setSearchParams(next, { replace: true });
  }, [requestedRecipeId, searchParams, selectedRecipeId, setSearchParams]);
  const selectRecipe = async (recipeId: string) => {
    // 抽出確認の未保存の修正は、そのレシピの抽出結果への修正。別のレシピへ持ち越さないよう、
    // 切り替える前に破棄を確認する（同じ解析の複製レシピは要素 ID が同じで、そのまま保存できてしまう。#944）。
    if (recipeId !== selectedRecipeId && hasReviewEdits) {
      const confirmed = await confirm({
        title: t("flow.review.edit.discardTitle"),
        description: t("flow.review.edit.switchRecipeDescription"),
        confirmLabel: t("flow.review.edit.switchRecipeConfirm"),
        tone: "warning",
      });
      if (!confirmed) return;
    }
    const next = new URLSearchParams(searchParams);
    next.set("recipe", recipeId);
    next.delete("chunk_id");
    next.delete("element_id");
    setSearchParams(next, { replace: true });
    setSelectedChunkId(null);
    setSelectedElementId(null);
    // 前のレシピへの投入・承認・再試行の結果（エラーや実行中の job）を次のレシピへ持ち越さない（#281）。
    enqueueIngestion.reset();
    approveDocument.reset();
    retryFailedSegments.reset();
    saveReviewEdits.reset();
  };
  const chunksQuery = useDocumentRecipeChunks(documentId, selectedRecipeId);
  const chunkPreview = usePreviewDocumentRecipeChunks();
  const [chunkPreviewSettings, setChunkPreviewSettings] = useState<ChunkPreviewForm>(() =>
    chunkPreviewForm(selectedRecipe)
  );
  const resetChunkPreview = chunkPreview.reset;
  // 選ぶレシピが変わったレンダーで、分割プレビューの入力をそのレシピの値に戻す。
  const recipeChanged = useValuesChanged([selectedRecipe, selectedRecipeId]);
  if (recipeChanged) setChunkPreviewSettings(chunkPreviewForm(selectedRecipe));
  // 前のレシピのプレビュー結果（mutation の状態）は effect で捨てる。
  useEffect(() => {
    resetChunkPreview();
  }, [resetChunkPreview, selectedRecipe, selectedRecipeId]);
  const chunkSetsQuery = useDocumentChunkSets(documentId);
  const documentJobsQuery = useDocumentIngestionJobs(documentId);
  const segmentsQuery = useDocumentIngestionSegments(documentId);
  // embedding は global 単一固定（既定の埋め込みモデル。#45）。診断の付帯情報のため、取得失敗時・
  // 未選択のときはチップを出さないだけにする。次元は backend の設定（Oracle の VECTOR 列）の値。
  const modelSettingsQuery = useModelSettings();
  const embeddingModel =
    modelSettingsQuery.data?.settings.enterprise_ai.default_embedding_model_id?.trim() || null;
  const embeddingDim = modelSettingsQuery.data?.embedding_dim ?? null;
  // 抽出結果は JSON で 1 回だけ取得し、構造化要素・parser の表示・抽出エクスポートの状態に使う。
  // Markdown / HTML は、抽出エクスポートでコピー・ダウンロードするときだけ取得する（#561）。
  const extractionJsonQuery = useDocumentRecipeExtractionExport(
    documentId,
    hasSelectedRecipeExtraction ? selectedRecipeId : null,
    "json"
  );
  const enqueueIngestion = useEnqueueDocumentRecipeJob();
  const approveDocument = useApproveDocumentRecipe();
  const saveReviewEdits = useSaveDocumentRecipeReviewEdits();
  const confirm = useConfirm();
  const retryFailedSegments = useRetryFailedDocumentIngestionSegments();
  const queuedJob = useIngestionJob(enqueueIngestion.data?.id ?? null);
  const approvedJob = useIngestionJob(approveDocument.data?.id ?? null);
  const retriedSegmentJob = useIngestionJob(retryFailedSegments.data?.id ?? null);
  const [localWatchProcessing, setLocalWatchProcessing] = useState(false);
  const [editingReview, setEditingReview] = useState(false);
  const [reviewEdits, setReviewEdits] =
    useState<DocumentReviewEditsRequest>(emptyReviewEdits);
  const [selectedElementId, setSelectedElementId] = useState<string | null>(null);
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(null);
  const [selectedTableCellKey, setSelectedTableCellKey] = useState<string | null>(null);
  const [previewVariant, setPreviewVariant] = useState<"original" | "prepared">("original");
  const [previewFocusSource, setPreviewFocusSource] =
    // page は章節ナビゲーションから開いたページ（#713）。強調の枠は出さない。
    useState<"chunk" | "element" | "table_cell" | "page">("chunk");
  const [focusRequest, setFocusRequest] = useState<WorkspaceFocusRequest | null>(null);
  const [urlFallbackFocus, setUrlFallbackFocus] = useState<UrlFallbackFocus | null>(null);
  const pageFocusSerialRef = useRef(0);
  // 適用済みの URL フォーカス要求（同じ要求で選択を上書きし直さないため）。
  const [appliedFocusRequest, setAppliedFocusRequest] = useState<string | null>(null);
  const requestedChunkId = searchParams.get("chunk_id");
  const requestedElementId = searchParams.get("element_id");
  const requestedTableId = searchParams.get("table_id");
  const requestedCellRefParam = searchParams.get("cell_ref");
  const requestedFormulaCellRef = searchParams.get("formula_cell_ref");
  const requestedCellRef = requestedFormulaCellRef ?? requestedCellRefParam;
  const requestedCellRow = integerSearchParam(searchParams.get("cell_row"));
  const requestedCellCol = integerSearchParam(searchParams.get("cell_col"));
  const hasReviewEdits =
    (reviewEdits.element_edits?.length ?? 0) > 0 ||
    (reviewEdits.table_cell_edits?.length ?? 0) > 0;
  // 選んでいるレシピが変わったら（切り替え・削除後の切り替え・URL の変更）、抽出確認の編集を閉じて修正を捨てる（#944）。
  const reviewRecipeChanged = useValuesChanged([selectedRecipeId]);
  if (reviewRecipeChanged && (editingReview || hasReviewEdits)) {
    setEditingReview(false);
    setReviewEdits(emptyReviewEdits());
  }
  // 抽出確認の未保存編集は共有の離脱ガードで守る（内部リンク・サイドナビ・再読込・タブを閉じる）。
  useCustomLeaveGuard(hasReviewEdits, () =>
    confirm({
      title: t("flow.review.edit.leaveTitle"),
      description: t("flow.review.edit.leaveDescription"),
      confirmLabel: t("flow.review.edit.leaveConfirm"),
      tone: "warning",
    })
  );
  // インスペクタ右ペインのタブ。要素/表セル指定の deep-link は構造化要素を、
  // chunk のみの引用 deep-link は Chunk タブを初期表示する。
  const [inspectorTab, setInspectorTab] = useState<"text" | "extraction" | "chunks" | "export">(() => {
    if (requestedElementId || requestedTableId || requestedCellRef) return "extraction";
    return requestedChunkId ? "chunks" : "text";
  });
  const requestedUrlFocus = useMemo<UrlFallbackFocus | null>(() => {
    const page = integerSearchParam(searchParams.get("page"));
    const bbox = bboxSearchParam(searchParams.get("bbox"));
    if (!page && !bbox) return null;
    return {
      key: [
        "citation",
        requestedChunkId ?? "",
        requestedElementId ?? "",
        requestedTableId ?? "",
        requestedFormulaCellRef ?? "",
        requestedCellRef ?? "",
        String(requestedCellRow ?? ""),
        String(requestedCellCol ?? ""),
        String(page ?? ""),
        searchParams.get("bbox") ?? "",
      ].join("\u0000"),
      page,
      bbox,
      bboxMode: bboxModeSearchParam(searchParams.get("bbox_mode")),
      bboxUnit: bboxUnitSearchParam(searchParams.get("bbox_unit")),
      pageSize: pageSizeSearchParams(
        searchParams.get("page_width"),
        searchParams.get("page_height"),
        searchParams.get("page_rotation")
      ),
    };
  }, [
    requestedCellCol,
    requestedCellRef,
    requestedCellRow,
    requestedChunkId,
    requestedElementId,
    requestedFormulaCellRef,
    requestedTableId,
    searchParams,
  ]);
  const recipeJobs = useMemo(
    () =>
      documentJobsQuery.data?.filter((job) => job.recipe_id === selectedRecipeId) ?? [],
    [documentJobsQuery.data, selectedRecipeId]
  );
  const recipeSegments = useMemo(
    () => segmentsQuery.data?.filter((segment) => segment.recipe_id === selectedRecipeId) ?? [],
    [segmentsQuery.data, selectedRecipeId]
  );
  // 見出しの状態（DocumentDetailPage の documentDisplayStatus）と同じ規則。
  const status = selectedRecipe?.status ?? query.data?.status ?? "UPLOADED";
  const latestDocumentJob = recipeJobs[0] ?? null;
  const latestDocumentJobActive = ingestionJobIsActive(latestDocumentJob?.status);
  const queuedIngestionJobStatus = queuedJob.data?.status ?? enqueueIngestion.data?.status;
  const approvedIngestionJobStatus = approvedJob.data?.status ?? approveDocument.data?.status;
  const retriedSegmentJobStatus = retriedSegmentJob.data?.status ?? retryFailedSegments.data?.status;
  const queuedJobErrorMessage =
    queuedJob.data?.status === "FAILED"
      ? queuedJob.data.error_message ?? t("flow.ingestFailed")
      : null;
  const retriedSegmentJobErrorMessage =
    retriedSegmentJob.data?.status === "FAILED"
      ? retriedSegmentJob.data.error_message ?? t("flow.ingestFailed")
      : null;
  const watchedJobStatuses = [
    latestDocumentJob?.status,
    queuedIngestionJobStatus,
    approvedIngestionJobStatus,
    retriedSegmentJobStatus,
  ];
  const activeSubmittedJob = watchedJobStatuses.some(ingestionJobIsActive);
  // 次の工程・再処理の job が worker の空きを待っている工程（見出しの「〈工程〉の順番待ち」）。
  const recipeQueuedPhase = recipeQueueWait(selectedRecipe)?.phase ?? null;
  // 再処理の job が順番待ち・実行中の間は、レシピが claim まで持つ前回の失敗を出さない（#2687）。
  const ingestionActive = recipeIngestionActive(selectedRecipe, watchedJobStatuses);
  // 新しい job が動いている間は、この画面で前に失敗した承認・投入の job を失敗として拾わない（#2687）。
  const failedDocumentJob = ingestionActive
    ? null
    : ([latestDocumentJob, queuedJob.data, approvedJob.data, retriedSegmentJob.data].find(
        (job) => job?.status === "FAILED"
      ) ?? null);
  const autoRefreshActive = documentWorkspaceShouldRefresh({
    documentStatus: status,
    watchProcessing,
    localWatchProcessing,
    jobStatuses: [
      latestDocumentJob?.status,
      queuedIngestionJobStatus,
      approvedIngestionJobStatus,
      retriedSegmentJobStatus,
    ],
    segmentStatuses: recipeSegments.map((segment) => segment.status),
  });
  // レシピの step で FAILED の工程（ステップ表示と同じ工程を Banner でも失敗として示す。#329）。
  const failedRecipePhase =
    selectedRecipe?.steps.find((step) => step.status === "FAILED")?.phase ?? null;
  // 文書失敗を 1 本化（messaging-spec §9 P2/P5）: 原因 1 本 + 失敗工程の導出。
  const documentFailure = useMemo(
    () =>
      resolveDocumentFailureView({
        documentStatus: status,
        ingestionActive,
        latestJobStatus: failedDocumentJob?.status ?? latestDocumentJob?.status,
        latestJobPhase: failedDocumentJob?.phase ?? latestDocumentJob?.phase,
        failedRecipePhase,
        latestJobErrorMessage:
          failedDocumentJob?.error_message ?? latestDocumentJob?.error_message,
        segments: recipeSegments,
        documentErrorMessage: selectedRecipe?.error_message ?? query.data?.error_message,
      }),
    [
      status,
      ingestionActive,
      failedDocumentJob?.status,
      failedDocumentJob?.phase,
      failedRecipePhase,
      failedDocumentJob?.error_message,
      latestDocumentJob?.status,
      latestDocumentJob?.phase,
      latestDocumentJob?.error_message,
      recipeSegments,
      selectedRecipe?.error_message,
      query.data?.error_message,
    ]
  );
  // 失敗工程のタイトル用 phase(failedStep は同じ入力から導出されるため対応が取れる)。
  const failedPhase = documentFailure.failedStep
    ? failedRecipePhase ?? failedDocumentJob?.phase ?? latestDocumentJob?.phase ?? null
    : null;
  const ingestionErrorDisplays = useMemo(
    () =>
      resolveIngestionErrorDisplayPlan({
        latestJobErrorMessage: latestDocumentJob?.error_message,
        segments: recipeSegments,
        documentErrorMessage: selectedRecipe?.error_message ?? query.data?.error_message,
        queuedJobErrorMessage,
        retriedSegmentJobErrorMessage,
        // 上部の原因バナーに昇格した本文は詳細側で再掲しない（§9 P2）。
        suppressMessages: [documentFailure.primaryMessage],
      }),
    [
      latestDocumentJob?.error_message,
      recipeSegments,
      selectedRecipe?.error_message,
      query.data?.error_message,
      queuedJobErrorMessage,
      retriedSegmentJobErrorMessage,
      documentFailure.primaryMessage,
    ]
  );
  // 取込・診断の折りたたみ。通常は閉じておき、取込中/失敗/エラー/セグメント失敗時のみ自動展開する。
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const diagnosticsAutoOpenedRef = useRef(false);
  // 索引完了は常設バナーではなく遷移時 toast で 1 回だけ通知する(messaging-spec §3.4)。
  const prevRecipeStatusRef = useRef<{ recipeId: string | null; status: string } | null>(null);
  useEffect(() => {
    const prev = prevRecipeStatusRef.current;
    const next = { recipeId: selectedRecipeId, status };
    prevRecipeStatusRef.current = next;
    if (isIndexedTransition(prev, next)) {
      toast.success(t("flow.indexed"));
    }
  }, [selectedRecipeId, status]);
  const diagnosticsHasActivity =
    status === "INGESTING" ||
    status === "ERROR" ||
    latestDocumentJobActive ||
    latestDocumentJob?.status === "FAILED" ||
    ingestionErrorDisplays.segmentIds.size > 0;
  useEffect(() => {
    if (diagnosticsHasActivity && !diagnosticsAutoOpenedRef.current) {
      diagnosticsAutoOpenedRef.current = true;
      setDiagnosticsOpen(true);
    }
  }, [diagnosticsHasActivity]);
  // 投入・承認の失敗は、失敗したときからレシピの状態が変わっていない間だけ出す（#2639）。
  const visibleApproveError = useRecipeOperationError(approveDocument.error, selectedRecipe);
  const visibleEnqueueError = useRecipeOperationError(enqueueIngestion.error, selectedRecipe);
  const approveErrorText =
    visibleApproveError != null ? errorMessage(visibleApproveError, t("flow.approveFailed")) : "";
  const saveReviewErrorText = saveReviewEdits.isError
    ? errorMessage(saveReviewEdits.error, t("flow.review.edit.saveError"))
    : "";
  const selectedExtraction = useMemo(
    () => selectedRecipe ? (extractionJsonQuery.data?.payload ?? {}) : {},
    [selectedRecipe, extractionJsonQuery.data?.payload]
  );
  const parsedExtraction = useMemo(
    () =>
      parseStructuredExtraction(
        selectedRecipe ? (extractionJsonQuery.data?.payload ?? {}) : {}
      ),
    [extractionJsonQuery.data?.payload, selectedRecipe]
  );
  const reviewHeaderSheets = useMemo(
    () => lowConfidenceHeaderSheets(parsedExtraction.warnings),
    [parsedExtraction.warnings]
  );
  const latestChunkSet = selectedRecipe?.active_chunk_set_id ?? null;
  const handlePhaseRestart = async (
    phase: IngestionJobPhase,
    mode: "reprocess" | "retry"
  ) => {
    if (!selectedRecipeId) return;
    const confirmed = await confirm({
      title: t(`flow.${mode}.${phase}.title` as I18nKey),
      description: t(`flow.${mode}.${phase}.description` as I18nKey),
      confirmLabel: t(mode === "retry" ? "flow.retry.confirm" : "flow.reprocess.confirm"),
      tone: "warning",
    });
    if (!confirmed) return;
    enqueueIngestion.mutate(
      { id: documentId, recipeId: selectedRecipeId, phase },
      {
        onSuccess: (job) => {
          setLocalWatchProcessing(job.status === "QUEUED" || job.status === "RUNNING");
        },
      }
    );
  };
  const refetchDocument = query.refetch;
  const refetchChunks = chunksQuery.refetch;
  const refetchChunkSets = chunkSetsQuery.refetch;
  const refetchDocumentJobs = documentJobsQuery.refetch;
  const refetchSegments = segmentsQuery.refetch;
  const refetchRecipes = recipesQuery.refetch;
  const refetchExtractionJson = extractionJsonQuery.refetch;
  // 変換後の原本がないレシピでは、変換後のプレビューを選べないので原本に戻す（render 中に調整）。
  if (!selectedRecipe?.preprocess_artifact && previewVariant === "prepared") {
    setPreviewVariant("original");
  }
  const resetEnqueueIngestion = enqueueIngestion.reset;
  // 409 の後に進行中ジョブを観測したか。観測する前の 409 は消さない（#281）。
  const conflictObservedActiveJobRef = useRef(false);
  useEffect(() => {
    const errorStatus =
      enqueueIngestion.error instanceof ApiError ? enqueueIngestion.error.status : null;
    if (errorStatus !== 409) {
      conflictObservedActiveJobRef.current = false;
      return;
    }
    if (activeSubmittedJob) conflictObservedActiveJobRef.current = true;
    if (
      ingestConflictBannerIsStale({
        errorStatus,
        hasActiveJob: activeSubmittedJob,
        observedActiveJob: conflictObservedActiveJobRef.current,
      })
    ) {
      conflictObservedActiveJobRef.current = false;
      resetEnqueueIngestion();
    }
  }, [enqueueIngestion.error, activeSubmittedJob, resetEnqueueIngestion]);
  const displayedChunks = useMemo(
    () => chunkPreview.data?.chunks ?? chunksQuery.data ?? [],
    [chunkPreview.data?.chunks, chunksQuery.data]
  );
  const selectedChunk = useMemo(
    () => displayedChunks.find((chunk) => chunk.chunk_id === selectedChunkId) ?? null,
    [displayedChunks, selectedChunkId]
  );
  const selectedElement = useMemo(
    () =>
      parsedExtraction.elements.find(
        (element) => workspaceElementKey(element) === selectedElementId
      ) ??
      null,
    [parsedExtraction.elements, selectedElementId]
  );
  const selectedTableCell = useMemo(
    () => findTableCellByKey(parsedExtraction.tables, selectedTableCellKey),
    [parsedExtraction.tables, selectedTableCellKey]
  );
  const focusPage =
    previewFocusSource === "page"
      ? null
      : previewFocusSource === "table_cell"
      ? selectedTableCell?.cell.page_number ??
        selectedTableCell?.table.page_number ??
        selectedElement?.page_number ??
        selectedChunk?.page_start ??
        null
      : previewFocusSource === "element"
        ? selectedElement?.page_number ?? selectedChunk?.page_start ?? null
        : selectedChunk?.page_start ?? selectedElement?.page_number ?? null;
  const effectiveFocusPage = focusPage ?? urlFallbackFocus?.page ?? null;
  const selectedChunkBbox = selectedChunk?.bbox ?? bboxFromMetadata(selectedChunk?.metadata);
  const selectedElementBbox =
    selectedElement?.bbox ?? bboxFromMetadata(selectedElement?.metadata);
  const selectedTableCellBbox =
    selectedTableCell?.cell.bbox ?? bboxFromMetadata(selectedTableCell?.cell.metadata);
  const focusBbox =
    previewFocusSource === "page"
      ? null
      : previewFocusSource === "table_cell"
      ? selectedTableCellBbox ?? selectedElementBbox ?? selectedChunkBbox ?? null
      : previewFocusSource === "element"
        ? selectedElementBbox ?? selectedChunkBbox ?? null
        : selectedChunkBbox ?? selectedElementBbox ?? null;
  const effectiveFocusBbox = focusBbox ?? urlFallbackFocus?.bbox ?? null;
  const focusBboxMode =
    previewFocusSource === "table_cell"
      ? bboxCoordinateModeFromMetadata(selectedTableCell?.cell.metadata) ??
        bboxCoordinateModeFromMetadata(selectedTableCell?.table.metadata) ??
        bboxCoordinateModeFromMetadata(selectedElement?.metadata) ??
        bboxCoordinateModeFromMetadata(selectedChunk?.metadata)
      : previewFocusSource === "element"
        ? bboxCoordinateModeFromMetadata(selectedElement?.metadata) ??
          bboxCoordinateModeFromMetadata(selectedChunk?.metadata)
        : bboxCoordinateModeFromMetadata(selectedChunk?.metadata) ??
          bboxCoordinateModeFromMetadata(selectedElement?.metadata);
  const effectiveFocusBboxMode = focusBboxMode ?? urlFallbackFocus?.bboxMode ?? null;
  const focusBboxUnit =
    previewFocusSource === "table_cell"
      ? bboxUnitFromMetadata(selectedTableCell?.cell.metadata) ??
        bboxUnitFromMetadata(selectedTableCell?.table.metadata) ??
        bboxUnitFromMetadata(selectedElement?.metadata) ??
        bboxUnitFromMetadata(selectedChunk?.metadata)
      : previewFocusSource === "element"
        ? bboxUnitFromMetadata(selectedElement?.metadata) ??
          bboxUnitFromMetadata(selectedChunk?.metadata)
        : bboxUnitFromMetadata(selectedChunk?.metadata) ??
          bboxUnitFromMetadata(selectedElement?.metadata);
  const effectiveFocusBboxUnit = focusBboxUnit ?? urlFallbackFocus?.bboxUnit ?? null;
  const focusPageSizeFromMetadata =
    previewFocusSource === "table_cell"
      ? bboxPageSizeFromMetadata(selectedTableCell?.cell.metadata) ??
        bboxPageSizeFromMetadata(selectedTableCell?.table.metadata) ??
        bboxPageSizeFromMetadata(selectedElement?.metadata) ??
        bboxPageSizeFromMetadata(selectedChunk?.metadata)
      : previewFocusSource === "element"
        ? bboxPageSizeFromMetadata(selectedElement?.metadata) ??
          bboxPageSizeFromMetadata(selectedChunk?.metadata)
        : bboxPageSizeFromMetadata(selectedChunk?.metadata) ??
          bboxPageSizeFromMetadata(selectedElement?.metadata);
  const focusPageRotationFromMetadata =
    previewFocusSource === "table_cell"
      ? bboxPageRotationFromMetadata(selectedTableCell?.cell.metadata) ??
        bboxPageRotationFromMetadata(selectedTableCell?.table.metadata) ??
        bboxPageRotationFromMetadata(selectedElement?.metadata) ??
        bboxPageRotationFromMetadata(selectedChunk?.metadata)
      : previewFocusSource === "element"
        ? bboxPageRotationFromMetadata(selectedElement?.metadata) ??
          bboxPageRotationFromMetadata(selectedChunk?.metadata)
        : bboxPageRotationFromMetadata(selectedChunk?.metadata) ??
          bboxPageRotationFromMetadata(selectedElement?.metadata);
  const focusPageSize = useMemo(() => {
    if (!effectiveFocusPage) return null;
    const page = parsedExtraction.pages.find((item) => item.page_number === effectiveFocusPage);
    if (page?.width && page?.height) {
      return withBboxPageRotation(
        { width: page.width, height: page.height },
        page.rotation ?? focusPageRotationFromMetadata ?? urlFallbackFocus?.pageSize?.rotation
      );
    }
    return withBboxPageRotation(
      focusPageSizeFromMetadata ?? urlFallbackFocus?.pageSize ?? null,
      focusPageRotationFromMetadata ?? urlFallbackFocus?.pageSize?.rotation
    );
  }, [
    effectiveFocusPage,
    focusPageRotationFromMetadata,
    focusPageSizeFromMetadata,
    parsedExtraction.pages,
    urlFallbackFocus,
  ]);
  // chunk の強調は、要素の表示領域（根拠にした要素ごとの bbox）があれば要素ごとに重ねる（#349）。
  const focusChunkRegions = useMemo(
    () =>
      previewFocusSource === "chunk" && selectedChunk
        ? displayRegionsFromMetadata(selectedChunk.metadata)
        : [],
    [previewFocusSource, selectedChunk]
  );
  const previewHighlights = useMemo(
    () =>
      buildPreviewHighlights({
        focusPage: effectiveFocusPage,
        focusBbox: effectiveFocusBbox,
        focusBboxMode: effectiveFocusBboxMode,
        focusBboxUnit: effectiveFocusBboxUnit,
        focusPageSize,
        regions: focusChunkRegions,
        pageSizeFor: (pageNumber) => {
          const page = parsedExtraction.pages.find((item) => item.page_number === pageNumber);
          return page?.width && page?.height
            ? withBboxPageRotation({ width: page.width, height: page.height }, page.rotation)
            : null;
        },
      }),
    [
      effectiveFocusBbox,
      effectiveFocusBboxMode,
      effectiveFocusBboxUnit,
      effectiveFocusPage,
      focusChunkRegions,
      focusPageSize,
      parsedExtraction.pages,
    ]
  );
  function selectElement(elementId: string) {
    const linkedChunk = displayedChunks.find((chunk) => chunk.element_ids.includes(elementId));
    setUrlFallbackFocus(null);
    setSelectedElementId(elementId);
    setSelectedTableCellKey(null);
    setSelectedChunkId(linkedChunk?.chunk_id ?? null);
    setPreviewFocusSource("element");
  }

  /** 章節を押したとき、プレビューをそのページへ移す（#713）。chunk・要素の選択は残す。 */
  function selectPage(page: number) {
    // 同じページを続けて押しても、プレビューを移し直す。
    pageFocusSerialRef.current += 1;
    setPreviewFocusSource("page");
    setUrlFallbackFocus({
      key: `section-page-${page}-${pageFocusSerialRef.current}`,
      page,
      bbox: null,
      bboxMode: null,
      bboxUnit: null,
      pageSize: null,
    });
  }

  function selectTableCell(table: ExtractionTable, cell: ExtractionTableCell) {
    const key = tableCellKey(table.table_id, cell);
    const linkedElementId = table.element_id ?? null;
    const linkedChunk = linkedElementId
      ? displayedChunks.find((chunk) => chunk.element_ids.includes(linkedElementId))
      : displayedChunks.find((chunk) => chunk.content_kind === "table");
    setSelectedTableCellKey(key);
    setUrlFallbackFocus(null);
    setSelectedElementId(linkedElementId);
    setSelectedChunkId(linkedChunk?.chunk_id ?? selectedChunkId ?? null);
    setPreviewFocusSource("table_cell");
  }

  useEffect(() => {
    if (!autoRefreshActive) return;
    const timer = window.setInterval(() => {
      void refetchDocument();
      void refetchDocumentJobs();
      void refetchSegments();
      void refetchChunks();
      void refetchChunkSets();
      void refetchRecipes();
      if (selectedRecipe?.active_extraction_recipe_id) {
        void refetchExtractionJson();
      }
    }, DOCUMENT_WORKSPACE_REFETCH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [
    autoRefreshActive,
    refetchChunkSets,
    refetchChunks,
    refetchDocument,
    refetchDocumentJobs,
    refetchExtractionJson,
    refetchRecipes,
    refetchSegments,
    selectedRecipe?.active_extraction_recipe_id,
  ]);

  // ジョブの実行中だけ 1 秒ごとに時計を進め、ジョブが変わったら刻み直す。
  const elapsedNowMs = useNowMs(
    latestDocumentJobActive,
    `${latestDocumentJob?.id ?? ""}\u0000${latestDocumentJob?.started_at ?? ""}`
  );

  // ジョブの有無か状態が変わったレンダーで、処理が終わっていれば投入直後の監視をやめる。
  const watchSourceChanged = useValuesChanged([activeSubmittedJob, status, queuedIngestionJobStatus]);
  if (
    watchSourceChanged &&
    shouldStopLocalProcessingWatch({
      activeSubmittedJob,
      documentStatus: status,
      submittedJobStatus: queuedIngestionJobStatus,
    })
  ) {
    setLocalWatchProcessing(false);
  }

  // URL の指定（chunk / 要素 / 表セル / ページ）と表示中の chunk が変わったレンダーで、選択とフォーカス要求を合わせる。
  // 依存が変わったときだけ動かす（effect の deps と同じ）ので、同じ要求を二度適用しない。
  const focusSourceChanged = useValuesChanged([
    displayedChunks,
    parsedExtraction.elements,
    parsedExtraction.tables,
    requestedCellCol,
    requestedCellRef,
    requestedCellRow,
    requestedChunkId,
    requestedElementId,
    requestedFormulaCellRef,
    requestedTableId,
    requestedUrlFocus,
    selectedChunkId,
    selectedElementId,
  ]);
  const applyRequestedFocus = () => {
    const chunks = displayedChunks;
    if (
      !chunks.length &&
      !requestedChunkId &&
      !requestedElementId &&
      !requestedTableId &&
      !requestedUrlFocus
    ) {
      return;
    }
    const requestedCellLocator =
      requestedCellRef || requestedCellRow != null || requestedCellCol != null
        ? {
            tableId: requestedTableId,
            cellRef: requestedCellRef,
            row: requestedCellRow,
            col: requestedCellCol,
          }
        : null;
    const requestedCell = requestedCellLocator
      ? findTableCellTarget(parsedExtraction.tables, requestedCellLocator)
      : null;
    const hasRequestedStructuredFocus = Boolean(
      requestedChunkId ||
        requestedElementId ||
        requestedTableId ||
        requestedCellRef ||
        requestedCellRow != null ||
        requestedCellCol != null
    );
    const requestedFocusKey = requestedCell
      ? `table_cell:${requestedCell.key}\u0000${requestedChunkId ?? ""}\u0000${
          requestedElementId ?? ""
        }`
      : requestedChunkId
      ? `chunk:${requestedChunkId}\u0000${requestedElementId ?? ""}`
      : requestedElementId
        ? `element:${requestedElementId}`
        : null;
    if (requestedFocusKey && appliedFocusRequest !== requestedFocusKey) {
      if (requestedCell) {
        const linkedElementId = requestedCell.table.element_id ?? requestedElementId;
        const linkedChunk = requestedChunkId
          ? chunks.find((chunk) => chunk.chunk_id === requestedChunkId)
          : linkedElementId
            ? chunks.find((chunk) => chunk.element_ids.includes(linkedElementId))
            : null;
        setSelectedChunkId(linkedChunk?.chunk_id ?? null);
        setSelectedElementId(linkedElementId ?? null);
        setSelectedTableCellKey(requestedCell.key);
        setUrlFallbackFocus(requestedUrlFocus);
        setPreviewFocusSource("table_cell");
        setFocusRequest({ key: requestedFocusKey, target: "table_cell" });
        setAppliedFocusRequest(requestedFocusKey);
        return;
      }
      if (requestedChunkId) {
        const requestedChunk = chunks.find((chunk) => chunk.chunk_id === requestedChunkId);
        if (requestedChunk) {
          setSelectedChunkId(requestedChunk.chunk_id);
          setSelectedElementId(
            requestedElementId && requestedChunk.element_ids.includes(requestedElementId)
              ? requestedElementId
              : requestedChunk.element_ids[0] ?? null
          );
          setSelectedTableCellKey(null);
          setUrlFallbackFocus(requestedUrlFocus);
          setPreviewFocusSource("chunk");
          setFocusRequest({ key: requestedFocusKey, target: "chunk" });
          setAppliedFocusRequest(requestedFocusKey);
          return;
        }
      } else if (requestedElementId) {
        const requestedElement = parsedExtraction.elements.find(
          (element) => workspaceElementKey(element) === requestedElementId
        );
        const linkedChunk = chunks.find((chunk) =>
          chunk.element_ids.includes(requestedElementId)
        );
        if (requestedElement || linkedChunk) {
          setSelectedChunkId(linkedChunk?.chunk_id ?? null);
          setSelectedElementId(requestedElementId);
          setSelectedTableCellKey(null);
          setUrlFallbackFocus(requestedUrlFocus);
          setPreviewFocusSource("element");
          setFocusRequest({ key: requestedFocusKey, target: "element" });
          setAppliedFocusRequest(requestedFocusKey);
          return;
        }
      }
      if (requestedUrlFocus) {
        setUrlFallbackFocus(requestedUrlFocus);
        setAppliedFocusRequest(requestedUrlFocus.key);
        return;
      }
    }
    if (
      requestedUrlFocus &&
      !requestedFocusKey &&
      appliedFocusRequest !== requestedUrlFocus.key
    ) {
      setUrlFallbackFocus(requestedUrlFocus);
      setAppliedFocusRequest(requestedUrlFocus.key);
      return;
    }
    if (requestedElementId && selectedElementId === requestedElementId) {
      const linkedChunk = chunks.find((chunk) => chunk.element_ids.includes(requestedElementId));
      if (!selectedChunkId && linkedChunk) {
        setSelectedChunkId(linkedChunk.chunk_id);
      }
      return;
    }
    if (selectedChunkId && chunks.some((chunk) => chunk.chunk_id === selectedChunkId)) {
      return;
    }
    if (hasRequestedStructuredFocus) {
      return;
    }
    const firstChunk = chunks[0];
    if (!firstChunk) return;
    setSelectedChunkId(firstChunk.chunk_id);
    setSelectedElementId(firstChunk.element_ids[0] ?? null);
    setSelectedTableCellKey(null);
  };
  if (focusSourceChanged) applyRequestedFocus();

  const inspectorScope = `${documentId}:${selectedRecipeId}:${previewVariant}`;
  const [inspectorPage, setInspectorPage] = useState<{ scope: string; page: number } | null>(null);
  const currentInspectorPage = inspectorPage?.scope === inspectorScope ? inspectorPage.page : null;
  const reportInspectorPage = useCallback((page: number) => {
    setInspectorPage(current => current?.scope === inspectorScope && current.page === page ? current : { scope: inspectorScope, page });
  }, [inspectorScope]);

  const pageExtraction = useMemo(
    () => extractionOnPage(selectedExtraction, currentInspectorPage),
    [selectedExtraction, currentInspectorPage]
  );
  const pageChunks = useMemo(
    () => chunksOnPage(displayedChunks, selectedExtraction, currentInspectorPage),
    [displayedChunks, selectedExtraction, currentInspectorPage]
  );

  if (query.isPending) {
    return (
      <TimedLoadingState
        label={t("documents.detail.loading")}
        operationKey="document-detail-load"
        placement="page"
        testId="document-detail-loading"
      >
        <Skeleton className="h-80 w-full rounded-lg" />
      </TimedLoadingState>
    );
  }
  // 取得済みの文書がある間は、ポーリング中の一時的な失敗で画面全体（入力中の分類・KB の選択を含む）を
  // エラー表示に置き換えない。前の内容のまま次の取得で回復させる（#281）。
  if (query.isError && !query.data) {
    return (
      <ErrorState
        message={errorMessage(query.error, t("workspace.notFound"))}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const doc = query.data;

  const sourceProfile = doc.source_profile ?? initialSourceProfile;
  const preparedArtifact = selectedRecipe?.preprocess_artifact ?? null;
  const hasPreparedArtifact = Boolean(preparedArtifact?.object_storage_path);
  // PREPROCESSED なのに使える処理後ファイル(object_storage_path)が無い状態。承認(EXTRACT)は
  // 必ず 409 になるため、converted の真偽や artifact の有無に関わらず「ファイル準備を再実行」へ
  // 誘導する(null artifact / passthrough・path欠落 / 変換成功・保存失敗 を全て包含)。
  const preparedArtifactMissing = !hasPreparedArtifact;
  // 変換なし(passthrough)の場合、object_storage_path は EXTRACT 再開用に元ファイルパスを
  // 再利用しており「別ファイルが存在する」ことを意味しない。プレビュー切替は converted も見る。
  const hasConvertedPreview = Boolean(preparedArtifact?.converted && preparedArtifact?.object_storage_path);
  const selectedPreviewVariant = previewVariant === "prepared" && hasConvertedPreview
    ? "prepared"
    : "original";
  const selectedPreviewFileName =
    selectedPreviewVariant === "prepared" ? preparedArtifact?.file_name ?? doc.file_name : doc.file_name;
  const selectedPreviewSourceProfile =
    selectedPreviewVariant === "original" ? sourceProfile : null;
  const selectedPreviewDownloadUrl = selectedRecipeId
    ? api.documentRecipeContentUrl(documentId, selectedRecipeId, {
        ...(selectedPreviewVariant === "prepared" ? { variant: selectedPreviewVariant } : {}),
        disposition: "attachment",
      })
    : api.documentContentUrl(documentId, {
    ...(selectedPreviewVariant === "prepared" ? { variant: selectedPreviewVariant } : {}),
    disposition: "attachment",
      });
  const duplicateSource = doc.duplicate_source;
  const duplicateMessage = duplicateSource
    ? t("upload.duplicateDetail", {
        name: duplicateSource.file_name,
        status: t(`status.${duplicateSource.status}` as I18nKey),
        uploadedAt: formatDateTime(duplicateSource.uploaded_at),
      })
    : t("upload.duplicate");
  const ingestionParser = resolveIngestionParserDisplay({
    segments: recipeSegments,
    extractionBackend: extractionJsonQuery.data?.parser_backend,
    extractionProfile: extractionJsonQuery.data?.parser_profile,
    loading:
      segmentsQuery.isPending || (hasSelectedRecipeExtraction && extractionJsonQuery.isPending),
  });
  const hasExtraction = Boolean(selectedRecipe?.active_extraction_recipe_id);
  // 索引の前の保存済み chunk_set(CHUNKED)も、Embedding / 索引から再実行できる Chunk に数える(#2467)。
  const hasChunkSet = Boolean(latestChunkSet || selectedRecipe?.pending_chunk_set_id);
  const actionPlan = resolveDocumentActionPlan({
    status,
    activeJob: activeSubmittedJob,
    // Chunk 作成から索引まで通す job の失敗では job の phase は CHUNK のため、レシピの失敗工程を優先する。
    latestFailedPhase: failedRecipePhase ?? failedDocumentJob?.phase,
    hasPreparedArtifact,
    hasExtraction,
    hasChunkSet,
    hasSelectedRecipe: Boolean(selectedRecipeId),
  });
  const activeProcessingJob = [
    queuedJob.data,
    approvedJob.data,
    retriedSegmentJob.data,
    latestDocumentJob,
    enqueueIngestion.data,
    approveDocument.data,
    retryFailedSegments.data,
  ].find((job) => ingestionJobIsActive(job?.status));
  const currentProcessingPhase =
    activeProcessingJob?.phase ??
    phaseForDocumentStatus(status) ??
    latestDocumentJob?.phase ??
    "PREPROCESS";
  const statusMessageSlot = resolveStatusMessageSlot({
    errored: documentFailure.errored,
    processingVisible: shouldShowProcessingWatchBanner({
      watchProcessing,
      documentStatus: status,
      latestJobStatus: latestDocumentJob?.status,
    }),
    documentStatus: status,
    preparedArtifactMissing,
  });
  // 表頭を推定できなかった Excel のシートで確認に止めた案内を、文書の先頭に出しているか。出しているときは、
  // 構造化要素のタブ・変換の記録に同じ案内を重ねない（同じ状態の案内は 1 か所。messaging §10.2。#2034）。
  const showsExcelHeaderReview =
    statusMessageSlot?.kind === "gate" &&
    statusMessageSlot.status === "REVIEW" &&
    !statusMessageSlot.artifactMissing &&
    reviewHeaderSheets.length > 0;
  const omitDocumentLevelWarning = showsExcelHeaderReview
    ? isHeaderLowConfidenceWarning
    : undefined;
  const submittedIngestionJob = queuedJob.data ?? enqueueIngestion.data;
  const showSubmittedIngestionStatus =
    !documentFailure.errored &&
    submittedIngestionJob != null &&
    ["QUEUED", "RUNNING", "SKIPPED"].includes(submittedIngestionJob.status);
  const retryPhase = actionPlan.primary?.kind === "retry" ? actionPlan.primary.phase : null;
  const failedSubmissionPhase = enqueueIngestion.variables?.phase ?? "PREPROCESS";
  const phaseStartFailedMessage = t("flow.phase.startFailed", {
    phase: t(phaseLabelKey(failedSubmissionPhase)),
  });
  const submissionErrorDetail =
    // 組み込みの例外（英語の文）は出さない（#906）。timeout・通信断は利用者向けの文の ApiError で届く。
    apiErrorMessage(visibleEnqueueError, "").trim();
  const submissionErrorMessage =
    submissionErrorDetail && submissionErrorDetail !== phaseStartFailedMessage
      ? `${phaseStartFailedMessage} ${submissionErrorDetail}`
      : phaseStartFailedMessage;
  // 押した操作のボタンだけを回す。処理の要求中に別のレシピを選んでも、そのレシピの操作は回さない（#819）。
  // variables は完了後も残るため、isPending のときだけ読む。
  const approvePendingForRecipe =
    approveDocument.isPending && approveDocument.variables?.recipeId === selectedRecipeId;
  const enqueuePendingFor = (phase?: string) =>
    enqueueIngestion.isPending &&
    enqueueIngestion.variables?.recipeId === selectedRecipeId &&
    (phase === undefined || enqueueIngestion.variables?.phase === phase);
  const showActionButtons =
    Boolean(selectedRecipeId) &&
    (actionPlan.primary != null || actionPlan.reprocessPhases.length > 0);
  const documentActionButtons = showActionButtons ? (
    <>
      {actionPlan.primary?.kind === "approve" ? (
        <Button
          onClick={() =>
            approveDocument.mutate(
              { id: documentId, recipeId: selectedRecipeId as string },
              {
                onSuccess: (job) => {
                  setLocalWatchProcessing(job.status === "QUEUED" || job.status === "RUNNING");
                  setEditingReview(false);
                  setReviewEdits(emptyReviewEdits());
                  toast.success(t(phaseStartedMessageKey(job.phase)));
                },
              }
            )
          }
          loading={approvePendingForRecipe}
          disabled={
            (approveDocument.isPending && !approvePendingForRecipe) ||
            saveReviewEdits.isPending ||
            enqueueIngestion.isPending ||
            (status === "REVIEW" && hasReviewEdits)
          }
          icon={Check}
        >
          {status === "PREPROCESSED"
            ? t("flow.approvePreprocess")
            : status === "CHUNKED"
              ? t("flow.approveChunks")
              : t("flow.approveExtraction")}
        </Button>
      ) : null}
      {actionPlan.primary?.kind === "enqueue" ? (
        <Button
          onClick={() =>
            enqueueIngestion.mutate(
              {
                id: documentId,
                recipeId: selectedRecipeId as string,
                phase: "PREPROCESS",
              },
              {
                onSuccess: (job) => {
                  setLocalWatchProcessing(job.status === "QUEUED" || job.status === "RUNNING");
                },
              }
            )
          }
          loading={enqueuePendingFor("PREPROCESS")}
          disabled={
            approveDocument.isPending ||
            (enqueueIngestion.isPending && !enqueuePendingFor("PREPROCESS"))
          }
          icon={Send}
        >
          {doc.duplicate_of_document_id
            ? t("action.enqueueDuplicateIngestion")
            : t("action.enqueueIngestion")}
        </Button>
      ) : null}
      {retryPhase ? (
        <Button
          onClick={() => void handlePhaseRestart(retryPhase, "retry")}
          loading={enqueuePendingFor(retryPhase)}
          disabled={
            approveDocument.isPending ||
            (enqueueIngestion.isPending && !enqueuePendingFor(retryPhase))
          }
          icon={RotateCcw}
        >
          {t(phaseRetryLabelKey(retryPhase))}
        </Button>
      ) : null}
      {actionPlan.reprocessPhases.map((phase) => (
        <Button
          key={phase}
          variant="ghost"
          icon={RotateCcw}
          onClick={() => void handlePhaseRestart(phase, "reprocess")}
          loading={enqueuePendingFor(phase)}
          disabled={
            approveDocument.isPending ||
            (enqueueIngestion.isPending && !enqueuePendingFor(phase))
          }
        >
          {t(`flow.reprocess.${phase.toLowerCase()}` as I18nKey)}
        </Button>
      ))}
    </>
  ) : null;
  // 押した操作の結果。投入・承認の失敗は文書の失敗状態とは別の出来事なので、ERROR の文書で「再試行」が
  // 失敗したときもボタンの直下に出す（#281）。投入した処理が実行中の案内に変わったら、開始の文は重ねない
  // （同じ場所の案内が「実行中」を示す。messaging §10.2。#2034）。
  const showSubmittedResult =
    showSubmittedIngestionStatus && statusMessageSlot?.kind !== "processing";
  const queuedJobFailure =
    ingestionErrorDisplays.queuedJobMessage && !documentFailure.errored
      ? ingestionErrorDisplays.queuedJobMessage
      : null;
  const reviewEditsPending = status === "REVIEW" && hasReviewEdits;
  const documentActionResult =
    selectedRecipeId &&
    (visibleApproveError != null ||
      reviewEditsPending ||
      visibleEnqueueError != null ||
      showSubmittedResult ||
      queuedJobFailure) ? (
      <div className="min-w-0 space-y-2" data-testid="document-action-result">
        {visibleApproveError != null ? (
          <FormStatus tone="danger" message={approveErrorText} />
        ) : null}
        {reviewEditsPending ? (
          <FormStatus tone="warning" message={t("flow.review.edit.pending")} />
        ) : null}
        {visibleEnqueueError != null ? (
          <FormStatus tone="danger" message={submissionErrorMessage} />
        ) : null}
        {showSubmittedResult ? (
          <FormStatus
            tone={submittedIngestionJob.status === "SKIPPED" ? "warning" : "success"}
            message={
              submittedIngestionJob.status === "SKIPPED"
                ? t("flow.phase.skipped", {
                    phase: t(phaseLabelKey(submittedIngestionJob.phase)),
                  })
                : t(phaseStartedMessageKey(submittedIngestionJob.phase))
            }
          />
        ) : null}
        {queuedJobFailure ? <FormStatus tone="danger" message={queuedJobFailure} /> : null}
      </div>
    ) : null;
  // 状態メッセージ単一スロットの案内（messaging-spec §9）。工程の操作と結果は同じ案内の中に置く（#2034）。
  const slotBanner: {
    severity: "danger" | "warning" | "info";
    title?: string;
    body: ReactNode;
  } | null =
    statusMessageSlot?.kind === "failure"
      ? {
          severity: "danger",
          title: failedPhase
            ? t("flow.error.atStep", { step: t(phaseLabelKey(failedPhase)) })
            : undefined,
          body: (
            <FailureMessage message={documentFailure.primaryMessage ?? t("flow.error.fallback")} />
          ),
        }
      : statusMessageSlot?.kind === "processing"
        ? { severity: "info", body: t(phaseRunningMessageKey(currentProcessingPhase)) }
        : statusMessageSlot?.kind === "gate"
          ? statusMessageSlot.artifactMissing
            ? {
                severity: "danger",
                body: preparedArtifact?.converted
                  ? t("flow.preprocessed.persistFailed")
                  : t("flow.preprocessed.preparedMissing"),
              }
            : showsExcelHeaderReview
              ? // 表頭を推定できなかった Excel のシートがあり、確認で止めた（#1229）。
                {
                  severity: "warning",
                  title: t("documents.excelWarning.reviewTitle"),
                  body: t("documents.excelWarning.reviewBody", {
                    sheets: reviewHeaderSheets.join("、"),
                  }),
                }
              : { severity: "info", body: t(GATE_MESSAGE_KEYS[statusMessageSlot.status]) }
          : null;

  return (
    <Card>
      {showTitle ? (
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="flex min-w-0 flex-1 items-center gap-2 text-base">
              <FileText size={20} className="text-accent-fg" aria-hidden />
              <span className="truncate" title={doc.file_name}>
                {doc.file_name}
              </span>
            </CardTitle>
            <StatusBadge status={status} queuedPhase={recipeQueuedPhase} />
          </div>
        </CardHeader>
      ) : null}
      <CardContent className={showTitle ? "space-y-5" : "space-y-5 pt-5"}>
        {doc.duplicate_of_document_id ? (
          <Banner severity="warning">
            <div className="space-y-1">
              <p>{duplicateMessage}</p>
              <p className="text-xs">{t("upload.duplicateForceHint")}</p>
            </div>
          </Banner>
        ) : null}

        <DocumentRecipeManager
          documentId={documentId}
          recipes={recipesQuery.data ?? []}
          selectedRecipeId={selectedRecipeId}
          onSelect={selectRecipe}
          loading={recipesQuery.isPending}
          error={initialLoadError(recipesQuery)}
          onRetry={() => void recipesQuery.refetch()}
          chunkSets={chunkSetsQuery.data}
          sourceModality={sourceProfile?.modality ?? null}
          // 未保存の抽出確認の修正があるあいだは、レシピの操作からも承認させない（本文側の承認と同じ。#944）。
          approveBlocked={status === "REVIEW" && hasReviewEdits}
        />
        <LanguagePackDocumentPanel key={selectedRecipeId ?? "default"} documentId={documentId} fileName={doc.file_name} use={doc.language_pack} recipe={selectedRecipe ?? null} />
        {/* 状態メッセージ単一スロット(messaging-spec §9): 失敗原因 > 実行中 > ゲート案内 を 1 本だけ表示する。
            工程の操作（承認・実行・再試行・再処理）はこの案内の中に置き、押した結果（投入の失敗・開始・承認の失敗）は
            同じ案内の中のボタンの直下に出す（UX 契約 messaging §10.1・§10.2。#2034）。押した後の実行中は、同じ場所の
            案内が「実行中」に変わる。案内が無い状態（未処理・索引済み）は、同じ場所にボタンと結果だけを出す。 */}
        {slotBanner ? (
          <div data-testid="document-status-actions">
            <Banner
              severity={slotBanner.severity}
              title={slotBanner.title}
              action={documentActionButtons}
              progress={documentActionResult}
            >
              {slotBanner.body}
            </Banner>
          </div>
        ) : documentActionButtons || documentActionResult ? (
          <div className="space-y-3" data-testid="document-status-actions">
            {documentActionButtons ? (
              <div className="flex flex-wrap items-center gap-2">{documentActionButtons}</div>
            ) : null}
            {documentActionResult}
          </div>
        ) : null}

        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs text-fg-muted">{t("flow.size")}</dt>
            <dd className="tnum mt-0.5 font-medium text-fg">
              {formatBytes(doc.file_size_bytes)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-fg-muted">{t("flow.uploadedAt")}</dt>
            <dd className="tnum mt-0.5 font-medium text-fg">
              {formatDateTime(doc.uploaded_at)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-fg-muted">{t("flow.indexedAt")}</dt>
            <dd className="tnum mt-0.5 font-medium text-fg">
              {formatDateTime(doc.indexed_at)}
            </dd>
          </div>
        </dl>

        <DocumentKnowledgeBaseEditor
          documentId={documentId}
          initialKnowledgeBases={doc.knowledge_bases}
        />

        <DocumentClassificationEditor
          documentId={documentId}
          classification={doc.classification ?? null}
        />

        <DocumentVersionEditor document={doc} />

        {/* 左右の幅（#579）。ページの大きさは左ペインの幅で決まる（高さは幅とページの縦横比から。#559）。
            - xl（1280px〜）: 今までどおりほぼ半分ずつ。右ペインの幅（約 450px）をこれ以上削ると、右のタブが入り切らない。
            - 2xl（1536px〜）: プレビュー 2 : 右 1 にし、右は 35rem（右のタブ 4 つが 1 行に入る幅）を下限にする。
              1920px で左は約 800 → 1040px、ペインの高さは A4 縦で約 1.55 倍（1052 → 1628px）になる。
              右ペインの幅（本文・構造化要素・Chunk・抽出エクスポートの作業）を残すため、2 : 1 より広くはしない。 */}
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)] 2xl:grid-cols-[minmax(0,2fr)_minmax(35rem,1fr)]">
          {/* 左ペイン: 原本プレビュー。高さは 1 画面分の固定ではなく、幅と文書のページの縦横比から決め、
              幅に合わせたときに 1 ページ全体（とツールバー・強調の状態の行）が縦スクロールなしで入る(#559)。
              - 決め方は DocumentPreview の sizing="page"（ページ画像は最も縦長のページ、寸法が分かるまでは A4 縦）。
              - 下限: xl 以上は今までの 1 画面分（上下の余白を除く）。横長のページでも右ペインの作業の高さを
                今より減らさない（余りはページの上下の余白になる）。xl 未満の縦積みは下限を設けず、ページの形の高さにする
                （375px で A4 縦ならおよそ 1 画面の 2/3。1 画面分の枠に小さなページが浮く今の表示を詰める）。
              - 上限: ビューア側で 2 画面分。極端に縦長の画像だけ内部スクロールになる。
              - sticky はやめた: 1 画面より高いペインは sticky でも止まらず（右ペインと同じ高さなので動く余地がない）、
                下端が画面の外に残るだけになるため。強調が画面の外にあるときは「強調した位置へ移動」(H) でページごと見せる。 */}
          <section
            data-testid="document-preview-pane"
            className="flex min-w-0 flex-col xl:min-h-[max(28rem,calc(100dvh-2rem))]"
          >
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-fg">{t("flow.preview")}</h3>
              <a
                href={selectedPreviewDownloadUrl}
                download={selectedPreviewFileName}
                className={cn(
                  buttonVariants({ variant: "secondary", size: "sm" }),
                  "whitespace-nowrap"
                )}
              >
                <Download size={14} aria-hidden />
                {t("flow.preview.download")}
              </a>
            </div>
            {/* 処理前 / 処理後は同じ文書の別の見方なので、共有の Tabs + TabPanel で切り替える(#396)。
                枠の中に Button を並べた手書きのセグメントは、枠線が二重になり選択状態も読み上げられなかった。 */}
            <Tabs
              idPrefix="preview"
              ariaLabel={t("flow.preview")}
              className="mb-2 shrink-0"
              value={selectedPreviewVariant}
              onChange={(value) => setPreviewVariant(value === "prepared" ? "prepared" : "original")}
              items={[
                { id: "original", label: t("flow.preview.before") },
                {
                  id: "prepared",
                  label: t("flow.preview.after"),
                  disabled: !hasConvertedPreview,
                  disabledReason: hasConvertedPreview
                    ? undefined
                    : preparedArtifact && !preparedArtifact.converted
                      ? t("flow.preview.preparedSkipped")
                      : t("flow.preview.preparedUnavailable"),
                },
              ]}
            />
            <TabPanel
              id={selectedPreviewVariant}
              value={selectedPreviewVariant}
              idPrefix="preview"
              className="flex flex-auto flex-col"
            >
              <DocumentPreview
                documentId={documentId}
                recipeId={selectedRecipeId}
                fileName={selectedPreviewFileName}
                variant={selectedPreviewVariant}
                sourceProfile={selectedPreviewSourceProfile}
                preparedArtifact={preparedArtifact}
                showFallbackDownload={false}
                focusPage={effectiveFocusPage}
                onPageChange={reportInspectorPage}
                focusRequestKey={urlFallbackFocus?.key ?? focusRequest?.key ?? null}
                focusBbox={effectiveFocusBbox}
                focusBboxMode={effectiveFocusBboxMode}
                focusBboxUnit={effectiveFocusBboxUnit}
                focusPageSize={focusPageSize}
                highlights={previewHighlights}
                sizing="page"
              />
            </TabPanel>
          </section>

          {/* 右ペイン: 本文 / 構造化要素 / Chunk / エクスポート をタブ切替。
              desktop は左のプレビューと同じ高さにそろえ、タブの内容は内部でスクロールする(#349 / #559)。
              h-0 + min-h-full: 右ペインの内容は行の高さに寄与させず（h-0）、左ペインが決めた行の高さいっぱいに
              伸ばす（min-h-full はグリッドの領域の高さ）。内容が多くても左より高くならない。 */}
          <section
            data-testid="document-inspector-pane"
            className="min-w-0 xl:flex xl:h-0 xl:min-h-full xl:flex-col"
          >
            <Tabs
              idPrefix="inspector"
              ariaLabel={t("flow.inspector.tabs")}
              className="mb-3"
              value={inspectorTab}
              onChange={(value) => setInspectorTab(value as typeof inspectorTab)}
              items={[
                { id: "text", label: t("flow.extraction.rawText") },
                { id: "extraction", label: t("flow.extraction.title") },
                {
                  id: "chunks",
                  label: t("flow.chunks.title"),
                  count: pageChunks.length || undefined,
                },
                { id: "export", label: t("flow.extractionExport.title") },
              ]}
            />

            {currentInspectorPage !== null && inspectorTab !== "export" ? (
              <p className="mb-2 text-xs text-fg-muted" data-testid="inspector-current-page">
                {t("flow.inspector.currentPage", { page: currentInspectorPage })}
              </p>
            ) : null}

            {inspectorTab === "text" ? (
              <div
                role="tabpanel"
                id="inspector-panel-text"
                aria-labelledby="inspector-tab-text"
                tabIndex={0}
                className="xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:pr-1 xl:[scrollbar-gutter:stable]"
              >
                <DocumentRawText extraction={pageExtraction} />
              </div>
            ) : null}

            {inspectorTab === "extraction" ? (
              <div
                role="tabpanel"
                id="inspector-panel-extraction"
                aria-labelledby="inspector-tab-extraction"
                tabIndex={0}
                className="xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:pr-1 xl:[scrollbar-gutter:stable]"
              >
                {status === "REVIEW" && selectedRecipeId ? (
                  <div className="mb-2 xl:sticky xl:top-0 xl:z-10 xl:bg-surface-sunken xl:pb-2">
                    {editingReview ? (
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          {hasReviewEdits ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={saveReviewEdits.isPending}
                              onClick={async () => {
                                const confirmed = await confirm({
                                  title: t("flow.review.edit.discardTitle"),
                                  description: t("flow.review.edit.discardDescription"),
                                  confirmLabel: t("flow.review.edit.discardConfirm"),
                                  tone: "warning",
                                });
                                if (!confirmed) return;
                                saveReviewEdits.reset();
                                setReviewEdits(emptyReviewEdits());
                                setEditingReview(false);
                              }} icon={RotateCcw}>
                              {t("flow.review.edit.discard")}
                            </Button>
                          ) : null}
                        </div>
                        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() =>
                              saveReviewEdits.mutate(
                                {
                                  id: documentId,
                                  recipeId: selectedRecipeId as string,
                                  payload: reviewEdits,
                                },
                                {
                                  onSuccess: () => {
                                    setReviewEdits(emptyReviewEdits());
                                    toast.success(t("flow.review.edit.saved"));
                                  },
                                }
                              )
                            }
                            loading={saveReviewEdits.isPending}
                            disabled={
                              !hasReviewEdits ||
                              approveDocument.isPending
                            } icon={Save}>
                            {t("flow.review.edit.save")}
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={saveReviewEdits.isPending}
                            onClick={() => setEditingReview(false)} icon={X}>
                            {t("flow.review.edit.close")}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex justify-end">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setEditingReview(true)} icon={Pencil}>
                          {t("flow.review.edit.structuredOpen")}
                        </Button>
                      </div>
                    )}
                    {editingReview && saveReviewEdits.isError ? (
                      <div className="mt-2">
                        <FormStatus tone="danger" message={saveReviewErrorText} />
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {status === "REVIEW" && editingReview ? (
                  <ReviewTextEditor
                    extraction={pageExtraction}
                    edits={reviewEdits}
                    onChange={(edits) => {
                      saveReviewEdits.reset();
                      setReviewEdits(edits);
                    }}
                  />
                ) : (
                  <DocumentExtraction
                    extraction={pageExtraction}
                    documentId={documentId}
                    selectedElementId={selectedElementId}
                    selectedTableCellKey={selectedTableCellKey}
                    focusRequestKey={focusRequest?.key ?? null}
                    focusSelectedElement={focusRequest?.target === "element"}
                    focusSelectedTableCell={focusRequest?.target === "table_cell"}
                    onElementSelect={selectElement}
                    onTableCellSelect={selectTableCell}
                    recipeId={selectedRecipeId}
                    onPageSelect={selectPage}
                    omitWarning={omitDocumentLevelWarning}
                  />
                )}
              </div>
            ) : null}

            {inspectorTab === "chunks" ? (
              <div
                role="tabpanel"
                id="inspector-panel-chunks"
                aria-labelledby="inspector-tab-chunks"
                tabIndex={0}
                className="xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:pr-1 xl:[scrollbar-gutter:stable]"
              >
                <div className="space-y-3">
                  {selectedRecipeId &&
                  hasSelectedRecipeExtraction &&
                  selectedRecipe?.active_chunk_set_id &&
                  selectedRecipe.chunk_count > 0 ? (
                    <DocumentChunksJsonDownload
                      documentId={documentId}
                      recipeId={selectedRecipeId}
                    />
                  ) : null}
                  {selectedRecipeId && (status === "REVIEW" || status === "CHUNKED") ? (
                    <ChunkPreviewControls
                      form={chunkPreviewSettings}
                      onChange={(update) =>
                        setChunkPreviewSettings((current) => ({ ...current, ...update }))
                      }
                      onRun={() =>
                        chunkPreview.mutate(
                          {
                            id: documentId,
                            recipeId: selectedRecipeId,
                            payload: chunkPreviewSettings,
                          },
                          {
                            onSuccess: () => {
                              setSelectedChunkId(null);
                              setSelectedElementId(null);
                              setSelectedTableCellKey(null);
                            },
                          }
                        )
                      }
                      pending={chunkPreview.isPending}
                      disabled={hasReviewEdits}
                      error={chunkPreview.error}
                      result={chunkPreview.data ?? null}
                    />
                  ) : null}
                  <DocumentChunksPanel
                    chunks={pageChunks}
                    // レシピが無い・取得できないときは chunk の取得自体が無効（isPending のまま）なので、
                    // 実際に取得している間（isLoading）だけ読込中にする（#281）。
                    loading={chunkPreview.isPending || (!chunkPreview.data && chunksQuery.isLoading)}
                    // 分割のやり直し中は、スピナーを「分割をやり直す」ボタンの loading と経過時間を上の
                    // ProcessingIndicator が出す。ここは同じ処理なので Skeleton だけにする（messaging §3.7、#416）。
                    rerunPending={chunkPreview.isPending}
                    error={!chunkPreview.data && chunksQuery.isError}
                    selectedChunkId={selectedChunkId}
                    focusRequestKey={focusRequest?.target === "chunk" ? focusRequest.key : null}
                    onSelect={(chunk) => {
                      setUrlFallbackFocus(null);
                      setSelectedChunkId(chunk.chunk_id);
                      setSelectedElementId(chunk.element_ids[0] ?? null);
                      setSelectedTableCellKey(null);
                      setPreviewFocusSource("chunk");
                    }}
                  />
                </div>
              </div>
            ) : null}

            {inspectorTab === "export" ? (
              <div
                role="tabpanel"
                id="inspector-panel-export"
                aria-labelledby="inspector-tab-export"
                tabIndex={0}
                className="xl:flex xl:min-h-0 xl:flex-1 xl:flex-col xl:overflow-y-auto xl:pr-1 xl:[scrollbar-gutter:stable]"
              >
                <DocumentExtractionExportPanel
                  documentId={documentId}
                  recipeId={selectedRecipeId}
                  hasExtraction={hasSelectedRecipeExtraction}
                  loading={hasSelectedRecipeExtraction && extractionJsonQuery.isPending}
                  error={initialLoadError(extractionJsonQuery) != null}
                />
              </div>
            ) : null}
          </section>
        </div>

        <Disclosure
          summary={t("flow.inspector.details")}
          icon={Wrench}
          open={diagnosticsOpen}
          onOpenChange={setDiagnosticsOpen}
          data-testid="document-diagnostics"
          contentClassName="space-y-5 px-4 py-3"
          summaryClassName="px-4"
        >
            {sourceProfile ? (
              <SourceProfilePanel documentId={documentId} profile={sourceProfile} />
            ) : null}
            {/* 派生系譜は変換あり時のみ。変換なしは工程行の「変換なし」チップに集約する。 */}
            {parsedExtraction.sourceDerivation?.converted ? (
              <SourceDerivationPanel
                derivation={parsedExtraction.sourceDerivation}
                omitWarning={omitDocumentLevelWarning}
                originalFileName={sourceProfile?.original_file_name ?? doc.file_name}
              />
            ) : null}
            <IngestionJobsPanel
              steps={selectedRecipe?.steps ?? []}
              jobs={recipeJobs}
              segments={recipeSegments}
              loading={documentJobsQuery.isPending}
              error={initialLoadError(documentJobsQuery) != null}
              nowMs={elapsedNowMs}
              suppressMessage={documentFailure.primaryMessage}
              ingestionParser={ingestionParser}
              parserVersion={sourceProfile?.parser_version ?? null}
              preprocessConverted={
                preparedArtifact?.converted ??
                parsedExtraction.sourceDerivation?.converted ??
                null
              }
              chunkCount={latestChunkSet ? selectedRecipe?.chunk_count ?? null : null}
              chunkSetCreatedAt={
                chunkSetsQuery.data?.find((chunkSet) => chunkSet.chunk_set_id === latestChunkSet)
                  ?.created_at ?? null
              }
              vectorCount={latestChunkSet ? selectedRecipe?.vector_count ?? null : null}
              embeddingLabel={
                embeddingModel && embeddingDim
                  ? t("flow.jobs.embedding", {
                      model: embeddingModel,
                      dim: embeddingDim,
                    })
                  : null
              }
            />
            <IngestionSegmentsPanel
              segments={segmentsWithProgress(
                recipeSegments,
                recipeJobs.find((job) => ingestionJobIsActive(job.status) && job.progress)
                  ?.progress
              )}
              loading={segmentsQuery.isPending}
              error={initialLoadError(segmentsQuery) != null}
              retrying={retryFailedSegments.isPending}
              visibleErrorSegmentIds={ingestionErrorDisplays.segmentIds}
              retryStatus={
                retryFailedSegments.isError
                  ? {
                      tone: "danger",
                      message: errorMessage(
                        retryFailedSegments.error,
                        t("flow.segments.retryFailedError")
                      ),
                    }
                  : ingestionErrorDisplays.retriedSegmentJobMessage
                    ? {
                        tone: "danger",
                        message: ingestionErrorDisplays.retriedSegmentJobMessage,
                      }
                    : retryFailedSegments.data &&
                        ["QUEUED", "RUNNING"].includes(
                          retriedSegmentJob.data?.status ?? retryFailedSegments.data.status
                        )
                      ? { tone: "success", message: t("flow.segments.retryQueued") }
                      : null
              }
              onRetryFailedSegments={() =>
                retryFailedSegments.mutate({ id: documentId, recipeId: selectedRecipeId }, {
                  onSuccess: (job) => {
                    setLocalWatchProcessing(job.status === "QUEUED" || job.status === "RUNNING");
                  },
                })
              }
            />
        </Disclosure>

      </CardContent>
    </Card>
  );
}

function IngestionJobsPanel({
  steps,
  jobs,
  segments,
  loading,
  error,
  nowMs,
  suppressMessage,
  ingestionParser,
  parserVersion,
  preprocessConverted,
  chunkCount,
  chunkSetCreatedAt,
  vectorCount,
  embeddingLabel,
}: {
  /** 選択中レシピの工程状態(単一状態源)。工程の実行有無はこちらを正とする。 */
  steps: DocumentRecipeStep[];
  jobs: IngestionJob[];
  segments: IngestionSegment[];
  loading: boolean;
  error: boolean;
  nowMs: number;
  /** 上部の原因バナーで表示済みの本文。一致時はここで再掲しない（§9 P2）。 */
  suppressMessage?: string | null;
  ingestionParser: IngestionParserDisplay;
  parserVersion: string | null;
  preprocessConverted: boolean | null;
  /** active chunk_set があるときのみ数値(無ければ null でチップ非表示)。 */
  chunkCount: number | null;
  chunkSetCreatedAt: string | null;
  vectorCount: number | null;
  embeddingLabel: string | null;
}) {
  if (loading) {
    return (
      <TimedLoadingState
        label={t("flow.jobs.loading")}
        operationKey="document-jobs-load"
        framed={false}
        testId="document-jobs-loading"
      >
        <Skeleton className="h-24 w-full rounded-md" />
      </TimedLoadingState>
    );
  }
  if (error) {
    return (
      <Banner severity="warning" title={t("flow.jobs.loadError")}>
        {t("flow.jobs.loadErrorHint")}
      </Banner>
    );
  }
  if (!jobs.length) return null;

  const rows = resolvePhaseRows(steps, jobs);
  // 抽出の段（解析・図表の説明）の進捗は、実行中の job（通しジョブなら起点の工程の job）が持つ
  // （#1999）。抽出の工程の行に出し、セグメントから作る「抽出: N / M ページ」とは重ねない。
  const stageProgressJob =
    jobs.find((job) => ingestionJobIsActive(job.status) && job.progress) ?? null;
  // 段の帯は「抽出: N / M ページ」と同じ記録（実行中の job の段の状態、無ければ抽出セグメントの行）
  // から作る（#2001）。段が 1 つ・PDF でない文書は出さない。
  const segmentStrip = resolveSegmentStrip(
    segmentsWithProgress(segments, stageProgressJob?.progress)
  );
  return (
    <section className="rounded-md border border-border bg-surface-sunken p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Clock3 size={16} className="text-accent-fg" aria-hidden />
          {t("flow.jobs.title")}
        </h3>
        <span className="text-xs text-fg-muted">
          {t("flow.jobs.count", { count: jobs.length })}
        </span>
      </div>
      <ol className="mt-3 space-y-2" aria-label={t("flow.jobs.title")}>
        {rows.map(({ phase, step, job }) => (
          <PhaseJobRow
            key={phase}
            phase={phase}
            step={step}
            job={job}
            segments={segments}
            nowMs={nowMs}
            stageProgress={phase === "EXTRACT" ? (stageProgressJob?.progress ?? null) : null}
            hasStageProgress={stageProgressJob != null}
            segmentStrip={phase === "EXTRACT" ? segmentStrip : null}
            suppressMessage={suppressMessage}
            ingestionParser={phase === "EXTRACT" ? ingestionParser : null}
            parserVersion={phase === "EXTRACT" ? parserVersion : null}
            preprocessConverted={phase === "PREPROCESS" ? preprocessConverted : null}
            chunkCount={phase === "CHUNK" ? chunkCount : null}
            chunkSetCreatedAt={phase === "CHUNK" ? chunkSetCreatedAt : null}
            vectorCount={phase === "INDEX" ? vectorCount : null}
            embeddingLabel={phase === "INDEX" ? embeddingLabel : null}
          />
        ))}
      </ol>
    </section>
  );
}

/** 工程1件分の実行状況行。未実行工程はミュート表示のみ。 */
function PhaseJobRow({
  phase,
  step,
  job,
  segments,
  nowMs,
  stageProgress,
  hasStageProgress,
  segmentStrip,
  suppressMessage,
  ingestionParser,
  parserVersion,
  preprocessConverted,
  chunkCount,
  chunkSetCreatedAt,
  vectorCount,
  embeddingLabel,
}: {
  phase: IngestionJobPhase;
  step: DocumentRecipeStep | null;
  job: IngestionJob | null;
  segments: IngestionSegment[];
  nowMs: number;
  /** 抽出の工程の行にだけ渡す、実行中の job の抽出の段の進捗（#1999）。 */
  stageProgress: IngestionJobProgress | null;
  /** 実行中の job が段の進捗を持つか（持つならセグメントからの進捗を出さない）。 */
  hasStageProgress: boolean;
  /** 抽出の工程の行にだけ渡す、段の帯（段が 2 つ以上のとき。#2001）。 */
  segmentStrip: SegmentStripView | null;
  suppressMessage?: string | null;
  ingestionParser: IngestionParserDisplay | null;
  parserVersion: string | null;
  preprocessConverted: boolean | null;
  chunkCount: number | null;
  chunkSetCreatedAt: string | null;
  vectorCount: number | null;
  embeddingLabel: string | null;
}) {
  const engineLabel = ingestionParser?.backend
    ? [parserBackendLabel(ingestionParser.backend), parserVersion].filter(Boolean).join(" ")
    : null;
  // 工程状態は steps(レシピ status 由来の単一状態源)を正とする(§9 P1)。ジョブの phase は
  // 起点工程のみで、通しジョブ内で実行済みの工程はジョブ行を持たず、逆に設定変更で無効化
  // された古いジョブ行が残ることもあるため、ジョブの有無/状態から工程状態を導出しない。
  if (!job || step?.status === "PENDING") {
    if (!step || step.status === "PENDING") {
      // PENDING で古いジョブ行が残っていても表示しない(前 revision の成果で現状態と矛盾する)。
      return (
        <li className="flex flex-wrap items-center gap-2 rounded-md border border-dashed border-border bg-surface px-3 py-2 text-xs text-fg-muted">
          <span className="font-medium text-fg">{t(jobPhaseKey(phase))}</span>
          <span>{t("flow.jobs.notStarted")}</span>
        </li>
      );
    }
    // 起点の工程の job の中で実行中の抽出（job の行を持たない）。段の進捗が無い間は、抽出
    // セグメントから「抽出: N / M ページ」を出す（#2001）。
    const inlineSummary =
      phase === "EXTRACT" && step.status === "RUNNING" && !stageProgress && !hasStageProgress
        ? resolveIngestionProgressSummary(segments)
        : null;
    const stepErrorMessage =
      step.status === "FAILED"
        ? (() => {
            const normalized = normalizeIngestionErrorMessage(step.error_message);
            return normalized && normalized !== suppressMessage ? normalized : null;
          })()
        : null;
    return (
      <li className="rounded-md border border-border bg-surface p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-fg">{t(jobPhaseKey(phase))}</span>
          <span
            data-surface-tint={surfaceTintFor(recipeStepStatusClass(step.status))}
            className={recipeStepStatusClass(step.status)}
          >
            {t(recipeStepStatusKey(step.status))}
          </span>
        </div>
        {stageProgress && step.status === "RUNNING" ? (
          <ExtractStageProgressView progress={stageProgress} nowMs={nowMs} strip={segmentStrip} />
        ) : inlineSummary ? (
          <IngestionProgressSummaryView summary={inlineSummary} strip={segmentStrip} />
        ) : step.status === "FAILED" && segmentStrip ? (
          <div className="mt-3">
            <IngestionSegmentStrip view={segmentStrip} />
          </div>
        ) : null}
        {stepErrorMessage ? (
          <Banner severity="danger" title={t("flow.jobs.errorReason")} className="mt-2">
            <FailureMessage message={stepErrorMessage} />
          </Banner>
        ) : null}
      </li>
    );
  }
  const active = ingestionJobIsActive(job.status);
  const failed = step ? step.status === "FAILED" : job.status === "FAILED";
  const normalizedError = normalizeIngestionErrorMessage(
    job.error_message ?? step?.error_message ?? null
  );
  const errorMessageText =
    failed && normalizedError && normalizedError !== suppressMessage ? normalizedError : null;
  const progressSummary =
    active && !hasStageProgress && (phase === "PREPROCESS" || phase === "EXTRACT")
      ? resolveIngestionProgressSummary(segments)
      : null;
  const showStageProgress =
    active && stageProgress != null && (step == null || step.status === "RUNNING");
  const showAttempt = job.attempt_count > 1 || job.status === "FAILED";
  return (
    <li className="rounded-md border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-fg">{t(jobPhaseKey(phase))}</span>
        <span
          data-surface-tint={surfaceTintFor(step ? recipeStepStatusClass(step.status) : jobStatusClass(job.status))}
          className={step ? recipeStepStatusClass(step.status) : jobStatusClass(job.status)}
        >
          {t(step ? recipeStepStatusKey(step.status) : jobStatusKey(job.status))}
        </span>
        {preprocessConverted != null ? (
          <span className="rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {preprocessConverted ? t("provenance.converted") : t("provenance.passthrough")}
          </span>
        ) : null}
        {engineLabel ? (
          <span className="rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {engineLabel}
          </span>
        ) : null}
        {chunkCount != null ? (
          <span className="tnum rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {t("flow.jobs.chunkCount", { count: chunkCount })}
          </span>
        ) : null}
        {chunkSetCreatedAt ? (
          <span className="tnum rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {t("flow.jobs.chunkSetCreatedAt", { time: formatDateTime(chunkSetCreatedAt) })}
          </span>
        ) : null}
        {vectorCount != null ? (
          <span className="tnum rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {t("flow.jobs.vectorCount", { count: vectorCount })}
          </span>
        ) : null}
        {embeddingLabel ? (
          <span className="rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
            {embeddingLabel}
          </span>
        ) : null}
        <span className="tnum ml-auto break-all text-xs text-fg-muted" title={job.id}>
          {t("flow.jobs.jobId", { id: shortJobId(job.id) })}
        </span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <JobMetric label={t("flow.jobs.startedAt")} value={formatDateTime(job.started_at)} />
        <JobMetric label={t("flow.jobs.finishedAt")} value={formatDateTime(job.finished_at)} />
        <JobMetric
          label={t("flow.jobs.elapsed")}
          value={formatJobElapsed(job, nowMs)}
          testId={`ingestion-job-elapsed-${phase.toLowerCase()}`}
        />
        {showAttempt ? (
          <JobMetric
            label={t("flow.jobs.attempt")}
            value={t("flow.jobs.attemptValue", {
              count: job.attempt_count,
              max: job.max_attempts,
            })}
          />
        ) : null}
      </dl>
      {active ? (
        <p className="mt-2 text-xs leading-relaxed text-info-fg">{t("flow.jobs.activeHint")}</p>
      ) : null}
      {progressSummary ? (
        <IngestionProgressSummaryView summary={progressSummary} strip={segmentStrip} />
      ) : null}
      {showStageProgress && stageProgress ? (
        <ExtractStageProgressView progress={stageProgress} nowMs={nowMs} strip={segmentStrip} />
      ) : null}
      {!progressSummary && !showStageProgress && failed && segmentStrip ? (
        <div className="mt-3">
          <IngestionSegmentStrip view={segmentStrip} />
        </div>
      ) : null}
      {errorMessageText ? (
        <Banner severity="danger" title={t("flow.jobs.errorReason")} className="mt-2">
          <FailureMessage message={errorMessageText} />
        </Banner>
      ) : null}
    </li>
  );
}

/**
 * 抽出の段（ページの解析・図表の説明）の進み具合（#1999）。段ごとに文言と bar を並べ、今の段が
 * 何を待っているかと、長く進まないときの経過を添える。スピナーは出さない（同じ job の処理中の
 * 表示は 1 つ。messaging §3.7）。総量が分からない段は不確定の bar にする。
 */
function ExtractStageProgressView({
  progress,
  nowMs,
  strip,
}: {
  progress: IngestionJobProgress;
  nowMs: number;
  /** 解析の段（「抽出: N / M ページ」）の直下に出す段の帯（#2001）。 */
  strip: SegmentStripView | null;
}) {
  const view = resolveExtractStageProgress(progress, nowMs);
  if (!view) return null;
  return (
    <div
      className="mt-3 space-y-2 rounded-md border border-border bg-surface-sunken px-3 py-2"
      data-testid="extract-stage-progress"
    >
      <ol className="space-y-2" aria-label={t("flow.stages.title")}>
        {view.stages.map((stage) => (
          <li key={stage.key} className="space-y-1.5">
            <p
              className={cn(
                "tnum text-xs",
                stage.done ? "text-fg-muted" : "font-medium text-fg"
              )}
            >
              {stage.label}
            </p>
            {stage.value != null && stage.max != null ? (
              <progress
                className="h-2 w-full"
                value={stage.value}
                max={stage.max}
                aria-label={stage.label}
              />
            ) : (
              <progress className="h-2 w-full" aria-label={stage.label} />
            )}
            {stage.key === "parse" && strip ? <IngestionSegmentStrip view={strip} /> : null}
          </li>
        ))}
      </ol>
      {view.waiting ? (
        <p className="pr-message-text text-xs leading-relaxed text-fg-muted">{view.waiting}</p>
      ) : null}
      {view.visionWaiting ? (
        <p className="pr-message-text text-xs leading-relaxed text-fg-muted">
          {view.visionWaiting}
        </p>
      ) : null}
      {view.apiWaiting ? (
        <p
          className="pr-message-text tnum text-xs leading-relaxed text-fg-muted"
          data-testid="extract-stage-api-waiting"
        >
          {view.apiWaiting}
        </p>
      ) : null}
      {view.stalled ? (
        <p className="pr-message-text tnum text-xs leading-relaxed text-warning-fg">
          {view.stalled}
        </p>
      ) : null}
    </div>
  );
}

function IngestionProgressSummaryView({
  summary,
  strip,
}: {
  summary: IngestionProgressSummary;
  /** 「抽出: N / M ページ」の直下に出す段の帯（#2001）。 */
  strip: SegmentStripView | null;
}) {
  const label = ingestionProgressLabel(summary);
  return (
    <div className="mt-3 space-y-1.5 rounded-md border border-border bg-surface-sunken px-3 py-2">
      <p className="text-xs font-medium text-fg">{label}</p>
      {summary.kind === "determinate" ? (
        <progress
          className="h-2 w-full"
          value={summary.completed + summary.failed}
          max={summary.total}
          aria-label={label}
        />
      ) : (
        <progress className="h-2 w-full" aria-label={label} />
      )}
      {strip ? <IngestionSegmentStrip view={strip} /> : null}
    </div>
  );
}

function ingestionProgressLabel(summary: IngestionProgressSummary): string {
  if (summary.kind === "indeterminate") {
    return t("flow.progress.indeterminate");
  }
  const unit = t(progressUnitLabelKey(summary.unit));
  if (summary.failed > 0) {
    return t("flow.progress.failed", {
      completed: summary.completed,
      failed: summary.failed,
      total: summary.total,
      unit,
    });
  }
  return t("flow.progress.determinate", {
    completed: summary.completed,
    total: summary.total,
    unit,
  });
}

function progressUnitLabelKey(unit: ProgressUnit): I18nKey {
  if (unit === "slide") return "flow.progress.unit.slide";
  if (unit === "sheet") return "flow.progress.unit.sheet";
  return "flow.progress.unit.page";
}

function segmentProgressLabel(segment: IngestionSegment): string {
  const start = segment.progress_start ?? segment.page_start;
  const end = segment.progress_end ?? segment.page_end ?? start;
  if (start == null || end == null || segment.progress_unit === "source") {
    return t("flow.segments.source");
  }
  if (segment.progress_unit === "slide") {
    return progressRangeLabel("flow.segments.slideSingle", "flow.segments.slideRange", start, end);
  }
  if (segment.progress_unit === "sheet") {
    return progressRangeLabel("flow.segments.sheetSingle", "flow.segments.sheetRange", start, end);
  }
  return progressRangeLabel("flow.segments.pageSingle", "flow.segments.pageRange", start, end);
}

function progressRangeLabel(
  singleKey: I18nKey,
  rangeKey: I18nKey,
  start: number,
  end: number
): string {
  return start === end
    ? t(singleKey, { number: start })
    : t(rangeKey, { start, end });
}

function JobMetric({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId?: string;
}) {
  return (
    <div data-testid={testId}>
      <dt className="text-fg-muted">{label}</dt>
      <dd className="tnum mt-0.5 font-medium text-fg">{value}</dd>
    </div>
  );
}

function IngestionSegmentsPanel({
  segments,
  loading,
  error,
  retrying,
  visibleErrorSegmentIds,
  retryStatus,
  onRetryFailedSegments,
}: {
  /** 段の状態は実行中の job の進捗を正にしたもの（`segmentsWithProgress`。#2001）。 */
  segments: (IngestionSegment & { reused?: boolean; completed_pages?: number })[];
  loading: boolean;
  error: boolean;
  retrying: boolean;
  visibleErrorSegmentIds: ReadonlySet<string>;
  retryStatus: { tone: "success" | "danger"; message: string } | null;
  onRetryFailedSegments: () => void;
}) {
  if (loading) {
    return (
      <TimedLoadingState
        label={t("flow.segments.loading")}
        operationKey="document-segments-load"
        framed={false}
        testId="document-segments-loading"
      >
        <Skeleton className="h-24 w-full rounded-md" />
      </TimedLoadingState>
    );
  }
  if (error) {
    return (
      <Banner severity="warning" title={t("flow.segments.loadError")}>
        {t("flow.segments.loadErrorHint")}
      </Banner>
    );
  }
  // 段が 1 つ（ページの少ない PDF・PDF でない文書）のときは段の一覧を出さない（#2001）。
  if (segments.length < 2) return null;

  const hasFailedSegments = segments.some((segment) => segment.status === "FAILED");
  const allSucceeded = segments.every((segment) => segment.status === "SUCCEEDED");
  // 段の再試行は 1 つの job で失敗・取消の段をまとめて解析し直す（backend は段を選べない）。ボタンは段の一覧の
  // 先頭の 1 つにし、押したボタンだけを送信の間 loading、結果はその直下に出す（段ごとのボタンが同じ状態を共有して
  // 一斉に回らない。messaging §3.7・§10.1。#2034）。再試行の後、段が待機中に変わっても結果を見せ続けるよう、
  // 結果があるあいだは操作の行を残す（ボタンは失敗した段が無ければ無効）。
  const showRetry = hasFailedSegments || retrying || retryStatus != null;

  // 段の一覧は既定で畳み、失敗した段があるときだけ開く（主の進捗は工程の行の 1 か所。#2001）。
  // 状態が変わったら開き直すため、失敗の有無を key にする（Disclosure の defaultOpen）。再試行の結果を
  // 出しているあいだは開いたままにする（段が待機中に変わって畳まれ、押した結果が見えなくならない。#2034）。
  return (
    <Disclosure
      key={showRetry ? "failed" : "active"}
      defaultOpen={showRetry}
      tone={hasFailedSegments ? "danger" : "neutral"}
      surface="sunken"
      icon={Route}
      summary={t("flow.segments.title")}
      description={t("flow.segments.detailsHint")}
      meta={
        <span className="tnum text-xs text-fg-muted">
          {t("flow.segments.count", { count: segments.length })}
        </span>
      }
      data-testid="document-segments"
      contentClassName="space-y-3"
    >
      {showRetry ? (
        <div className="min-w-0 space-y-2" data-testid="document-segments-retry">
          <p className="text-xs leading-relaxed text-fg-muted">{t("flow.segments.errorRecovery")}</p>
          <div>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={retrying}
              disabled={!hasFailedSegments && !retrying}
              onClick={onRetryFailedSegments}
              icon={RotateCcw}
            >
              {t("flow.segments.retryFailed")}
            </Button>
          </div>
          {retryStatus ? (
            <FormStatus tone={retryStatus.tone} message={retryStatus.message} />
          ) : null}
        </div>
      ) : null}
      {allSucceeded ? (
        <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs text-fg-muted">
          {t("flow.segments.allSucceeded", { count: segments.length })}
        </p>
      ) : (
        <ol
          aria-label={t("flow.segments.title")}
          data-testid="document-segment-list"
          className={cn(
            "grid grid-cols-1 content-start gap-2 rounded-md border border-border bg-surface p-2 [scrollbar-gutter:stable] lg:grid-cols-2",
            INFORMATION_LIST_SCROLL_CLASS
          )}
        >
          {segments.map((segment) => {
            const segmentErrorMessage = visibleErrorSegmentIds.has(segment.segment_id)
              ? normalizeIngestionErrorMessage(segment.error_message)
              : null;
            const pages =
              segment.page_start != null && segment.page_end != null
                ? segment.page_end - segment.page_start + 1
                : null;
            return (
              <li
                key={segment.segment_id}
                className="rounded-md border border-border bg-surface-sunken p-3 text-sm"
                data-testid="document-segment"
                data-status={segment.status}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    data-surface-tint={surfaceTintFor(segmentStatusClass(segment.status))}
                    className={segmentStatusClass(segment.status)}
                  >
                    {t(segmentStatusLabelKey(segment.status))}
                  </span>
                  <span className="tnum text-xs text-fg-muted">
                    {segmentProgressLabel(segment)}
                  </span>
                  {segment.status === "RUNNING" && pages != null ? (
                    <span className="tnum text-xs text-fg-muted">
                      {t("flow.segments.runningPages", {
                        completed: Math.min(segment.completed_pages ?? 0, pages),
                        pages,
                      })}
                    </span>
                  ) : null}
                  {segment.status === "SUCCEEDED" && segment.reused ? (
                    <span className="text-xs text-fg-muted">{t("flow.segments.reused")}</span>
                  ) : null}
                </div>
                <p className="mt-2 break-all text-xs text-fg-muted">
                  {parserBackendLabel(segment.parser_backend)}
                  {!isSameParserBackend(segment.parser_profile, segment.parser_backend)
                    ? ` / ${segment.parser_profile}`
                    : ""}
                </p>
                {/* 失敗は danger の Banner、エラーコードは「詳細」に畳む（失敗のときだけ開く。messaging.md §10）。 */}
                {segmentErrorMessage ? (
                  <Banner severity="danger" title={t("flow.segments.errorReason")} className="mt-2">
                    <FailureMessage message={segmentErrorMessage} code={segment.error_code} />
                  </Banner>
                ) : null}
                {segment.status === "CANCELLED" && segment.error_message ? (
                  <p className="mt-2 text-xs leading-relaxed text-fg-muted">
                    {segment.error_message}
                  </p>
                ) : null}
                {/* 原因を上部の Banner に出した segment も、エラーコードは対処の後の「詳細」に残す。 */}
                {!segmentErrorMessage && segment.error_code && segment.status !== "CANCELLED" ? (
                  <ErrorCodeDetails
                    code={segment.error_code}
                    open={segment.status === "FAILED"}
                    className="mt-1"
                  />
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
    </Disclosure>
  );
}

function jobStatusKey(status: IngestionJob["status"]): I18nKey {
  switch (status) {
    case "QUEUED":
      return "upload.job.status.QUEUED";
    case "RUNNING":
      return "upload.job.status.RUNNING";
    case "SUCCEEDED":
      return "upload.job.status.SUCCEEDED";
    case "FAILED":
      return "upload.job.status.FAILED";
    case "SKIPPED":
      return "upload.job.status.SKIPPED";
    case "CANCELLED":
      return "upload.job.status.CANCELLED";
    default:
      return "upload.job.status.QUEUED";
  }
}

/** レシピ工程状態のラベル。PENDING は呼び出し側で「未実行」行にするため対象外。 */
function recipeStepStatusKey(status: DocumentRecipeStepStatus): I18nKey {
  switch (status) {
    case "NEEDS_REVIEW":
      return "documents.recipes.status.review";
    case "RUNNING":
      return "upload.job.status.RUNNING";
    case "SUCCEEDED":
      return "upload.job.status.SUCCEEDED";
    case "FAILED":
      return "upload.job.status.FAILED";
    case "CANCELLED":
      return "upload.job.status.CANCELLED";
    default:
      return "upload.job.status.QUEUED";
  }
}

function recipeStepStatusClass(status: DocumentRecipeStepStatus): string {
  const base = "rounded-full px-2 py-0.5 text-xs font-medium";
  switch (status) {
    case "QUEUED":
    case "RUNNING":
      return `${base} bg-info-subtle text-info-fg`;
    case "SUCCEEDED":
      return `${base} bg-success-subtle text-success-fg`;
    case "FAILED":
      return `${base} bg-danger-subtle text-danger-fg`;
    case "NEEDS_REVIEW":
      return `${base} bg-warning-subtle text-warning-fg`;
    default:
      return `${base} bg-surface-sunken text-fg-muted`;
  }
}

function jobPhaseKey(phase: IngestionJob["phase"]): I18nKey {
  if (phase === "INDEX") return "flow.jobs.phase.index";
  if (phase === "CHUNK") return "flow.jobs.phase.chunk";
  if (phase === "PREPROCESS") return "flow.jobs.phase.preprocess";
  return "flow.jobs.phase.extract";
}

function jobStatusClass(status: IngestionJob["status"]): string {
  const base = "rounded-full px-2 py-0.5 text-xs font-medium";
  switch (status) {
    case "QUEUED":
    case "RUNNING":
      return `${base} bg-info-subtle text-info-fg`;
    case "SUCCEEDED":
      return `${base} bg-success-subtle text-success-fg`;
    case "FAILED":
      return `${base} bg-danger-subtle text-danger-fg`;
    case "SKIPPED":
      return `${base} bg-warning-subtle text-warning-fg`;
    case "CANCELLED":
      return `${base} bg-surface-sunken text-fg-muted`;
    default:
      return `${base} bg-surface-sunken text-fg-muted`;
  }
}

function shortJobId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

function formatJobElapsed(job: IngestionJob, nowMs = Date.now()): string {
  const startDate = parseApiDateTime(job.started_at ?? job.queued_at);
  const endDate = job.finished_at ? parseApiDateTime(job.finished_at) : null;
  const start = startDate?.getTime();
  const end = job.finished_at ? endDate?.getTime() : nowMs;
  if (start == null || end == null || Number.isNaN(end) || end < start) return "—";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return t("flow.jobs.elapsedSeconds", { seconds });
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest
    ? t("flow.jobs.elapsedMinutesSeconds", { minutes, seconds: rest })
    : t("flow.jobs.elapsedMinutes", { minutes });
}

/** インスペクタ右ペインのタブ(本文 / 構造化要素 / Chunk / エクスポート切替)。 */

function ChunkPreviewControls({
  form,
  onChange,
  onRun,
  pending,
  disabled,
  error,
  result,
}: {
  form: ChunkPreviewForm;
  onChange: (update: Partial<ChunkPreviewForm>) => void;
  onRun: () => void;
  pending: boolean;
  disabled: boolean;
  error: unknown;
  result: DocumentChunkPreviewResponse | null;
}) {
  const validationError = chunkPreviewValidationError(form);
  const fixedDelimiter = form.chunking_strategy === "fixed_delimiter";
  const fixedSize = form.chunking_strategy === "fixed_size";
  const semanticBoundary = isSemanticBoundaryStrategy(form.chunking_strategy);
  const chunkSizeField = (
    <PreviewNumberField
      label={t(chunkSizeLabelKey(form.chunking_strategy))}
      value={form.chunk_size}
      min={CHUNK_SIZE_MIN_CHARS}
      max={CHUNK_SIZE_MAX_CHARS}
      disabled={pending}
      onChange={(value) => onChange({ chunk_size: value })}
    />
  );
  const overlapField = (
    <PreviewNumberField
      label={t(overlapLabelKey(form.chunking_strategy))}
      value={form.chunk_overlap}
      min={0}
      max={CHUNK_OVERLAP_MAX_CHARS}
      disabled={pending}
      onChange={(value) => onChange({ chunk_overlap: value })}
    />
  );
  const minCharsField = !fixedSize ? (
    <PreviewNumberField
      label={t("settings.chunking.params.minChars")}
      value={form.chunk_min_chars}
      min={0}
      max={2000}
      disabled={pending}
      onChange={(value) => onChange({ chunk_min_chars: value })}
    />
  ) : null;
  return (
    <section
      aria-label={t("flow.chunkPreview.title")}
      className="space-y-3 rounded-md border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-fg">
            <FileSearch size={16} className="text-accent-fg" aria-hidden />
            {t("flow.chunkPreview.title")}
          </h4>
          <p className="mt-1 text-xs leading-relaxed text-fg-muted">
            {t("flow.chunkPreview.description")}
          </p>
        </div>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          loading={pending}
          disabled={disabled || Boolean(validationError)}
          onClick={onRun} icon={FileSearch}>
          {t(result ? "flow.chunkPreview.rerun" : "flow.chunkPreview.run")}
        </Button>
      </div>
      {pending ? (
        // 分割のやり直しは文書の大きさに比例して数秒以上かかる。スピナーはボタンの loading が担う。
        <ProcessingIndicator
          active
          label={t("flow.chunkPreview.running")}
          operationKey="chunk-preview"
          placement="action"
          activityIcon="none"
          testId="chunk-preview-processing"
        />
      ) : null}

      <FormGrid>
        <SelectField
          id="chunk-preview-strategy"
          width="md"
          label={t("flow.chunkPreview.strategy")}
          value={form.chunking_strategy}
          options={CHUNK_PREVIEW_STRATEGIES}
          onValueChange={(value) => {
            const preset = chunkingStrategyPreset(value);
            onChange({
              chunking_strategy: value,
              chunk_size: preset.chunkSize,
              chunk_overlap: preset.overlap,
            });
          }}
        />
        {fixedDelimiter ? (
          <TextField
            id="chunk-preview-delimiter"
            className="col-span-full"
            width="sm"
            label={t("settings.chunking.params.delimiter")}
            value={form.chunk_delimiter}
            maxLength={256}
            required
            disabled={pending}
            onValueChange={(chunk_delimiter) => onChange({ chunk_delimiter })}
          />
        ) : form.chunking_strategy === "small_to_big" ? (
          // 親子階層（small-to-big）は chunk サイズ等を使わず、子と親の大きさの 5 項目で分割する。
          SMALL_TO_BIG_PARAMS.map((spec) => (
            <PreviewNumberField
              key={spec.field}
              label={t(spec.labelKey)}
              value={form[spec.field]}
              min={spec.min}
              max={spec.max}
              step={spec.step}
              disabled={pending}
              onChange={(value) => onChange({ [spec.field]: value })}
            />
          ))
        ) : (
          <>
            {semanticBoundary ? (
              <Disclosure
                key={form.chunking_strategy}
                summary={t("settings.chunking.params.semanticDetails")}
                surface="sunken"
                className="col-span-full"
              >
                <p className="mb-3 text-xs leading-relaxed text-fg-muted">
                  {t(
                    form.chunking_strategy === "markdown_heading"
                      ? "settings.chunking.params.headingDescription"
                      : "settings.chunking.params.pageDescription"
                  )}
                </p>
                <FormGrid>
                  {chunkSizeField}
                  {overlapField}
                  {minCharsField}
                </FormGrid>
              </Disclosure>
            ) : (
              <>
                {chunkSizeField}
                {overlapField}
              </>
            )}
            {!semanticBoundary ? minCharsField : null}
          </>
        )}
      </FormGrid>

      {disabled ? (
        <FormStatus tone="warning" message={t("flow.chunkPreview.unsavedEdits")} />
      ) : validationError ? (
        <FormStatus tone="danger" message={validationError} />
      ) : error ? (
        <FormStatus
          tone="danger"
          message={errorMessage(error, t("flow.chunkPreview.error"))}
        />
      ) : null}

      {result ? (
        <>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <PreviewStat
              label={t("flow.chunkPreview.stats.count")}
              value={result.stats.chunk_count}
              chars={false}
            />
            <PreviewStat label={t("flow.chunkPreview.stats.min")} value={result.stats.min_chars} />
            <PreviewStat
              label={t("flow.chunkPreview.stats.average")}
              value={result.stats.average_chars}
            />
            <PreviewStat label={t("flow.chunkPreview.stats.max")} value={result.stats.max_chars} />
            <PreviewStat
              label={t("flow.chunkPreview.stats.overflow")}
              value={result.stats.overflow_count}
              chars={false}
            />
            <PreviewStat
              label={t("flow.chunkPreview.stats.embeddingOverflow")}
              value={result.stats.embedding_overflow_count}
              chars={false}
            />
          </dl>
          {result.warnings.length ? (
            <FormStatus tone="warning" message={result.warnings.join(" ")} />
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function PreviewNumberField({
  label,
  value,
  min,
  max,
  step,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  const id = useId();
  // 数値の欄は空では試せない（chunkPreviewValidationError が止める）ので必須（#531）。
  return (
    <TextField
      id={id}
      width="xs"
      label={label}
      required
      type="number"
      inputMode="numeric"
      value={Number.isFinite(value) ? value : ""}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      onValueChange={(next) => onChange(Number.parseInt(next, 10))}
    />
  );
}

function PreviewStat({
  label,
  value,
  chars = true,
}: {
  label: string;
  value: number;
  chars?: boolean;
}) {
  return (
    <div className="rounded-md border border-border bg-surface-sunken p-2">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="tnum mt-0.5 text-sm font-semibold text-fg">
        {chars
          ? t("flow.chunkPreview.stats.chars", { count: formatNumber(value) })
          : formatNumber(value)}
      </dd>
    </div>
  );
}

function DocumentChunksPanel({
  chunks,
  loading,
  rerunPending = false,
  error,
  selectedChunkId,
  focusRequestKey,
  onSelect,
}: {
  chunks: DocumentChunkView[];
  loading: boolean;
  /** 分割のやり直し（ボタンの loading と ProcessingIndicator が状態を出す）による読込か。 */
  rerunPending?: boolean;
  error: boolean;
  selectedChunkId: string | null;
  focusRequestKey?: string | null;
  onSelect: (chunk: DocumentChunkView) => void;
}) {
  const selectedChunkRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLOListElement | null>(null);

  useEffect(() => {
    if (!focusRequestKey || !selectedChunkId || !selectedChunkRef.current) return;
    scrollFocusedControlIntoView(selectedChunkRef.current, { focus: true });
  }, [focusRequestKey, selectedChunkId]);

  // 一覧は 5 / 8 行の高さで中をスクロールする（xl 以上はプレビューと同じ高さのパネルがスクロールする。#403）。
  // 要素・表セルの選択やタブの切り替えで、選ばれた chunk が見える範囲の外にあれば、
  // そのスクロール領域の中だけを動かして見せる（ページは動かさない）。
  // URL の指定（focusRequestKey）は上の effect がフォーカスごと動かす。再取得では動かさない。
  useEffect(() => {
    if (focusRequestKey || loading || !selectedChunkRef.current) return;
    const container = nearestVerticalScrollContainer(listRef.current);
    if (container) revealWithinScrollContainer(container, selectedChunkRef.current);
  }, [focusRequestKey, loading, selectedChunkId]);

  if (loading && rerunPending) {
    // 同じ処理のスピナーと経過時間は、ボタンと上の ProcessingIndicator の 1 つずつにする（messaging §3.7、#416）。
    return (
      <div aria-busy="true" data-testid="document-chunks-loading">
        <ListSkeleton
          rowClassName="h-[7rem]"
          className={cn("overflow-hidden", INFORMATION_LIST_SCROLL_CLASS, "xl:max-h-none")}
        />
      </div>
    );
  }
  if (loading) {
    return (
      <TimedLoadingState
        label={t("flow.chunks.loading")}
        operationKey="document-chunks-load"
        framed={false}
        testId="document-chunks-loading"
      >
        <ListSkeleton
          rowClassName="h-[7rem]"
          className={cn("overflow-hidden", INFORMATION_LIST_SCROLL_CLASS, "xl:max-h-none")}
        />
      </TimedLoadingState>
    );
  }
  if (error) {
    return (
      <Banner severity="warning" title={t("flow.chunks.loadError")}>
        {t("flow.chunks.loadErrorHint")}
      </Banner>
    );
  }
  if (!chunks.length) {
    return (
      <div className="rounded-md border border-border bg-surface-sunken">
        <EmptyState title={t("flow.chunks.empty")} />
      </div>
    );
  }

  const fellBackToStructure = smallToBigFellBack(chunks);
  const list = (
    <ol
      ref={listRef}
      aria-label={t("flow.chunks.listLabel", { count: chunks.length })}
      data-testid="document-chunk-list"
      className={cn(
        "space-y-3 rounded-lg border border-border bg-surface-sunken p-3 [scrollbar-gutter:stable]",
        INFORMATION_LIST_SCROLL_CLASS,
        // xl 以上はプレビューの横に並び、タブのパネル（プレビューと同じ高さ）がスクロールする。
        // 一覧にも高さを付けると、スクロールが二重になりパネルの下が空く。
        "xl:max-h-none xl:overflow-visible"
      )}
    >
      {chunks.map((chunk) => {
        const selected = chunk.chunk_id === selectedChunkId;
        return (
          <li key={chunk.chunk_id}>
            <button
              data-surface-tint={selected ? "accent" : undefined}
              ref={selected ? selectedChunkRef : undefined}
              type="button"
              className={`w-full rounded-md border p-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                selected ? "border-accent-emphasis bg-accent-subtle" : "border-border bg-surface hover:bg-surface-hover"
              }`}
              aria-pressed={selected}
              onClick={() => onSelect(chunk)}
            >
              <div className="flex flex-wrap items-center gap-2">
                <IndexBadge>#{chunk.chunk_index + 1}</IndexBadge>
                {chunk.content_kind ? (
                  <span data-surface-tint="accent" className="rounded-full bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent-fg">
                    {chunk.content_kind}
                  </span>
                ) : null}
                {chunk.page_start ? (
                  <span data-surface-tint="info" className="tnum rounded-full bg-info-subtle px-2 py-0.5 text-xs text-info-fg">
                    {progressRangeLabel(
                      "flow.segments.pageSingle",
                      "flow.segments.pageRange",
                      chunk.page_start,
                      chunk.page_end ?? chunk.page_start
                    )}
                  </span>
                ) : null}
                {chunk.bbox ? (
                  <span data-surface-tint="success" className="inline-flex items-center gap-1 rounded-full bg-success-subtle px-2 py-0.5 text-xs text-success-fg">
                    <LocateFixed size={14} aria-hidden />
                    bbox
                  </span>
                ) : null}
              </div>
              {chunk.section_path ? (
                <div className="mt-2 flex">
                  <InfoChip icon={Route} label={chunk.section_path} />
                </div>
              ) : null}
              <div className="mt-2">
                <ExtractedText text={chunk.text} clamp />
              </div>
              <div className="mt-2 flex">
                <InfoChip
                  icon={ListTree}
                  label={
                    chunk.element_ids.length
                      ? chunk.element_ids.join(", ")
                      : t("flow.chunks.noElements")
                  }
                />
              </div>
            </button>
          </li>
        );
      })}
    </ol>
  );
  if (!fellBackToStructure) return list;
  // 以前の版で親子階層（small-to-big）の代わりに構造認識で分割した chunk(#300)。作り直しを案内する(#3377)。
  return (
    <div className="space-y-3">
      <Banner severity="warning" title={t("flow.chunks.smallToBigFallbackTitle")}>
        {t("flow.chunks.smallToBigFallback")}
      </Banner>
      {list}
    </div>
  );
}

/** 派生系譜(溯源)。変換あり時のみ呼び出し側で表示する(変換なしは工程行のチップに集約)。 */
function SourceDerivationPanel({
  derivation,
  originalFileName,
  omitWarning,
}: {
  derivation: SourceDerivationView;
  originalFileName: string;
  /** 文書の先頭の案内が出している warning（ここでは重ねない。#2034）。 */
  omitWarning?: (warning: string) => boolean;
}) {
  const pageCount = Object.keys(derivation.pageMap).length;
  const warnings = omitWarning
    ? derivation.warnings.filter((warning) => !omitWarning(warning))
    : derivation.warnings;
  return (
    <section className="rounded-md border border-border bg-surface-sunken p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
        <GitBranch size={16} className="text-accent-fg" aria-hidden />
        {t("provenance.title")}
      </h3>
      {/* 原本 → 処理後ファイル → 抽出 の対応(画面では「変換の記録」)。原本は保全され、処理後ファイルから追跡できる。 */}
      <ol className="mt-3 space-y-2 text-sm">
        <li className="rounded-md border border-border bg-surface px-3 py-2">
          <div className="text-xs text-fg-muted">{t("provenance.original")}</div>
          <div className="mt-0.5 break-all font-medium text-fg">{originalFileName}</div>
        </li>
        <li className="rounded-md border border-border bg-surface px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs text-fg-muted">{t("provenance.canonical")}</div>
            <span data-surface-tint="info" className="rounded bg-info-subtle px-1.5 py-0.5 text-xs font-medium text-info-fg">
              {t(`settings.preprocess.profile.${derivation.preprocessProfile}` as I18nKey)}
            </span>
          </div>
          <div className="mt-0.5 break-all font-medium text-fg">
            {derivation.derivedObjectPath ?? derivation.derivedContentType ?? "-"}
          </div>
          <div className="tnum mt-0.5 break-all text-xs text-fg-muted">
            {derivation.converterName} {derivation.converterVersion}
            {derivation.derivedSha256
              ? ` · sha256: ${derivation.derivedSha256.slice(0, 16)}…`
              : ""}
            {pageCount ? ` · ${t("provenance.pageMap")}: ${pageCount}` : ""}
          </div>
        </li>
      </ol>
      {warnings.length > 0 ? (
        <ul className="mt-3 space-y-1 text-xs text-warning-fg">
          {warnings.map((warning) => (
            <li key={warning}>{excelWarningLabel(warning) ?? warning}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * 原本情報。原本のファクトのみ表示し、処理系の情報は工程行(IngestionJobsPanel)へ寄せる。
 * 文書 ID は品質評価の Excel の「期待する根拠」に `文書ID:` で書けるよう、コピーできる形で出す（#2086）。
 */
function SourceProfilePanel({ documentId, profile }: { documentId: string; profile: SourceProfile }) {
  const warnings = visibleSourceWarningKeys(profile.quality_warnings ?? [], profile.unsupported_reason);
  return (
    <section className="rounded-md border border-border bg-surface-sunken p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
        <FileSearch size={16} className="text-accent-fg" aria-hidden />
        {t("sourceProfile.documentWorkspaceTitle")}
      </h3>
      <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <div className="sm:col-span-3" data-testid="document-id">
          <dt className="text-xs text-fg-muted">{t("sourceProfile.documentId")}</dt>
          <dd className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="break-all font-mono text-xs font-medium text-fg">{documentId}</span>
            <CopyButton
              text={documentId}
              variant="ghost"
              size="sm"
              label={t("sourceProfile.documentIdCopy")}
              copiedMessage={t("sourceProfile.documentIdCopied")}
              failedMessage={t("sourceProfile.documentIdCopyFailed")}
              testId="document-id-copy"
            />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">{t("sourceProfile.parser")}</dt>
          <dd className="mt-0.5 font-medium text-fg">
            {t(parserProfileKey(profile.parser_profile))}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">{t("sourceProfile.contentType")}</dt>
          <dd className="mt-0.5 break-all font-medium text-fg">
            {profile.content_type}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">{t("sourceProfile.hash")}</dt>
          <dd className="tnum mt-0.5 font-medium text-fg">
            {profile.content_sha256.slice(0, 12)}
          </dd>
        </div>
      </dl>
      {profile.unsupported_reason ? (
        <p className="mt-3 text-xs text-warning-fg">
          {t("sourceProfile.unsupportedReason")}:{" "}
          {unsupportedReasonLabel(profile.unsupported_reason)}
        </p>
      ) : null}
      {warnings.length > 0 ? (
        <ul className="mt-3 space-y-1 text-xs text-warning-fg">
          {warnings.map((warning) => (
            <li key={warning}>{t(warning)}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function segmentStatusClass(status: string): string {
  const base = "rounded-full px-2 py-0.5 text-xs font-medium";
  switch (status) {
    case "SUCCEEDED":
      return `${base} bg-success-subtle text-success-fg`;
    case "FAILED":
      return `${base} bg-danger-subtle text-danger-fg`;
    case "RUNNING":
      return `${base} bg-info-subtle text-info-fg`;
    default:
      return `${base} bg-surface-sunken text-fg-muted`;
  }
}

function DocumentKnowledgeBaseEditor({
  documentId,
  initialKnowledgeBases,
}: {
  documentId: string;
  initialKnowledgeBases: KnowledgeBaseRef[];
}) {
  const membership = useDocumentKnowledgeBases(documentId);
  const replace = useReplaceDocumentKnowledgeBases();
  const initialIds = useMemo(
    () => initialKnowledgeBases.map((knowledgeBase) => knowledgeBase.id),
    [initialKnowledgeBases]
  );
  const initialIdsKey = useMemo(() => idSetKey(initialIds), [initialIds]);
  const [selectedIds, setSelectedIds] = useState(initialIds);
  const [savedIds, setSavedIds] = useState(initialIds);

  // 所属 KB の取得前は文書一覧の値、取得後は取得した値を、変わったレンダーで選択と保存値に入れる。
  const initialChanged = useValuesChanged([initialIdsKey, initialIds, membership.data]);
  const membershipChanged = useValuesChanged([membership.data]);
  if (initialChanged && !membership.data) {
    setSelectedIds(initialIds);
    setSavedIds(initialIds);
  }
  if (membershipChanged && membership.data) {
    const ids = membership.data.map((knowledgeBase) => knowledgeBase.id);
    setSelectedIds(ids);
    setSavedIds(ids);
  }

  const isDirty = !isSameIdSet(selectedIds, savedIds);
  // KB 所属の未保存の選択（順序は無視して集合で比べる）があるときだけ離脱を確認する。
  useLeaveGuard(isDirty);
  // 0 件は押せないボタンだけにせず、保存を押したときに選択欄の直下へ理由を出す（#541）。
  const canSave = isDirty && !membership.isPending;
  const [requiredError, setRequiredError] = useState<string | null>(null);

  const onSave = () => {
    if (!canSave) return;
    if (selectedIds.length === 0) {
      setRequiredError(t("documents.knowledgeBases.required"));
      document
        .getElementById(DOCUMENT_KB_PICKER_ID)
        ?.querySelector<HTMLElement>("input, button")
        ?.focus();
      return;
    }
    replace.mutate(
      {
        id: documentId,
        payload: { knowledge_base_ids: selectedIds },
      },
      {
        onSuccess: (refs) => {
          const ids = refs.map((knowledgeBase) => knowledgeBase.id);
          setSelectedIds(ids);
          setSavedIds(ids);
        },
      }
    );
  };

  return (
    <section className="space-y-3 border-t border-border pt-4">
      <div>
        <h3 className="text-sm font-semibold text-fg">
          {t("documents.knowledgeBases.title")}
        </h3>
        <p className="mt-1 text-xs text-fg-muted">{t("documents.knowledgeBases.description")}</p>
      </div>

      {membership.isError ? (
        <Banner severity="warning" title={t("documents.knowledgeBases.loadWarning")}>
          <p>{errorMessage(membership.error, t("documents.knowledgeBases.loadWarningHint"))}</p>
        </Banner>
      ) : null}

      <div id={DOCUMENT_KB_PICKER_ID}>
        <KnowledgeBaseScopePicker
          selectedIds={selectedIds}
          onChange={(ids) => {
            setSelectedIds(ids);
            setRequiredError(null);
          }}
          disabled={replace.isPending || membership.isPending}
          label={t("documents.knowledgeBases.pickerLabel")}
          helper={t("documents.knowledgeBases.helper")}
          // 所属先を 0 件にはできない（保存を押すと欄の下に理由を出し、backend も 1 件以上を必須にする）。
          required
          errorId={requiredError ? DOCUMENT_KB_REQUIRED_ERROR_ID : undefined}
          emptySelectionText={t("documents.knowledgeBases.noneSelected")}
          width="lg"
        />
        <FieldError id={DOCUMENT_KB_REQUIRED_ERROR_ID} message={requiredError} className="mt-1" />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          size="md"
          onClick={onSave}
          loading={replace.isPending}
          disabled={!canSave} icon={Save}>
          {t("documents.knowledgeBases.save")}
        </Button>
        {replace.isSuccess && !isDirty && selectedIds.length > 0 ? (
          <FormStatus tone="success" message={t("documents.knowledgeBases.saved")} />
        ) : null}
        {replace.isError ? (
          <FormStatus
            tone="danger"
            message={errorMessage(replace.error, t("documents.knowledgeBases.saveError"))}
          />
        ) : null}
      </div>
    </section>
  );
}

const DOCUMENT_KB_PICKER_ID = "document-knowledge-base-picker";
const DOCUMENT_KB_REQUIRED_ERROR_ID = "document-knowledge-base-required-error";

const EMPTY_CLASSIFICATION: DocumentClassification = {
  large_category: null,
  middle_category: null,
  small_category: null,
  effective_from: null,
  effective_to: null,
};
const CLASSIFICATION_DATE_FIELDS = ["effective_from", "effective_to"] as const;

/** 文書の分類と有効期間（rag_poc の「ファイル分類」）。検索の分類フィルタと基準日の絞り込みに使う。 */
function DocumentClassificationEditor({
  documentId,
  classification,
}: {
  documentId: string;
  classification: DocumentClassification | null;
}) {
  const save = useSaveDocumentClassification();
  const saved = { ...EMPTY_CLASSIFICATION, ...classification };
  const savedKey = JSON.stringify(saved);
  const [form, setForm] = useState(saved);
  // 文書詳細は処理中に定期的に再取得される。保存値が実際に変わったときだけ入力を入れ替える。
  if (useValuesChanged([savedKey])) setForm(saved);

  const isDirty = JSON.stringify(form) !== savedKey;
  useLeaveGuard(isDirty);
  // 期間の誤りは送る前に終了日の欄の直下で知らせる。backend の 422 は技術的な形（`body: Value error, …`）で返るため（#944）。
  const [periodError, setPeriodError] = useState<string | null>(null);
  const update = (key: keyof DocumentClassification, value: string) => {
    setForm((current) => ({ ...current, [key]: value || null }));
    if (key === "effective_from" || key === "effective_to") setPeriodError(null);
  };
  const onSave = () => {
    if (!isDirty) return;
    if (!classificationPeriodIsValid(form)) {
      setPeriodError(t("documents.classification.periodError"));
      document.getElementById("document-classification-effective_to")?.focus();
      return;
    }
    save.mutate({ id: documentId, payload: form });
  };

  return (
    <section className="space-y-3 border-t border-border pt-4">
      <div>
        <h3 className="text-sm font-semibold text-fg">{t("documents.classification.title")}</h3>
        <p className="mt-1 text-xs text-fg-muted">{t("documents.classification.description")}</p>
      </div>
      {/* 分類は「文書の分類」の一覧から選ぶ（必須ではない。#2131）。名前の段（md）、有効期間の開始日・終了日は
          日付の段（sm）のペア（#1891）。 */}
      <FormGrid>
        <CategorySelectFields
          idPrefix="document-classification"
          labels={{
            large_category: t("documents.classification.large_category"),
            middle_category: t("documents.classification.middle_category"),
            small_category: t("documents.classification.small_category"),
          }}
          values={{
            large_category: form.large_category ?? "",
            middle_category: form.middle_category ?? "",
            small_category: form.small_category ?? "",
          }}
          disabled={save.isPending}
          onChange={(values) =>
            setForm((current) => ({
              ...current,
              large_category: values.large_category || null,
              middle_category: values.middle_category || null,
              small_category: values.small_category || null,
            }))
          }
        />
      </FormGrid>
      <FormGrid>
        {CLASSIFICATION_DATE_FIELDS.map((key) => (
          <TextField
            key={key}
            id={`document-classification-${key}`}
            width="sm"
            type="date"
            label={t(`documents.classification.${key}`)}
            helper={key === "effective_to" ? t("documents.classification.effectiveToHelper") : undefined}
            error={key === "effective_to" ? (periodError ?? undefined) : undefined}
            value={form[key] ?? ""}
            disabled={save.isPending}
            onValueChange={(value) => update(key, value)}
          />
        ))}
      </FormGrid>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          size="md"
          onClick={onSave}
          loading={save.isPending}
          disabled={!isDirty}
          icon={Save}
        >
          {t("documents.classification.save")}
        </Button>
        {save.isSuccess && !isDirty ? (
          <FormStatus tone="success" message={t("documents.classification.saved")} />
        ) : null}
        {save.isError ? (
          <FormStatus
            tone="danger"
            message={errorMessage(save.error, t("documents.classification.saveError"))}
          />
        ) : null}
      </div>
    </section>
  );
}

function idSetKey(ids: string[]) {
  return [...ids].sort().join("\u0000");
}

function isSameIdSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  return idSetKey(left) === idSetKey(right);
}

/**
 * 処理の失敗の本文（messaging.md §10。#705）。原因と対処を本文に出し、エラーコードなどの技術的な詳細は
 * 「詳細」に分ける（失敗なので開いて出す）。
 */
/**
 * 失敗の本文（原因 + 対処）。本文の末尾の「エラーコード: …」と、別に渡したエラーコードは本文から分け、
 * 「詳細」に出す（失敗なので開く。messaging.md §10.3）。
 */
function FailureMessage({ message, code: explicitCode }: { message: string; code?: string | null }) {
  const { message: text, code: embeddedCode } = splitErrorCode(message);
  const code = explicitCode || embeddedCode;
  return (
    <div className="space-y-2">
      <p className="break-words">{text}</p>
      {code ? <ErrorCodeDetails code={code} open /> : null}
    </div>
  );
}

/** エラーコードを「詳細」（Disclosure）に畳んで出す。開くのは失敗のときだけ。 */
function ErrorCodeDetails({
  code,
  open,
  className,
}: {
  code: string;
  open: boolean;
  className?: string;
}) {
  return (
    <Disclosure
      variant="plain"
      size="sm"
      summary={t("flow.error.details")}
      defaultOpen={open}
      className={className}
    >
      <p className="break-all text-xs text-fg-muted">{t("flow.segments.errorCode", { code })}</p>
    </Disclosure>
  );
}
