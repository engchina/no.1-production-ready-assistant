import { X } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Button } from "../src/components/ui/button";
import {
  computeTooltipLayout,
  createTooltipController,
  Tooltip,
  TOOLTIP_HIDE_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  tooltipDescribesTrigger,
} from "../src/components/ui/tooltip";

// #372: 共有の Tooltip（WAI-ARIA APG の Tooltip パターン）。

function openTag(html: string) {
  return html.slice(0, html.indexOf(">") + 1);
}

describe("Tooltip の位置（computeTooltipLayout）", () => {
  const desktop = { viewportWidth: 1280, viewportHeight: 800 };
  // 32 × 32 のアイコンボタン。
  const trigger = (top: number, left: number) => ({ top, bottom: top + 32, left, right: left + 32, width: 32 });

  it("既定は上に出し、トリガーの中央にそろえる", () => {
    const layout = computeTooltipLayout({ ...desktop, triggerRect: trigger(300, 600), tooltipWidth: 80, tooltipHeight: 24 });
    expect(layout).toEqual({ placement: "top", left: 600 + 16 - 40, top: 300 - 4 - 24 });
  });

  it("画面の上端で上に入らなければ下に反転する", () => {
    const layout = computeTooltipLayout({ ...desktop, triggerRect: trigger(12, 600), tooltipWidth: 80, tooltipHeight: 24 });
    expect(layout.placement).toBe("bottom");
    expect(layout.top).toBe(12 + 32 + 4);
  });

  it("下を指定しても、画面の下端で入らなければ上に反転する", () => {
    const layout = computeTooltipLayout({
      ...desktop,
      triggerRect: trigger(760, 600),
      tooltipWidth: 80,
      tooltipHeight: 24,
      placement: "bottom",
    });
    expect(layout.placement).toBe("top");
    expect(layout.top).toBe(760 - 4 - 24);
  });

  it("375px の左右の端では、画面の内側（左右 8px）にずらす", () => {
    const phone = { viewportWidth: 375, viewportHeight: 812 };
    const left = computeTooltipLayout({ ...phone, triggerRect: trigger(300, 0), tooltipWidth: 160, tooltipHeight: 24 });
    expect(left.left).toBe(8);
    const right = computeTooltipLayout({ ...phone, triggerRect: trigger(300, 343), tooltipWidth: 160, tooltipHeight: 24 });
    expect(right.left).toBe(375 - 8 - 160);
  });

  it("上下どちらにも入らなければ広い側に出し、画面の内側に収める", () => {
    const layout = computeTooltipLayout({
      viewportWidth: 1280,
      viewportHeight: 120,
      triggerRect: trigger(30, 600),
      tooltipWidth: 80,
      tooltipHeight: 60,
    });
    expect(layout.placement).toBe("bottom");
    expect(layout.top).toBe(120 - 8 - 60);
  });
});

