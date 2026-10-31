import { DEFAULT_PAGE_SIZE } from "@production-ready/ui";

import type {
  BulkIngestionOutcome,
  BulkIngestionReasonCount,
  BulkIngestionResult,
  FileStatus,
  IngestionJob,
  IngestionJobPhase,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { queueWaitLabel } from "@/lib/ingestion-queue";
import { isPageSize } from "@/lib/list-paging";

/**
 * 文書インデックス（FileListClient）の純粋ロジック。Vitest で境界を検証する（#281）。
 */

/** 取込の job がレシピ1で順番待ちの段の絞り込みの値（行に「〈工程〉の順番待ち」と出る。#2677）。 */
export type FileListQueuedFilter = `QUEUED_${IngestionJobPhase}`;

/** 状態の絞り込みの値。行に出る状態の 1 つ（文書の状態か、順番待ちの段）か「すべて」。 */
export type FileListFilter = "ALL" | FileStatus | FileListQueuedFilter;

/**
 * 状態の絞り込みの選択肢（#2677）。行に出る状態をすべて、処理の順に並べる。「すべて」は先頭、エラーは最後。
 * 各段は 投入前・確認待ち → 〈工程〉の順番待ち（worker の空きを待つ）→ 〈工程〉中 の順に進む。
 */
export const FILE_LIST_FILTERS: readonly FileListFilter[] = [
  "ALL",
  "UPLOADED",
  "QUEUED_PREPROCESS",
  "PREPROCESSING",
  "PREPROCESSED",
  "QUEUED_EXTRACT",
  "INGESTING",
  "REVIEW",
  "QUEUED_CHUNK",
  "CHUNKING",
  "CHUNKED",
  "QUEUED_INDEX",
  "INDEXING",
  "INDEXED",
  "ERROR",
];

const QUEUED_FILTER_PREFIX = "QUEUED_";

function queuedFilterPhase(filter: FileListFilter): IngestionJobPhase | null {
  return filter.startsWith(QUEUED_FILTER_PREFIX)
    ? (filter.slice(QUEUED_FILTER_PREFIX.length) as IngestionJobPhase)
    : null;
}

/**
 * 絞り込みの値 → 一覧の API の条件。`status` は順番待ちでない文書の状態、`queued_phase` は順番待ちの段
 * （backend が SQL で絞り、ページングと件数がそろう）。
 */
export function fileListFilterParams(filter: FileListFilter): {
  status?: FileStatus;
  queued_phase?: IngestionJobPhase;
} {
  if (filter === "ALL") return {};
  const phase = queuedFilterPhase(filter);
  return phase ? { queued_phase: phase } : { status: filter as FileStatus };
}

/** 選択肢の文言。行の状態の表示（`StatusBadge`）と同じ文言にする。 */
export function fileListFilterLabel(filter: FileListFilter): string {
  if (filter === "ALL") return t("fileList.filterAll");
  const phase = queuedFilterPhase(filter);
  return phase ? queueWaitLabel(phase) : t(`status.${filter as FileStatus}`);
}

/** 一覧の検索語の上限。backend の `q`（max_length=200）と同じ値にする。 */
export const FILE_LIST_QUERY_MAX_LENGTH = 200;

export interface FileListView {
  filter: FileListFilter;
  languagePackRelation?: import("@/lib/api").LanguagePackRelation;
  languagePackReleaseId?: string;
  q: string;
  knowledgeBaseId: string;
  offset: number;
  /** 1 ページの件数（表示件数。10 / 25 / 50 / 100。#1891）。 */
  limit: number;
}

export const INITIAL_FILE_LIST_VIEW: FileListView = {
  filter: "ALL",
  q: "",
  knowledgeBaseId: "ALL",
  offset: 0,
  limit: DEFAULT_PAGE_SIZE,
};

export function isFileListView(value: unknown): value is FileListView {
  const view = value as FileListView;
  return (
    typeof view === "object" &&
    view !== null &&
    (FILE_LIST_FILTERS as unknown[]).includes(view.filter) &&
    (view.languagePackRelation === undefined || ["current", "equivalent", "different", "unrecorded"].includes(view.languagePackRelation)) &&
    (view.languagePackReleaseId === undefined || typeof view.languagePackReleaseId === "string") &&
    typeof view.q === "string" &&
    view.q.length <= FILE_LIST_QUERY_MAX_LENGTH &&
    typeof view.knowledgeBaseId === "string" &&
    Number.isInteger(view.offset) &&
    view.offset >= 0 &&
    isPageSize(view.limit)
  );
}

export type EnqueueOutcome =
  | { kind: "queued"; job: IngestionJob }
  | { kind: "skipped"; job: IngestionJob }
  | { kind: "failed"; message: string }
  | { kind: "unconfirmed"; message: string };

/** 取込 job の投入結果を分類する。SKIPPED は「何も起きない」ように見えるため、理由を知らせる。 */
export function classifyEnqueuedJob(job: IngestionJob): EnqueueOutcome {
  return job.status === "SKIPPED" ? { kind: "skipped", job } : { kind: "queued", job };
}

/** 一括投入の結果の面（Banner）の重さ。受付不可があれば warning、1 件も受け付けなければ danger。 */
export type BulkIngestSeverity = "success" | "info" | "warning" | "danger";

/**
 * 一括投入の結果（#2674）の見せ方。すべて受け付けたら Toast だけ、それ以外は一括操作のバーの直下に
 * 件数と理由を残す（自動で消さない。messaging.md §10.4）。
 */
export function bulkIngestSeverity(result: BulkIngestionResult): BulkIngestSeverity {
  if (result.accepted_count === result.unique_count) return "success";
  if (result.rejected_count > 0) return result.accepted_count > 0 ? "warning" : "danger";
  return "info";
}

/** 一括投入の後も選択に残す文書（受付不可だけ。失敗分まで選択を解除しない）。 */
export function idsToKeepSelected(result: BulkIngestionResult): string[] {
  return result.items
    .filter((item) => item.outcome === "rejected")
    .map((item) => item.document_id);
}

/** 内訳の並び（受付不可 → 処理中 → スキップ）。同じ結果の中は件数の多い順（backend の並び）。 */
const OUTCOME_ORDER: readonly BulkIngestionOutcome[] = ["rejected", "already_active", "skipped"];

export function orderedBulkReasons(
  reasons: readonly BulkIngestionReasonCount[]
): BulkIngestionReasonCount[] {
  return OUTCOME_ORDER.flatMap((outcome) => reasons.filter((entry) => entry.outcome === outcome));
}

export type DeleteOutcome =
  | { kind: "deleted"; warnings: readonly string[] }
  | { kind: "failed"; message: string }
  | { kind: "unconfirmed"; message: string };

export interface DeleteSummary {
  deleted: number;
  /** 削除できたが、保存先のファイルの後始末の警告があった件数（#699）。 */
  warned: number;
  failed: number;
  firstWarning: string | null;
  firstError: string | null;
  unconfirmed: number;
  firstUnconfirmed: string | null;
}

/** 一括削除の結果をまとめる。後始末の警告を、1 件の削除と同じく成功として黙らせない（#281 / #699）。 */
export function summarizeDeleteOutcomes(outcomes: readonly DeleteOutcome[]): DeleteSummary {
  const summary: DeleteSummary = {
    deleted: 0,
    warned: 0,
    failed: 0,
    firstWarning: null,
    firstError: null,
    unconfirmed: 0,
    firstUnconfirmed: null,
  };
  for (const outcome of outcomes) {
    if (outcome.kind === "unconfirmed") {
      summary.unconfirmed++;
      summary.firstUnconfirmed ??= outcome.message;
      continue;
    }
    if (outcome.kind === "failed") {
      summary.failed += 1;
      summary.firstError = summary.firstError ?? outcome.message;
      continue;
    }
    summary.deleted += 1;
    if (outcome.warnings.length > 0) {
      summary.warned += 1;
      summary.firstWarning = summary.firstWarning ?? outcome.warnings.join(" ");
    }
  }
  return summary;
}
