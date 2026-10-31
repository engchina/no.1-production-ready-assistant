/**
 * TanStack Query フック。query key を一元管理する。
 */

import {
  DATABASE_STATUS_QUERY_KEY,
  SYSTEM_TABLES_QUERY_KEY,
} from "@production-ready/system-settings";
import { DEFAULT_RUN_PERIOD, runPeriodSince, type RunPeriod } from "@production-ready/ui";
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  ANSWER_TRACE_ID_FILTER_MAX,
  EVALUATION_JOB_POLL_INTERVAL_MS,
  api,
  ApiError,
  type BatchUploadFailedItem,
  type BatchUploadResult,
  type KnowledgeBaseSummary,
  type HuggingFaceSettingsUpdate,
  type DomainKeywordImportMode,
  type DocumentApproveRequest,
  type DocumentChunkPreviewRequest,
  type DocumentReviewEditsRequest,
  type DocumentDetail,
  type DocumentSummary,
  type DocumentClassification,
  type AnswerPromptKey,
  type RunHistoryListParams,
  type AnsweringSettingsUpdate,
  type DocumentKnowledgeBaseReplaceRequest,
  type DocumentSupersededByRequest,
  type DocumentProcessingConfig,
  type DocumentExtractionExportFormat,
  type EvaluationJob,
  type EvaluationProcessRow,
  type EvaluationRunRequestBody,
  type FileStatus,
  type FeedbackListParams,
  type FeedbackRequestBody,
  type DocumentSectionsSaveRequest,
  type SectionRule,
  type SectionRulesMode,
  type IngestionJobPhase,
  type IngestionJobStatus,
  type KnowledgeBaseCreateRequest,
  type KnowledgeBaseStatus,
  type KnowledgeBaseUpdateRequest,
  type SearchAnswerProfileCreateRequest,
  type SearchAnswerProfileStatus,
  type SearchAnswerProfileUpdateRequest,
  type ConversationCreateBody,
  type ConversationUpdateBody,
  type ParserAdapterSettingsUpdate,
  type ParserAdapterSettingsData,
  type ChunkingSettingsData,
  type ChunkingSettingsUpdate,
  type LanguagePackData,
  type LanguagePackSections,
  type PreprocessSettingsData,
  type PreprocessSettingsUpdate,
  type ServiceAction,
  type ServiceCatalogData,
  type ServiceControlResultData,
  type ServiceLogsData,
  type ServiceStatusData,
  type ExtractionFieldDefinition,
  type ExtractionFieldsSettingsData,
  type KnowledgeBaseExtractionFieldsData,
  type SearchExtractionFieldsData,
  type PipelineSettingsData,
  type PipelineSettingsUpdate,
  type GuardrailSettingsData,
  type GuardrailSettingsUpdate,
  type VectorIndexSettingsData,
  type VectorIndexSettingsUpdate,
  type EvaluationSettingsData,
  type EvaluationSettingsUpdate,
  type EntityIndexSettingsData,
  type GraphSettingsData,
  type GraphSettingsUpdate,
  type ApprovedFaqListData,
  type ApprovedFaqMutationData,
  type RuntimeKnowledgeEditRequest,
  type SupportGuideContent,
  type SupportGuideDetail,
} from "./api";
import { t } from "./i18n";
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  failedUploadItem,
  formatByteSize,
  mergeBatchUploadResults,
  planUploadRequests,
  totalUploadBytes,
  uploadProgressOf,
  type UploadProgress,
} from "./upload-requests";

export const queryKeys = {
  // DB の状態は3製品共通の query key（#325）。
  databaseStatus: DATABASE_STATUS_QUERY_KEY,
  documents: (params: {
    status?: FileStatus;
    queued_phase?: IngestionJobPhase;
    q?: string;
    knowledge_base_id?: string;
    language_pack_relation?: import("./api").LanguagePackRelation;
    current_release_id?: string;
    limit?: number;
    offset?: number;
  }) => ["documents", params] as const,
  document: (id: string) => ["documents", id] as const,
  documentCategories: ["document-categories"] as const,
  documentChunkSets: (id: string) => ["documents", id, "chunk-sets"] as const,
  documentRecipes: (id: string) => ["documents", id, "recipes"] as const,
  documentPreviewPages: (id: string, recipeId: string | null, variant: string) =>
    ["documents", id, "preview-pages", recipeId ?? "", variant] as const,
  documentRecipeChunks: (id: string, recipeId: string) =>
    ["documents", id, "recipes", recipeId, "chunks"] as const,
  documentRecipeExtractionExport: (
    id: string,
    recipeId: string,
    format: DocumentExtractionExportFormat,
  ) =>
    [
      "documents",
      id,
      "recipes",
      recipeId,
      "extraction-export",
      format,
    ] as const,
  documentIngestionJobs: (id: string) =>
    ["documents", id, "ingestion-jobs"] as const,
  documentIngestionSegments: (id: string) =>
    ["documents", id, "ingestion-segments"] as const,
  documentKnowledgeBases: (id: string) =>
    ["documents", id, "knowledge-bases"] as const,
  documentSections: (id: string, recipeId: string | null) =>
    ["documents", id, "sections", recipeId] as const,
  knowledgeBases: (params: {
    status?: KnowledgeBaseStatus;
    q?: string;
    limit?: number;
    offset?: number;
    ids?: string[];
  }) => ["knowledge-bases", params] as const,
  knowledgeBaseSearch: (params: { status?: KnowledgeBaseStatus; q?: string }) =>
    ["knowledge-bases", "search", params] as const,
  knowledgeBase: (id: string) => ["knowledge-bases", id] as const,
  knowledgeBaseGraph: (id: string) => ["knowledge-bases", id, "graph"] as const,
  knowledgeBaseExtractionFields: (id: string) =>
    ["knowledge-bases", id, "extraction-fields"] as const,
  searchExtractionFields: (searchAnswerProfileId: string) =>
    ["search", "extraction-fields", searchAnswerProfileId] as const,
  searchAnswerProfiles: (params: {
    status?: SearchAnswerProfileStatus;
    q?: string;
    limit?: number;
    offset?: number;
  }) => ["search-answer-profiles", params] as const,
  searchAnswerProfile: (id: string) => ["search-answer-profiles", id] as const,
  entityIndexCoverage: (knowledgeBaseIds: readonly string[]) =>
    ["search-answer-profiles", "entity-index-coverage", ...knowledgeBaseIds] as const,
  inheritedAnswerContact: () => ["search-answer-profiles", "inherited-contact"] as const,
  conversations: (params: {
    search_answer_profile_id?: string;
    limit?: number;
    offset?: number;
  }) => ["conversations", params] as const,
  conversation: (id: string) => ["conversations", id] as const,
  currentFeedback: (traceId: string) =>
    ["feedback", "current", traceId] as const,
  feedback: (params: FeedbackListParams) => ["feedback", params] as const,
  feedbackDetail: (id: string) => ["feedback", "detail", id] as const,
  compareModels: ["chat", "models"] as const,
  searchAnswerModels: ["search", "models"] as const,
  modelSettings: ["settings", "model"] as const,
  databaseSettings: ["settings", "database"] as const,
  systemTables: SYSTEM_TABLES_QUERY_KEY,
  adbInfo: ["settings", "database", "adb"] as const,
  huggingfaceSettings: ["settings", "huggingface"] as const,
  uploadStorageSettings: ["settings", "upload-storage"] as const,
  parserAdapterSettings: ["settings", "parser-adapters"] as const,
  preprocessSettings: ["settings", "preprocess"] as const,
  chunkingSettings: ["settings", "chunking"] as const,
  languagePack: ["settings", "language-pack"] as const,
  languagePackHistory: ["settings", "language-pack", "history"] as const,
  guardrailSettings: ["settings", "guardrail"] as const,
  extractionFieldsSettings: ["settings", "extraction-fields"] as const,
  pipelineSettings: ["settings", "pipeline"] as const,
  vectorIndexSettings: ["settings", "vector-index"] as const,
  evaluationSettings: ["settings", "evaluation-suite"] as const,
  entityIndexSettings: ["settings", "entity-index"] as const,
  graphSettings: ["settings", "graph"] as const,
  services: ["services"] as const,
  serviceCatalog: ["services", "catalog"] as const,
  serviceStatus: (serviceId: string) =>
    ["services", "status", serviceId] as const,
  serviceLogs: (serviceId: string, lines: number) =>
    ["services", "logs", serviceId, lines] as const,
};

function clearDocumentProcessingCache(
  qc: QueryClient,
  documentId: string,
  options: { clearPreprocessArtifact?: boolean } = {},
) {
  qc.setQueryData<DocumentDetail | undefined>(
    queryKeys.document(documentId),
    (current) =>
      current
        ? {
            ...current,
            status: "UPLOADED",
            preprocess_artifact: options.clearPreprocessArtifact
              ? null
              : current.preprocess_artifact,
            extraction: {},
            error_message: null,
            indexed_at: null,
          }
        : current,
  );
  qc.setQueryData(queryKeys.documentIngestionSegments(documentId), []);
  qc.removeQueries({ queryKey: queryKeys.documentChunkSets(documentId) });
}

function invalidateDocumentProcessingQueries(
  qc: QueryClient,
  documentId: string,
) {
  qc.invalidateQueries({ queryKey: ["documents"] });
  qc.invalidateQueries({ queryKey: queryKeys.document(documentId) });
  qc.invalidateQueries({ queryKey: queryKeys.documentChunkSets(documentId) });
  qc.invalidateQueries({
    queryKey: queryKeys.documentIngestionJobs(documentId),
  });
  qc.invalidateQueries({
    queryKey: queryKeys.documentIngestionSegments(documentId),
  });
  qc.invalidateQueries({ queryKey: ["documents", "ingestion-jobs"] });
}

