import type {
  DocumentChunkPreviewRequest,
  DocumentRecipeView,
  FileStatus,
  IngestionJobPhase,
  IngestionJobProgress,
  IngestionJobStatus,
  IngestionParseProgress,
  IngestionParseSegmentProgress,
  IngestionParseSegmentStatus,
  IngestionSegment,
  IngestionVisionProgress,
  IngestionCallProgress,
} from "@/lib/api";
import {
  CHUNK_OVERLAP_MAX_CHARS,
  CHUNK_SIZE_MAX_CHARS,
  CHUNK_SIZE_MIN_CHARS,
  DEFAULT_SMALL_TO_BIG_PARAMS,
  JSONL_RECORD_BOUNDARY,
  chunkSizeLabelKey,
  invalidSmallToBigParam,
  overlapLabelKey,
} from "@/lib/chunking";
import { parseApiDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { parserBackendLabel } from "@/lib/source-profile-labels";

export type ChunkPreviewForm = Required<DocumentChunkPreviewRequest>;

/**
 * URL の `?recipe=` で選んだ処理レシピ。無い・見つからないときは先頭のレシピ。
 * 文書詳細の見出し（PageHeader の状態）と本文（DocumentWorkspace）で同じレシピを指す（#581）。
 */
export function selectDocumentRecipe(
  recipes: readonly DocumentRecipeView[] | undefined,
  requestedRecipeId: string | null
): DocumentRecipeView | null {
  return recipes?.find((recipe) => recipe.recipe_id === requestedRecipeId) ?? recipes?.[0] ?? null;
}

/** 文書の表示上の状態（選んだレシピの状態、無ければ文書の状態）。 */
export function documentDisplayStatus(
  recipe: DocumentRecipeView | null,
  documentStatus: FileStatus | undefined
): FileStatus {
  return recipe?.status ?? documentStatus ?? "UPLOADED";
}

/**
 * 選んだレシピの新しい取込の job が順番待ち・実行中か（#2687）。
 *
 * レシピは worker が job を claim するまで前回の ERROR と本文（error_message・failed_phase）を持ったまま。
 * 見出しはこの間「〈工程〉の順番待ち」（レシピの step の QUEUED）を出すため、本文の失敗の判定も同じ
 * step を見て、この画面が見ている job（最新の job・投入した job）の QUEUED / RUNNING を足す。真の間は
 * 前回の失敗を出さない。新しい job が FAILED になれば偽に戻り、新しい失敗を出す。
 */
export function recipeIngestionActive(
  recipe: DocumentRecipeView | null,
  jobStatuses: readonly (IngestionJobStatus | null | undefined)[]
): boolean {
  const activeStep = recipe?.steps.some(
    (step) => step.status === "QUEUED" || step.status === "RUNNING"
  );
  return (
    Boolean(activeStep) ||
    jobStatuses.some((status) => status === "QUEUED" || status === "RUNNING")
  );
}

export function chunkPreviewForm(recipe: DocumentRecipeView | null): ChunkPreviewForm {
  const config = recipe?.effective_processing_config;
  return {
    // 既定は親子階層（small-to-big）(backend の RAG_CHUNKING_STRATEGY の既定と同じ。#594)。
    chunking_strategy: config?.chunking_strategy ?? "small_to_big",
    chunk_size: config?.chunk_size ?? 800,
    chunk_overlap: config?.chunk_overlap ?? 120,
    chunk_min_chars: config?.chunk_min_chars ?? 120,
    chunk_delimiter: config?.chunk_delimiter ?? JSONL_RECORD_BOUNDARY,
    chunk_child_target_chars:
      config?.chunk_child_target_chars ?? DEFAULT_SMALL_TO_BIG_PARAMS.chunk_child_target_chars,
    chunk_table_child_target_chars:
      config?.chunk_table_child_target_chars ??
      DEFAULT_SMALL_TO_BIG_PARAMS.chunk_table_child_target_chars,
    chunk_parent_target_chars:
      config?.chunk_parent_target_chars ??
      DEFAULT_SMALL_TO_BIG_PARAMS.chunk_parent_target_chars,
    chunk_parent_max_pages:
      config?.chunk_parent_max_pages ?? DEFAULT_SMALL_TO_BIG_PARAMS.chunk_parent_max_pages,
    chunk_parent_max_children:
      config?.chunk_parent_max_children ??
      DEFAULT_SMALL_TO_BIG_PARAMS.chunk_parent_max_children,
  };
}

export function chunkPreviewValidationError(form: ChunkPreviewForm): string | null {
  if (form.chunking_strategy === "fixed_delimiter") {
    return form.chunk_delimiter.trim() ? null : t("settings.chunking.params.delimiter");
  }
  if (form.chunking_strategy === "small_to_big") {
    // 親子階層（small-to-big）は chunk サイズ等を使わない。5 項目の範囲だけを確かめる。
    const invalid = invalidSmallToBigParam(form);
    return invalid
      ? `${t(invalid.labelKey)}: ${invalid.min.toLocaleString("ja-JP")}〜${invalid.max.toLocaleString("ja-JP")}`
      : null;
  }
  if (
    !Number.isFinite(form.chunk_size) ||
    form.chunk_size < CHUNK_SIZE_MIN_CHARS ||
    form.chunk_size > CHUNK_SIZE_MAX_CHARS
  ) {
    return t(chunkSizeLabelKey(form.chunking_strategy));
  }
  if (
    !Number.isFinite(form.chunk_overlap) ||
    form.chunk_overlap < 0 ||
    form.chunk_overlap > CHUNK_OVERLAP_MAX_CHARS
  ) {
    return t(overlapLabelKey(form.chunking_strategy));
  }
  if (form.chunk_overlap >= form.chunk_size) {
    return `${t(overlapLabelKey(form.chunking_strategy))} < ${t(
      chunkSizeLabelKey(form.chunking_strategy)
    )}`;
  }
  if (
    !["fixed_size", "fixed_delimiter"].includes(form.chunking_strategy) &&
    form.chunk_min_chars >= form.chunk_size
  ) {
    return `${t("settings.chunking.params.minChars")} < ${t(
      "settings.chunking.params.chunkSize"
    )}`;
  }
  return null;
}

export type DocumentPrimaryAction =
  | { kind: "enqueue"; phase: "PREPROCESS" }
  | { kind: "approve" }
  | { kind: "retry"; phase: IngestionJobPhase };

export type DocumentActionPlan = {
  primary: DocumentPrimaryAction | null;
  reprocessPhases: IngestionJobPhase[];
};

const NO_DOCUMENT_ACTIONS: DocumentActionPlan = {
  primary: null,
  reprocessPhases: [],
};

const PROCESSING_STATUSES: ReadonlySet<FileStatus> = new Set([
  "PREPROCESSING",
  "INGESTING",
  "CHUNKING",
  "INDEXING",
]);

const PHASE_LABEL_KEYS: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.jobs.phase.preprocess",
  EXTRACT: "flow.jobs.phase.extract",
  CHUNK: "flow.jobs.phase.chunk",
  INDEX: "flow.jobs.phase.index",
};