describe("Tooltip の開閉（createTooltipController）", () => {
  let changes: Array<[boolean, string | null]>;
  // 偽のタイマーの時計はテストごとに実時間から始まるため、前のテストより十分に先へ進めておく。
  let clock = Date.now();
  // 開いている吹き出しは全 Tooltip で 1 つだけなので、テストの終わりに破棄して次のテストへ持ち越さない。
  let made: Array<ReturnType<typeof createTooltipController>> = [];
  const make = (options: { coarse?: boolean } = {}) => {
    const controller = createTooltipController({
      onOpenChange: (open, reason) => changes.push([open, reason]),
      isCoarsePointer: () => Boolean(options.coarse),
    });
    made.push(controller);
    return controller;
  };

  beforeEach(() => {
    vi.useFakeTimers();
    // 前のテストで閉じた時刻から十分に離す（隣のトリガーへ移ったときの「待たずに出す」を効かせない）。
    clock += 3_600_000;
    vi.setSystemTime(clock);
    changes = [];
  });
  afterEach(() => {
    for (const controller of made) controller.dispose();
    made = [];
    vi.useRealTimers();
  });

  it("ホバーは待ち時間の後に出し、待ち時間の前に離れたら出さない", () => {
    const tooltip = make();
    tooltip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS - 1);
    expect(tooltip.isOpen()).toBe(false);
    vi.advanceTimersByTime(1);
    expect(tooltip.isOpen()).toBe(true);
    expect(changes).toEqual([[true, "hover"]]);

    const passing = make();
    passing.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS / 2);
    passing.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    expect(passing.isOpen()).toBe(false);
  });

  it("キーボードのフォーカスはすぐに出し、マウスで押したときのフォーカスでは出さない", () => {
    const keyboard = make();
    keyboard.focus(true);
    expect(keyboard.isOpen()).toBe(true);
    expect(changes).toEqual([[true, "focus"]]);
    keyboard.blur();
    expect(keyboard.isOpen()).toBe(false);

    const mouse = make();
    mouse.focus(false);
    expect(mouse.isOpen()).toBe(false);
  });

  it("Escape で閉じ、ポインタが離れるまで（またはフォーカスし直すまで）出し直さない", () => {
    const tooltip = make();
    tooltip.focus(true);
    expect(tooltip.escape()).toBe(true);
    expect(tooltip.isOpen()).toBe(false);
    // 閉じているときの Escape は握りつぶさない（囲むモーダルに渡す）。
    expect(tooltip.escape()).toBe(false);

    tooltip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS * 2);
    expect(tooltip.isOpen()).toBe(false);

    tooltip.pointerLeaveTrigger();
    tooltip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltip.isOpen()).toBe(true);
  });

  it("トリガーから吹き出しへポインタを移しても消えず（WCAG 1.4.13）、両方から離れたら猶予の後に閉じる", () => {
    const tooltip = make();
    tooltip.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    tooltip.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS / 2);
    tooltip.pointerEnterTooltip();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS * 5);
    expect(tooltip.isOpen()).toBe(true);

    tooltip.pointerLeaveTooltip();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS - 1);
    expect(tooltip.isOpen()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(tooltip.isOpen()).toBe(false);
  });

  it("フォーカス中はポインタが離れても閉じない", () => {
    const tooltip = make();
    tooltip.focus(true);
    tooltip.pointerEnterTrigger("mouse");
    tooltip.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS * 5);
    expect(tooltip.isOpen()).toBe(true);
  });

  it("トリガーを押したら閉じ、ポインタが離れるまで出さない（押した結果を隠さない）", () => {
    const tooltip = make();
    tooltip.pointerEnterTrigger("mouse");
    tooltip.pointerDown();
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS * 2);
    expect(tooltip.isOpen()).toBe(false);
    expect(changes).toEqual([]);
  });

  it("タッチ端末（pointer: coarse）とタッチのポインタでは出さない", () => {
    const coarse = make({ coarse: true });
    coarse.pointerEnterTrigger("mouse");
    coarse.focus(true);
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS * 2);
    expect(coarse.isOpen()).toBe(false);

    const touch = make();
    touch.pointerEnterTrigger("touch");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS * 2);
    expect(touch.isOpen()).toBe(false);
    expect(changes).toEqual([]);
  });

  it("ホバーで開いた Tooltip が閉じた直後に隣のトリガーへ移ったら、待たずに出す", () => {
    const first = make();
    first.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    first.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS);
    expect(first.isOpen()).toBe(false);

    const second = make();
    second.pointerEnterTrigger("mouse");
    expect(second.isOpen()).toBe(true);

    // 時間が空いたら、また待ち時間を置く。
    second.pointerLeaveTrigger();
    vi.advanceTimersByTime(TOOLTIP_HIDE_DELAY_MS + TOOLTIP_SKIP_DELAY_MS + 1);
    const third = make();
    third.pointerEnterTrigger("mouse");
    expect(third.isOpen()).toBe(false);
  });

  it("吹き出しは 1 つだけ出し、別の Tooltip が開いたら先に開いていたものを閉じる", () => {
    // ポインタを置いたままのボタンの吹き出しは、キーボードで別のボタンへ移ったら閉じる（#655）。
    const hovered = make();
    hovered.pointerEnterTrigger("mouse");
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    expect(hovered.isOpen()).toBe(true);

    const focused = make();
    focused.focus(true);
    expect(focused.isOpen()).toBe(true);
    expect(hovered.isOpen()).toBe(false);
    expect(changes).toEqual([
      [true, "hover"],
      [false, null],
      [true, "focus"],
    ]);

    // 閉じた側は、ポインタが入り直せばまた出せる。そのときはフォーカスの吹き出しを閉じる。
    hovered.pointerLeaveTrigger();
    hovered.pointerEnterTrigger("mouse");
    expect(hovered.isOpen()).toBe(true);
    expect(focused.isOpen()).toBe(false);
  });
});