/**
 * 自動更新(条件付きポーリング)の遷移状態判定。
 *
 * `refetchInterval` を関数形式で使い、状態遷移中だけポーリングして安定/終了したら
 * 止める。純粋関数として切り出し Vitest で境界を検証する。
 */

/** 取込/索引が進行中で一覧を再取得すべき文書状態。 */
export const DOCUMENT_ACTIVE_STATUSES: ReadonlySet<FileStatus> =
  new Set<FileStatus>(["PREPROCESSING", "INGESTING", "CHUNKING", "INDEXING"]);

/** ポーリング間隔(ms)。 */
export const ACTIVE_REFETCH_INTERVAL_MS = 4000;

/**
 * 文書一覧に取込/索引進行中の文書が含まれるか。一覧の状態はレシピ1の状態（詳細と同じ正本。#2012）で、
 * 待機中の job（worker が拾う前で状態がまだ変わらない）がある文書も進行中に数える。
 */
export function documentsHaveActiveWork(
  items: ReadonlyArray<Pick<DocumentSummary, "status" | "ingestion_active">> | undefined,
): boolean {
  return Boolean(
    items?.some(
      (item) => item.ingestion_active === true || DOCUMENT_ACTIVE_STATUSES.has(item.status),
    ),
  );
}

/** 取込 job がまだキュー待ち/実行中か。 */
export function ingestionJobIsActive(
  status: IngestionJobStatus | null | undefined,
): boolean {
  return status === "QUEUED" || status === "RUNNING";
}

export function documentWorkspaceShouldRefresh({
  documentStatus,
  watchProcessing = false,
  localWatchProcessing = false,
  jobStatuses = [],
  segmentStatuses = [],
}: {
  documentStatus: FileStatus | null | undefined;
  watchProcessing?: boolean;
  localWatchProcessing?: boolean;
  jobStatuses?: ReadonlyArray<IngestionJobStatus | null | undefined>;
  segmentStatuses?: ReadonlyArray<string | null | undefined>;
}): boolean {
  if (jobStatuses.some(ingestionJobIsActive)) return true;
  if (
    segmentStatuses.some(
      (status) => status === "QUEUED" || status === "RUNNING",
    )
  )
    return true;
  if (documentStatus != null && DOCUMENT_ACTIVE_STATUSES.has(documentStatus))
    return true;

  // PREPROCESSED / REVIEW / CHUNKED / INDEXED / ERROR は通常は安定状態。ただし job 投入直後の
  // 引き継ぎ窓では mutation 側の job status が上の判定に入るため、ここでは止めてよい。
  const terminal =
    documentStatus === "INDEXED" ||
    documentStatus === "ERROR" ||
    documentStatus === "PREPROCESSED" ||
    documentStatus === "REVIEW";
  const stageReview = documentStatus === "CHUNKED";
  return Boolean(
    (watchProcessing || localWatchProcessing) && !terminal && !stageReview,
  );
}

/**
 * ドキュメント一覧（ページング・絞り込み）。
 *
 * 取込/索引が進行中の文書が 1 件でもある間は自動再取得して状態バッジを更新する。
 * `graceActive` は「取込ジョブ投入直後の UPLOADED→INGESTING 引き継ぎ窓」で、まだ
 * アクティブ状態が現れていない間もポーリングを継続させるためにコンポーネントが渡す。
 */
export function useDocuments(
  params: {
    status?: FileStatus;
    queued_phase?: IngestionJobPhase;
    q?: string;
    knowledge_base_id?: string;
    language_pack_relation?: import("./api").LanguagePackRelation;
    current_release_id?: string;
    limit?: number;
    offset?: number;
  },
  options: { graceActive?: boolean; enabled?: boolean } = {},
) {
  return useQuery({
    enabled: options.enabled ?? true,
    queryKey: queryKeys.documents({
      status: params.status,
      queued_phase: params.queued_phase,
      q: params.q,
      knowledge_base_id: params.knowledge_base_id,
      language_pack_relation: params.language_pack_relation,
      current_release_id: params.current_release_id,
      limit: params.limit,
      offset: params.offset,
    }),
    queryFn: () => api.listDocuments(params),
    // 検索語・絞り込み・ページを変えている間は、前の一覧を出したまま取り直す（#535。useKnowledgeBases と同じ）。
    placeholderData: keepPreviousData,
    refetchInterval: (query) =>
      documentsHaveActiveWork(query.state.data?.items) || options.graceActive
        ? ACTIVE_REFETCH_INTERVAL_MS
        : false,
  });
}

/**
 * 一覧の件数の「N 件（全 M 件）」の M（条件を外した全件）を数える（#1891）。絞り込み中（`enabled`）だけ取得する。
 * query key は一覧と同じ接頭辞（`["documents", …]` など）にし、一覧の invalidate で一緒に数え直す。
 * 一覧の query（ポーリング・`keepPreviousData`）とは別の query にして、一覧の再取得・失敗の扱いに影響させない。
 */
export function useListTotal(
  prefix: string,
  params: Record<string, unknown>,
  fetchTotal: () => Promise<number>,
  enabled: boolean,
) {
  return useQuery({
    queryKey: [prefix, "list-total", params] as const,
    queryFn: fetchTotal,
    enabled,
  });
}

/** 404 以外の失敗だけを最大 3 回まで再試行する(TanStack Query の既定の回数に合わせる)。 */
export function retryUnlessNotFound(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status === 404) return false;
  return failureCount < 3;
}

/**
 * 初回の取得に失敗したときだけエラーを返す(#311)。
 * データがあるときの再取得の失敗では前の内容を出したままにする(UX 契約 messaging.md §3.6)。
 */
export function initialLoadError(query: { data: unknown; error: unknown }): unknown {
  return query.data === undefined ? query.error : null;
}

/** ドキュメント詳細。 */
export function useDocument(
  id: string | null,
  options: { refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: queryKeys.document(id ?? ""),
    queryFn: () => api.getDocument(id as string),
    enabled: id != null,
    // 削除済み・存在しない文書(404)は再試行しても変わらない。既定の 3 回の再試行で
    // 数秒 Skeleton のままにせず、すぐに見つからない旨を出す(#281)。
    retry: retryUnlessNotFound,
    refetchInterval: options.refetchInterval,
  });
}

/** 文書の chunk_set(variant)一覧。展開時のみ lazy 取得する。 */
export function useDocumentChunkSets(id: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.documentChunkSets(id ?? ""),
    queryFn: () => api.listDocumentChunkSets(id as string),
    enabled: id != null && enabled,
    retry: retryUnlessNotFound,
  });
}

/** 文書取込 segment/checkpoint 状態。 */
export function useDocumentIngestionSegments(id: string | null) {
  return useQuery({
    queryKey: queryKeys.documentIngestionSegments(id ?? ""),
    queryFn: () => api.listDocumentIngestionSegments(id as string),
    enabled: id != null,
    retry: retryUnlessNotFound,
  });
}

/** 文書単位の取込 job 履歴。workspace 側の一括ポーリングで更新する。 */
export function useDocumentIngestionJobs(id: string | null) {
  return useQuery({
    queryKey: queryKeys.documentIngestionJobs(id ?? ""),
    queryFn: () => api.listDocumentIngestionJobs(id as string),
    enabled: id != null,
    retry: retryUnlessNotFound,
  });
}

/** 失敗した segment checkpoint のみを再試行する取込 job を投入する。 */
export function useRetryFailedDocumentIngestionSegments() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, recipeId }: { id: string; recipeId: string | null }) =>
      api.retryFailedDocumentIngestionSegments(id, recipeId),
    onSuccess: (job, variables) => {
      qc.invalidateQueries({ queryKey: queryKeys.document(variables.id) });
      qc.invalidateQueries({
        queryKey: queryKeys.documentRecipes(variables.id),
      });
      qc.invalidateQueries({
        queryKey: queryKeys.documentIngestionJobs(variables.id),
      });
      qc.invalidateQueries({
        queryKey: queryKeys.documentIngestionSegments(variables.id),
      });
      qc.invalidateQueries({ queryKey: ["documents", "ingestion-jobs"] });
      qc.invalidateQueries({
        queryKey: ["documents", "ingestion-jobs", job.id],
      });
    },
  });
}

/** 文書が所属するナレッジベース一覧。 */
export function useDocumentKnowledgeBases(id: string | null) {
  return useQuery({
    queryKey: queryKeys.documentKnowledgeBases(id ?? ""),
    queryFn: () => api.listDocumentKnowledgeBases(id as string),
    enabled: id != null,
  });
}

/** 文書の章節（章節ナビゲーション。#713）。人の修正が無ければ抽出結果の章節。 */
export function useDocumentSections(id: string | null, recipeId: string | null) {
  return useQuery({
    queryKey: queryKeys.documentSections(id ?? "", recipeId),
    queryFn: () => api.getDocumentSections(id as string, recipeId),
    enabled: id != null,
  });
}

/** 人が修正した章節の保存と、抽出結果への戻し（#713）。章節はすべての処理レシピで共有する。 */
export function useSaveDocumentSections(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DocumentSectionsSaveRequest) => api.saveDocumentSections(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["documents", id, "sections"] }),
  });
}

export function useResetDocumentSections(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (recipeId: string | null) => api.resetDocumentSections(id, recipeId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["documents", id, "sections"] }),
  });
}

/** 章節の抽出規則の全体の既定（#715）。 */
export function useSectionRulesSettings() {
  return useQuery({
    queryKey: ["settings", "section-rules"] as const,
    queryFn: () => api.getSectionRulesSettings(),
  });
}

