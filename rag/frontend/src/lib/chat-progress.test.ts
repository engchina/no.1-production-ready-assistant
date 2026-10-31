import { chatProgressStepsFromEvents, type ChatProgressEvent } from "@production-ready/ui";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CHAT_PROGRESS_DEFINITIONS, chatSubmitProgressSteps } from "./chat-progress";
import { streamChatMessage } from "./chat-stream";

afterEach(() => vi.unstubAllGlobals());

const TARGET = "a".repeat(32);
let seq = 0;

function step(
  stepId: string,
  status: "pending" | "running" | "done" | "failed" | "skipped",
  extra: Partial<Extract<ChatProgressEvent, { type: "step" }>> = {}
): ChatProgressEvent {
  seq += 1;
  return {
    schema_version: 1,
    seq,
    target_id: TARGET,
    attempt: 0,
    emitted_at: "2026-10-04T00:00:00.000Z",
    type: "step",
    step_id: stepId,
    status,
    ...extra,
  };
}

// 処理の段階の定義（Issue 1146 / 1359）。
describe("CHAT_PROGRESS_DEFINITIONS", () => {
  it("工程の span は種類と工程名から名前を付け、LLM の回数・結果の説明を補足にする（名前は状態で変えない）", () => {
    seq = 0;
    const steps = chatProgressStepsFromEvents(
      [
        step("a".repeat(16), "done", {
          kind: "history_rewrite",
          name: "rag.history_rewrite",
          started_at: "2026-10-04T00:00:00.000Z",
          finished_at: "2026-10-04T00:00:01.000Z",
        }),
        step("b".repeat(16), "running", { kind: "answer_step", name: "rag.engine.stage", params: { name: "文書検索" } }),
        step("c".repeat(16), "failed", {
          kind: "answer_step",
          name: "rag.engine.stage",
          params: { name: "質問の理解", llm_calls: 1, llm_sends: 2, note: "該当なし" },
        }),
      ],
      CHAT_PROGRESS_DEFINITIONS
    );
    expect(steps.map((item) => [item.label, item.status, item.meta ?? null])).toEqual([
      ["前の会話を踏まえて質問を補う", "done", null],
      ["検索文から根拠の候補を集める", "running", null],
      ["質問から対象と条件を読み取る", "failed", "該当なし · LLM 1 回・送信 2 回"],
    ]);
    expect(steps[0]).toMatchObject({ startedAt: "2026-10-04T00:00:00.000Z", finishedAt: "2026-10-04T00:00:01.000Z" });
  });

  it("未知の段階は id をそのまま出す", () => {
    seq = 0;
    expect(chatProgressStepsFromEvents([step("new_step", "running")], CHAT_PROGRESS_DEFINITIONS)).toEqual([
      { id: "new_step", label: "new_step", status: "running" },
    ]);
  });

  it("送信の応答待ちは「質問を送信しています」を送った時刻から出す", () => {
    expect(chatSubmitProgressSteps(Date.parse("2026-10-04T00:00:00.000Z"))).toEqual([
      { id: "submit", label: "質問を送信しています", status: "running", startedAt: "2026-10-04T00:00:00.000Z" },
    ]);
  });

  it("チャットの SSE の chat_progress（古い配信）は受け取らない（処理の経過は trace の API で読む。Issue 3226）", async () => {
    seq = 0;
    const event = step("retrieve", "running");
    const body = [
      `event: chat_progress\ndata: ${JSON.stringify({ model_id: "m1", message_id: TARGET, event })}\n\n`,
      `event: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`,
    ].join("");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }))
    );
    const onAllDone = vi.fn();
    await expect(streamChatMessage("c1", { content: "質問" }, { onAllDone })).resolves.toEqual({ completed: true });
    expect(onAllDone).toHaveBeenCalledTimes(1);
  });
});
