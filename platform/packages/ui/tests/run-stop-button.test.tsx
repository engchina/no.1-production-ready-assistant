// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { SendHorizontal } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunStopButton, isRepeatedActivationKey, runStopClickAction } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("runStopClickAction（#413 / #805）", () => {
  it("待機中の 1 回のクリックとキーボードの暗黙のクリックは実行する", () => {
    expect(runStopClickAction({ running: false, clickCount: 1 })).toBe("run");
    expect(runStopClickAction({ running: false, clickCount: 0 })).toBe("run");
  });

  it("実行中の 1 回のクリックとキーボードの暗黙のクリックは停止する", () => {
    expect(runStopClickAction({ running: true, clickCount: 1 })).toBe("stop");
    expect(runStopClickAction({ running: true, clickCount: 0 })).toBe("stop");
  });

  it("ダブルクリックの 2 回目以降は、実行中でも停止しない", () => {
    expect(runStopClickAction({ running: true, clickCount: 2 })).toBe("ignore");
    expect(runStopClickAction({ running: true, clickCount: 3 })).toBe("ignore");
    expect(runStopClickAction({ running: false, clickCount: 2 })).toBe("ignore");
  });

  it("実行できない間は何もしない。実行中の停止は実行できない条件に左右されない", () => {
    expect(runStopClickAction({ running: false, runDisabled: true, clickCount: 1 })).toBe("ignore");
    expect(runStopClickAction({ running: true, runDisabled: true, clickCount: 1 })).toBe("stop");
  });
});

describe("isRepeatedActivationKey", () => {
  it("押し続けた Enter / Space の繰り返しだけを止める", () => {
    expect(isRepeatedActivationKey({ key: "Enter", repeat: true })).toBe(true);
    expect(isRepeatedActivationKey({ key: " ", repeat: true })).toBe(true);
    expect(isRepeatedActivationKey({ key: "Enter", repeat: false })).toBe(false);
    expect(isRepeatedActivationKey({ key: "Enter" })).toBe(false);
    expect(isRepeatedActivationKey({ key: "Tab", repeat: true })).toBe(false);
  });
});

describe("RunStopButton（#805）", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  function render(props: Partial<Parameters<typeof RunStopButton>[0]> & { running: boolean }) {
    const onRun = props.onRun ?? vi.fn();
    const onStop = props.onStop ?? vi.fn();
    act(() =>
      root.render(
        <RunStopButton
          runLabel="送信"
          stopLabel="停止"
          runIcon={SendHorizontal}
          testId="run-stop"
          {...props}
          onRun={onRun}
          onStop={onStop}
        />
      )
    );
    return { onRun, onStop, button: host.querySelector<HTMLButtonElement>("[data-testid=run-stop]")! };
  }

  function click(button: HTMLButtonElement, detail = 1) {
    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail }));
    });
  }

  it("待機中は primary の実行、実行中は同じ要素のまま secondary の停止になる", () => {
    const idle = render({ running: false });
    expect(idle.button.dataset.state).toBe("idle");
    expect(idle.button.className).toContain("bg-accent-emphasis");
    expect(idle.button.textContent).toContain("送信");
    const running = render({ running: true });
    // 要素が替わらない（フォーカスが外れない）。
    expect(running.button).toBe(idle.button);
    expect(running.button.dataset.state).toBe("running");
    expect(running.button.className).not.toContain("bg-accent-emphasis");
    expect(running.button.className).toContain("bg-surface");
  });

  it("見えないラベルは invisible で重ね、幅を長い方にそろえる", () => {
    const { button } = render({ running: true });
    const [runLabel, stopLabel] = Array.from(button.querySelectorAll(".col-start-1"));
    expect(runLabel?.className).toContain("invisible");
    expect(stopLabel?.className).not.toContain("invisible");
  });

  it("クリックは待機中に実行、実行中に停止を呼ぶ。ダブルクリックの 2 回目は無視する", () => {
    const idle = render({ running: false });
    click(idle.button);
    expect(idle.onRun).toHaveBeenCalledTimes(1);
    const running = render({ running: true });
    click(running.button, 2);
    expect(running.onStop).not.toHaveBeenCalled();
    click(running.button);
    expect(running.onStop).toHaveBeenCalledTimes(1);
  });

  it("実行できない間は aria-disabled（フォーカスを受ける）で、押しても実行しない", () => {
    const { button, onRun } = render({ running: false, runDisabled: true });
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.disabled).toBe(false);
    click(button);
    expect(onRun).not.toHaveBeenCalled();
  });

  it("実行中は runDisabled でも停止できる", () => {
    const { button, onStop } = render({ running: true, runDisabled: true });
    expect(button.getAttribute("aria-disabled")).toBeNull();
    click(button);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("押し続けた Enter の繰り返しの keydown は既定の動作を止める", () => {
    const { button } = render({ running: true });
    const event = new KeyboardEvent("keydown", { key: "Enter", repeat: true, bubbles: true, cancelable: true });
    act(() => {
      button.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
  });
});
