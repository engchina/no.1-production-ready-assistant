// @vitest-environment happy-dom
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TOAST_TOP_CLEARANCE_VAR, useToastTopClearance } from "../src/components/ui/use-toast-top-clearance";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
let toastBottom = 160;
let resized: () => void;
const disconnect = vi.fn();
function Harness({ enabled = true }) {
  const ref = useRef<HTMLDivElement>(null);
  useToastTopClearance(ref, enabled);
  return <div ref={ref} />;
}
const clearance = () => document.documentElement.style.getPropertyValue(TOAST_TOP_CLEARANCE_VAR);
beforeEach(() => {
  vi.useFakeTimers();
  disconnect.mockClear();
  toastBottom = 160;
  const bar = document.createElement("div");
  bar.dataset.navDrawerBar = "";
  document.body.appendChild(bar);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return { bottom: this === bar ? 49 : toastBottom } as DOMRect;
  });
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resized = callback; }
    observe() { /* サイズ変更はテストから通知する。 */ }
    disconnect = disconnect;
  });
});
afterEach(() => {
  act(() => root.unmount());
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
it("複数行の通知がバーからはみ出す分だけ本文の前に確保する", () => {
  act(() => root.render(<Harness />));
  expect(clearance()).toBe("calc(111px + var(--space-2))");
  toastBottom = 230;
  act(() => { resized(); vi.advanceTimersByTime(32); });
  expect(clearance()).toBe("calc(181px + var(--space-2))");
});
it("通知を閉じたとき・desktopへ広げたときに確保した高さを解放する", () => {
  act(() => root.render(<Harness />));
  act(() => root.render(<Harness enabled={false} />));
  expect(clearance()).toBe("");
  expect(disconnect).toHaveBeenCalled();
  act(() => root.render(<Harness />));
  expect(clearance()).not.toBe("");
  act(() => root.unmount());
  root = createRoot(container);
  expect(clearance()).toBe("");
});
it("バーに収まる通知では本文の高さを変えない", () => {
  toastBottom = 45;
  act(() => root.render(<Harness />));
  expect(clearance()).toBe("");
});
