// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadStylesAfterFirstRender } from "../src/lib/deferred-styles";

const OPTIONS = { serviceName: "production-ready-platform" } as const;

/** MutationObserver の通知（microtask）を流す。 */
const flushMicrotasks = () => vi.advanceTimersByTimeAsync(0);

/** requestAnimationFrame の待ち。`nextFrame` で 1 frame を進める（frame が進まない場合も表せる）。 */
let frames: FrameRequestCallback[] = [];
const nextFrame = async () => {
  for (const callback of frames.splice(0)) callback(0);
  await vi.advanceTimersByTimeAsync(0);
};

beforeEach(() => {
  frames = [];
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("loadStylesAfterFirstRender（最初の描画の後に CSS を読む。#3305）", () => {
  it("root に最初の子が入るまで読まず、入った後の次の frame の後に 1 回だけ読む", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const load = vi.fn(() => Promise.resolve());
    loadStylesAfterFirstRender(root, load, OPTIONS);

    await vi.advanceTimersByTimeAsync(5000);
    await nextFrame();
    expect(load).not.toHaveBeenCalled();

    root.append(document.createElement("main"));
    await flushMicrotasks();
    expect(load).not.toHaveBeenCalled();

    // 次の frame（rAF）→ その後の task（setTimeout 0）で読む。
    await nextFrame();
    expect(load).toHaveBeenCalledTimes(1);

    root.append(document.createElement("aside"));
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("既に描画済みの root なら、そのまま次の frame の後に読む", async () => {
    const root = document.createElement("div");
    root.append(document.createElement("main"));
    const load = vi.fn(() => Promise.resolve());
    loadStylesAfterFirstRender(root, load, OPTIONS);
    expect(load).not.toHaveBeenCalled();
    await nextFrame();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("frame が進まない（背景のタブ）ときも fallbackMs の後に読む", async () => {
    const root = document.createElement("div");
    root.append(document.createElement("main"));
    const load = vi.fn(() => Promise.resolve());
    loadStylesAfterFirstRender(root, load, { ...OPTIONS, fallbackMs: 300 });
    await vi.advanceTimersByTimeAsync(299);
    expect(load).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(1);
    await nextFrame();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("読む前に取り消せる", async () => {
    const root = document.createElement("div");
    root.append(document.createElement("main"));
    const load = vi.fn(() => Promise.resolve());
    const cancel = loadStylesAfterFirstRender(root, load, OPTIONS);
    cancel();
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).not.toHaveBeenCalled();
  });

  it("読み込みの失敗は投げず、Error の原文を出さない WARNING の診断だけを出す", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const root = document.createElement("div");
    root.append(document.createElement("main"));
    loadStylesAfterFirstRender(root, () => Promise.reject(new TypeError("chunk path secret")), OPTIONS);
    await vi.advanceTimersByTimeAsync(5000);
    expect(warn).toHaveBeenCalledTimes(1);
    const wire = JSON.parse(String(warn.mock.calls[0]?.[0])) as Record<string, unknown>;
    expect(wire).toMatchObject({ level: "WARNING", event: "deferred_styles_failed", exception_type: "TypeError" });
    expect(JSON.stringify(wire)).not.toContain("secret");
  });
});