describe("Tooltip の読み上げ（aria-describedby）", () => {
  it("読み上げ名と違う文言だけを説明として結び付ける", () => {
    expect(tooltipDescribesTrigger("閉じる", "閉じる")).toBe(false);
    expect(tooltipDescribesTrigger(" 閉じる ", "閉じる")).toBe(false);
    expect(tooltipDescribesTrigger("前のページ（PageUp）", "前のページ")).toBe(true);
    expect(tooltipDescribesTrigger("前のページ", undefined)).toBe(true);
  });

  it("iconOnly の Button は既定で aria-label と同じ文言を出し、aria-describedby は付けず title も付けない", () => {
    const tag = openTag(
      renderToStaticMarkup(<Button variant="ghost" iconOnly icon={X} aria-label="閉じる" title="閉じる" />)
    );
    expect(tag).toContain('aria-label="閉じる"');
    expect(tag).not.toContain("aria-describedby");
    expect(tag).not.toContain("title=");
  });

  it("aria-label と違う文言は aria-describedby で結び付ける（既存の説明も残す）", () => {
    const tag = openTag(
      renderToStaticMarkup(
        <Button
          variant="ghost"
          iconOnly
          icon={X}
          aria-label="前のページ"
          aria-describedby="hint"
          tooltip="前のページ（PageUp）"
        />
      )
    );
    expect(tag).toMatch(/aria-describedby="hint \S+"/);
  });

  it("tooltip={false} では出さず、title はそのまま渡す。iconOnly でないボタンは既定で出さない", () => {
    const optOut = openTag(
      renderToStaticMarkup(<Button iconOnly icon={X} aria-label="閉じる" tooltip={false} title="閉じる" />)
    );
    expect(optOut).toContain('title="閉じる"');
    const labelled = openTag(renderToStaticMarkup(<Button icon={X} aria-describedby="hint">保存</Button>));
    expect(labelled).toContain('aria-describedby="hint"');
  });

  it("Tooltip を直接使うと、トリガーに説明を結び付ける（disabled では外す）", () => {
    const described = openTag(
      renderToStaticMarkup(
        <Tooltip content="候補の出現数: 3">
          <button type="button">売上</button>
        </Tooltip>
      )
    );
    expect(described).toMatch(/aria-describedby="\S+"/);
    const disabled = openTag(
      renderToStaticMarkup(
        <Tooltip content="候補の出現数: 3" disabled>
          <button type="button">売上</button>
        </Tooltip>
      )
    );
    expect(disabled).not.toContain("aria-describedby");
    // describe={false}: 読み上げ名に同じ内容が入っているときは結び付けない（#421 の RowTitleButton の全文表示）。
    const visualOnly = openTag(
      renderToStaticMarkup(
        <Tooltip content="候補の出現数: 3" describe={false}>
          <button type="button">売上</button>
        </Tooltip>
      )
    );
    expect(visualOnly).not.toContain("aria-describedby");
  });
});