/** 章節の抽出規則の保存・既定に戻す（#715）。章節ナビゲーションは取得のたびに規則を当てるので読み直す。 */
export function useSaveSectionRulesSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { mode: SectionRulesMode; rules: SectionRule[] }) =>
      api.saveSectionRulesSettings(body),
    onSuccess: (data) => {
      qc.setQueryData(["settings", "section-rules"], data);
      void qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

export function useResetSectionRulesSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.resetSectionRulesSettings(),
    onSuccess: (data) => {
      qc.setQueryData(["settings", "section-rules"], data);
      void qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

/** 文書の1〜3件の独立処理レシピ。 */
/** PDF のページ画像プレビュー用のページ一覧。描けないファイル（422）は再試行せず iframe に戻す。 */
export function useDocumentPreviewPages(
  id: string | null,
  options: { recipeId?: string | null; variant?: "original" | "prepared"; enabled?: boolean } = {},
) {
  const variant = options.variant ?? "original";
  return useQuery({
    queryKey: queryKeys.documentPreviewPages(id ?? "", options.recipeId ?? null, variant),
    queryFn: () =>
      api.getDocumentPreviewPages(id as string, { recipeId: options.recipeId, variant }),
    enabled: id != null && (options.enabled ?? true),
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useDocumentRecipes(id: string | null) {
  return useQuery({
    queryKey: queryKeys.documentRecipes(id ?? ""),
    queryFn: () => api.listDocumentRecipes(id as string),
    enabled: id != null,
    retry: retryUnlessNotFound,
    refetchInterval: (query) => {
      const recipes = query.state.data;
      return recipes?.some((recipe) =>
        recipe.steps.some(
          (step) => step.status === "QUEUED" || step.status === "RUNNING",
        ),
      )
        ? 1_500
        : false;
    },
  });
}

export function useDocumentRecipeChunks(
  id: string | null,
  recipeId: string | null,
) {
  return useQuery({
    queryKey: queryKeys.documentRecipeChunks(id ?? "", recipeId ?? ""),
    queryFn: () =>
      api.listDocumentRecipeChunks(id as string, recipeId as string),
    enabled: id != null && recipeId != null,
    retry: retryUnlessNotFound,
  });
}

export function usePreviewDocumentRecipeChunks() {
  return useMutation({
    mutationFn: ({
      id,
      recipeId,
      payload,
    }: {
      id: string;
      recipeId: string;
      payload: DocumentChunkPreviewRequest;
    }) => api.previewDocumentRecipeChunks(id, recipeId, payload),
  });
}

export function useDocumentRecipeExtractionExport(
  id: string | null,
  recipeId: string | null,
  format: DocumentExtractionExportFormat,
) {
  return useQuery({
    queryKey: queryKeys.documentRecipeExtractionExport(
      id ?? "",
      recipeId ?? "",
      format,
    ),
    queryFn: () =>
      api.exportDocumentRecipeExtraction(
        id as string,
        recipeId as string,
        format,
      ),
    enabled: id != null && recipeId != null,
    retry: retryUnlessNotFound,
  });
}

function invalidateDocumentRecipeQueries(qc: QueryClient, documentId: string) {
  qc.invalidateQueries({ queryKey: queryKeys.documentRecipes(documentId) });
  qc.invalidateQueries({ queryKey: ["documents", documentId, "recipes"] });
  qc.invalidateQueries({
    queryKey: queryKeys.documentIngestionJobs(documentId),
  });
  qc.invalidateQueries({
    queryKey: queryKeys.documentIngestionSegments(documentId),
  });
  qc.invalidateQueries({ queryKey: queryKeys.document(documentId) });
}

export function useCreateDocumentRecipe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, copyFrom }: { id: string; copyFrom: string | null }) =>
      api.createDocumentRecipe(id, copyFrom),
    onSuccess: (_recipe, variables) =>
      invalidateDocumentRecipeQueries(qc, variables.id),
  });
}

export function useDeleteDocumentRecipe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, recipeId }: { id: string; recipeId: string }) =>
      api.deleteDocumentRecipe(id, recipeId),
    onSuccess: (_result, variables) =>
      invalidateDocumentRecipeQueries(qc, variables.id),
  });
}

export function useUpdateDocumentRecipe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      recipeId,
      config,
    }: {
      id: string;
      recipeId: string;
      config: DocumentProcessingConfig;
    }) => api.updateDocumentRecipe(id, recipeId, config),
    onSuccess: (recipe, variables) => {
      qc.setQueryData(
        queryKeys.documentRecipes(variables.id),
        (current: Array<typeof recipe> | undefined) =>
          current?.map((item) =>
            item.recipe_id === recipe.recipe_id ? recipe : item,
          ),
      );
    },
  });
}

export function useEnqueueDocumentRecipeJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      recipeId,
      phase,
    }: {
      id: string;
      recipeId: string;
      phase: IngestionJobPhase;
    }) => api.enqueueDocumentRecipeJob(id, recipeId, phase),
    onSuccess: (_job, variables) =>
      invalidateDocumentRecipeQueries(qc, variables.id),
  });
}

export function useApproveDocumentRecipe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      recipeId,
      payload,
    }: {
      id: string;
      recipeId: string;
      payload?: DocumentApproveRequest;
    }) => api.approveDocumentRecipe(id, recipeId, payload),
    onSuccess: (_job, variables) =>
      invalidateDocumentRecipeQueries(qc, variables.id),
  });
}

export function useSaveDocumentRecipeReviewEdits() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      recipeId,
      payload,
    }: {
      id: string;
      recipeId: string;
      payload: DocumentReviewEditsRequest;
    }) => api.saveDocumentRecipeReviewEdits(id, recipeId, payload),
    onSuccess: (_recipe, variables) =>
      invalidateDocumentRecipeQueries(qc, variables.id),
    // 409（ほかの保存が先に同じ箇所を変えた・承認済み）は、今の抽出結果と状態を読み込み直す（#2616）。
    onError: (error, variables) => {
      if (error instanceof ApiError && error.status === 409) {
        invalidateDocumentRecipeQueries(qc, variables.id);
      }
    },
  });
}

/** 文書の分類の一覧（大分類 → 中分類 → 小分類。#2131）。文書の詳細・RAG 検索の選択肢と管理の画面で使う。 */
export function useDocumentCategories() {
  return useQuery({
    queryKey: queryKeys.documentCategories,
    queryFn: () => api.listDocumentCategories(),
    staleTime: 60_000,
  });
}

/** 分類の一覧を変える操作（作成・名前の変更・並べ替え・削除）。終わったら一覧と文書を読み直す。 */
export function useDocumentCategoryMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.documentCategories });
      // 名前の変更は文書の分類の値も変える（文書の詳細・一覧を読み直す。どちらも "documents" で始まる）。
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

/** 文書の分類と有効期間を保存する。 */
export function useSaveDocumentClassification() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DocumentClassification }) =>
      api.saveDocumentClassification(id, payload),
    onSuccess: (detail) => {
      qc.setQueryData(queryKeys.document(detail.id), detail);
      // 分類の一覧の文書の数を読み直す。
      qc.invalidateQueries({ queryKey: queryKeys.documentCategories });
    },
  });
}

/** 文書を置き換えた新しい版を設定・解除する（#1248）。一覧の旧版の印も変わるので取り直す。 */
export function useSaveDocumentSupersededBy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DocumentSupersededByRequest }) =>
      api.saveDocumentSupersededBy(id, payload),
    onSuccess: (detail) => {
      qc.setQueryData(queryKeys.document(detail.id), detail);
      // 一覧（key の 2 番目が条件の object）だけを取り直す。開いている文書の詳細・レシピなどは取り直さない。
      qc.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "documents" &&
          typeof query.queryKey[1] === "object" &&
          query.queryKey[1] !== null,
      });
    },
  });
}

export function useReplaceDocumentKnowledgeBases() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: DocumentKnowledgeBaseReplaceRequest;
    }) => api.replaceDocumentKnowledgeBases(id, payload),
    onSuccess: (_refs, variables) => {
      qc.invalidateQueries({
        queryKey: queryKeys.documentKnowledgeBases(variables.id),
      });
      qc.invalidateQueries({ queryKey: queryKeys.document(variables.id) });
      qc.invalidateQueries({
        queryKey: queryKeys.documentChunkSets(variables.id),
      });
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/** ドキュメント本体と検索 index を削除する。 */
export function useDeleteDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({id, fileName}: {id: string; fileName: string}) => api.deleteDocument(id, fileName),
    onSuccess: (result) => {
      qc.removeQueries({ queryKey: queryKeys.document(result.id) });
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["documents", "ingestion-jobs"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/** ファイルアップロード。成功時に一覧を無効化。 */
export function useUploadDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      knowledgeBaseIds = [],
      onProgress,
    }: {
      file: File;
      knowledgeBaseIds?: string[];
      /** 送信の進み具合（送信済み / 合計のバイト数。#306）。 */
      onProgress?: (progress: UploadProgress) => void;
    }) =>
      api.uploadDocument(file, knowledgeBaseIds, (transfer) =>
        onProgress?.(uploadProgressOf(0, file.size, transfer, file.size)),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/**
 * 複数ファイルアップロード。
 *
 * 1 リクエストの合計が 1 ファイルの上限以内になるよう分けて送り（前段 nginx の body 上限で一括全体が
 * 413 にならないように）、上限を超えるファイルは送らずに失敗として返す（#280）。途中のまとまりが
 * 失敗しても、それまでに保存できたファイルの結果は残す。認証切れ・権限なしは中断して投げ直す。
 * 送信の進み具合は、送るファイル全体のバイト数に対する送信済みのバイト数で通知する（#306）。
 */
export function useBatchUploadDocuments() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      files,
      knowledgeBaseIds = [],
      maxUploadBytes = DEFAULT_MAX_UPLOAD_BYTES,
      onProgress,
    }: {
      files: File[];
      knowledgeBaseIds?: string[];
      /** 1 ファイルの上限（upload-storage 設定の `max_upload_bytes`）。 */
      maxUploadBytes?: number;
      /** 送信の進み具合（送信済み / 合計のバイト数。#306）。 */
      onProgress?: (progress: UploadProgress) => void;
    }): Promise<BatchUploadResult> => {
      const { groups, oversized } = planUploadRequests(files, maxUploadBytes);
      const results: BatchUploadResult[] = [];
      const failed: BatchUploadFailedItem[] = oversized.map((file) =>
        failedUploadItem(
          file,
          413,
          t("upload.error.fileTooLarge", { size: formatByteSize(maxUploadBytes) }),
        ),
      );
      const totalBytes = totalUploadBytes(groups.flat());
      let doneBytes = 0;
      for (const group of groups) {
        const groupBytes = totalUploadBytes(group);
        try {
          results.push(
            await api.batchUploadDocuments(group, knowledgeBaseIds, (transfer) =>
              onProgress?.(uploadProgressOf(doneBytes, groupBytes, transfer, totalBytes)),
            ),
          );
        } catch (error) {
          if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
            throw error;
          }
          const status = error instanceof ApiError ? error.status : 0;
          const message = uploadErrorMessage(error);
          failed.push(...group.map((file) => failedUploadItem(file, status, message)));
        } finally {
          // 失敗したまとまりも「送信を終えた」量として数え、進み具合を後戻りさせない。
          doneBytes += groupBytes;
          onProgress?.({ sentBytes: doneBytes, totalBytes });
        }
      }
      return mergeBatchUploadResults(results, failed);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function uploadErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    // 前段の proxy が返す 413 は ApiResponse の本文を持たないため、既定の「APIエラー (413)」を言い換える。
    if (error.status === 413 && error.isFallbackMessage) {
      return t("upload.error.requestTooLarge");
    }
    return error.message;
  }
  return t("upload.error.failed");
}

