import { describe, expect, it } from "vitest";

import { apiWaitingMessage, resolveExtractStageProgress } from "./DocumentWorkspace.logic";
import type {
  IngestionCallProgress,
  IngestionJobProgress,
  IngestionParseSegmentProgress,
  IngestionVisionProgress,
} from "@/lib/api";

// #2002: 段の解析と並べて図・表を説明する（流水線）ときの進捗と、API の待ちの理由。
const updatedAt = "2026-10-15T08:37:27Z";
const now = Date.parse(updatedAt) + 5_000;

function segments(done: number, total: number): IngestionParseSegmentProgress[] {
  return Array.from({ length: total }, (_, index) => ({
    segment_id: `doc-1:p${index * 10 + 1}-${index * 10 + 10}`,
    page_start: index * 10 + 1,
    page_end: index * 10 + 10,
    status: index < done ? "SUCCEEDED" : index === done ? "RUNNING" : "QUEUED",
    completed: index === done ? 4 : 0,
    reused: false,
  }));
}

function vision(overrides: Partial<IngestionVisionProgress> = {}): IngestionVisionProgress {
  return {
    phase: "segments",
    succeeded: 12,
    failed: 0,
    skipped: 0,
    running: 4,
    pending: 4,
    total_known: 20,
    total_final: false,
    current_page: 14,
    current_kind: "picture",
    finished: false,
    segment_page_start: 11,
    segment_page_end: 20,
    segments_completed: 1,
    segments_total: 9,
    ...overrides,
  };
}

function calls(overrides: Partial<IngestionCallProgress> = {}): IngestionCallProgress {
  return {
    stage: "vision",
    succeeded: 12,
    failed: 0,
    retries: 1,
    total: null,
    concurrency: 4,
    max_concurrency: 4,
    in_flight: 4,
    waiting_reason: null,
    waiting_seconds: null,
    finished: false,
    ...overrides,
  };
}

function pipelineProgress(overrides: Partial<IngestionJobProgress> = {}): IngestionJobProgress {
  return {
    stage: "parse",
    parse: {
      engine: "docling",
      unit: "page",
      completed: 4,
      failed: 0,
      total: 10,
      finished: false,
      segments: segments(2, 9),
    },
    vision: vision(),
    calls: calls(),
    updated_at: updatedAt,
    ...overrides,
  };
}

describe("段の解析と並べた図・表の説明", () => {
  it("解析の途中に、説明の済んだ段の数と、並べて説明している段を出す", () => {
    const view = resolveExtractStageProgress(pipelineProgress(), now);
    expect(view?.stages.map((stage) => stage.key)).toEqual(["parse", "vision"]);
    expect(view?.stages[1].label).toBe(
      "図・表の説明（Vision）: 12 / 20 件（説明の済んだ段 1 / 9）"
    );
    expect(view?.stages[1].value).toBe(12);
    expect(view?.waiting).toContain("Docling が p.21-30（段 3 / 9）を解析しています");
    expect(view?.visionWaiting).toBe(
      "解析の済んだ p.11-20 の図・表を、次の段の解析と並べて説明しています。"
    );
    expect(view?.apiWaiting).toBeNull();
  });

  it("解析が終わって Vision の段になったら、並べている文は出さない", () => {
    const view = resolveExtractStageProgress(
      pipelineProgress({ stage: "vision", vision: vision({ segments_total: 0, total_final: true }) }),
      now
    );
    expect(view?.visionWaiting).toBeNull();
    expect(view?.stages[1].label).toBe("図・表の説明（Vision）: 12 / 20 件");
  });

  it("API の制限で待っている間は、残りの秒数と減らした同時の呼び出しの数を出す", () => {
    const view = resolveExtractStageProgress(
      pipelineProgress({
        calls: calls({ waiting_reason: "rate_limited", waiting_seconds: 26, concurrency: 2 }),
      }),
      now
    );
    expect(view?.apiWaiting).toBe(
      "図・表の説明（Vision）: API の制限のため 26 秒待っています（同時の呼び出しを 2 / 4 件に減らしています）。"
    );
  });
});

describe("apiWaitingMessage", () => {
  it.each([
    [{ waiting_reason: "rate_limited", waiting_seconds: 30 }, "図・表の説明（Vision）: API の制限のため 30 秒待っています。"],
    [
      { waiting_reason: "breaker_open", waiting_seconds: 30 },
      "図・表の説明（Vision）: API の失敗が続いたため呼び出しを止めています。30 秒後に 1 件で試します。",
    ],
    [
      { waiting_reason: "final_retry", waiting_seconds: 12 },
      "図・表の説明（Vision）: 再試行しても失敗した件を、12 秒後にまとめてもう一度試します。",
    ],
    [
      { waiting_reason: "unavailable", waiting_seconds: 3, stage: "embedding" as const },
      "埋め込み（Embedding）: 一時的なエラーのため 3 秒待って再試行します。",
    ],
    [
      { waiting_reason: "rate_limited", waiting_seconds: 8, stage: "raptor_summary" as const },
      "RAPTOR の階層の要約: API の制限のため 8 秒待っています。",
    ],
  ])("待ちの理由 %o", (overrides, expected) => {
    expect(apiWaitingMessage(calls(overrides))).toBe(expected);
  });

  it("待っていない・終わった段は出さない", () => {
    expect(apiWaitingMessage(null)).toBeNull();
    expect(apiWaitingMessage(calls())).toBeNull();
    expect(
      apiWaitingMessage(calls({ waiting_reason: "rate_limited", waiting_seconds: 5, finished: true }))
    ).toBeNull();
  });
});