const PHASE_STARTED_MESSAGE_KEYS: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.phase.started.preprocess",
  EXTRACT: "flow.phase.started.extract",
  CHUNK: "flow.phase.started.chunk",
  INDEX: "flow.phase.started.index",
};

const PHASE_RUNNING_MESSAGE_KEYS: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.phase.running.preprocess",
  EXTRACT: "flow.phase.running.extract",
  CHUNK: "flow.phase.running.chunk",
  INDEX: "flow.phase.running.index",
};

const PHASE_RETRY_LABEL_KEYS: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.retry.preprocess",
  EXTRACT: "flow.retry.extract",
  CHUNK: "flow.retry.chunk",
  INDEX: "flow.retry.index",
};

export function phaseLabelKey(phase: IngestionJobPhase): I18nKey {
  return PHASE_LABEL_KEYS[phase];
}

export function phaseStartedMessageKey(phase: IngestionJobPhase): I18nKey {
  return PHASE_STARTED_MESSAGE_KEYS[phase];
}

export function phaseRunningMessageKey(phase: IngestionJobPhase): I18nKey {
  return PHASE_RUNNING_MESSAGE_KEYS[phase];
}

export function phaseRetryLabelKey(phase: IngestionJobPhase): I18nKey {
  return PHASE_RETRY_LABEL_KEYS[phase];
}

export function phaseForDocumentStatus(status: FileStatus): IngestionJobPhase | null {
  if (status === "PREPROCESSING") return "PREPROCESS";
  if (status === "INGESTING") return "EXTRACT";
  if (status === "CHUNKING") return "CHUNK";
  if (status === "INDEXING") return "INDEX";
  return null;
}