/** 取込 job 詳細。 */
export function useIngestionJob(id: string | null) {
  return useQuery({
    queryKey: ["documents", "ingestion-jobs", id] as const,
    queryFn: () => api.getIngestionJob(id as string),
    enabled: id != null,
    refetchInterval: (query) =>
      ingestionJobIsActive(query.state.data?.status) ? 2000 : false,
  });
}

/** ナレッジベース一覧。 */
export function useKnowledgeBases(params: {
  status?: KnowledgeBaseStatus;
  q?: string;
  limit?: number;
  offset?: number;
}) {
  return useQuery({
    queryKey: queryKeys.knowledgeBases(params),
    queryFn: () => api.listKnowledgeBases(params),
    // 検索語・絞り込み・ページを変えている間は、前の一覧を出したまま取り直す（表を Skeleton に戻さない。#535）。
    // 応答は query key ごとに持つので、遅れて返った古い条件の応答が新しい条件の一覧を上書きしない。
    placeholderData: keepPreviousData,
  });
}

/** ナレッジベース詳細(adapter_config を含む)。 */
export function useKnowledgeBase(id: string | null) {
  return useQuery({
    queryKey: queryKeys.knowledgeBase(id ?? ""),
    queryFn: () => api.getKnowledgeBase(id as string),
    enabled: id != null,
    // URL の対象が無い（404）ときは再試行せず、すぐ「対象が見つかりません」を出す（検索・回答プロファイルと同じ。#555）。
    retry: retryUnlessNotFound,
  });
}

/** ナレッジベースの項目抽出の定義（#548）。 */
export function useKnowledgeBaseExtractionFields(id: string) {
  return useQuery<KnowledgeBaseExtractionFieldsData>({
    queryKey: queryKeys.knowledgeBaseExtractionFields(id),
    queryFn: () => api.getKnowledgeBaseExtractionFields(id),
    retry: false,
  });
}

/** ナレッジベースの項目抽出の定義を保存する（#548）。 */
export function useUpdateKnowledgeBaseExtractionFields(id: string) {
  const onSuccess = useKnowledgeBaseExtractionFieldsSaved(id);
  return useMutation({
    mutationFn: (fields: ExtractionFieldDefinition[]) =>
      api.updateKnowledgeBaseExtractionFields(id, { fields }),
    onSuccess,
  });
}

/** ナレッジベースの項目抽出の定義を消し、全体の既定に戻す（#548）。 */
export function useResetKnowledgeBaseExtractionFields(id: string) {
  const onSuccess = useKnowledgeBaseExtractionFieldsSaved(id);
  return useMutation({
    mutationFn: () => api.updateKnowledgeBaseExtractionFields(id, { fields: null }),
    onSuccess,
  });
}

function useKnowledgeBaseExtractionFieldsSaved(id: string) {
  const qc = useQueryClient();
  return (data: KnowledgeBaseExtractionFieldsData) => {
    qc.setQueryData(queryKeys.knowledgeBaseExtractionFields(id), data);
    void qc.invalidateQueries({ queryKey: ["search", "extraction-fields"] });
  };
}

/** 検索の絞り込みに使える項目（選んだ検索・回答プロファイルの KB の定義の和集合。#549）。 */
export function useSearchExtractionFields(searchAnswerProfileId: string | null, enabled = true) {
  return useQuery<SearchExtractionFieldsData>({
    queryKey: queryKeys.searchExtractionFields(searchAnswerProfileId ?? ""),
    queryFn: () => api.getSearchExtractionFields(searchAnswerProfileId ?? ""),
    enabled: enabled && Boolean(searchAnswerProfileId),
    retry: false,
  });
}

export function useKnowledgeBaseGraph(id: string | null) {
  return useQuery({
    queryKey: queryKeys.knowledgeBaseGraph(id ?? ""),
    queryFn: () => api.getKnowledgeBaseGraph(id as string),
    enabled: id != null,
  });
}

