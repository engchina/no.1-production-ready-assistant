// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/app-shell/AppShell";
import { PageHeader, type PageHeaderAction } from "../src/components/app-shell/PageHeader";
import { Toaster } from "../src/components/ui/toast";
import { resolveToastPlacement } from "../src/lib/toast-placement";
import { toast, useToastStore } from "../src/store/toast-store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// #1728: 通知は画面幅ごとに決まった 1 か所に出し、ページの内容（PageHeader の操作の有無・幅・スクロール）で動かさない。
// 以前（#411）は PageHeader の操作の位置を測ってその左に置いたため、操作のある画面では見出しの中ほど、無い画面では右端に出た。
describe("resolveToastPlacement", () => {
  it("md 以上: 画面の右下（右端から 1rem、下端から --toast-offset-bottom）。上端には置かない", () => {
    const { mode, style } = resolveToastPlacement({ narrow: false });
    expect(mode).toBe("bottom-right");
    // 下端に固定された入力欄（`--toast-clear-bottom`）があれば、その上端から 1rem 上（#2103）。
    expect(style.bottom).toBe(
      "max(var(--toast-offset-bottom), calc(var(--toast-clear-bottom, 0px) + 1rem), env(safe-area-inset-bottom))"
    );
    expect(style.right).toBe("max(1rem, env(safe-area-inset-right))");
    expect(style.top).toBeUndefined();
    expect(style.left).toBeUndefined();
    expect(style.maxHeight).toBe(
      "calc(100dvh - max(var(--toast-offset-bottom), calc(var(--toast-clear-bottom, 0px) + 1rem), env(safe-area-inset-bottom)) - 1rem)"
    );
    // 下から現れる。
    expect(style["--toast-enter-y"]).toBe("0.5rem");
  });

  it("md 以上: 幅は固定にせず、内容に合わせて下限〜上限のトークンの間で広がる（#899）", () => {
    const { style } = resolveToastPlacement({ narrow: false });
    expect(style).not.toHaveProperty("width");
    expect(style.minWidth).toBe("min(var(--toast-width-min), calc(100vw - 2 * 1rem))");
    expect(style.maxWidth).toBe("min(var(--toast-width-max), calc(100vw - 2 * 1rem))");
  });

  it("md 未満: 上端の全幅。上端のバーに重ね、メニューのボタン（左の 0.5rem + 44px）を覆わない", () => {
    const { mode, style } = resolveToastPlacement({ narrow: true });
    expect(mode).toBe("top-bar");
    expect(style.top).toBe("max(0.5rem, env(safe-area-inset-top))");
    expect(style.maxHeight).toBe("min(var(--toast-max-height-narrow), calc(100dvh - max(0.5rem, env(safe-area-inset-top)) - 1rem))");
    expect(style.left).toBe("calc(1rem + var(--control-height-touch))");
    expect(style.right).toBe("max(1rem, env(safe-area-inset-right))");
    expect(style.bottom).toBeUndefined();
    expect(style.minWidth).toBeUndefined();
    expect(style.maxWidth).toBeUndefined();
    // 上から現れる。
    expect(style["--toast-enter-y"]).toBe("-0.5rem");
  });
});

let container: HTMLDivElement;
let root: Root;

function mockViewport(narrow: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: narrow,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

function render(node: ReactNode) {
  act(() => root.render(node));
}

function Page({ actions }: { actions: PageHeaderAction[] }) {
  return (
    <AppShell sidebar={<nav aria-label="サイドナビゲーション" />}>
      <PageHeader title="コメント管理" actions={actions} />
      <Toaster />
    </AppShell>
  );
}

const region = () => document.body.querySelector<HTMLElement>('[role="region"][aria-label="通知"]');

/** 通知の領域の置き場所（data 属性と inline style）。 */
function placementOf(el: HTMLElement) {
  return {
    mode: el.dataset.toastPlacement,
    top: el.style.top,
    bottom: el.style.bottom,
    left: el.style.left,
    right: el.style.right,
    minWidth: el.style.minWidth,
    maxWidth: el.style.maxWidth,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  useToastStore.getState().clear();
  useToastStore.getState().resume();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  act(() => useToastStore.getState().clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Toaster の置き場所（#1728）", () => {
  it("md 以上: ページの操作の有無・数によらず、同じ右下に出る", () => {
    mockViewport(false);
    const placements = [] as ReturnType<typeof placementOf>[];
    const variants: PageHeaderAction[][] = [
      [],
      [{ id: "save", kind: "primary", label: "保存" }],
      [
        { id: "refresh", kind: "utility", label: "最新情報を取得" },
        { id: "fetch", kind: "secondary", label: "DB 構造を再取得" },
        { id: "save", kind: "primary", label: "保存" },
      ],
    ];
    for (const actions of variants) {
      render(<Page actions={actions} />);
      act(() => {
        toast.success("保存しました");
      });
      placements.push(placementOf(region()!));
      act(() => useToastStore.getState().clear());
    }
    // happy-dom は `max(…, env(…))` の値を style に残さないので、値そのものは resolveToastPlacement のテストで確かめる。
    expect(placements[0]).toMatchObject({ mode: "bottom-right", top: "", left: "" });
    expect(placements[1]).toEqual(placements[0]);
    expect(placements[2]).toEqual(placements[0]);
    expect(region()!.style.getPropertyValue("--toast-enter-y")).toBe("0.5rem");
  });

  it("md 以上: スクロールしても位置を変えない", () => {
    mockViewport(false);
    render(<Page actions={[{ id: "save", kind: "primary", label: "保存" }]} />);
    act(() => {
      toast.success("保存しました");
    });
    const before = placementOf(region()!);
    act(() => {
      document.dispatchEvent(new Event("scroll"));
      window.dispatchEvent(new Event("resize"));
      vi.advanceTimersByTime(32);
    });
    expect(placementOf(region()!)).toEqual(before);
  });

  it("新しい通知は下に足す（DOM の順 = 読み上げ・Tab の順 = 見た目の上下の順）", () => {
    mockViewport(false);
    render(<Page actions={[]} />);
    act(() => {
      toast.success("1 件目");
      toast.info("2 件目");
    });
    const texts = Array.from(region()!.querySelectorAll('[role="status"]')).map((node) => node.textContent);
    expect(texts[0]).toContain("1 件目");
    expect(texts[1]).toContain("2 件目");
  });

  it("md 未満: 上端のバーに重ね、メニューのボタンを覆わない（ページの操作の有無によらない）", () => {
    mockViewport(true);
    for (const actions of [[], [{ id: "save", kind: "primary", label: "保存" }]] as PageHeaderAction[][]) {
      render(<Page actions={actions} />);
      act(() => {
        toast.success("保存しました");
      });
      const el = region()!;
      expect(el.dataset.toastPlacement).toBe("top-bar");
      expect(el.style.left).toBe("calc(1rem + var(--control-height-touch))");
      expect(el.style.bottom).toBe("");
      expect(el.style.getPropertyValue("--toast-enter-y")).toBe("-0.5rem");
      act(() => useToastStore.getState().clear());
    }
  });
});
