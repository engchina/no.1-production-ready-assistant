import type { FileStatus, IngestionJobPhase, IngestionSegment } from "@/lib/api";

type IngestionErrorDisplayPlan = {
  jobMessage: string | null;
  segmentIds: Set<string>;
  documentMessage: string | null;
  queuedJobMessage: string | null;
  retriedSegmentJobMessage: string | null;
};

/** 工程(レシピの処理段階)のうち、取込ジョブの phase が対応しうるもの。 */
export type FailedFlowStep = "PREPROCESSING" | "INGESTING" | "CHUNKING" | "INDEXING";

const PHASE_TO_STEP: Record<IngestionJobPhase, FailedFlowStep> = {
  PREPROCESS: "PREPROCESSING",
  EXTRACT: "INGESTING",
  CHUNK: "CHUNKING",
  INDEX: "INDEXING",
};

export type DocumentFailureView = {
  errored: boolean;
  /** どの工程で失敗したか（レシピの FAILED の step、無ければ最新 FAILED ジョブの phase。不明なら null）。 */
  failedStep: FailedFlowStep | null;
  /** 「原因 + 対処」を 1 本化した本文（最具体レイヤ採用。無ければ null）。 */
  primaryMessage: string | null;
};

/**
 * 文書の失敗を 1 本化する（messaging-spec §9 P2/P5）。
 * 原因は最も具体的なレイヤから 1 つだけ採る: 最新ジョブ → 失敗セグメント → 文書。
 * これを上部の要約バナーへ昇格し、詳細パネルでは suppress して二重表示を防ぐ。
 *
 * 再処理の job が順番待ち・実行中（`ingestionActive`）の間は、レシピが claim まで ERROR と前回の本文を
 * 持ったままでも失敗にしない（見出しの「〈工程〉の順番待ち」と同じ規則。#2687）。
 */
export function resolveDocumentFailureView({
  documentStatus,
  ingestionActive = false,
  latestJobStatus,
  latestJobPhase,
  failedRecipePhase,
  latestJobErrorMessage,
  segments,
  documentErrorMessage,
}: {
  documentStatus?: FileStatus | null;
  /**
   * このレシピの新しい job が順番待ち・実行中か（レシピの step の QUEUED、または最新・投入した job の
   * QUEUED / RUNNING）。真なら前回の失敗は過去のもので、出さない。
   */
  ingestionActive?: boolean;
  latestJobStatus?: string | null;
  latestJobPhase?: IngestionJobPhase | null;
  /**
   * レシピの step のうち FAILED の工程。レシピの CHUNK ジョブは索引まで行うため、ジョブの phase より
   * 実際に失敗した工程を正確に示す。ステップ表示と同じ工程を失敗として出すため優先する（#329）。
   */
  failedRecipePhase?: IngestionJobPhase | null;
  latestJobErrorMessage?: string | null;
  segments: Pick<IngestionSegment, "status" | "error_message">[];
  documentErrorMessage?: string | null;
}): DocumentFailureView {
  if (documentStatus !== "ERROR" || ingestionActive) {
    return { errored: false, failedStep: null, primaryMessage: null };
  }
  const failedSegment = segments.find(
    (segment) =>
      segment.status === "FAILED" && normalizeIngestionErrorMessage(segment.error_message)
  );
  const primaryMessage =
    normalizeIngestionErrorMessage(latestJobErrorMessage) ??
    normalizeIngestionErrorMessage(failedSegment?.error_message) ??
    normalizeIngestionErrorMessage(documentErrorMessage);
  const failedStep = failedRecipePhase
    ? PHASE_TO_STEP[failedRecipePhase]
    : latestJobStatus === "FAILED" && latestJobPhase
      ? PHASE_TO_STEP[latestJobPhase]
      : null;
  return { errored: true, failedStep, primaryMessage };
}

export function resolveIngestionErrorDisplayPlan({
  latestJobErrorMessage,
  segments,
  documentErrorMessage,
  queuedJobErrorMessage,
  retriedSegmentJobErrorMessage,
  suppressMessages = [],
}: {
  latestJobErrorMessage?: string | null;
  segments: Pick<IngestionSegment, "segment_id" | "status" | "error_message">[];
  documentErrorMessage?: string | null;
  queuedJobErrorMessage?: string | null;
  retriedSegmentJobErrorMessage?: string | null;
  /** 既に上位バナーで表示済みの文字列。ここに seed して詳細側の再掲を抑止する（§9 P2）。 */
  suppressMessages?: readonly (string | null | undefined)[];
}): IngestionErrorDisplayPlan {
  const seen = new Set<string>();
  for (const message of suppressMessages) {
    const normalized = normalizeIngestionErrorMessage(message);
    if (normalized) seen.add(normalized);
  }
  const take = (message?: string | null): string | null => {
    const normalized = normalizeIngestionErrorMessage(message);
    if (!normalized || seen.has(normalized)) return null;
    seen.add(normalized);
    return normalized;
  };

  const jobMessage = take(latestJobErrorMessage);
  const segmentIds = new Set<string>();
  for (const segment of segments) {
    if (segment.status === "FAILED" && take(segment.error_message)) {
      segmentIds.add(segment.segment_id);
    }
  }

  return {
    jobMessage,
    segmentIds,
    documentMessage: take(documentErrorMessage),
    queuedJobMessage: take(queuedJobErrorMessage),
    retriedSegmentJobMessage: take(retriedSegmentJobErrorMessage),
  };
}

export function normalizeIngestionErrorMessage(message?: string | null): string | null {
  const normalized = message?.trim();
  return normalized ? normalized : null;
}

/**
 * backend が利用者向けの本文の末尾に付けた「エラーコード: <code>」を本文から分ける（messaging.md §10。#705）。
 * 本文は原因と対処だけにし、エラーコードは「詳細」に出す。
 */
export function splitErrorCode(message: string): { message: string; code: string | null } {
  const match = /\s*エラーコード:\s*([\w.-]+)\s*$/.exec(message);
  if (!match) return { message, code: null };
  return { message: message.slice(0, match.index), code: match[1] };
}