/** ナレッジベース作成。 */
export function useCreateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: KnowledgeBaseCreateRequest) =>
      api.createKnowledgeBase(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/** ナレッジベース更新。 */
export function useUpdateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: KnowledgeBaseUpdateRequest;
    }) => api.updateKnowledgeBase(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/** ナレッジベースをアーカイブする。 */
export function useArchiveKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.archiveKnowledgeBase(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

/** 選択肢として 1 回に取得する KB の件数。これを超える分は「さらに表示」で次のページを取る。 */
export const KNOWLEDGE_BASE_SEARCH_PAGE_SIZE = 50;

/**
 * KB を選ぶ UI 用のサーバー側検索（名前・説明の部分一致 + ページング。#302）。
 * 先頭のページだけで打ち切らず、`fetchNextPage` で続きを取れる。検索語を変えている間は
 * 直前の結果を出したままにする（一覧が空に戻ってちらつかないように）。
 */
export function useKnowledgeBaseSearch(
  params: { status?: KnowledgeBaseStatus; q?: string },
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useInfiniteQuery({
    queryKey: queryKeys.knowledgeBaseSearch(params),
    queryFn: ({ pageParam }) =>
      api.listKnowledgeBases({
        ...params,
        limit: KNOWLEDGE_BASE_SEARCH_PAGE_SIZE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage) =>
      lastPage.has_next ? lastPage.offset + lastPage.items.length : undefined,
    placeholderData: keepPreviousData,
    enabled,
  });
}

/** 文書を選ぶ一覧（`DocumentPicker`）で 1 回に取得する件数。続きは「さらに読み込む」で取る（#600）。 */
export const DOCUMENT_CANDIDATE_PAGE_SIZE = 100;

/**
 * 文書を選ぶ一覧の候補（文書名のサーバー側検索 + ページング。#600）。数千〜数万件の文書を一度に読まず、
 * `fetchNextPage` で続きを積む。検索語を変えている間は直前の候補を出したままにする。
 * `knowledgeBaseIds` を渡すと、そのナレッジベースのどれかに所属する文書に絞る（業務ガイドの資料。#2749）。
 */
export function useDocumentCandidates(
  params: { q?: string; knowledgeBaseIds?: readonly string[] },
  options: { enabled?: boolean } = {},
) {
  const knowledgeBaseId = params.knowledgeBaseIds ? [...params.knowledgeBaseIds].sort().join(",") : undefined;
  return useInfiniteQuery({
    queryKey: [
      ...queryKeys.documents({
        q: params.q,
        knowledge_base_id: knowledgeBaseId,
        limit: DOCUMENT_CANDIDATE_PAGE_SIZE,
      }),
      "candidates",
    ],
    queryFn: ({ pageParam }) =>
      api.listDocuments({
        q: params.q,
        knowledge_base_id: knowledgeBaseId,
        limit: DOCUMENT_CANDIDATE_PAGE_SIZE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage) =>
      lastPage.has_next ? lastPage.offset + lastPage.items.length : undefined,
    placeholderData: keepPreviousData,
    enabled: options.enabled ?? true,
  });
}

/**
 * 画面側で絞り込む KB の件数の上限（= 一覧 API の `limit` の最大値。1 回の取得で全件が揃う）。
 * これを超えるときはサーバー側の検索（`GET /api/knowledge-bases?q=`）に切り替え、全件を読まない（#578）。
 */
export const KNOWLEDGE_BASE_LOCAL_FILTER_LIMIT = 200;

/**
 * KB の選択肢（検索できる選択部品用。#578）。
 *
 * - まず先頭の 200 件（`KNOWLEDGE_BASE_LOCAL_FILTER_LIMIT`）を 1 回で取る。全件がそこに収まれば、
 *   選択肢は手元の全件で、検索は画面側で絞り込む（`remote: false`。1 文字ごとに問い合わせない）。
 * - 収まらない（201 件以上）ときは、検索語 `q` でサーバー側を検索し、50 件ずつ続きを読む（`remote: true`）。
 *   全件をページ送りで読み切らない（数百件でも最初の表示と検索が遅くならない）。
 */
export function useKnowledgeBaseChoices(params: { status?: KnowledgeBaseStatus; q: string }) {
  const head = useQuery({
    queryKey: queryKeys.knowledgeBases({
      status: params.status,
      limit: KNOWLEDGE_BASE_LOCAL_FILTER_LIMIT,
      offset: 0,
    }),
    queryFn: () =>
      api.listKnowledgeBases({
        status: params.status,
        limit: KNOWLEDGE_BASE_LOCAL_FILTER_LIMIT,
        offset: 0,
      }),
  });
  const remote = Boolean(head.data?.has_next);
  const search = useKnowledgeBaseSearch(
    { status: params.status, q: params.q || undefined },
    { enabled: remote },
  );
  const items: KnowledgeBaseSummary[] = remote
    ? (search.data?.pages.flatMap((page) => page.items) ?? [])
    : (head.data?.items ?? []);
  return {
    /** サーバー側で検索している（全件を持たない）。 */
    remote,
    items,
    /** 条件（status）に合う KB の総数（検索語によらない）。 */
    total: head.data?.total ?? 0,
    /** 検索語に一致する件数（サーバー側の検索のとき）。 */
    matchedTotal: remote ? (search.data?.pages[0]?.total ?? 0) : items.length,
    isPending: head.isPending || (remote && search.isPending),
    isError: head.isError || (remote && search.isError),
    error: head.error ?? search.error,
    isFetching: head.isFetching || search.isFetching,
    /** 検索語を変えて取り直している（前の結果を出したまま）。 */
    searching: remote && search.isPlaceholderData && search.isFetching,
    hasMore: remote && Boolean(search.hasNextPage),
    loadingMore: remote && search.isFetchingNextPage,
    loadMore: () => void search.fetchNextPage(),
    refetch: () => {
      void head.refetch();
      if (remote) void search.refetch();
    },
  };
}

/** 一覧 API の `ids` の上限（backend の `MAX_KNOWLEDGE_BASE_ID_FILTER`）。 */
const KNOWLEDGE_BASE_ID_LOOKUP_LIMIT = 200;

/**
 * 選択済みの KB を ID で引く（アーカイブ済みを含む。#302）。検索結果のページに無い選択済みの
 * KB もチップに名前と状態を出すために使う。返らない ID は、存在しないか利用者の範囲外。
 */
export function useKnowledgeBasesByIds(ids: string[]) {
  const sorted = [...new Set(ids)].sort().slice(0, KNOWLEDGE_BASE_ID_LOOKUP_LIMIT);
  return useQuery({
    queryKey: queryKeys.knowledgeBases({ ids: sorted, limit: KNOWLEDGE_BASE_ID_LOOKUP_LIMIT }),
    queryFn: () => api.listKnowledgeBases({ ids: sorted, limit: KNOWLEDGE_BASE_ID_LOOKUP_LIMIT }),
    enabled: sorted.length > 0,
    placeholderData: keepPreviousData,
  });
}

/** 検索・回答プロファイル(Search Answer Profile)一覧。 */
export function useSearchAnswerProfiles(params: {
  status?: SearchAnswerProfileStatus;
  q?: string;
  limit?: number;
  offset?: number;
}) {
  return useQuery({
    queryKey: queryKeys.searchAnswerProfiles(params),
    queryFn: () => api.listSearchAnswerProfiles(params),
    // 検索語・絞り込み・ページを変えている間は、前の一覧を出したまま取り直す（#535。useKnowledgeBases と同じ）。
    placeholderData: keepPreviousData,
  });
}

/** 単一選択の候補。後続ページも揃えてから、名前・参照 KB・選択中の対象を解決する。 */
export function useAllSearchAnswerProfiles(params: { status?: SearchAnswerProfileStatus }) {
  return useQuery({
    queryKey: ["search-answer-profiles", { ...params, scope: "all-choices" }],
    queryFn: async () => {
      const first = await api.listSearchAnswerProfiles({ ...params, limit: 50, offset: 0 });
      const items = [...first.items];
      const warnings = [...first.warning_messages];
      let page = first;
      while (page.has_next && page.items.length > 0) {
        page = await api.listSearchAnswerProfiles({
          ...params,
          limit: 50,
          offset: page.offset + page.items.length,
        });
        items.push(...page.items);
        warnings.push(...page.warning_messages);
      }
      return { ...first, items, has_next: page.has_next, warning_messages: warnings };
    },
  });
}

/** 検索・回答プロファイル詳細(config・参照 KB を含む)。 */
export function useSearchAnswerProfile(id: string | null) {
  return useQuery({
    queryKey: queryKeys.searchAnswerProfile(id ?? ""),
    queryFn: () => api.getSearchAnswerProfile(id as string),
    enabled: id != null,
    // URL の `?id=` の対象が無い（404）ときは再試行せず、すぐ「見つかりません」を出す。
    retry: retryUnlessNotFound,
  });
}

/**
 * 参照するナレッジベースで実体の索引を持つ文書の数(#1388)。実体の 1 段の拡張を選んだときだけ引き、
 * 0 件なら開閉の横に「文書レシピで実体の索引を有効にすると効きます」と案内する。
 */
export function useEntityIndexCoverage(knowledgeBaseIds: readonly string[], enabled: boolean) {
  const ids = [...knowledgeBaseIds].sort();
  return useQuery({
    queryKey: queryKeys.entityIndexCoverage(ids),
    queryFn: () => api.entityIndexCoverage(ids),
    enabled: enabled && ids.length > 0,
    staleTime: 30_000,
  });
}

/**
 * プロファイルの窓口の上書きと業務ガイドの引き継ぎ先が継承する、システム設定の窓口（#2683）。
 * 空の欄の placeholder と「有効な窓口」の出どころに使う（値は入力欄にコピーしない）。
 */
export function useInheritedAnswerContact() {
  return useQuery({
    queryKey: queryKeys.inheritedAnswerContact(),
    queryFn: () => api.getInheritedAnswerContact(),
    staleTime: 30_000,
  });
}

export function useQuestionUnderstanding(id: string) {
  return useQuery({
    queryKey: [...queryKeys.searchAnswerProfile(id), "question-understanding"],
    queryFn: () => api.getQuestionUnderstanding(id),
    retry: false,
  });
}

export function useSaveQuestionUnderstanding(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (overrides: LanguagePackSections) => api.saveQuestionUnderstanding(id, overrides),
    onSuccess: (data) => {
      qc.setQueryData([...queryKeys.searchAnswerProfile(id), "question-understanding"], data);
      void qc.invalidateQueries({ queryKey: queryKeys.searchAnswerProfile(id), exact: true });
    },
  });
}

export function usePreviewQuestionUnderstanding(id: string) {
  return useMutation({
    mutationFn: ({ text, overrides }: { text: string; overrides: LanguagePackSections }) =>
      api.previewQuestionUnderstanding(id, text, overrides),
  });
}

/** 検索・回答プロファイルのドメインキーワード。 */
export function useDomainKeywords(searchAnswerProfileId: string) {
  return useQuery({
    queryKey: ["search-answer-profiles", searchAnswerProfileId, "domain-keywords"],
    queryFn: () => api.getDomainKeywords(searchAnswerProfileId),
  });
}

/** ドメインキーワードの保存(全置換)。 */
export function useSaveDomainKeywords(searchAnswerProfileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (keywords: string[]) =>
      api.saveDomainKeywords(searchAnswerProfileId, keywords),
    onSuccess: (data) => {
      qc.setQueryData(
        ["search-answer-profiles", searchAnswerProfileId, "domain-keywords"],
        data,
      );
    },
  });
}

/** Excel 取込後の保存値を左の編集欄にも反映する。 */
export function useImportDomainKeywords(searchAnswerProfileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { file: File; mode: DomainKeywordImportMode }) =>
      api.importDomainKeywords(searchAnswerProfileId, args.file, args.mode),
    onSuccess: (data) => {
      qc.setQueryData(
        ["search-answer-profiles", searchAnswerProfileId, "domain-keywords"],
        data,
      );
    },
  });
}

/** 検索・回答プロファイルの承認済み FAQ(類似問)。 */
export function useApprovedFaq(searchAnswerProfileId: string) {
  return useQuery({
    queryKey: ["search-answer-profiles", searchAnswerProfileId, "approved-faq"],
    queryFn: () => api.getApprovedFaq(searchAnswerProfileId),
  });
}

/** FAQ の追加・削除・取込。成功時は一覧 cache を結果で置き換える。 */
export function useApprovedFaqMutation<TArgs, TData extends ApprovedFaqListData = ApprovedFaqMutationData>(
  searchAnswerProfileId: string,
  mutationFn: (args: TArgs) => Promise<TData>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: (data) => {
      qc.setQueryData(["search-answer-profiles", searchAnswerProfileId, "approved-faq"], data);
    },
  });
}

/** 検索・回答プロファイルの用語・同義語と回答ルール。 */
export function useRuntimeKnowledge(searchAnswerProfileId: string) {
  return useQuery({
    queryKey: ["search-answer-profiles", searchAnswerProfileId, "runtime-knowledge"],
    queryFn: () => api.getRuntimeKnowledge(searchAnswerProfileId),
  });
}

/** 用語・同義語と回答ルールの 1 行追加・更新・削除。 */
export function useEditRuntimeKnowledge(searchAnswerProfileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RuntimeKnowledgeEditRequest) =>
      api.editRuntimeKnowledge(searchAnswerProfileId, body),
    onSuccess: (data) => {
      qc.setQueryData(
        ["search-answer-profiles", searchAnswerProfileId, "runtime-knowledge"],
        data,
      );
    },
  });
}

// --- 業務ガイド（#1237） ---

function supportGuideKeys(searchAnswerProfileId: string) {
  const root = ["search-answer-profiles", searchAnswerProfileId, "support-guides"] as const;
  return {
    root,
    list: (includeArchived: boolean) => [...root, "list", includeArchived] as const,
    detail: (guideId: string) => [...root, "detail", guideId] as const,
    revision: (guideId: string, revision: number) =>
      [...root, "revision", guideId, revision] as const,
  };
}

