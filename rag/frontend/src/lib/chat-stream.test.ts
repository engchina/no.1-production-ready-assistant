import { afterEach, describe, expect, it, vi } from "vitest";

import { isChatStreamGone, newChatClientMessageId, resumeChatStream, streamChatMessage } from "./chat-stream";

function sseResponse(blocks: string[]): Response {
  return new Response(blocks.join(""), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("streamChatMessage", () => {
  it("単一モデル: start → delta → citations → done → all_done を解析する", async () => {
    const body = [
      `event: start\ndata: ${JSON.stringify({
        conversation_id: "c1",
        user_message: { message_id: "u1", role: "USER", content: "質問" },
        columns: [{ model_id: "m1", label: "MODEL 1" }],
      })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "回答" })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "です。" })}\n\n`,
      `event: metadata\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1", trace_id: "t1", elapsed_ms: 5 })}\n\n`,
      `event: citations\ndata: ${JSON.stringify({ model_id: "m1", citations: [{ chunk_id: "ch1" }] })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
      `event: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`,
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(body)));

    const answers: Record<string, string> = {};
    let columns: string[] = [];
    let citationCount = 0;
    let traceId = "";
    let allDone = false;
    let userContent = "";
    await streamChatMessage(
      "c1",
      { content: "質問" },
      {
        onStart: ({ user_message, columns: cols }) => {
          userContent = user_message.content;
          columns = cols.map((c) => c.model_id);
        },
        onDelta: (modelId, text) => {
          answers[modelId] = (answers[modelId] ?? "") + text;
        },
        onMetadata: ({ trace_id }) => (traceId = trace_id),
        onCitations: (_modelId, citations) => (citationCount = citations.length),
        onAllDone: () => (allDone = true),
      }
    );

    expect(userContent).toBe("質問");
    expect(columns).toEqual(["m1"]);
    expect(answers.m1).toBe("回答です。");
    expect(citationCount).toBe(1);
    expect(traceId).toBe("t1");
    expect(allDone).toBe(true);
  });

  it("マルチモデル: モデル別に delta を振り分ける", async () => {
    const body = [
      `event: start\ndata: ${JSON.stringify({
        conversation_id: "c1",
        user_message: { message_id: "u1", role: "USER", content: "比較" },
        columns: [
          { model_id: "m1", label: "M1" },
          { model_id: "m2", label: "M2" },
        ],
      })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m1", text: "A" })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ model_id: "m2", text: "B" })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
      `event: done\ndata: ${JSON.stringify({ model_id: "m2", message_id: "a2" })}\n\n`,
      `event: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`,
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(body)));

    const answers: Record<string, string> = {};
    const doneModels: string[] = [];
    await streamChatMessage(
      "c1",
      { content: "比較", model_ids: ["m1", "m2"] },
      {
        onDelta: (modelId, text) => {
          answers[modelId] = (answers[modelId] ?? "") + text;
        },
        onModelDone: ({ model_id }) => doneModels.push(model_id),
      }
    );

    expect(answers).toEqual({ m1: "A", m2: "B" });
    expect(doneModels.sort()).toEqual(["m1", "m2"]);
  });

  it("error event は onModelError を呼ぶ", async () => {
    const body = [
      `event: error\ndata: ${JSON.stringify({ model_id: "m1", message: "失敗しました。", error_type: "ValueError" })}\n\n`,
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(body)));

    let errorMessage = "";
    await streamChatMessage(
      "c1",
      { content: "x" },
      { onModelError: ({ message }) => (errorMessage = message) }
    );
    expect(errorMessage).toBe("失敗しました。");
  });

  it("all_done まで届いたら completed、届く前に終わったら completed: false を返す（接続が切れた）", async () => {
    const start = `event: start\ndata: ${JSON.stringify({
      conversation_id: "c1",
      user_message: { message_id: "u1", role: "USER", content: "質問" },
      columns: [{ model_id: "m1", label: "MODEL 1" }],
    })}\n\n`;
    const allDone = `event: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([start, allDone])));
    await expect(streamChatMessage("c1", { content: "質問" }, {})).resolves.toEqual({ completed: true });

    // 接続が切れた（all_done の前に本文が終わった）。
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([start])));
    await expect(streamChatMessage("c1", { content: "質問" }, {})).resolves.toEqual({ completed: false });
  });

  it("heartbeat のコメントも含め、届いたバイトごとに onActivity を呼ぶ（event としては扱わない）", async () => {
    const encoder = new TextEncoder();
    const chunks = [
      ": keepalive\n\n",
      `event: chat_progress\ndata: ${JSON.stringify({
        model_id: "m1",
        message_id: "a1",
        event: { schema_version: 1, seq: 1, target_id: "a1", attempt: 0, emitted_at: "", type: "step", step_id: "retrieve", status: "pending" },
      })}\n\n`,
      ": keepalive\n\n",
      `event: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`,
    ];
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }))
    );
    const onActivity = vi.fn();
    const outcome = await streamChatMessage("c1", { content: "質問" }, { onActivity });
    expect(outcome).toEqual({ completed: true });
    // 応答の受け取り（1 回）と、届いたバイトごと（知らない event も途絶えの判定のバイトに数える）。
    expect(onActivity.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("非 2xx は ApiError を投げる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "チャット機能は現在無効です。" }), { status: 404 })
      )
    );

    await expect(
      streamChatMessage("c1", { content: "x" }, {})
    ).rejects.toMatchObject({ status: 404, messages: ["チャット機能は現在無効です。"] });
  });

  it("event の連番（id:）を処理の後に渡す（再購読の位置。Issue 1175）", async () => {
    const body = [
      `id: 1\nevent: start\ndata: ${JSON.stringify({
        conversation_id: "c1",
        user_message: { message_id: "u1", role: "USER", content: "質問" },
        columns: [{ model_id: "m1", label: "M1", message_id: "a1" }],
      })}\n\n`,
      ": keepalive\n\n",
      `id: 2\nevent: error\ndata: ${JSON.stringify({ model_id: "m1", message: "回答の作成を停止しました。", cancelled: true })}\n\n`,
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(body)));
    const ids: number[] = [];
    const errors: unknown[] = [];
    let columns: unknown[] = [];
    const outcome = await streamChatMessage(
      "c1",
      { content: "質問" },
      {
        onEventId: (id) => ids.push(id),
        onModelError: (payload) => errors.push(payload),
        onStart: ({ columns: cols }) => (columns = cols),
      }
    );
    expect(outcome).toEqual({ completed: false });
    expect(ids).toEqual([1, 2]);
    expect(columns).toEqual([{ model_id: "m1", label: "M1", message_id: "a1" }]);
    expect(errors).toEqual([{ model_id: "m1", message: "回答の作成を停止しました。", cancelled: true }]);
  });
});

describe("resumeChatStream", () => {
  it("質問の id と Last-Event-ID で続きを購読する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        `id: 3\nevent: done\ndata: ${JSON.stringify({ model_id: "m1", message_id: "a1" })}\n\n`,
        `id: 4\nevent: all_done\ndata: ${JSON.stringify({ conversation_id: "c1" })}\n\n`,
      ])
    );
    vi.stubGlobal("fetch", fetchMock);
    const done: string[] = [];
    const outcome = await resumeChatStream("c1", "u1", 2, {
      onModelDone: ({ message_id }) => done.push(message_id),
    });
    expect(outcome).toEqual({ completed: true });
    expect(done).toEqual(["a1"]);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/chat/conversations/c1/messages/u1/stream");
    expect(init.method).toBe("GET");
    expect((init.headers as Record<string, string>)["Last-Event-ID"]).toBe("2");
  });

  it("このプロセスで作成していない（404）・SSE でない応答は、続きを購読できないものとして扱う", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "x" }), { status: 404 }))
    );
    const gone = await resumeChatStream("c1", "u1", 1, {}).catch((error: unknown) => error);
    expect(isChatStreamGone(gone)).toBe(true);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<html></html>", { status: 200, headers: { "content-type": "text/html" } })
      )
    );
    const notStream = await resumeChatStream("c1", "u1", 1, {}).catch((error: unknown) => error);
    expect(isChatStreamGone(notStream)).toBe(true);
  });
});

describe("newChatClientMessageId", () => {
  it("32 桁の 16 進を毎回新しく作る", () => {
    const first = newChatClientMessageId();
    expect(first).toMatch(/^[0-9a-f]{32}$/);
    expect(newChatClientMessageId()).not.toBe(first);
  });
});
