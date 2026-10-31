// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTraceProgressSnapshot, type TraceProgressSnapshot } from "../src";

// #3226（案 A）: 処理の経過を共通の span の表の snapshot から読む。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const span = (seq: number, id: string, status: "running" | "done", extra: Record<string, unknown> = {}) => ({
  schema_version: 1, seq, target_id: "run-1", attempt: 0, emitted_at: "2026-01-01T00:00:00Z", type: "step",
  step_id: id, status, name: "gen_ai.chat", trace_id: "a".repeat(32), started_at: "2026-01-01T00:00:00Z",
  ...(status === "done" ? { finished_at: "2026-01-01T00:00:01Z" } : {}), ...extra,
});

const terminal = (seq: number, status: "done" | "failed" = "done") => ({
  schema_version: 1, seq, target_id: "run-1", attempt: 0, emitted_at: "2026-01-01T00:00:02Z", type: "terminal", status,
});

let host: HTMLDivElement;
let root: Root;
let latest: TraceProgressSnapshot | null = null;

function Probe(props: { active: boolean; fetchSnapshot: (signal: AbortSignal) => Promise<unknown>; timeoutMs?: number }) {
  latest = useTraceProgressSnapshot({ key: "run-1", definitions: {}, intervalMs: 1000, settleDelayMs: 1500, ...props });
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  host = document.createElement("div");
  root = createRoot(host);
  latest = null;
});

afterEach(() => {
  act(() => root.unmount());
  vi.useRealTimers();
});

describe("useTraceProgressSnapshot", () => {
  it("実行中は読み直して木を更新し、終わったら書き終わりを待って 1 回読み直し、終端が届いたら止める", async () => {
    const snapshots = [
      [span(1, "r", "running", { root: true }), span(2, "c", "running", { parent_id: "r" })],
      [span(1, "r", "running", { root: true }), span(2, "c", "done", { parent_id: "r" })],
      [span(1, "r", "done", { root: true }), span(2, "c", "done", { parent_id: "r" }), terminal(3)],
    ];
    const fetchSnapshot = vi.fn(async () => snapshots[Math.min(fetchSnapshot.mock.calls.length - 1, 2)]);
    await act(async () => root.render(<Probe active fetchSnapshot={fetchSnapshot} />));
    expect(latest?.steps).toMatchObject([{ status: "running" }]);
    expect(latest?.traceId).toBe("a".repeat(32));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(latest?.steps).toMatchObject([{ status: "done" }]);
    await act(async () => root.render(<Probe active={false} fetchSnapshot={fetchSnapshot} />));
    expect(latest?.settling).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(latest?.terminal).toBe("done");
    expect(latest?.settling).toBe(false);
    const calls = fetchSnapshot.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchSnapshot.mock.calls.length).toBe(calls);
  });

  it("終わった記録の snapshot に span が無い間は決まった回数だけ読み直し、その後に古い形式とする", async () => {
    const legacyEvent = { schema_version: 1, seq: 1, target_id: "run-1", attempt: 0, emitted_at: "x", type: "step", step_id: "s1", status: "done" };
    const fetchSnapshot = vi.fn(async () => [legacyEvent]);
    await act(async () => root.render(<Probe active={false} fetchSnapshot={fetchSnapshot} />));
    expect(latest?.legacy).toBe(false);
    expect(latest?.settling).toBe(true);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    }
    expect(fetchSnapshot).toHaveBeenCalledTimes(3);
    expect(latest?.settling).toBe(false);
    expect(latest?.legacy).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchSnapshot).toHaveBeenCalledTimes(3);
  });

  it("読めなかったら前の内容を残し、応答しない読み出しは上限の時間で打ち切って読み直しを続ける", async () => {
    let mode: "ok" | "fail" | "hang" = "ok";
    const fetchSnapshot = vi.fn(
      (signal: AbortSignal) =>
        new Promise<unknown>((resolve, reject) => {
          if (mode === "fail") reject(new Error("synthetic"));
          else if (mode === "hang") signal.addEventListener("abort", () => reject(new Error("aborted")));
          else resolve([span(1, "r", "running", { root: true })]);
        }),
    );
    await act(async () => root.render(<Probe active fetchSnapshot={fetchSnapshot} timeoutMs={3000} />));
    expect(latest?.events).toHaveLength(1);
    mode = "fail";
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(latest?.failed).toBe(true);
    expect(latest?.events).toHaveLength(1);
    mode = "hang";
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    const calls = fetchSnapshot.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000 + 1000); });
    expect(fetchSnapshot.mock.calls.length).toBeGreaterThan(calls);
  });

  it("書けなかった span の数を受け取り、一部だけの記録とする（古い形式とはしない）", async () => {
    const fetchSnapshot = vi.fn(async () => ({ events: [span(1, "r", "done", { root: true }), terminal(2)], dropped_spans: 2 }));
    await act(async () => root.render(<Probe active={false} fetchSnapshot={fetchSnapshot} />));
    expect(latest?.droppedSpans).toBe(2);
    expect(latest?.partial).toBe(true);
    expect(latest?.legacy).toBe(false);
  });
});