/** 業務ガイドの一覧（アーカイブを含めるかを選べる）。 */
export function useSupportGuides(
  searchAnswerProfileId: string,
  includeArchived: boolean,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    enabled: options.enabled ?? true,
    queryKey: supportGuideKeys(searchAnswerProfileId).list(includeArchived),
    queryFn: () => api.listSupportGuides(searchAnswerProfileId, includeArchived),
  });
}

/** 業務ガイドの下書き・公開の版・履歴。 */
export function useSupportGuide(searchAnswerProfileId: string, guideId: string | null) {
  return useQuery({
    queryKey: supportGuideKeys(searchAnswerProfileId).detail(guideId ?? ""),
    queryFn: () => api.getSupportGuide(searchAnswerProfileId, guideId as string),
    enabled: guideId != null,
  });
}

/** 公開した版の内容（「この版を見る」）。 */
export function useSupportGuideRevision(
  searchAnswerProfileId: string,
  guideId: string | null,
  revision: number | null,
) {
  return useQuery({
    queryKey: supportGuideKeys(searchAnswerProfileId).revision(guideId ?? "", revision ?? 0),
    queryFn: () =>
      api.getSupportGuideRevision(searchAnswerProfileId, guideId as string, revision as number),
    enabled: guideId != null && revision != null,
  });
}

/**
 * 業務ガイドを変える操作（作成・下書きの保存・公開・ロールバック・アーカイブ）。
 * 返った詳細を詳細の cache に入れ、一覧を読み直す。
 */
export function useSupportGuideMutation<TArgs>(
  searchAnswerProfileId: string,
  mutationFn: (args: TArgs) => Promise<SupportGuideDetail>,
) {
  const qc = useQueryClient();
  const keys = supportGuideKeys(searchAnswerProfileId);
  return useMutation({
    mutationFn,
    onSuccess: (data) => {
      qc.setQueryData(keys.detail(data.guide_id), data);
      void qc.invalidateQueries({ queryKey: [...keys.root, "list"] });
    },
  });
}

/** 下書きの保存（guideId が無ければ作成）。 */
export function useSaveSupportGuide(searchAnswerProfileId: string) {
  return useSupportGuideMutation(
    searchAnswerProfileId,
    ({
      guideId,
      draft,
      baseRevision,
    }: {
      guideId: string | null;
      draft: SupportGuideContent;
      baseRevision: number | null;
    }) =>
      guideId && baseRevision != null
        ? api.saveSupportGuideDraft(searchAnswerProfileId, guideId, draft, baseRevision)
        : api.createSupportGuide(searchAnswerProfileId, draft),
  );
}

/** 保存した下書きを公開の前と同じ規則で検証する（参照する資料も確かめる）。 */
export function useValidateSupportGuide(searchAnswerProfileId: string) {
  return useMutation({
    mutationFn: (guideId: string) => api.validateSupportGuide(searchAnswerProfileId, guideId),
  });
}

/** 取り込み（検証を通ったガイドを下書きとして作る）。 */
/**
 * 保存した下書きで試しに答える（#1288）。公開の版・一覧は変わらないので、キャッシュは無効にしない。
 */
export function useTrySupportGuideDraft(searchAnswerProfileId: string) {
  return useMutation({
    mutationFn: (args: {
      guideId: string;
      query: string;
      draftRevision: number;
      conditions: Record<string, string>;
    }) =>
      api.trySupportGuideDraft(searchAnswerProfileId, args.guideId, {
        query: args.query,
        draft_revision: args.draftRevision,
        conditions: args.conditions,
      }),
  });
}

export function useImportSupportGuides(searchAnswerProfileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (guides: unknown[]) => api.importSupportGuides(searchAnswerProfileId, guides),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...supportGuideKeys(searchAnswerProfileId).root, "list"] });
    },
  });
}

/**
 * 指定した trace_id のうち、保存された回答があるもの（チャットの会話の回答用。#304）。
 * 開いている会話の回答だけを引き当てる。
 */
export function useSavedAnswerTraceIds(searchAnswerProfileId: string | null, traceIds: string[]) {
  const ids = traceIds.slice(-ANSWER_TRACE_ID_FILTER_MAX);
  return useQuery({
    queryKey: ["answer-records", searchAnswerProfileId, "trace-ids", ids],
    queryFn: async () => {
      const page = await api.listAnswerRecords({
        searchAnswerProfileId: searchAnswerProfileId as string,
        limit: ids.length,
        traceIds: ids,
      });
      return new Set(page.items.map((answer) => answer.trace_id));
    },
    enabled: Boolean(searchAnswerProfileId) && ids.length > 0,
  });
}

/** 保存された回答 1 件。traceId が null の間は取得しない。 */
/** 実行履歴の一覧（チャット・RAG 検索の実行。#1890）。サーバー側で絞り込み・ページングする。 */
export function useRunHistory({
  period = DEFAULT_RUN_PERIOD,
  ...params
}: Omit<RunHistoryListParams, "since"> & { period?: RunPeriod }) {
  return useQuery({
    queryKey: ["run-history", { ...params, period }],
    // 期間の開始（`since`）は取得のたびにその時刻から求める（key に時刻を入れると描画のたびに取り直す。#1955）。
    queryFn: () => api.listRuns({ ...params, since: runPeriodSince(period) ?? undefined }),
    // 検索語・絞り込み・ページを変えている間は、前の一覧を出したまま取り直す（#535）。
    placeholderData: keepPreviousData,
  });
}

/** 実行履歴の詳細（回答・根拠・資料との照合・処理の経過・フィードバック）。 */
export function useRunHistoryDetail(runId: string | null) {
  return useQuery({
    queryKey: ["run-history", "detail", runId],
    queryFn: () => api.getRun(runId as string),
    enabled: Boolean(runId),
    // 見え方の外・保存期間を過ぎた実行は 404。理由をすぐに出す（再試行しない）。
    retry: retryUnlessNotFound,
  });
}

export function useAnswerRecord(traceId: string | null) {
  return useQuery({
    queryKey: ["answer-record", traceId],
    queryFn: () => api.getAnswerRecord(traceId as string),
    enabled: Boolean(traceId),
  });
}

/** 編集できるプロンプトと、回答フローの各段の読み取り専用プロンプト。 */
export function useAnswerPrompts() {
  return useQuery({ queryKey: ["settings", "answer-prompts"], queryFn: api.getAnswerPrompts });
}

/** 編集できるプロンプトの保存(content あり)と既定値への復帰(content なし)。 */
export function useSaveAnswerPrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, content }: { key: AnswerPromptKey; content: string | null }) =>
      content === null ? api.resetAnswerPrompt(key) : api.saveAnswerPrompt(key, content),
    onSuccess: (data) => qc.setQueryData(["settings", "answer-prompts"], data),
  });
}

/** 回答 feedback を検索・回答プロファイルの Approved FAQ へ登録する(同じ質問は置き換える)。 */
export function usePromoteFeedbackToApprovedFaq() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedbackId: string) => api.promoteFeedbackToApprovedFaq(feedbackId),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["search-answer-profiles", result.search_answer_profile_id, "approved-faq"] });
    },
  });
}

/** 回答 feedback から品質評価のケース（評価リクエストの Excel の 1 行）を作ってダウンロードする（#2065）。 */
export function useFeedbackEvaluationCase() {
  return useMutation({
    mutationFn: (feedbackId: string) => api.downloadFeedbackEvaluationCase(feedbackId),
  });
}

/** 回答の検索と生成の全体既定(#593)。 */
export function useAnsweringSettings() {
  return useQuery({
    queryKey: ["settings", "answering"],
    queryFn: api.getAnsweringSettings,
    retry: false,
  });
}

/** 回答の検索と生成の全体既定を保存する(送った項目だけを変える)。 */
export function useUpdateAnsweringSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: AnsweringSettingsUpdate) => api.updateAnsweringSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(["settings", "answering"], data);
    },
  });
}

/** 回答の記録の保持日数。 */
export function useAnswerRecordSettings() {
  return useQuery({
    queryKey: ["settings", "answer-records"],
    queryFn: api.getAnswerRecordSettings,
  });
}

/** 回答の記録の保持日数を保存する(期限切れは backend が削除する)。 */
export function useUpdateAnswerRecordSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (retentionDays: number) =>
      api.updateAnswerRecordSettings({ retention_days: retentionDays }),
    onSuccess: (data) => {
      qc.setQueryData(["settings", "answer-records"], data);
      qc.invalidateQueries({ queryKey: ["answer-records"] });
    },
  });
}

/** 検索・回答プロファイル作成。 */
export function useCreateSearchAnswerProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SearchAnswerProfileCreateRequest) =>
      api.createSearchAnswerProfile(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["search-answer-profiles"] });
    },
  });
}

/** 検索・回答プロファイル更新。 */
export function useUpdateSearchAnswerProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: SearchAnswerProfileUpdateRequest;
    }) => api.updateSearchAnswerProfile(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["search-answer-profiles"] });
    },
  });
}

/** チャット会話一覧(検索・回答プロファイル scope)。 */
export function useConversations(params: {
  search_answer_profile_id?: string;
  limit?: number;
  offset?: number;
}) {
  return useQuery({
    queryKey: queryKeys.conversations(params),
    queryFn: () => api.listConversations(params),
    enabled: params.search_answer_profile_id != null,
    retry: false,
    // ページを送る間は前のページを出したままにする（同じ検索・回答プロファイルの間だけ。#403）。
    placeholderData: (previous, previousQuery) =>
      (previousQuery?.queryKey[1] as { search_answer_profile_id?: string } | undefined)?.search_answer_profile_id ===
      params.search_answer_profile_id
        ? previous
        : undefined,
  });
}