/** 文書状態と保存済み成果物から、画面に出してよい操作だけを解決する。 */
export function resolveDocumentActionPlan({
  status,
  activeJob,
  latestFailedPhase,
  hasPreparedArtifact,
  hasExtraction,
  hasChunkSet,
  hasSelectedRecipe = true,
}: {
  status: FileStatus;
  activeJob: boolean;
  latestFailedPhase?: IngestionJobPhase | null;
  hasPreparedArtifact: boolean;
  hasExtraction: boolean;
  hasChunkSet: boolean;
  hasSelectedRecipe?: boolean;
}): DocumentActionPlan {
  if (!hasSelectedRecipe || activeJob || PROCESSING_STATUSES.has(status)) {
    return NO_DOCUMENT_ACTIONS;
  }

  if (status === "UPLOADED") {
    return {
      primary: { kind: "enqueue", phase: "PREPROCESS" },
      reprocessPhases: [],
    };
  }

  if (status === "PREPROCESSED") {
    return hasPreparedArtifact
      ? {
          primary: { kind: "approve" },
          reprocessPhases: ["PREPROCESS"],
        }
      : {
          primary: { kind: "retry", phase: "PREPROCESS" },
          reprocessPhases: [],
        };
  }

  if (status === "REVIEW") {
    return {
      primary: { kind: "approve" },
      reprocessPhases: ["PREPROCESS", ...(hasPreparedArtifact ? (["EXTRACT"] as const) : [])],
    };
  }

  if (status === "CHUNKED" || status === "INDEXED") {
    return {
      primary: status === "CHUNKED" ? { kind: "approve" } : null,
      reprocessPhases: [
        "PREPROCESS",
        ...(hasPreparedArtifact ? (["EXTRACT"] as const) : []),
        ...(hasExtraction ? (["CHUNK"] as const) : []),
        ...(status === "INDEXED" && hasChunkSet ? (["INDEX"] as const) : []),
      ],
    };
  }

  if (status === "ERROR") {
    const retryPhase = resolveRetryPhase({
      failedPhase: latestFailedPhase,
      hasPreparedArtifact,
      hasExtraction,
      hasChunkSet,
    });
    return {
      primary: {
        kind: "retry",
        phase: retryPhase,
      },
      reprocessPhases: [
        ...(retryPhase !== "PREPROCESS" ? (["PREPROCESS"] as const) : []),
        ...(hasPreparedArtifact && (retryPhase === "CHUNK" || retryPhase === "INDEX")
          ? (["EXTRACT"] as const)
          : []),
        ...(hasExtraction && retryPhase === "INDEX" ? (["CHUNK"] as const) : []),
      ],
    };
  }

  return NO_DOCUMENT_ACTIONS;
}

function resolveRetryPhase({
  failedPhase,
  hasPreparedArtifact,
  hasExtraction,
  hasChunkSet,
}: {
  failedPhase?: IngestionJobPhase | null;
  hasPreparedArtifact: boolean;
  hasExtraction: boolean;
  hasChunkSet: boolean;
}): IngestionJobPhase {
  if (failedPhase === "INDEX" && hasChunkSet) return "INDEX";
  if ((failedPhase === "INDEX" || failedPhase === "CHUNK") && hasExtraction) return "CHUNK";
  if (
    (failedPhase === "INDEX" || failedPhase === "CHUNK" || failedPhase === "EXTRACT") &&
    hasPreparedArtifact
  ) {
    return "EXTRACT";
  }
  return "PREPROCESS";
}

export const INGESTION_PHASE_ORDER: readonly IngestionJobPhase[] = [
  "PREPROCESS",
  "EXTRACT",
  "CHUNK",
  "INDEX",
];

/**
 * 工程順(ファイル準備→抽出→Chunk 作成→Embedding/索引)に、工程状態と最新ジョブを解決する。
 * 状態は steps(レシピ status 由来の単一状態源)を正とする。ジョブの phase は「起点工程」で
 * しかなく、1 本のジョブが後続工程を通し実行するため、job の有無から工程の実行有無は判定
 * できない(messaging-spec §9 P1)。jobs は新しい順で渡す前提(各工程の先頭一致 = 最新)。
 */
export function resolvePhaseRows<
  S extends { phase: IngestionJobPhase },
  T extends { phase: IngestionJobPhase },
>(
  steps: S[],
  jobs: T[]
): Array<{ phase: IngestionJobPhase; step: S | null; job: T | null }> {
  return INGESTION_PHASE_ORDER.map((phase) => ({
    phase,
    step: steps.find((step) => step.phase === phase) ?? null,
    job: jobs.find((job) => job.phase === phase) ?? null,
  }));
}

export type IngestionParserDisplay = {
  backend: string | null;
  profile: string | null;
  source: "segment" | "extraction" | "pending" | "unavailable";
};

export type ProgressUnit = "page" | "slide" | "sheet";

export type IngestionProgressSummary =
  | {
      kind: "determinate";
      unit: ProgressUnit;
      completed: number;
      failed: number;
      total: number;
    }
  | { kind: "indeterminate" };

export function resolveIngestionParserDisplay({
  segments,
  extractionBackend,
  extractionProfile,
  loading,
}: {
  segments: Pick<IngestionSegment, "status" | "parser_backend" | "parser_profile">[];
  extractionBackend?: string | null;
  extractionProfile?: string | null;
  loading?: boolean;
}): IngestionParserDisplay {
  const segment = selectDisplaySegment(segments);
  if (segment) {
    return {
      backend: segment.parser_backend,
      profile: segment.parser_profile,
      source: "segment",
    };
  }
  if (extractionBackend || extractionProfile) {
    return {
      backend: extractionBackend ?? null,
      profile: extractionProfile ?? null,
      source: "extraction",
    };
  }
  return {
    backend: null,
    profile: null,
    source: loading ? "pending" : "unavailable",
  };
}

