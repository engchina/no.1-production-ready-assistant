// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ChatProgress,
  useChatProgressStream,
  type ChatProgressEvent,
  type ChatProgressPage,
  type ChatProgressStepDefinitions,
  type ChatProgressStepStatus,
  type ChatProgressStream,
  type ChatProgressStreamOptions,
} from "../src";

// #1359: チャットの処理の段階の配信（SSE を優先し、polling に縮退。途絶え・再接続・取り直し）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DEFINITIONS: ChatProgressStepDefinitions = {
  prepare: { label: (status) => `準備（${status}）` },
  generate: { label: (status) => `生成（${status}）` },
};

function step(seq: number, stepId: string, status: ChatProgressStepStatus, attempt = 0): ChatProgressEvent {
  return {
    schema_version: 1,
    type: "step",
    seq,
    target_id: "job-1",
    attempt,
    emitted_at: "2026-10-09T00:00:00.000Z",
    step_id: stepId,
    status,
    started_at: "2026-10-09T00:00:00.000Z",
  };
}

function terminal(seq: number): ChatProgressEvent {
  return {
    schema_version: 1,
    type: "terminal",
    seq,
    target_id: "job-1",
    attempt: 0,
    emitted_at: "2026-10-09T00:00:05.000Z",
    status: "done",
  };
}

function page(events: ChatProgressEvent[], { terminal: done = false }: { terminal?: boolean } = {}): ChatProgressPage {
  return { target_id: "job-1", attempt: 0, events, last_seq: events.at(-1)?.seq ?? 0, terminal: done };
}

/** テスト用の EventSource（イベントを手で流す）。 */
class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  closed = false;
  constructor(readonly url: string) {
    super();
    FakeEventSource.instances.push(this);
  }
  open() {
    this.readyState = 1;
    this.dispatchEvent(new Event("open"));
  }
  send(event: ChatProgressEvent) {
    this.dispatchEvent(new MessageEvent("chat_progress", { data: JSON.stringify(event), lastEventId: String(event.seq) }));
  }
  heartbeat() {
    this.dispatchEvent(new MessageEvent("heartbeat", { data: "{}" }));
  }
  fail(closed: boolean) {
    this.readyState = closed ? 2 : 0;
    this.dispatchEvent(new Event("error"));
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
}

let host: HTMLDivElement;
let root: Root;
let latest: ChatProgressStream;

function Harness({ options }: { options: ChatProgressStreamOptions }) {
  latest = useChatProgressStream(options);
  return <ChatProgress {...latest.progressProps} testId="progress" />;
}