/** チャット会話詳細(メッセージ列を含む)。 */
/** 作成中（STREAMING）の回答がある会話を取り直す間隔（#1175）。 */
export const CONVERSATION_STREAMING_POLL_MS = 2_000;

/**
 * チャット会話の詳細。`pollStreaming` のときは、作成中（STREAMING）の回答がある間だけ取り直す（再読込の後・
 * 配信を購読し直せないとき、保存済みの作成中の回答が完了に変わるまで。#1175）。
 */
export function useConversation(id: string | null, options: { pollStreaming?: boolean } = {}) {
  const pollStreaming = options.pollStreaming ?? false;
  return useQuery({
    queryKey: queryKeys.conversation(id ?? ""),
    queryFn: () => api.getConversation(id as string),
    enabled: id != null,
    refetchInterval: (query) =>
      pollStreaming && query.state.data?.messages.some((message) => message.status === "STREAMING")
        ? CONVERSATION_STREAMING_POLL_MS
        : false,
  });
}

/** チャット会話作成。 */
export function useCreateConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ConversationCreateBody) =>
      api.createConversation(payload),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/** trace ごとの自分の最新フィードバック。 */
export function useCurrentFeedback(traceId: string | null) {
  return useQuery({
    queryKey: queryKeys.currentFeedback(traceId ?? ""),
    queryFn: () => api.getCurrentFeedback(traceId as string),
    enabled: Boolean(traceId),
  });
}

/** 回答/引用フィードバックを追記する。 */
export function useSubmitFeedback() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: FeedbackRequestBody) => api.submitFeedback(payload),
    onSuccess: (saved) => {
      qc.setQueryData(
        queryKeys.currentFeedback(saved.trace_id),
        (current: unknown) => {
          const items = Array.isArray(current) ? current : [];
          const remaining = items.filter((item) => {
            if (!item || typeof item !== "object") return false;
            const candidate = item as Partial<typeof saved>;
            return !(
              candidate.target_type === saved.target_type &&
              (candidate.document_id ?? null) === (saved.document_id ?? null) &&
              (candidate.chunk_id ?? null) === (saved.chunk_id ?? null)
            );
          });
          return [
            ...remaining,
            { ...saved, created_at: new Date().toISOString() },
          ];
        },
      );
      qc.invalidateQueries({ queryKey: ["feedback"] });
    },
  });
}

/** 管理者向けフィードバック集計。 */
export function useFeedbackDashboard(params: FeedbackListParams) {
  return useQuery({
    queryKey: queryKeys.feedback(params),
    queryFn: () => api.listFeedback(params),
  });
}

/** 管理者向けフィードバック本文・実行情報を、drawer を開いた時だけ取得する。 */
export function useFeedbackDetail(id: string | null) {
  return useQuery({
    queryKey: queryKeys.feedbackDetail(id ?? ""),
    queryFn: () => api.getFeedbackDetail(id as string),
    enabled: id != null,
  });
}

/** チャット会話名更新。 */
export function useUpdateConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: ConversationUpdateBody;
    }) => api.updateConversation(id, payload),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/** チャット会話削除。削除した会話の詳細は再取得（404）させずに捨てる。 */
export function useDeleteConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteConversation(id),
    onSuccess: async (_data, id) => {
      qc.removeQueries({ queryKey: queryKeys.conversation(id), exact: true });
      await qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/**
 * チャットの「回答するモデル」の候補（既定のテキストモデルと Vision モデル。#675）。
 *
 * 候補は今の既定のモデルから作るので、cache を長く使わない（staleTime は既定の 0）。画面を開いたとき・
 * タブに戻ったときに取り直し、別のタブ・別の製品で保存したモデル設定も反映する。この画面のモデル設定の
 * 保存は `ModelSettingsClient` が捨てる（#1416）。
 */
export function useCompareModels(enabled = true) {
  return useQuery({
    queryKey: queryKeys.compareModels,
    queryFn: () => api.listCompareModels(),
    enabled,
  });
}

/** RAG 検索の回答に選べるモデル（既定のテキストモデルと Vision モデル。#675）。cache は `useCompareModels` と同じ（#1416）。 */
export function useProfileAnswerModels() {
  return useQuery({
    queryKey: ["search-answer-profiles", "answer-models"],
    queryFn: () => api.listProfileAnswerModels(),
  });
}

export function useSearchAnswerModels(enabled = true) {
  return useQuery({
    queryKey: queryKeys.searchAnswerModels,
    queryFn: () => api.listSearchAnswerModels(),
    enabled,
  });
}

/** 検索・回答プロファイルをアーカイブする。 */
export function useArchiveSearchAnswerProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.archiveSearchAnswerProfile(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["search-answer-profiles"] });
    },
  });
}

/** KB へ文書を追加する API の 1 回の上限（backend の `AssignDocumentsRequest.document_ids`）。 */
const ASSIGN_DOCUMENTS_BATCH_SIZE = 200;

/**
 * 文書の追加が 2 回目以降の送信で失敗した（それまでの回の文書は追加済み）。
 * `cause` は失敗した送信の例外、`assignedIds` は追加できた文書の ID。
 */
export class AssignDocumentsPartialError extends Error {
  readonly assignedIds: string[];
  readonly total: number;

  constructor(cause: unknown, assignedIds: string[], total: number) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = "AssignDocumentsPartialError";
    this.assignedIds = assignedIds;
    this.total = total;
  }
}

/** 既存文書をナレッジベースへ追加する。 */
export function useAssignDocumentsToKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    // API は 1 回に 200 件まで（`document_ids` の max_length）。それを超える選択は 200 件ずつ順に送る（#600）。
    // 途中の回で失敗したら、それまでに追加できた文書の ID を `AssignDocumentsPartialError` で返す。
    mutationFn: async ({ id, documentIds }: { id: string; documentIds: string[] }) => {
      for (let start = 0; start < documentIds.length; start += ASSIGN_DOCUMENTS_BATCH_SIZE) {
        try {
          await api.assignDocumentsToKnowledgeBase(id, {
            document_ids: documentIds.slice(start, start + ASSIGN_DOCUMENTS_BATCH_SIZE),
          });
        } catch (error) {
          if (start === 0) throw error;
          throw new AssignDocumentsPartialError(
            error,
            documentIds.slice(0, start),
            documentIds.length
          );
        }
      }
    },
    // 途中で失敗しても、それまでの回の分は追加済みなので、成功・失敗のどちらでも一覧を取り直す。
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

/** 文書をナレッジベースから外す。 */
export function useRemoveDocumentFromKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      knowledgeBaseId,
      documentId,
    }: {
      knowledgeBaseId: string;
      documentId: string;
    }) => api.removeDocumentFromKnowledgeBase(knowledgeBaseId, documentId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

/** 文書を取込 job へ投入する。 */
export function useEnqueueDocumentIngestionJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      force,
      phase,
    }: {
      id: string;
      force?: boolean;
      phase?: IngestionJobPhase;
    }) => api.enqueueDocumentIngestionJob(id, force, phase),
    onSuccess: (job) => {
      if (
        (job.phase === "PREPROCESS" || job.phase === "EXTRACT") &&
        job.status === "QUEUED"
      ) {
        clearDocumentProcessingCache(qc, job.document_id, {
          clearPreprocessArtifact: job.phase === "PREPROCESS",
        });
      }
      invalidateDocumentProcessingQueries(qc, job.document_id);
    },
  });
}

/** 品質評価の job の query key（#390）。 */
export const evaluationJobQueryKey = (jobId: string) => ["evaluation", "jobs", jobId] as const;

/** RAG golden set 評価を job として投入する（#390）。 */
export function useSubmitRunEvaluationJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: EvaluationRunRequestBody) => api.submitRunEvaluationJob(payload),
    onSuccess: (job) => qc.setQueryData(evaluationJobQueryKey(job.job_id), job),
  });
}

/** 評価リクエストの Excel を検証してケースにする（#2065。サーバーには保存しない）。 */
export function useImportEvaluationCases() {
  return useMutation({
    mutationFn: ({ file, searchAnswerProfileId }: { file: File; searchAnswerProfileId: string }) =>
      api.importEvaluationCases(file, searchAnswerProfileId),
  });
}

/** 回答の過程から評価の期待の下書きを作る（#2402。1 回 10 行まで。保存しない）。 */
export function useDraftEvaluationProcess() {
  return useMutation({
    mutationFn: (body: { knowledge_base_ids: string[]; rows: EvaluationProcessRow[] }) =>
      api.draftEvaluationProcess(body),
  });
}

/**
 * 実行中の品質評価の job は状態を取得し続け、終わったら止める。
 * 取得の失敗（再起動・502 / 503・通信断）では止めず、最後に分かっている状態が実行中なら取得を続ける
 * （止めると、回復しても画面が「実行中」のまま変わらない。#977）。見つからない job（404）だけは止める。
 */
export function evaluationJobRefetchInterval(
  job: EvaluationJob | undefined,
  error: unknown = null
): number | false {
  if (error instanceof ApiError && error.status === 404) return false;
  return job?.status === "RUNNING" ? EVALUATION_JOB_POLL_INTERVAL_MS : false;
}

/**
 * 品質評価の job の状態（進捗・結果）。画面を離れて戻っても、保存した job id でサーバーの状態を
 * 確かめる（workspace-state.md）。見つからない job（404）は再試行しない。
 */
export function useEvaluationJob(jobId: string | null) {
  return useQuery({
    queryKey: evaluationJobQueryKey(jobId ?? ""),
    queryFn: () => api.getEvaluationJob(jobId ?? ""),
    enabled: Boolean(jobId),
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === 404) && failureCount < 3,
    refetchInterval: (query) =>
      evaluationJobRefetchInterval(query.state.data, query.state.error),
  });
}

