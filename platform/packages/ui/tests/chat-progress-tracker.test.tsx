// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ChatProgress,
  chatProgressBackoffMs,
  useChatProgressTracker,
  type ChatProgressStep,
  type ChatProgressTracker,
  type ChatProgressTrackerOptions,
} from "../src";

// #1160: チャットの回答の処理の状態を追う hook（3 製品共通）。配信が途絶えても終端まで追う。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const running: ChatProgressStep[] = [
  { id: "queue", label: "処理を開始しました", status: "done", startedAt: "2026-10-05T00:00:00.000Z", finishedAt: "2026-10-05T00:00:01.000Z" },
  { id: "prepare", label: "質問と対象の表を準備しています", status: "running", startedAt: "2026-10-05T00:00:01.000Z" },
];
const done: ChatProgressStep[] = running.map((step) => ({ ...step, status: "done", finishedAt: "2026-10-05T00:00:05.000Z" }));

let host: HTMLDivElement;
let root: Root;
let latest: ChatProgressTracker;

// `key` は React が消費するので、options として渡す。
function Harness({ options }: { options: ChatProgressTrackerOptions }) {
  latest = useChatProgressTracker(options);
  return <ChatProgress {...latest.progressProps} testId="progress" />;
}

function render(props: ChatProgressTrackerOptions) {
  act(() => root.render(<Harness options={props} />));
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const q = (testId: string) => host.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null;

function setVisibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

beforeEach(() => {
  vi.useFakeTimers({ now: Date.parse("2026-10-05T00:00:10.000Z") });
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

describe("chatProgressBackoffMs", () => {
  it("1 秒・2 秒・4 秒 … と倍にし、上限で止める", () => {
    expect([1, 2, 3, 4, 5, 6, 7].map((n) => chatProgressBackoffMs(n))).toEqual([
      1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000,
    ]);
    expect(chatProgressBackoffMs(3, 3_000)).toBe(3_000);
  });
});

describe("useChatProgressTracker", () => {
  it("配信が届いている間は取り直さず、途絶えたら取り直して「接続を確認しています」を出し、届いたら消す", async () => {
    const refresh = vi.fn();
    const base = { key: "job-1", steps: running, refresh, staleAfterMs: 10_000 };
    render({ ...base, receivedAt: Date.now() });
    for (let i = 0; i < 4; i += 1) {
      await advance(3_000);
      render({ ...base, receivedAt: Date.now() });
    }
    expect(refresh).not.toHaveBeenCalled();
    expect(q("progress-reconnecting")).toBeNull();

    await advance(10_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh.mock.calls[0]![0]).toBeInstanceOf(AbortSignal);
    expect(latest.reconnecting).toBe(true);
    expect(q("progress-reconnecting")?.textContent).toBe("接続を確認しています。");
    expect(q("progress-current")?.getAttribute("data-reconnecting")).toBe("true");

    await advance(10);
    render({ ...base, receivedAt: Date.now() });
    expect(latest.reconnecting).toBe(false);
    expect(q("progress-reconnecting")).toBeNull();
  });

  it("取り直しが失敗し続けても、backoff して終端まで続ける", async () => {
    const refresh = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const base = { key: "job-1", steps: running, refresh, staleAfterMs: 5_000, maxBackoffMs: 8_000 };
    render(base);
    await advance(5_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    // 1 秒・2 秒・4 秒・8 秒（上限）・8 秒。
    for (const [wait, calls] of [[1_000, 2], [2_000, 3], [4_000, 4], [8_000, 5], [8_000, 6]] as const) {
      await advance(wait - 1);
      expect(refresh).toHaveBeenCalledTimes(calls - 1);
      await advance(1);
      expect(refresh).toHaveBeenCalledTimes(calls);
    }
    expect(latest.reconnecting).toBe(true);

    // 終端（完了）になったら取り直しをやめ、案内も消す。
    render({ ...base, steps: done });
    expect(latest.active).toBe(false);
    expect(latest.reconnecting).toBe(false);
    await advance(60_000);
    expect(refresh).toHaveBeenCalledTimes(6);
    expect(host.querySelector("[data-chat-progress-state]")?.getAttribute("data-chat-progress-state")).toBe("done");
  });

  it("応答しない取り直しは上限で中止して、次の取り直しへ進む（止まったままにしない）", async () => {
    const signals: AbortSignal[] = [];
    const refresh = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise(() => undefined);
    });
    render({ key: "job-1", steps: running, refresh, staleAfterMs: 5_000, refreshTimeoutMs: 3_000 });
    await advance(5_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    await advance(3_000);
    expect(signals[0]!.aborted).toBe(true);
    await advance(1_000);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("push の配信は touch() で知らせる。touch が続く間は取り直さない", async () => {
    const refresh = vi.fn();
    render({ key: "stream-1", steps: running, refresh, staleAfterMs: 5_000 });
    for (let i = 0; i < 5; i += 1) {
      await advance(3_000);
      act(() => latest.touch());
    }
    expect(refresh).not.toHaveBeenCalled();
    await advance(5_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(latest.reconnecting).toBe(true);
    await advance(10);
    act(() => latest.touch());
    expect(latest.reconnecting).toBe(false);
  });

  it("refreshNow() は待たずに取り直し、配信が届くまで backoff して続ける（配信が終端の前に終わったとき）", async () => {
    const refresh = vi.fn();
    render({ key: "stream-1", steps: running, refresh, staleAfterMs: 30_000 });
    // 直前まで配信は届いていた（途絶えの時間には満たない）。
    await advance(1_000);
    act(() => latest.touch());
    act(() => latest.refreshNow());
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(latest.reconnecting).toBe(true);
    // 取り直しても配信が届かなければ、途絶えの時間を待たずに 1 秒・2 秒 … で続ける。
    await advance(1_000);
    expect(refresh).toHaveBeenCalledTimes(2);
    await advance(2_000);
    expect(refresh).toHaveBeenCalledTimes(3);
    // 配信が届いたら止め、途絶えの判定に戻る。
    await advance(10);
    act(() => latest.touch());
    expect(latest.reconnecting).toBe(false);
    await advance(29_000);
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it("タブが非表示の間は取り直さず、表示に戻ったら待たずに取り直す", async () => {
    const refresh = vi.fn();
    render({ key: "job-1", steps: running, refresh, staleAfterMs: 5_000 });
    setVisibility("hidden");
    await advance(60_000);
    expect(refresh).not.toHaveBeenCalled();
    setVisibility("visible");
    await advance(0);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("対象が変わったら（2 回目の質問の別のジョブ）、途絶えの判定を初めから数える", async () => {
    const refresh = vi.fn().mockRejectedValue(new Error("x"));
    render({ key: "job-1", steps: running, refresh, staleAfterMs: 5_000 });
    await advance(5_000);
    expect(latest.reconnecting).toBe(true);
    render({ key: "job-2", steps: running, refresh, staleAfterMs: 5_000 });
    expect(latest.reconnecting).toBe(false);
    const calls = refresh.mock.calls.length;
    await advance(4_999);
    expect(refresh).toHaveBeenCalledTimes(calls);
    await advance(1);
    expect(refresh).toHaveBeenCalledTimes(calls + 1);
  });

  it("refresh が無い・無効・終端のときは何もしない", async () => {
    const refresh = vi.fn();
    render({ key: "job-1", steps: running });
    await advance(60_000);
    render({ key: "job-1", steps: running, refresh, enabled: false });
    await advance(60_000);
    expect(refresh).not.toHaveBeenCalled();
    render({ key: "job-1", steps: running, refresh, active: false });
    await advance(60_000);
    expect(refresh).not.toHaveBeenCalled();
    render({ key: null, steps: running, refresh });
    await advance(60_000);
    expect(refresh).not.toHaveBeenCalled();
  });
});
