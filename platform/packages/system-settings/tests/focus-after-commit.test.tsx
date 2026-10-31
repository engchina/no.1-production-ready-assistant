// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useFocusAfterCommit } from "../src/users-roles/shared";

// #424: 送信のエラーの後のフォーカス移動は、欄が操作できる状態（送信中の disabled が外れた状態）を
// commit した後に行う。requestAnimationFrame だと busy = false の commit より先に走ることがあり、
// disabled の欄への focus() が無視されていた（NL2SQL の e2e「ロールコード競合はコード欄へ結び付き…」が不安定だった）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let schedule: (focus: () => void) => void = () => undefined;

function Harness({ busy }: { busy: boolean }) {
  schedule = useFocusAfterCommit(!busy);
  return <input aria-label="ロールコード" disabled={busy} />;
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("useFocusAfterCommit", () => {
  it("送信中（ready = false）に予約したフォーカスは、操作できる状態を commit した後に一度だけ実行する", () => {
    const focus = vi.fn();
    act(() => root.render(<Harness busy />));
    schedule(focus);
    // 送信中のまま別の state が commit されても、欄は disabled なので移さない。
    act(() => root.render(<Harness busy />));
    expect(focus).not.toHaveBeenCalled();

    act(() => root.render(<Harness busy={false} />));
    expect(focus).toHaveBeenCalledTimes(1);
    // 実行済みの予約は残さない（後の再描画で入力中のフォーカスを奪わない）。
    act(() => root.render(<Harness busy={false} />));
    expect(focus).toHaveBeenCalledTimes(1);
  });

  it("操作できる状態で予約したときは、次の commit で実行する（送信前の入力検査）", () => {
    act(() => root.render(<Harness busy={false} />));
    const input = host.querySelector("input") as HTMLInputElement;
    schedule(() => input.focus());
    expect(document.activeElement).not.toBe(input);
    act(() => root.render(<Harness busy={false} />));
    expect(document.activeElement).toBe(input);
  });
});