function selectDisplaySegment(
  segments: Pick<IngestionSegment, "status" | "parser_backend" | "parser_profile">[]
): Pick<IngestionSegment, "status" | "parser_backend" | "parser_profile"> | null {
  return (
    segments.find((segment) => segment.status === "RUNNING") ??
    segments.find((segment) => segment.status === "QUEUED") ??
    segments.find((segment) => segment.status === "FAILED") ??
    segments.find((segment) => segment.status === "SUCCEEDED") ??
    segments[0] ??
    null
  );
}

/** 抽出の中の 1 つの段（解析・図表の説明）の表示（#1999）。 */
export type ExtractStageView = {
  key: "parse" | "vision";
  label: string;
  /** progress bar の値。null は総量が不明（不確定の bar）。 */
  value: number | null;
  max: number | null;
  done: boolean;
};

export type ExtractStageProgressView = {
  stages: ExtractStageView[];
  /** 今の段が何を待っているか（長く進まないときに止まって見えないようにする）。 */
  waiting: string | null;
  /** 解析と並べて図・表を説明している段（流水線。#2002）。 */
  visionWaiting: string | null;
  /** API の制限・一時的なエラーで待っている理由と残りの秒数（#2002）。 */
  apiWaiting: string | null;
  /** 最後に進捗が書かれてからの時間（閾値を超えたときだけ）。 */
  stalled: string | null;
};

/** 進捗が書かれないまま、この秒数を超えたら「最後に進んでから」を出す。 */
export const EXTRACT_STAGE_STALLED_SECONDS = 60;

/**
 * 実行中の job の抽出の段の進捗（`IngestionJob.progress`）を表示用にする（#1999）。
 *
 * PDF を段ごとに解析するとき（#2001）は、段の状態（`parse.segments`）から「抽出: N / M ページ完了」
 * （段が終わるたびに進む）を出し、待っている相手に解析中の段とその中のページを添える。段に分けずに
 * 原本全体を 1 回で解析するときは「ページの解析（Docling）: N / M ページ」。その下に
 * 「図・表の説明（Vision）: N / M 件」を出す。
 */
export function resolveExtractStageProgress(
  progress: IngestionJobProgress | null | undefined,
  nowMs: number
): ExtractStageProgressView | null {
  if (!progress) return null;
  const stages: ExtractStageView[] = [];
  const parse = progress.parse;
  const engine = parserBackendLabel(parse?.engine ?? null);
  const engineLabel = parse?.engine ? engine : t("flow.stages.parse.engineUnknown");
  if (parse) stages.push(parseStageView(parse, engineLabel));
  const vision = progress.vision;
  if (vision) stages.push(visionStageView(vision));
  if (!stages.length) return null;
  return {
    stages,
    waiting: stageWaitingMessage(progress, engineLabel),
    visionWaiting: pipelineVisionMessage(progress),
    apiWaiting: apiWaitingMessage(progress.calls ?? null),
    stalled: stalledMessage(progress.updated_at, nowMs),
  };
}

/** 解析の途中に、解析の済んだ段の図・表を説明しているときの文（#2002）。 */
function pipelineVisionMessage(progress: IngestionJobProgress): string | null {
  const vision = progress.vision;
  if (progress.stage !== "parse" || !vision || vision.finished || !vision.segments_total) {
    return null;
  }
  if (vision.segment_page_start != null && vision.segment_page_end != null) {
    return t("flow.stages.waiting.visionPipeline", {
      range: pageRangeLabel(vision.segment_page_start, vision.segment_page_end),
    });
  }
  return t("flow.stages.waiting.visionPipelineNext");
}

const CALL_STAGE_LABELS: Record<IngestionCallProgress["stage"], I18nKey> = {
  vision: "flow.stages.api.stage.vision",
  heading_levels: "flow.stages.api.stage.headingLevels",
  field_extraction: "flow.stages.api.stage.fieldExtraction",
  navigation_summary: "flow.stages.api.stage.navigationSummary",
  raptor_summary: "flow.stages.api.stage.raptorSummary",
  embedding: "flow.stages.api.stage.embedding",
  rerank: "flow.stages.api.stage.rerank",
};

/**
 * AI の呼び出しが待っている理由（#2002）。待っている間だけ出す（残りの秒数は worker が 10 秒ごとに
 * 書き直す）。流量の制限（429）で同時の呼び出しを減らしているときは、今の数と上限を添える。
 */
