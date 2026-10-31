// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ViewportScrollRegion } from "../src/components/data/viewport-scroll-region";

afterEach(() => vi.restoreAllMocks());

it("高い desktop ではスクロール復元を差し引いて上限を伸ばす", () => {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: true } as MediaQueryList);
  vi.spyOn(window, "innerHeight", "get").mockReturnValue(1400);
  vi.spyOn(window, "scrollY", "get").mockReturnValue(100);
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({ top: 200 } as DOMRect);
  document.documentElement.style.fontSize = "14px";
  act(() => root.render(<ViewportScrollRegion aria-label="合成一覧">本文</ViewportScrollRegion>));
  const region = host.firstElementChild as HTMLElement;
  expect(parseFloat(region.style.maxHeight)).toBeCloseTo((1400 - 200 - 100 - 84) / 14);
  expect(region.tabIndex).toBe(0);
  expect(region.classList.contains("desktop")).toBe(false);
  act(() => root.unmount());
  host.remove();
});

it("375px 相当では共有の表示密度を維持する", () => {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: false } as MediaQueryList);
  act(() => root.render(<ViewportScrollRegion aria-label="合成一覧">本文</ViewportScrollRegion>));
  const region = host.firstElementChild as HTMLElement;
  expect(region.style.maxHeight).toBe("");
  expect(region.className).toContain("max-h-[17.5rem]");
  act(() => root.unmount());
  host.remove();
});