function render(options: Partial<ChatProgressStreamOptions>) {
  act(() =>
    root.render(
      <Harness
        options={{
          key: "job-1",
          definitions: DEFINITIONS,
          createEventSource: (url) => new FakeEventSource(url) as unknown as EventSource,
          ...options,
        }}
      />
    )
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const lastSource = () => FakeEventSource.instances.at(-1) as FakeEventSource;
const statuses = () => Object.fromEntries(latest.steps.map((s) => [s.id, s.status]));

beforeEach(() => {
  vi.useFakeTimers({ now: Date.parse("2026-10-09T00:00:01.000Z") });
  FakeEventSource.instances = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

describe("useChatProgressStream", () => {
  it("SSE のイベントを積み、名前を付ける。終端で閉じて onTerminal を 1 回呼ぶ", async () => {
    const onTerminal = vi.fn();
    render({ streamUrl: "/api/jobs/job-1/progress/stream", onTerminal });
    expect(latest.transport).toBe("sse");
    const source = lastSource();
    expect(source.url).toBe("/api/jobs/job-1/progress/stream?since=0");
    act(() => {
      source.open();
      source.send(step(1, "prepare", "running"));
      source.send(step(2, "prepare", "done"));
      source.send(step(3, "generate", "running"));
    });
    expect(latest.steps.map((s) => s.label)).toEqual(["準備（done）", "生成（running）"]);
    expect(latest.cursor).toBe(3);
    expect(latest.active).toBe(true);
    act(() => {
      source.send(step(4, "generate", "done"));
      source.send(terminal(5));
    });
    expect(latest.active).toBe(false);
    expect(latest.terminal).toBe("done");
    expect(source.closed).toBe(true);
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledWith("done");
    expect(host.querySelector('[data-testid="progress-summary"]')).not.toBeNull();
  });

  it("重複・古いイベントで状態を戻さない", () => {
    render({ streamUrl: "/stream" });
    const source = lastSource();
    act(() => {
      source.send(step(1, "prepare", "running"));
      source.send(step(2, "prepare", "done"));
      source.send(step(1, "prepare", "running"));
      source.send(step(2, "prepare", "done"));
    });
    expect(statuses()).toEqual({ prepare: "done" });
    expect(latest.cursor).toBe(2);
  });

  it("番号が飛んだら、前の番号から取り直して埋める", async () => {
    const fetchEvents = vi.fn(async (since: number) => page([step(2, "prepare", "done"), step(3, "generate", "running")].filter((e) => e.seq > since)));
    render({ streamUrl: "/stream", fetchEvents });
    const source = lastSource();
    act(() => {
      source.send(step(1, "prepare", "running"));
      source.send(step(3, "generate", "running"));
    });
    expect(statuses()).toEqual({ prepare: "running" });
    await advance(0);
    expect(fetchEvents).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    expect(statuses()).toEqual({ prepare: "done", generate: "running" });
    expect(latest.cursor).toBe(3);
  });

  it("SSE が閉じたら（4xx / 5xx・使えない）polling に縮退し、since の続きを取る", async () => {
    const fetchEvents = vi.fn(async (since: number) =>
      since === 0 ? page([step(1, "prepare", "running")]) : page([terminal(2)].filter((e) => e.seq > since), { terminal: true })
    );
    render({ streamUrl: "/stream", fetchEvents, pollIntervalMs: 1_000 });
    act(() => lastSource().fail(true));
    expect(latest.transport).toBe("polling");
    await advance(0);
    expect(fetchEvents).toHaveBeenLastCalledWith(0, expect.any(AbortSignal));
    expect(statuses()).toEqual({ prepare: "running" });
    await advance(1_000);
    expect(fetchEvents).toHaveBeenLastCalledWith(1, expect.any(AbortSignal));
    expect(latest.terminal).toBe("done");
    expect(statuses()).toEqual({ prepare: "running" });
    const calls = fetchEvents.mock.calls.length;
    await advance(5_000);
    expect(fetchEvents.mock.calls.length).toBe(calls);
  });

  it("続けて接続に失敗したら polling に縮退する（ブラウザが張り直している間は待つ）", () => {
    render({ streamUrl: "/stream", fetchEvents: async () => page([]) });
    const source = lastSource();
    act(() => source.fail(false));
    act(() => source.fail(false));
    expect(latest.transport).toBe("sse");
    act(() => source.fail(false));
    expect(latest.transport).toBe("polling");
    expect(source.closed).toBe(true);
  });

  it("途絶えたら「接続を確認しています」を出し、張り直して続きから受け取る", async () => {
    const fetchEvents = vi.fn(async () => page([]));
    render({ streamUrl: "/stream", fetchEvents, staleAfterMs: 5_000 });
    const first = lastSource();
    act(() => {
      first.open();
      first.send(step(1, "prepare", "running"));
    });
    await advance(4_000);
    act(() => first.heartbeat());
    await advance(4_000);
    expect(latest.reconnecting).toBe(false);
    await advance(2_000);
    expect(latest.reconnecting).toBe(true);
    expect(first.closed).toBe(true);
    const second = lastSource();
    expect(second).not.toBe(first);
    expect(second.url).toBe("/stream?since=1");
    act(() => second.send(step(2, "prepare", "done")));
    expect(latest.reconnecting).toBe(false);
    expect(statuses()).toEqual({ prepare: "done" });
  });

  it("製品が受け取ったイベント（events）を積み、保存済みの終わった対象は配信を開かない", () => {
    render({ streamUrl: "/stream", events: [step(1, "prepare", "done"), terminal(2)] });
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(latest.transport).toBe("idle");
    expect(latest.active).toBe(false);
    expect(statuses()).toEqual({ prepare: "done" });
  });

  it("push（SSE も polling も無い）は events と touch だけで追う", () => {
    const events = [step(1, "prepare", "running")];
    render({ events, active: true });
    expect(latest.transport).toBe("push");
    render({ events: [...events, step(2, "prepare", "done"), step(3, "generate", "running")], active: true });
    expect(statuses()).toEqual({ prepare: "done", generate: "running" });
  });

  it("events に新しい番号が届いたら、配信を受け取ったとして途絶えを数え直す", async () => {
    const onStalled = vi.fn();
    const events = [step(1, "prepare", "running")];
    render({ events, active: true, onStalled, staleAfterMs: 5_000 });
    await advance(4_000);
    render({ events: [...events, step(2, "prepare", "done")], active: true, onStalled, staleAfterMs: 5_000 });
    await advance(4_000);
    expect(onStalled).not.toHaveBeenCalled();
    expect(latest.reconnecting).toBe(false);
    await advance(2_000);
    expect(onStalled).toHaveBeenCalled();
    expect(latest.reconnecting).toBe(true);
  });

  it("pollEvents が false なら polling せず、途絶えの取り直しと穴埋めにだけ fetchEvents を使う", async () => {
    const onStalled = vi.fn();
    const fetchEvents = vi.fn(async (since: number) => page([step(2, "prepare", "done")].filter((e) => e.seq > since)));
    const options = { active: true, onStalled, fetchEvents, pollEvents: false, staleAfterMs: 5_000 };
    render({ ...options, events: [step(1, "prepare", "running")] });
    expect(latest.transport).toBe("push");
    await advance(4_000);
    expect(fetchEvents).not.toHaveBeenCalled();
    await advance(2_000);
    // 途絶えた: 製品の配信を張り直させ、記録から取り直す。取り直しの成功では途絶えを解かない。
    expect(onStalled).toHaveBeenCalled();
    expect(fetchEvents).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    expect(statuses()).toEqual({ prepare: "done" });
    expect(latest.reconnecting).toBe(true);
    act(() => latest.touch());
    expect(latest.reconnecting).toBe(false);
  });

  it("試行が変わったら作り直し、progressKey に試行を入れる", () => {
    render({ events: [step(1, "prepare", "done"), step(2, "generate", "running")] });
    expect(latest.progressProps.progressKey).toBe("job-1#0");
    render({ events: [step(1, "prepare", "done"), step(2, "generate", "running"), step(3, "prepare", "running", 1)] });
    expect(statuses()).toEqual({ prepare: "running" });
    expect(latest.progressProps.progressKey).toBe("job-1#1");
  });

  it("対象が変わったら前の対象の段階を出さず、新しい対象の配信を開く", () => {
    render({ streamUrl: "/stream/job-1" });
    act(() => lastSource().send(step(1, "prepare", "running")));
    render({ key: "job-2", streamUrl: "/stream/job-2" });
    expect(latest.steps).toEqual([]);
    expect(FakeEventSource.instances[0].closed).toBe(true);
    expect(lastSource().url).toBe("/stream/job-2?since=0");
  });

  it("enabled が false の間は配信を開かない", () => {
    render({ streamUrl: "/stream", enabled: false });
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(latest.transport).toBe("idle");
  });
});