export function apiWaitingMessage(calls: IngestionCallProgress | null): string | null {
  if (!calls || calls.finished || !calls.waiting_reason || calls.waiting_seconds == null) {
    return null;
  }
  const stage = t(CALL_STAGE_LABELS[calls.stage] ?? "flow.stages.api.stage.vision");
  const seconds = calls.waiting_seconds;
  switch (calls.waiting_reason) {
    case "rate_limited":
      return calls.concurrency < calls.max_concurrency
        ? t("flow.stages.api.rateLimitedReduced", {
            stage,
            seconds,
            concurrency: calls.concurrency,
            max: calls.max_concurrency,
          })
        : t("flow.stages.api.rateLimited", { stage, seconds });
    case "breaker_open":
      return t("flow.stages.api.breakerOpen", { stage, seconds });
    case "final_retry":
      return t("flow.stages.api.finalRetry", { stage, seconds });
    default:
      return t("flow.stages.api.retry", { stage, seconds });
  }
}

/** 段ごとに解析しているか（段が 2 つ以上。#2001）。 */
export function isSegmentedParse(
  parse: IngestionParseProgress | null | undefined
): parse is IngestionParseProgress & { segments: IngestionParseSegmentProgress[] } {
  return (parse?.segments?.length ?? 0) > 1;
}

function segmentSpanPages(segment: Pick<IngestionParseSegmentProgress, "page_start" | "page_end">) {
  return Math.max(0, segment.page_end - segment.page_start + 1);
}

/**
 * 段ごとの解析の「抽出: N / M ページ完了」（#2001）。N は完了した段のページの合計で、段が
 * 終わるたびに進む。段の帯・段のカードと同じ記録（`parse.segments`）から作る。
 */
function segmentedParseStageView(
  parse: IngestionParseProgress & { segments: IngestionParseSegmentProgress[] }
): ExtractStageView {
  const unit = t("flow.progress.unit.page");
  const total = parse.segments.reduce((sum, segment) => sum + segmentSpanPages(segment), 0);
  const pagesWith = (status: IngestionParseSegmentStatus) =>
    parse.segments
      .filter((segment) => segment.status === status)
      .reduce((sum, segment) => sum + segmentSpanPages(segment), 0);
  const completed = pagesWith("SUCCEEDED");
  const failed = pagesWith("FAILED");
  if (parse.finished) {
    return {
      key: "parse",
      label: t("flow.stages.parse.segmentedDone", {
        total,
        segments: parse.segments.length,
      }),
      value: total,
      max: total,
      done: true,
    };
  }
  return {
    key: "parse",
    label:
      failed > 0
        ? t("flow.progress.failed", { completed, failed, total, unit })
        : t("flow.progress.determinate", { completed, total, unit }),
    value: Math.min(completed + failed, total),
    max: total,
    done: false,
  };
}

function parseStageView(parse: IngestionParseProgress, engine: string): ExtractStageView {
  if (isSegmentedParse(parse)) return segmentedParseStageView(parse);
  const processed = parse.completed + parse.failed;
  if (parse.finished) {
    return {
      key: "parse",
      label:
        parse.total == null
          ? t("flow.stages.parse.doneUnknown", { engine })
          : parse.failed > 0
            ? t("flow.stages.parse.doneFailed", {
                engine,
                total: parse.total,
                failed: parse.failed,
              })
            : t("flow.stages.parse.done", { engine, total: parse.total }),
      value: parse.total ?? 1,
      max: parse.total ?? 1,
      done: true,
    };
  }
  if (parse.total == null || parse.total <= 0) {
    return {
      key: "parse",
      label: t("flow.stages.parse.preparing", { engine }),
      value: null,
      max: null,
      done: false,
    };
  }
  return {
    key: "parse",
    label:
      parse.failed > 0
        ? t("flow.stages.parse.runningFailed", {
            engine,
            completed: parse.completed,
            failed: parse.failed,
            total: parse.total,
          })
        : t("flow.stages.parse.running", {
            engine,
            completed: parse.completed,
            total: parse.total,
          }),
    value: Math.min(processed, parse.total),
    max: parse.total,
    done: false,
  };
}

function visionStageView(vision: IngestionVisionProgress): ExtractStageView {
  const processed = vision.succeeded + vision.failed + vision.skipped;
  const total = Math.max(vision.total_known, processed);
  if (vision.finished) {
    return {
      key: "vision",
      label:
        total === 0
          ? t("flow.stages.vision.none")
          : vision.failed > 0
            ? t("flow.stages.vision.doneFailed", { total, failed: vision.failed })
            : t("flow.stages.vision.done", { total }),
      value: 1,
      max: 1,
      done: true,
    };
  }
  if (total === 0) {
    return {
      key: "vision",
      label: t("flow.stages.vision.preparing"),
      value: null,
      max: null,
      done: false,
    };
  }
  return {
    key: "vision",
    label: vision.segments_total
      ? t("flow.stages.vision.runningSegments", {
          done: processed,
          total,
          completed: vision.segments_completed ?? 0,
          segments: vision.segments_total,
        })
      : vision.total_final
        ? t("flow.stages.vision.running", { done: processed, total })
        : t("flow.stages.vision.runningOpen", { done: processed, total }),
    value: processed,
    max: total,
    done: false,
  };
}

