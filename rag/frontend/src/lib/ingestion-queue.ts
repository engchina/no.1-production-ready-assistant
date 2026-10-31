import { elapsedMsBetween, formatElapsedClock } from "@production-ready/ui";

import type { DocumentRecipeView, IngestionJobPhase, IngestionQueueWait } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

/**
 * 取込の job の順番待ち（#2026）。job が QUEUED で worker の空きを待っている間は、人の確認待ちでなく
 * 「〈工程〉の順番待ち」と、前にある job の数・待っている理由（同時に処理できる数）・経過を出す。
 */

const PHASE_STEP_LABEL: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.step.preprocess",
  EXTRACT: "flow.step.extract",
  CHUNK: "flow.step.chunk",
  INDEX: "flow.step.indexing",
};

/** 順番待ちの工程（詳細の工程・一覧の情報に共通）。 */
export interface QueueWaitView {
  phase: IngestionJobPhase;
  jobsAhead: number | null;
  concurrency: number | null;
  queuedAt: string | null;
}

/** 「〈工程〉の順番待ち」。 */
export function queueWaitLabel(phase: IngestionJobPhase): string {
  return t("ingestionQueue.status", { step: t(PHASE_STEP_LABEL[phase]) });
}

export function queueWaitView(wait: IngestionQueueWait | null | undefined): QueueWaitView | null {
  if (!wait) return null;
  return {
    phase: wait.phase,
    jobsAhead: wait.jobs_ahead ?? null,
    concurrency: wait.concurrency ?? null,
    queuedAt: wait.queued_at ?? null,
  };
}

/** レシピの順番待ちの工程（QUEUED の工程）。無ければ null。 */
export function recipeQueueWait(recipe: DocumentRecipeView | null | undefined): QueueWaitView | null {
  const step = recipe?.steps.find((item) => item.status === "QUEUED");
  if (!step) return null;
  return (
    queueWaitView(step.queue_wait) ?? {
      phase: step.phase,
      jobsAhead: null,
      concurrency: null,
      queuedAt: null,
    }
  );
}

/** 前の件数（0 件なら「次に処理します」）。分からなければ null。 */
export function queueAheadText(wait: QueueWaitView): string | null {
  if (wait.jobsAhead === null) return null;
  return wait.jobsAhead === 0
    ? t("ingestionQueue.next")
    : t("ingestionQueue.ahead", { count: wait.jobsAhead });
}

/** 待っている理由（同時に処理できる数の空き）。 */
export function queueReasonText(wait: QueueWaitView): string | null {
  if (wait.concurrency === null) return null;
  return t("ingestionQueue.reason", { concurrency: wait.concurrency });
}

/** 投入からの経過（mm:ss）。投入の時刻が無ければ null。 */
export function queueElapsedText(wait: QueueWaitView, nowMs: number): string | null {
  const elapsed = elapsedMsBetween(wait.queuedAt, null, nowMs);
  if (elapsed === null) return null;
  return t("ingestionQueue.elapsed", { elapsed: formatElapsedClock(elapsed) });
}
