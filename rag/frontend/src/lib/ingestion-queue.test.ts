import { describe, expect, it } from "vitest";

import type { DocumentRecipeStep, DocumentRecipeView } from "./api";
import {
  queueAheadText,
  queueElapsedText,
  queueReasonText,
  queueWaitLabel,
  queueWaitView,
  recipeQueueWait,
} from "./ingestion-queue";

function step(
  phase: DocumentRecipeStep["phase"],
  status: DocumentRecipeStep["status"]
): DocumentRecipeStep {
  return { phase, status, started_at: null, finished_at: null, error_message: null };
}

function recipe(steps: DocumentRecipeStep[]): DocumentRecipeView {
  return { steps } as DocumentRecipeView;
}

describe("queueWaitLabel", () => {
  it("工程の名前で「〈工程〉の順番待ち」", () => {
    expect(queueWaitLabel("CHUNK")).toBe("Chunk 作成の順番待ち");
    expect(queueWaitLabel("PREPROCESS")).toBe("ファイル準備の順番待ち");
    expect(queueWaitLabel("INDEX")).toBe("Embedding / 索引の順番待ち");
  });
});

describe("recipeQueueWait", () => {
  it("QUEUED の工程の順番待ちの情報を返す", () => {
    const queued: DocumentRecipeStep = {
      ...step("CHUNK", "QUEUED"),
      queue_wait: {
        phase: "CHUNK",
        job_id: "job-1",
        queued_at: "2026-10-15T00:00:00Z",
        jobs_ahead: 3,
        concurrency: 4,
      },
    };
    expect(
      recipeQueueWait(
        recipe([step("PREPROCESS", "SUCCEEDED"), step("EXTRACT", "SUCCEEDED"), queued])
      )
    ).toEqual({ phase: "CHUNK", jobsAhead: 3, concurrency: 4, queuedAt: "2026-10-15T00:00:00Z" });
  });

  it("確認待ち（QUEUED の工程が無い）は null", () => {
    expect(
      recipeQueueWait(recipe([step("PREPROCESS", "SUCCEEDED"), step("EXTRACT", "NEEDS_REVIEW")]))
    ).toBeNull();
    expect(recipeQueueWait(null)).toBeNull();
  });

  it("情報の無い QUEUED の工程は工程だけ返す", () => {
    expect(recipeQueueWait(recipe([step("EXTRACT", "QUEUED")]))).toEqual({
      phase: "EXTRACT",
      jobsAhead: null,
      concurrency: null,
      queuedAt: null,
    });
  });
});

describe("順番待ちの文言", () => {
  const wait = queueWaitView({
    phase: "CHUNK",
    job_id: "job-1",
    queued_at: "2026-10-15T00:00:00Z",
    jobs_ahead: 3,
    concurrency: 4,
  })!;

  it("前の件数・理由・経過", () => {
    expect(queueAheadText(wait)).toBe("前に 3 件");
    expect(queueReasonText(wait)).toBe("同時に処理できる数（4）の空きを待っています");
    expect(queueElapsedText(wait, Date.parse("2026-10-15T00:01:30Z"))).toBe("経過 01:30");
  });

  it("前が 0 件なら次に処理します", () => {
    expect(queueAheadText({ ...wait, jobsAhead: 0 })).toBe("次に処理します");
  });

  it("分からない値は出さない（自動進行の投入までの猶予の間）", () => {
    const handoff = { ...wait, jobsAhead: null, queuedAt: null };
    expect(queueAheadText(handoff)).toBeNull();
    expect(queueElapsedText(handoff, Date.now())).toBeNull();
    expect(queueReasonText({ ...wait, concurrency: null })).toBeNull();
  });
});