function stageWaitingMessage(progress: IngestionJobProgress, engine: string): string | null {
  const { parse, vision } = progress;
  if (progress.stage === "vision" && vision) {
    if (vision.finished) return t("flow.stages.waiting.afterStages");
    if (vision.current_page != null) {
      return t("flow.stages.waiting.visionItem", {
        page: vision.current_page,
        kind: t(vision.current_kind === "table" ? "flow.stages.kind.table" : "flow.stages.kind.picture"),
      });
    }
    return t("flow.stages.waiting.visionPreparing");
  }
  if (!parse) return null;
  if (parse.finished) return t("flow.stages.waiting.afterParse");
  if (isSegmentedParse(parse)) return segmentWaitingMessage(parse, engine);
  if (parse.total != null && parse.total > 0 && parse.completed + parse.failed >= parse.total) {
    return t("flow.stages.waiting.parseAssembling", { engine });
  }
  return t("flow.stages.waiting.parse", { engine });
}

/** 段ごとの解析で、今どの段を解析しているか（段の中のページの進み具合を添える。#2001）。 */
function segmentWaitingMessage(
  parse: IngestionParseProgress & { segments: IngestionParseSegmentProgress[] },
  engine: string
): string {
  const running = parse.segments.filter((segment) => segment.status === "RUNNING");
  if (!running.length) return t("flow.stages.waiting.segmentNext");
  const pages = running.reduce((sum, segment) => sum + segmentSpanPages(segment), 0);
  const completed = running.reduce(
    (sum, segment) => sum + Math.min(segment.completed, segmentSpanPages(segment)),
    0
  );
  if (running.length === 1) {
    const [segment] = running;
    return t("flow.stages.waiting.segment", {
      engine,
      range: pageRangeLabel(segment.page_start, segment.page_end),
      index: parse.segments.indexOf(segment) + 1,
      count: parse.segments.length,
      pages,
      completed,
    });
  }
  return t("flow.stages.waiting.segments", {
    engine,
    ranges: running.map((segment) => pageRangeLabel(segment.page_start, segment.page_end)).join("・"),
    pages,
    completed,
  });
}

function pageRangeLabel(start: number, end: number): string {
  return start === end
    ? t("flow.segments.pageSingle", { number: start })
    : t("flow.segments.pageRange", { start, end });
}

/** 段の帯の 1 区画（#2001）。 */
export type SegmentStripItem = {
  segmentId: string;
  status: IngestionParseSegmentStatus;
  /** 読み上げ・吹き出しの文（「p.1-10: 完了」）。 */
  label: string;
};

export type SegmentStripView = {
  items: SegmentStripItem[];
  /** 状態ごとの段の数（色だけに頼らない凡例。0 の状態は出さない）。 */
  counts: { status: IngestionParseSegmentStatus; count: number }[];
  /** 帯全体の読み上げ名（「段ごとの解析の状態: 完了 3・処理中 1・待機中 5」）。 */
  summary: string;
};

const SEGMENT_STRIP_STATUS_ORDER: readonly IngestionParseSegmentStatus[] = [
  "SUCCEEDED",
  "RUNNING",
  "QUEUED",
  "FAILED",
  "CANCELLED",
];

/** 段の状態を表示用の状態にする（不明な値は待機中）。 */
export function normalizeSegmentStatus(status: string): IngestionParseSegmentStatus {
  return (SEGMENT_STRIP_STATUS_ORDER as readonly string[]).includes(status)
    ? (status as IngestionParseSegmentStatus)
    : "QUEUED";
}

export function segmentStatusLabelKey(status: string): I18nKey {
  switch (normalizeSegmentStatus(status)) {
    case "RUNNING":
      return "flow.segments.status.running";
    case "SUCCEEDED":
      return "flow.segments.status.succeeded";
    case "FAILED":
      return "flow.segments.status.failed";
    case "CANCELLED":
      return "flow.segments.status.cancelled";
    default:
      return "flow.segments.status.queued";
  }
}

/** 段の帯に並べる段（ページの段）。段の状態の正は job の進捗、無ければ抽出セグメントの行。 */
export type SegmentStateSource = {
  segment_id: string;
  status: string;
  page_start: number | null;
  page_end: number | null;
  progress_unit?: string;
  reused?: boolean;
};

/**
 * 段の帯（段ごとの小さな区画）の表示（#2001）。ページの段が 2 つ以上のときだけ出す
 * （段が 1 つ・PDF でない文書は帯を出さない）。
 */
