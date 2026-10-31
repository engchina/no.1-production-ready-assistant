import { describe, expect, it } from "vitest";

import type { RunState } from "@/lib/api";
import { runFailure } from "@/lib/process-failure";

const NOW = "2026-10-18T06:30:41.000Z";

function run(overrides: Partial<RunState> = {}): RunState {
  return {
    id: "run-1",
    goal: "q",
    agent_id: "default",
    runtime_id: "builtin",
    status: "failed",
    steps: [],
    events: [
      {
        id: "e1",
        run_id: "run-1",
        type: "runtime.failed",
        message: "モデルの呼び出しに失敗しました。",
        payload: { error_code: "runtime.model_failed" },
        created_at: NOW,
      },
    ],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  } as RunState;
}

describe("Run の失敗を共通の失敗のカードに写す（Issue 2342）", () => {
  const steps = [
    { label: "回答を作成", status: "done" },
    // 名前を状態で変える段階は、名詞の名前（name）を止まった段に使う。
    { label: "資料の検索ができませんでした", name: "ツール: 資料の検索", status: "failed" },
    { label: "資料との照合", status: "pending" },
  ];

  it("チャットは止まった段・失敗のイベントの文・コード・実行 ID・時刻を出す", () => {
    expect(runFailure(run(), steps, "chat")).toMatchObject({
      summary: "「ツール: 資料の検索」で処理が止まりました。",
      cause: "モデルの呼び出しに失敗しました。",
      failedStep: "ツール: 資料の検索",
      errorCode: "runtime.model_failed",
      traceId: "run-1",
      occurredAt: NOW,
    });
  });

  it("実行の詳細は概要に実行 ID があるので重ねず、チャットでもう一度質問する案内にする", () => {
    const failure = runFailure(run(), [], "detail");
    expect(failure?.summary).toBe("回答できませんでした。");
    expect(failure?.traceId).toBeNull();
    expect(failure?.nextAction).toContain("チャットでもう一度質問してください");
  });

  it("失敗していない Run は null、失敗のイベントの無い Run は既定の文", () => {
    expect(runFailure(run({ status: "completed" }), [], "chat")).toBeNull();
    expect(runFailure(run({ events: [] }), [], "chat")?.cause).toBe(
      "実行に失敗しました。時間をおいて、もう一度送ってください。"
    );
  });
});
