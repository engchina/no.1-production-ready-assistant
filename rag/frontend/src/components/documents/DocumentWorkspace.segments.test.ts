import { describe, expect, it } from "vitest";

import {
  resolveExtractStageProgress,
  resolveSegmentStrip,
  segmentsWithProgress,
} from "./DocumentWorkspace.logic";
import type {
  IngestionJobProgress,
  IngestionParseSegmentProgress,
  IngestionParseSegmentStatus,
  IngestionSegment,
} from "@/lib/api";

// #2001: PDF を段ごとに解析するとき、「抽出: N / M ページ」・段の帯・段のカードを同じ記録から作る。
const updatedAt = "2026-10-15T08:37:27Z";
const now = Date.parse(updatedAt) + 5_000;

function segment(
  start: number,
  end: number,
  status: IngestionParseSegmentStatus,
  overrides: Partial<IngestionParseSegmentProgress> = {}
): IngestionParseSegmentProgress {
  return {
    segment_id: `doc-1:p${start}-${end}`,
    page_start: start,
    page_end: end,
    status,
    completed: 0,
    reused: false,
    ...overrides,
  };
}

function progress(segments: IngestionParseSegmentProgress[], finished = false): IngestionJobProgress {
  const running = segments.filter((item) => item.status === "RUNNING");
  return {
    stage: "parse",
    parse: {
      engine: "docling",
      unit: "page",
      completed: running.reduce((sum, item) => sum + item.completed, 0),
      failed: 0,
      total: running.reduce((sum, item) => sum + item.page_end - item.page_start + 1, 0) || null,
      finished,
      segments,
    },
    vision: null,
    updated_at: updatedAt,
  };
}

function dbSegment(start: number, end: number, status: string): IngestionSegment {
  return {
    segment_id: `doc-1:p${start}-${end}`,
    document_id: "doc-1",
    recipe_id: null,
    status,
    parser_backend: "docling",
    parser_profile: "docling",
    page_start: start,
    page_end: end,
    progress_unit: "page",
    progress_start: start,
    progress_end: end,
    attempt_count: 0,
    artifact_path: null,
    error_code: null,
    error_message: null,
  };
}

describe("段ごとの解析の進捗", () => {
  const nine = (statuses: IngestionParseSegmentStatus[]) =>
    statuses.map((status, index) =>
      segment(index * 10 + 1, Math.min(index * 10 + 10, 82), status, {
        completed: status === "RUNNING" ? 4 : 0,
      })
    );

  it("「抽出: N / M ページ完了」は完了した段のページで進み、待っている段とその中のページを添える", () => {
    const view = resolveExtractStageProgress(
      progress(nine(["SUCCEEDED", "SUCCEEDED", "RUNNING", "QUEUED", "QUEUED", "QUEUED", "QUEUED", "QUEUED", "QUEUED"])),
      now
    );

    expect(view?.stages[0]).toEqual({
      key: "parse",
      label: "抽出: 20 / 82 ページ完了",
      value: 20,
      max: 82,
      done: false,
    });
    expect(view?.waiting).toBe("Docling が p.21-30（段 3 / 9）を解析しています（10 ページ中 4 ページ）。");
  });

  it("失敗した段のページを分けて出し、すべて終わったら段の数を添える", () => {
    const failed = resolveExtractStageProgress(
      progress(nine(["SUCCEEDED", "FAILED", "CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED"])),
      now
    );
    expect(failed?.stages[0].label).toBe("抽出: 完了 10 / 失敗 10 / 全 82 ページ");
    expect(failed?.waiting).toBe("次の段の解析を準備しています。");

    const done = resolveExtractStageProgress(progress(nine(Array(9).fill("SUCCEEDED")), true), now);
    expect(done?.stages[0]).toMatchObject({ label: "抽出: 完了（82 ページ・9 段）", done: true });
  });

  it("段の帯は段ごとの状態とページの範囲を読み上げ、凡例に状態ごとの数を出す", () => {
    const strip = resolveSegmentStrip([
      segment(1, 10, "SUCCEEDED", { reused: true }),
      segment(11, 20, "RUNNING"),
      segment(21, 23, "QUEUED"),
    ]);

    expect(strip?.items.map((item) => item.label)).toEqual([
      "p.1-10: 完了（前の解析の結果を使用）",
      "p.11-20: 処理中",
      "p.21-23: 待機中",
    ]);
    expect(strip?.counts).toEqual([
      { status: "SUCCEEDED", count: 1 },
      { status: "RUNNING", count: 1 },
      { status: "QUEUED", count: 1 },
    ]);
    expect(strip?.summary).toBe("段ごとの解析の状態: 完了 1・処理中 1・待機中 1");
  });

  it("段が 1 つ・ページの段でない（原本全体・slide）ときは帯を出さない", () => {
    expect(resolveSegmentStrip([segment(1, 6, "RUNNING")])).toBeNull();
    expect(
      resolveSegmentStrip([
        { ...dbSegment(1, 1, "RUNNING"), progress_unit: "slide" },
        { ...dbSegment(2, 2, "RUNNING"), progress_unit: "slide" },
      ])
    ).toBeNull();
    expect(resolveSegmentStrip([{ ...dbSegment(1, 1, "RUNNING"), page_start: null }])).toBeNull();
  });

  it("段のカードの状態は実行中の job の進捗を正にする（前の試行の待機中の行と食い違わない）", () => {
    const rows = [dbSegment(1, 10, "QUEUED"), dbSegment(11, 20, "QUEUED")];
    const live = progress([segment(1, 10, "SUCCEEDED"), segment(11, 20, "RUNNING", { completed: 3 })]);

    const merged = segmentsWithProgress(rows, live);

    expect(merged.map((item) => [item.status, item.completed_pages])).toEqual([
      ["SUCCEEDED", 0],
      ["RUNNING", 3],
    ]);
    // 進捗が無い（job の外・古い backend）ときは行のまま。
    expect(segmentsWithProgress(rows, null)).toEqual(rows);
    // 行がまだ読めていない段は進捗で補う。
    expect(segmentsWithProgress([], live).map((item) => item.segment_id)).toEqual([
      "doc-1:p1-10",
      "doc-1:p11-20",
    ]);
  });
});
