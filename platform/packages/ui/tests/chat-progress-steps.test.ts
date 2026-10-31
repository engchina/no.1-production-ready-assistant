import { describe, expect, it } from "vitest";

import {
  mergeChatProgressSteps,
  reduceChatProgressSteps,
  type ChatProgressStep,
  type ChatProgressStepStatus,
  type ChatProgressStepsState,
} from "../src";

// #1358: 処理中は一度出した段階を消さず、状態を戻さない（3 製品共通の規則）。

function step(id: string, status: ChatProgressStepStatus, extra: Partial<ChatProgressStep> = {}): ChatProgressStep {
  return { id, label: `${id}:${status}`, status, ...extra };
}

const summary = (steps: readonly ChatProgressStep[]) => steps.map((item) => `${item.id}:${item.status}`);

/** 一覧を順に流し、毎回の「完了した段階」（ChatProgress の「N ステップ完了」の中身）を返す。 */
function finishedAfterEach(snapshots: ChatProgressStep[][], key = "job-1"): string[][] {
  let state: ChatProgressStepsState | null = null;
  return snapshots.map((steps) => {
    state = reduceChatProgressSteps(state, { key, steps, active: true });
    return state.steps.filter((item) => item.status !== "running" && item.status !== "pending").map((item) => item.id);
  });
}

describe("mergeChatProgressSteps", () => {
  it("報告の再現: 完了した段階が実行中に戻っても、完了の一覧から消さない", () => {
    // RAG の回答フロー（検索 → 並べ替え → 根拠確認で検索に戻る）と同じ形（step2 が消えて、また現れる）。
    const finished = finishedAfterEach([
      [step("step1", "done"), step("step2", "done"), step("step3", "running"), step("step4", "pending")],
      [step("step1", "done"), step("step2", "running"), step("step3", "done"), step("step4", "pending")],
      [step("step1", "done"), step("step2", "done"), step("step3", "done"), step("step4", "running")],
    ]);
    expect(finished).toEqual([
      ["step1", "step2"],
      ["step1", "step2", "step3"],
      ["step1", "step2", "step3"],
    ]);
  });

  it("新しい一覧に無い段階も残し、前の順序を保つ", () => {
    const merged = mergeChatProgressSteps(
      [step("a", "done"), step("b", "done"), step("c", "running")],
      [step("a", "done"), step("c", "running")]
    );
    expect(summary(merged)).toEqual(["a:done", "b:done", "c:running"]);
  });

  it("新しい段階は、新しい一覧の中で直前にある段階の後ろへ入れる", () => {
    const merged = mergeChatProgressSteps(
      [step("plan", "done"), step("tool:a", "done"), step("respond", "running")],
      [step("plan", "done"), step("tool:a", "done"), step("approval#2", "running"), step("tool:b", "pending"), step("respond", "pending")]
    );
    expect(summary(merged)).toEqual(["plan:done", "tool:a:done", "approval#2:running", "tool:b:pending", "respond:running"]);
    // 先頭に増えた段階は先頭に入れる。
    expect(summary(mergeChatProgressSteps([step("b", "running")], [step("a", "done"), step("b", "running")]))).toEqual([
      "a:done",
      "b:running",
    ]);
  });

  it("状態は進む方へだけ変える（完了 → 実行中・待機中、実行中 → 待機中は前を残す）", () => {
    expect(summary(mergeChatProgressSteps([step("a", "done")], [step("a", "running")]))).toEqual(["a:done"]);
    expect(summary(mergeChatProgressSteps([step("a", "done")], [step("a", "pending")]))).toEqual(["a:done"]);
    expect(summary(mergeChatProgressSteps([step("a", "running")], [step("a", "pending")]))).toEqual(["a:running"]);
    expect(summary(mergeChatProgressSteps([step("a", "pending")], [step("a", "running")]))).toEqual(["a:running"]);
    expect(summary(mergeChatProgressSteps([step("a", "running")], [step("a", "failed")]))).toEqual(["a:failed"]);
    // 終わった段階どうしの確定（完了 → 失敗・スキップ）は新しい方を使う。
    expect(summary(mergeChatProgressSteps([step("a", "done")], [step("a", "skipped")]))).toEqual(["a:skipped"]);
  });

  it("同じ状態の間の更新（補足・名前・時刻）は新しい方を使い、変わらなければ前の配列を返す", () => {
    const previous = [step("a", "running", { detail: "1 回目" })];
    const merged = mergeChatProgressSteps(previous, [step("a", "running", { detail: "2 回目", startedAt: "2026-10-09T00:00:00Z" })]);
    expect(merged[0].detail).toBe("2 回目");
    expect(merged[0].startedAt).toBe("2026-10-09T00:00:00Z");
    expect(mergeChatProgressSteps(previous, [step("a", "running", { detail: "1 回目" })])).toBe(previous);
  });

  it("同じ id が重なった一覧は最初の 1 つにする", () => {
    expect(summary(mergeChatProgressSteps([], [step("a", "done"), step("a", "running")]))).toEqual(["a:done"]);
  });
});

describe("reduceChatProgressSteps", () => {
  it("古い snapshot（polling の順序の入れ替わり・取り直し）で戻さない", () => {
    const newer = [step("queue", "done"), step("prepare", "done"), step("generate", "running")];
    const older = [step("queue", "done"), step("prepare", "running"), step("generate", "pending")];
    let state = reduceChatProgressSteps(null, { key: "job-1", steps: newer, active: true });
    state = reduceChatProgressSteps(state, { key: "job-1", steps: older, active: true });
    expect(summary(state.steps)).toEqual(["queue:done", "prepare:done", "generate:running"]);
  });

  it("終端では新しい一覧で確定する（処理中に残した段階も作り直す）", () => {
    let state = reduceChatProgressSteps(null, {
      key: "run-1",
      steps: [step("submit", "running"), step("plan", "running")],
      active: true,
    });
    state = reduceChatProgressSteps(state, { key: "run-1", steps: [step("plan", "done"), step("respond", "done")], active: false });
    expect(summary(state.steps)).toEqual(["plan:done", "respond:done"]);
  });

  it("対象（送信・Run・ジョブ）が変わったら作り直す", () => {
    let state = reduceChatProgressSteps(null, { key: "job-1", steps: [step("a", "done"), step("b", "running")], active: true });
    state = reduceChatProgressSteps(state, { key: "job-2", steps: [step("a", "running")], active: true });
    expect(state.key).toBe("job-2");
    expect(summary(state.steps)).toEqual(["a:running"]);
  });

  it("変わらなければ同じ状態を返す（描画し直さない）", () => {
    const steps = [step("a", "done"), step("b", "running")];
    const state = reduceChatProgressSteps(null, { key: "k", steps, active: true });
    expect(reduceChatProgressSteps(state, { key: "k", steps: steps.map((item) => ({ ...item })), active: true })).toBe(state);
    const done = reduceChatProgressSteps(null, { key: "k", steps, active: false });
    expect(reduceChatProgressSteps(done, { key: "k", steps: steps.map((item) => ({ ...item })), active: false })).toBe(done);
  });
});