export function resolveSegmentStrip(
  segments: readonly SegmentStateSource[]
): SegmentStripView | null {
  const pageSegments = segments.filter(
    (segment) =>
      (segment.progress_unit == null || segment.progress_unit === "page") &&
      segment.page_start != null &&
      segment.page_end != null
  );
  if (pageSegments.length < 2) return null;
  const items = [...pageSegments]
    .sort((a, b) => (a.page_start ?? 0) - (b.page_start ?? 0))
    .map((segment) => {
      const status = normalizeSegmentStatus(segment.status);
      const range = pageRangeLabel(segment.page_start ?? 0, segment.page_end ?? 0);
      return {
        segmentId: segment.segment_id,
        status,
        label:
          status === "SUCCEEDED" && segment.reused
            ? t("flow.segments.strip.itemReused", { range })
            : t("flow.segments.strip.item", { range, status: t(segmentStatusLabelKey(status)) }),
      };
    });
  const counts = SEGMENT_STRIP_STATUS_ORDER.map((status) => ({
    status,
    count: items.filter((item) => item.status === status).length,
  })).filter((item) => item.count > 0);
  return {
    items,
    counts,
    summary: t("flow.segments.strip.summary", {
      counts: counts
        .map((item) =>
          t("flow.segments.strip.count", {
            status: t(segmentStatusLabelKey(item.status)),
            count: item.count,
          })
        )
        .join("・"),
    }),
  };
}

/**
 * 段のカード・帯に出す段の状態（#2001）。実行中の job の進捗が段の状態を持つときはそれを正にし、
 * 抽出セグメントの行（polling の間隔がずれる）と食い違わないようにする。
 */
export function segmentsWithProgress(
  segments: readonly IngestionSegment[],
  progress: IngestionJobProgress | null | undefined
): (IngestionSegment & { reused?: boolean; completed_pages?: number })[] {
  const live = progress?.parse?.segments ?? [];
  if (!live.length) return [...segments];
  const byId = new Map(live.map((segment) => [segment.segment_id, segment]));
  const known = new Set(segments.map((segment) => segment.segment_id));
  const merged = segments.map((segment) => {
    const state = byId.get(segment.segment_id);
    return state
      ? {
          ...segment,
          status: state.status,
          reused: state.reused,
          completed_pages: state.completed,
        }
      : segment;
  });
  // 段の行がまだ読めていない(作った直後)ときは、進捗の段で補う。
  const missing = live
    .filter((segment) => !known.has(segment.segment_id))
    .map(
      (segment): IngestionSegment & { reused?: boolean; completed_pages?: number } => ({
        segment_id: segment.segment_id,
        document_id: "",
        recipe_id: null,
        status: segment.status,
        parser_backend: progress?.parse?.engine ?? "",
        parser_profile: progress?.parse?.engine ?? "",
        page_start: segment.page_start,
        page_end: segment.page_end,
        progress_unit: "page",
        progress_start: segment.page_start,
        progress_end: segment.page_end,
        attempt_count: 0,
        artifact_path: null,
        error_code: null,
        error_message: null,
        reused: segment.reused,
        completed_pages: segment.completed,
      })
    );
  return [...merged, ...missing].sort((a, b) => (a.page_start ?? 0) - (b.page_start ?? 0));
}

function stalledMessage(updatedAt: string, nowMs: number): string | null {
  const updated = parseApiDateTime(updatedAt)?.getTime();
  if (updated == null || Number.isNaN(updated)) return null;
  const seconds = Math.floor((nowMs - updated) / 1000);
  if (seconds < EXTRACT_STAGE_STALLED_SECONDS) return null;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  const elapsed = rest
    ? t("flow.jobs.elapsedMinutesSeconds", { minutes, seconds: rest })
    : t("flow.jobs.elapsedMinutes", { minutes });
  return t("flow.stages.stalled", { elapsed });
}

export function resolveIngestionProgressSummary(
  segments: Pick<
    IngestionSegment,
    "status" | "progress_unit" | "progress_start" | "progress_end"
  >[]
): IngestionProgressSummary | null {
  if (!segments.length) return null;
  const unit = segments.find(isDeterminateSegment)?.progress_unit as ProgressUnit | undefined;
  if (!unit) return { kind: "indeterminate" };
  const scoped = segments.filter(
    (segment) => segment.progress_unit === unit && isDeterminateSegment(segment)
  );
  const total = scoped.reduce((sum, segment) => sum + segmentSpan(segment), 0);
  if (total <= 0) return { kind: "indeterminate" };
  return {
    kind: "determinate",
    unit,
    completed: scoped
      .filter((segment) => segment.status === "SUCCEEDED")
      .reduce((sum, segment) => sum + segmentSpan(segment), 0),
    failed: scoped
      .filter((segment) => segment.status === "FAILED")
      .reduce((sum, segment) => sum + segmentSpan(segment), 0),
    total,
  };
}

function isDeterminateSegment(
  segment: Pick<IngestionSegment, "progress_unit" | "progress_start" | "progress_end">
): boolean {
  return (
    (segment.progress_unit === "page" ||
      segment.progress_unit === "slide" ||
      segment.progress_unit === "sheet") &&
    segment.progress_start != null &&
    segment.progress_end != null
  );
}

