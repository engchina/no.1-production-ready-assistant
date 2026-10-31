// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { COPY_FEEDBACK_MS, CopyButton, type CopyButtonProps, useToastStore } from "../src";

// #2099: コピーのボタンは共有の CopyButton 1 つ。成功はアイコンの Check と読み上げ、失敗は Toast か同じ場所。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let writeText: ReturnType<typeof vi.fn>;

function render(props: Partial<CopyButtonProps> = {}) {
  act(() =>
    root.render(
      <CopyButton
        text="SELECT 1 FROM DUAL"
        label="SQL をコピー"
        copiedMessage="SQL をコピーしました"
        failedMessage="SQL をコピーできませんでした。"
        testId="copy"
        {...props}
      />
    )
  );
}

function button() {
  return host.querySelector<HTMLButtonElement>('[data-testid="copy"]')!;
}

function status() {
  return host.querySelector('[role="status"]')!;
}

async function click() {
  await act(async () => {
    button().click();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  writeText = vi.fn(async () => undefined);
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  useToastStore.setState({ toasts: [] });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("CopyButton", () => {
  it("押すとコピーし、一時的に Check にして読み上げる（ラベルは変えず、成功の Toast は出さない）", async () => {
    render();
    expect(button().textContent).toBe("SQL をコピー");
    expect(button().type).toBe("button");
    expect(button().dataset.copyState).toBe("idle");
    expect(status().getAttribute("aria-live")).toBe("polite");
    // 画面の左上に固定し、本文の横はみ出し・スクロール領域からの抜け出しを作らない。
    expect(status().classList).toContain("sr-only");
    expect(status().classList).toContain("fixed!");
    expect(status().classList).toContain("left-0");
    expect(status().classList).toContain("top-0");
    expect(status().textContent).toBe("");

    await click();

    expect(writeText).toHaveBeenCalledWith("SELECT 1 FROM DUAL");
    expect(button().dataset.copyState).toBe("copied");
    expect(button().querySelector("svg")?.classList.contains("lucide-check")).toBe(true);
    expect(button().textContent).toBe("SQL をコピー");
    expect(status().textContent).toBe("SQL をコピーしました");
    expect(useToastStore.getState().toasts).toHaveLength(0);

    act(() => vi.advanceTimersByTime(COPY_FEEDBACK_MS));
    expect(button().dataset.copyState).toBe("idle");
    expect(button().querySelector("svg")?.classList.contains("lucide-copy")).toBe(true);
    expect(status().textContent).toBe("");
  });

  it("失敗は既定で Toast（error）に出す", async () => {
    writeText.mockRejectedValueOnce(new Error("denied"));
    render();

    await click();

    expect(button().dataset.copyState).toBe("idle");
    const toasts = useToastStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({ tone: "danger", message: "SQL をコピーできませんでした。" });
  });

  it("onCopyError を渡すと Toast を出さず、文言を呼び出し側に渡す", async () => {
    writeText.mockRejectedValueOnce(new Error("denied"));
    const onCopyError = vi.fn();
    const onCopied = vi.fn();
    render({ onCopyError, onCopied });

    await click();
    expect(onCopyError).toHaveBeenCalledWith("SQL をコピーできませんでした。");
    expect(useToastStore.getState().toasts).toHaveLength(0);

    await click();
    expect(onCopied).toHaveBeenCalledTimes(1);
  });

  it("関数の text は押したときに求め、Promise の間は loading にする", async () => {
    let resolveText: (value: string) => void = () => undefined;
    const text = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveText = resolve;
        })
    );
    render({ text });
    expect(text).not.toHaveBeenCalled();

    act(() => button().click());
    expect(text).toHaveBeenCalledTimes(1);
    expect(button().getAttribute("aria-busy")).toBe("true");

    await act(async () => resolveText("# 見出し"));
    expect(writeText).toHaveBeenCalledWith("# 見出し");
    expect(button().getAttribute("aria-busy")).toBeNull();
    expect(button().dataset.copyState).toBe("copied");
  });

  it("iconOnly ではラベルを aria-label にし、文字を出さない", () => {
    render({ iconOnly: true });
    expect(button().getAttribute("aria-label")).toBe("SQL をコピー");
    expect(button().textContent).toBe("");
  });
});
