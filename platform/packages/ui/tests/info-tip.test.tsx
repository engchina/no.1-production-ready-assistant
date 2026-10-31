// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InfoTip, INFO_TIP_SHOW_DELAY_MS } from "../src/components/ui/info-tip";
import { createTooltipController, TOOLTIP_HIDE_DELAY_MS } from "../src/components/ui/tooltip";

// #901: 補足の説明を常設せず、info アイコンから吹き出しで出す（hover / focus / 押す・Esc・外側で閉じる）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("InfoTip の開閉（createTooltipController の press / dismiss）", () => {
  let changes: Array<[boolean, string | null]>;
  let made: Array<ReturnType<typeof createTooltipController>> = [];
  let clock = Date.now();
  const make = (options: { coarse?: boolean } = {}) => {
    const controller = createTooltipController({
      onOpenChange: (open, reason) => changes.push([open, reason]),
      isCoarsePointer: () => Boolean(options.coarse),
      showDelayMs: INFO_TIP_SHOW_DELAY_MS,
    });
    made.push(controller);
    return controller;
  };

  beforeEach(() => {
    vi.useFakeTimers();
    clock += 3_600_000;
    vi.setSystemTime(clock);
    changes = [];
  });
  afterEach(() => {
    for (const controller of made) controller.dispose();
    made = [];
    vi.useRealTimers();
  });

  it("押すと開いたまま固定し、ポインタが離れても閉じない。もう一度押すと閉じる", () => {
    const tip = make();
    tip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(INFO_TIP_SHOW_DELAY_MS);
    expect(changes).toEqual([[true, "hover"]]);
    tip.press();
    expect(changes.at(-1)).toEqual([true, "press"]);
    tip.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS * 5);
    expect(tip.isOpen()).toBe(true);

    tip.press();
    expect(tip.isOpen()).toBe(false);
  });

  it("押して閉じた後は、ポインタが離れるまでホバーで出し直さない", () => {
    const tip = make();
    tip.pointerEnterTrigger("mouse");
    tip.press();
    tip.press();
    vi.advanceTimersByTime(INFO_TIP_SHOW_DELAY_MS * 3);
    expect(tip.isOpen()).toBe(false);
    tip.pointerLeaveTrigger();
    tip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(INFO_TIP_SHOW_DELAY_MS);
    expect(tip.isOpen()).toBe(true);
  });

  it("タッチ端末（pointer: coarse）でも押せば開く（ホバー・フォーカスでは開かない）", () => {
    const tip = make({ coarse: true });
    tip.pointerEnterTrigger("touch");
    tip.focus(true);
    expect(tip.isOpen()).toBe(false);
    tip.press();
    expect(tip.isOpen()).toBe(true);
    expect(changes).toEqual([[true, "press"]]);
  });

  it("固定して開いたものも、Escape・外側を押す・フォーカスを外すで閉じる", () => {
    const escaped = make();
    escaped.press();
    expect(escaped.escape()).toBe(true);
    expect(escaped.isOpen()).toBe(false);

    const dismissed = make();
    dismissed.press();
    dismissed.dismiss();
    expect(dismissed.isOpen()).toBe(false);

    const blurred = make();
    blurred.focus(true);
    blurred.press();
    blurred.blur();
    expect(blurred.isOpen()).toBe(false);
  });

  it("吹き出しの中を押してフォーカスが外れても閉じない（文を選べる）", () => {
    const tip = make();
    tip.press();
    tip.pointerEnterTooltip();
    tip.blur();
    expect(tip.isOpen()).toBe(true);
  });

  it("キーボードはフォーカスですぐ開き、Enter（press）で固定、もう一度で閉じる", () => {
    const tip = make();
    tip.focus(true);
    expect(changes).toEqual([[true, "focus"]]);
    tip.press();
    expect(changes.at(-1)).toEqual([true, "press"]);
    tip.press();
    expect(tip.isOpen()).toBe(false);
  });

  it("別の吹き出しが開いたら、固定して開いていたものも閉じる（画面に 1 つだけ）", () => {
    const first = make();
    first.press();
    const second = make();
    second.focus(true);
    expect(first.isOpen()).toBe(false);
    expect(second.isOpen()).toBe(true);
  });
});

describe("InfoTip の DOM（ボタン・読み上げ・開閉）", () => {
  let container: HTMLDivElement;
  let root: Root;
  const button = () => document.querySelector<HTMLButtonElement>("button[data-info-tip]")!;
  const bubble = () => document.querySelector<HTMLDivElement>('[data-testid="tip"]')!;

  beforeEach(() => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() =>
      root.render(
        <>
          <span>回答するモデル</span>
          <InfoTip label="回答するモデルの説明" content="未選択ならテキストモデルで回答します。" contentTestId="tip" />
          <button type="button" data-testid="outside">
            外側
          </button>
        </>
      )
    );
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("info アイコンのボタン（読み上げ名と説明の結び付け）で、閉じている間も説明を読める", () => {
    expect(button().getAttribute("type")).toBe("button");
    expect(button().getAttribute("aria-label")).toBe("回答するモデルの説明");
    expect(button().querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    const describedBy = button().getAttribute("aria-describedby")!;
    expect(document.getElementById(describedBy)).toBe(bubble());
    expect(bubble().getAttribute("role")).toBe("tooltip");
    expect(bubble().hidden).toBe(true);
    expect(bubble().textContent).toBe("未選択ならテキストモデルで回答します。");
    expect(button().getAttribute("data-state")).toBe("closed");
  });

  it("押すと開き、外側を押すと閉じる", () => {
    act(() => button().click());
    expect(bubble().hidden).toBe(false);
    expect(bubble().getAttribute("data-tooltip-reason")).toBe("press");
    expect(button().getAttribute("data-state")).toBe("open");

    act(() => {
      document
        .querySelector('[data-testid="outside"]')!
        .dispatchEvent(new Event("pointerdown", { bubbles: true, cancelable: true }));
    });
    expect(bubble().hidden).toBe(true);
  });

  it("押して開いた後の Escape は吹き出しだけを閉じ、外へ伝えない", () => {
    const outer = vi.fn();
    window.addEventListener("keydown", outer);
    act(() => button().click());
    act(() => {
      button().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    expect(bubble().hidden).toBe(true);
    expect(outer).not.toHaveBeenCalled();
    window.removeEventListener("keydown", outer);
  });
});
