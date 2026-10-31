// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useActionPending, type ActionPending } from "../src";

// #819: 「最新情報を取得」などは、押した処理の間だけ loading にする。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let latest: ActionPending;

function Harness() {
  latest = useActionPending();
  return null;
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe("useActionPending", () => {
  it("track に渡した処理の間だけ pending、重なった処理はすべて終わるまで pending", async () => {
    expect(latest.pending).toBe(false);
    const first = deferred();
    const second = deferred();
    let firstDone!: Promise<void>;
    let secondDone!: Promise<void>;
    act(() => {
      firstDone = latest.track(() => first.promise);
      secondDone = latest.track(() => second.promise);
    });
    expect(latest.pending).toBe(true);

    await act(async () => {
      first.resolve();
      await firstDone;
    });
    expect(latest.pending).toBe(true);

    await act(async () => {
      second.resolve();
      await secondDone;
    });
    expect(latest.pending).toBe(false);
  });

  it("失敗しても pending を戻し、例外はそのまま返す", async () => {
    const work = deferred();
    let done!: Promise<void>;
    act(() => {
      done = latest.track(() => work.promise);
    });
    expect(latest.pending).toBe(true);
    await act(async () => {
      work.reject(new Error("取得に失敗"));
      await expect(done).rejects.toThrow("取得に失敗");
    });
    expect(latest.pending).toBe(false);
  });
});