/** 品質評価の job を取り消す。 */
export function useCancelEvaluationJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => api.cancelEvaluationJob(jobId),
    onSuccess: (job) => qc.setQueryData(evaluationJobQueryKey(job.job_id), job),
  });
}

/** モデル設定。 */
export function useModelSettings() {
  return useQuery({
    queryKey: queryKeys.modelSettings,
    queryFn: api.getModelSettings,
  });
}

/** HuggingFace モデルダウンロード設定。 */
export function useHuggingFaceSettings() {
  return useQuery({
    queryKey: queryKeys.huggingfaceSettings,
    queryFn: api.getHuggingFaceSettings,
  });
}

/** HuggingFace token / mirror endpoint のランタイム保存。 */
export function useUpdateHuggingFaceSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: HuggingFaceSettingsUpdate) =>
      api.updateHuggingFaceSettings(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.huggingfaceSettings });
    },
  });
}

/** アップロード原本の保存先設定。 */
export function useUploadStorageSettings() {
  return useQuery({
    queryKey: queryKeys.uploadStorageSettings,
    queryFn: api.getUploadStorageSettings,
  });
}

/** 任意 parser adapter の runtime readiness。 */
export function useParserAdapterSettings(enabled = true) {
  return useQuery<ParserAdapterSettingsData>({
    queryKey: queryKeys.parserAdapterSettings,
    queryFn: api.getParserAdapterSettings,
    enabled,
    retry: false,
  });
}

/** 実体・別名索引を構築する全体の既定。 */
export function useEntityIndexSettings() {
  return useQuery({
    queryKey: queryKeys.entityIndexSettings,
    queryFn: api.getEntityIndexSettings,
    retry: false,
  });
}

export function useUpdateEntityIndexSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: EntityIndexSettingsData) => api.updateEntityIndexSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.entityIndexSettings, data);
      void qc.invalidateQueries({ queryKey: queryKeys.pipelineSettings });
    },
  });
}

/** 文書構造索引の構築（構築する / しない）の設定。 */
export function useGraphSettings() {
  return useQuery<GraphSettingsData>({
    queryKey: queryKeys.graphSettings,
    queryFn: api.getGraphSettings,
    retry: false,
  });
}

/** 文書構造索引の構築の設定をランタイム保存。 */
export function useUpdateGraphSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: GraphSettingsUpdate) =>
      api.updateGraphSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.graphSettings, data);
    },
  });
}

/** Evaluation アダプター(評価の基準 = 閾値のプリセット)の runtime 設定。 */
export function useEvaluationSettings() {
  return useQuery<EvaluationSettingsData>({
    queryKey: queryKeys.evaluationSettings,
    queryFn: api.getEvaluationSettings,
    retry: false,
  });
}

/** Evaluation アダプター設定をランタイム保存。 */
export function useUpdateEvaluationSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: EvaluationSettingsUpdate) =>
      api.updateEvaluationSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.evaluationSettings, data);
    },
  });
}

/** Vector Index アダプター(索引/検索精度)の runtime 設定。 */
export function useVectorIndexSettings() {
  return useQuery<VectorIndexSettingsData>({
    queryKey: queryKeys.vectorIndexSettings,
    queryFn: api.getVectorIndexSettings,
    retry: false,
  });
}

/** Vector Index アダプター設定をランタイム保存。 */
export function useUpdateVectorIndexSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: VectorIndexSettingsUpdate) =>
      api.updateVectorIndexSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.vectorIndexSettings, data);
      void qc.invalidateQueries({ queryKey: queryKeys.pipelineSettings });
    },
  });
}

/** メタデータ/項目抽出のスキーマ定義。項目抽出トグルの警告表示などに使う。 */
export function useExtractionFieldsSettings(enabled = true) {
  return useQuery<ExtractionFieldsSettingsData>({
    queryKey: queryKeys.extractionFieldsSettings,
    queryFn: api.getExtractionFieldsSettings,
    enabled,
    retry: false,
  });
}

/** Guardrail アダプター(安全)の runtime 設定。 */
export function useGuardrailSettings() {
  return useQuery<GuardrailSettingsData>({
    queryKey: queryKeys.guardrailSettings,
    queryFn: api.getGuardrailSettings,
    retry: false,
  });
}

/** Guardrail アダプター設定をランタイム保存。 */
export function useUpdateGuardrailSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: GuardrailSettingsUpdate) =>
      api.updateGuardrailSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.guardrailSettings, data);
    },
  });
}

/** Chunking アダプター(分割戦略)の runtime 設定。 */
export function usePreprocessSettings() {
  return useQuery<PreprocessSettingsData>({
    queryKey: queryKeys.preprocessSettings,
    queryFn: api.getPreprocessSettings,
    retry: false,
  });
}

/** マイクロサービス一覧の静的メタデータ。稼働プローブは行わず、画面初期表示を軽くする。 */
export function useServiceCatalog(
  options: { refetchInterval?: number | false } = {},
) {
  return useQuery<ServiceCatalogData>({
    queryKey: queryKeys.serviceCatalog,
    queryFn: api.getServiceCatalog,
    retry: false,
    refetchInterval: options.refetchInterval ?? false,
  });
}

/** マイクロサービスの稼働状態をサービス単位で取得する。 */
export function useServiceStatusQueries(
  serviceIds: string[],
  options: { refetchInterval?: number | false } = {},
): UseQueryResult<ServiceStatusData>[] {
  return useQueries({
    queries: serviceIds.map((serviceId) => ({
      queryKey: queryKeys.serviceStatus(serviceId),
      queryFn: () => api.getServiceStatus(serviceId),
      retry: false,
      refetchInterval: options.refetchInterval ?? 5000,
    })),
  }) as UseQueryResult<ServiceStatusData>[];
}

/** サービスログ末尾。ユーザーがログを開いた時だけ取得する。 */
export function useServiceLogs(serviceId: string | null, lines = 200) {
  return useQuery<ServiceLogsData>({
    queryKey: queryKeys.serviceLogs(serviceId ?? "", lines),
    queryFn: () => api.getServiceLogs(serviceId ?? "", lines),
    enabled: Boolean(serviceId),
    retry: false,
  });
}

/** サービスを起動/停止/再起動する(成功時に一覧を即時 invalidate)。 */
export function useControlService() {
  const qc = useQueryClient();
  return useMutation<
    ServiceControlResultData,
    unknown,
    { serviceId: string; action: ServiceAction }
  >({
    mutationFn: ({ serviceId, action }) =>
      api.controlService(serviceId, action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.services });
    },
  });
}

/** 前処理(Preprocess)アダプター設定をランタイム保存。 */
export function useUpdatePreprocessSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: PreprocessSettingsUpdate) =>
      api.updatePreprocessSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.preprocessSettings, data);
    },
  });
}

export function useChunkingSettings() {
  return useQuery<ChunkingSettingsData>({
    queryKey: queryKeys.chunkingSettings,
    queryFn: api.getChunkingSettings,
    retry: false,
  });
}

/** 言語資源（#2181）。 */
export function useLanguagePack() {
  return useQuery<LanguagePackData>({
    queryKey: queryKeys.languagePack,
    queryFn: api.getLanguagePack,
    retry: false,
  });
}

/** 例の文を、保存した言語資源と編集中の上書きのそれぞれで読む（保存しない）。 */
export function usePreviewLanguagePack() {
  return useMutation({
    mutationFn: ({ text, overrides }: { text: string; overrides: LanguagePackSections }) =>
      api.previewLanguagePack(text, overrides),
  });
}

/** Chunking アダプター設定をランタイム保存。 */
export function useUpdateChunkingSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ChunkingSettingsUpdate) =>
      api.updateChunkingSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.chunkingSettings, data);
    },
  });
}

/** 任意 parser adapter 設定をランタイム保存。 */
export function useUpdateParserAdapterSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ParserAdapterSettingsUpdate) =>
      api.updateParserAdapterSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.parserAdapterSettings, data);
      // 設定の概要の全体の既定（解析エンジン・解析後の処理）も変わる（#528）。
      void qc.invalidateQueries({ queryKey: queryKeys.pipelineSettings });
    },
  });
}

/** 設定の概要: 工程の自動進行と、レシピ 11 項目の全体の既定（#528）。 */
export function usePipelineSettings() {
  return useQuery<PipelineSettingsData>({
    queryKey: queryKeys.pipelineSettings,
    queryFn: api.getPipelineSettings,
    retry: false,
  });
}

/** 工程の自動進行を保存する（#528）。 */
export function useUpdatePipelineSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: PipelineSettingsUpdate) => api.updatePipelineSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.pipelineSettings, data);
    },
  });
}

/** 抽出項目の定義を保存する（文書解析の「解析後の処理」。#528）。 */
export function useUpdateExtractionFieldsSettings() {
  const onSuccess = useExtractionFieldsSettingsSaved();
  return useMutation({
    mutationFn: (fields: ExtractionFieldDefinition[]) =>
      api.updateExtractionFieldsSettings({ fields }),
    onSuccess,
  });
}

/** 保存した全体の既定を消し、標準の項目に戻す（#556）。 */
export function useResetExtractionFieldsSettings() {
  const onSuccess = useExtractionFieldsSettingsSaved();
  return useMutation({ mutationFn: () => api.resetExtractionFieldsSettings(), onSuccess });
}

function useExtractionFieldsSettingsSaved() {
  const qc = useQueryClient();
  return (data: ExtractionFieldsSettingsData) => {
    qc.setQueryData(queryKeys.extractionFieldsSettings, data);
    // 全体の既定は、定義を持たない KB と検索の項目の候補にも効く（#548）。
    void qc.invalidateQueries({
      predicate: (query) =>
        query.queryKey[0] === "knowledge-bases" && query.queryKey[2] === "extraction-fields",
    });
    void qc.invalidateQueries({ queryKey: ["search", "extraction-fields"] });
  };
}