function segmentSpan(
  segment: Pick<IngestionSegment, "progress_start" | "progress_end">
): number {
  if (segment.progress_start == null || segment.progress_end == null) return 0;
  return Math.max(0, segment.progress_end - segment.progress_start + 1);
}

/**
 * 「このドキュメントは現在取込中です。」(409 競合)は取込が進行中の間だけ意味を持つ。
 * 取込完了後も react-query の mutation エラーが残り banner が消えないため、
 * 409 の後に進行中ジョブを一度観測し、それが無くなったら stale とみなして消す。
 *
 * 進行中ジョブをまだ観測していない 409（別タブで投入した job がまだ一覧に無い、
 * 「レシピ設定が更新されました」など）は、表示した瞬間に消さない（#281）。
 */
export function ingestConflictBannerIsStale({
  errorStatus,
  hasActiveJob,
  observedActiveJob,
}: {
  errorStatus: number | null | undefined;
  hasActiveJob: boolean;
  observedActiveJob: boolean;
}): boolean {
  return errorStatus === 409 && observedActiveJob && !hasActiveJob;
}

/**
 * 投入直後の監視（localWatchProcessing）をやめるか。
 *
 * 状態が確認待ち・完了・失敗になったときに加え、投入した job がキャンセル・スキップで終わり
 * 文書が UPLOADED のまま残ったときもやめる。以前はこの場合に監視が残り、
 * 画面を開いている間ずっとポーリングが続いた（#281）。
 */
export function shouldStopLocalProcessingWatch({
  activeSubmittedJob,
  documentStatus,
  submittedJobStatus,
}: {
  activeSubmittedJob: boolean;
  documentStatus: FileStatus;
  submittedJobStatus: IngestionJobStatus | null | undefined;
}): boolean {
  if (activeSubmittedJob) return false;
  if (
    documentStatus === "INDEXED" ||
    documentStatus === "ERROR" ||
    documentStatus === "PREPROCESSED" ||
    documentStatus === "REVIEW"
  ) {
    return true;
  }
  // SUCCEEDED / FAILED のときは文書の状態が次の取得で進む（REVIEW・ERROR など）ので、ここでは
  // やめない（やめると失敗の表示を取り逃す）。状態が UPLOADED のまま残るのはキャンセル・スキップだけ。
  return (
    documentStatus === "UPLOADED" &&
    (submittedJobStatus === "CANCELLED" || submittedJobStatus === "SKIPPED")
  );
}

/** 状態メッセージ単一スロット(messaging-spec §9)の表示内容。優先順: 失敗原因 > 実行中 > 承認待ちゲート案内。 */
export type StatusMessageSlot =
  | { kind: "failure" }
  | { kind: "processing" }
  | { kind: "gate"; status: "PREPROCESSED" | "REVIEW" | "CHUNKED"; artifactMissing: boolean }
  | null;

export function resolveStatusMessageSlot({
  errored,
  processingVisible,
  documentStatus,
  preparedArtifactMissing,
}: {
  errored: boolean;
  processingVisible: boolean;
  documentStatus: string;
  preparedArtifactMissing: boolean;
}): StatusMessageSlot {
  if (errored) return { kind: "failure" };
  if (processingVisible) return { kind: "processing" };
  if (
    documentStatus === "PREPROCESSED" ||
    documentStatus === "REVIEW" ||
    documentStatus === "CHUNKED"
  ) {
    return {
      kind: "gate",
      status: documentStatus,
      artifactMissing: documentStatus === "PREPROCESSED" && preparedArtifactMissing,
    };
  }
  return null;
}

/** 索引完了 toast は「同一レシピで INDEXING → INDEXED へ遷移した」時だけ 1 回出す(messaging-spec §3.4)。 */
export function isIndexedTransition(
  prev: { recipeId: string | null; status: string } | null,
  next: { recipeId: string | null; status: string }
): boolean {
  return (
    prev != null &&
    prev.recipeId === next.recipeId &&
    prev.status === "INDEXING" &&
    next.status === "INDEXED"
  );
}

export function shouldShowProcessingWatchBanner({
  watchProcessing,
  documentStatus,
  latestJobStatus,
}: {
  watchProcessing: boolean;
  documentStatus: string | null | undefined;
  latestJobStatus: IngestionJobStatus | null | undefined;
}): boolean {
  return (
    watchProcessing &&
    latestJobStatus !== "FAILED" &&
    !["REVIEW", "CHUNKED", "INDEXED", "ERROR"].includes(documentStatus ?? "")
  );
}

/** 有効期間の終了日は開始日より後（終了日は排他的。backend の DocumentClassification と同じ規則）。 */
export function classificationPeriodIsValid(
  classification: { effective_from: string | null; effective_to: string | null }
): boolean {
  const { effective_from: from, effective_to: to } = classification;
  // date の入力は YYYY-MM-DD なので文字列で比べられる。
  return !from || !to || from < to;
}
