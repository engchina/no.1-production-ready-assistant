// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SideSheet } from "../src/components/ui/side-sheet";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function Harness({ onEscapeInside = false }: { onEscapeInside?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" data-testid="opener" aria-expanded={open} onClick={() => setOpen(true)}>
        会話の履歴
      </button>
      <SideSheet
        open={open}
        onClose={() => setOpen(false)}
        title="会話の履歴"
        closeLabel="会話の履歴を閉じる"
        id="history-sheet"
        data-testid="sheet"
      >
        <button type="button" data-testid="item">
          会話 1
        </button>
        <input
          data-testid="rename"
          onKeyDown={(event) => {
            // 中の部品が自分で処理した Escape（名前の編集の取消など）。
            if (onEscapeInside && event.key === "Escape") event.preventDefault();
          }}
        />
      </SideSheet>
    </>
  );
}

const $ = <T extends Element = HTMLElement>(selector: string) => document.querySelector<T>(selector)!;
const sheet = () => $<HTMLDivElement>('[data-testid="sheet"]');
const opener = () => $<HTMLButtonElement>('[data-testid="opener"]');
const closeButton = () => sheet().querySelector<HTMLButtonElement>('button[aria-label="会話の履歴を閉じる"]')!;
const click = (target: Element) =>
  act(() => {
    (target as HTMLElement).click();
  });
const key = (target: Element, init: KeyboardEventInit) =>
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  });

beforeEach(() => {
  // タッチ端末として描く（閉じるボタンの Tooltip を出さない）。Tooltip が開いている間の 1 回目の Escape は
  // 吹き出しだけを閉じる（#372）ため、ここではシートの Escape だけを確かめる。
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe("SideSheet（#664）", () => {
  it("閉じている間も body に描き、inert で隠す。名前は見出し", () => {
    act(() => root.render(<Harness />));
    expect(sheet().parentElement).toBe(document.body);
    expect(sheet().getAttribute("role")).toBe("dialog");
    expect(sheet().getAttribute("aria-modal")).toBe("true");
    expect(sheet().id).toBe("history-sheet");
    expect(sheet().hasAttribute("inert")).toBe(true);
    expect(sheet().getAttribute("data-state")).toBe("closed");
    const titleId = sheet().getAttribute("aria-labelledby")!;
    expect(document.getElementById(titleId)?.textContent).toBe("会話の履歴");
  });

  it("開くと閉じるボタンへフォーカスし、Escape で閉じて開く前の要素へフォーカスを戻す", () => {
    act(() => root.render(<Harness />));
    opener().focus();
    click(opener());
    expect(sheet().hasAttribute("inert")).toBe(false);
    expect(sheet().getAttribute("data-state")).toBe("open");
    expect(document.activeElement).toBe(closeButton());

    key(document.activeElement!, { key: "Escape" });
    expect(sheet().hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(opener());
  });

  it("閉じるボタン・scrim のタップで閉じる", () => {
    act(() => root.render(<Harness />));
    click(opener());
    click(closeButton());
    expect(opener().getAttribute("aria-expanded")).toBe("false");

    click(opener());
    click($('[data-testid="sheet-scrim"]'));
    expect(opener().getAttribute("aria-expanded")).toBe("false");
  });

  it("Tab / Shift+Tab はシートの端で反対の端へ回る", () => {
    act(() => root.render(<Harness />));
    click(opener());
    const close = closeButton();
    key(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe($('[data-testid="rename"]'));
    key(document.activeElement!, { key: "Tab" });
    expect(document.activeElement).toBe(close);
  });

  it("中の部品が処理した Escape（preventDefault 済み）では閉じない", () => {
    act(() => root.render(<Harness onEscapeInside />));
    click(opener());
    const input = $<HTMLInputElement>('[data-testid="rename"]');
    input.focus();
    key(input, { key: "Escape" });
    expect(sheet().hasAttribute("inert")).toBe(false);
  });

  it("size の既定は 22rem、wide は sm 以上 64rem・sm 未満は全画面の幅のクラスを持つ（#1154）", () => {
    act(() =>
      root.render(
        <>
          <SideSheet open={false} onClose={() => {}} title="既定" closeLabel="閉じる" data-testid="default-sheet">
            <p>既定</p>
          </SideSheet>
          <SideSheet open={false} onClose={() => {}} title="結果" closeLabel="閉じる" size="wide" side="right" data-testid="wide-sheet">
            <p>結果</p>
          </SideSheet>
        </>
      )
    );
    const defaultClasses = $('[data-testid="default-sheet"]').className.split(/\s+/);
    expect(defaultClasses).toContain("w-[22rem]");
    const wideClasses = $('[data-testid="wide-sheet"]').className.split(/\s+/);
    expect(wideClasses).toEqual(expect.arrayContaining(["w-full", "max-w-full", "sm:w-[64rem]", "sm:max-w-[calc(100vw-3.5rem)]"]));
    expect(wideClasses).not.toContain("w-[22rem]");
  });
  it("size=md は 40rem（画面幅 − 3.5rem まで）、pager は見出しの行に前へ / 次へを出す（#2281）", () => {
    const onPrevious = vi.fn();
    act(() =>
      root.render(
        <SideSheet
          open
          onClose={() => {}}
          title="ケースの詳細"
          closeLabel="閉じる"
          size="md"
          side="right"
          pager={{ onPrevious, onNext: null, testId: "case-pager" }}
          data-testid="md-sheet"
        >
          <p>詳細</p>
        </SideSheet>
      )
    );
    const classes = $('[data-testid="md-sheet"]').className.split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(["w-[40rem]", "max-w-[calc(100vw-3.5rem)]"]));
    expect(classes).not.toContain("w-[22rem]");
    const previous = $<HTMLButtonElement>('[data-testid="case-pager-previous"]');
    const next = $<HTMLButtonElement>('[data-testid="case-pager-next"]');
    expect(previous.getAttribute("aria-label")).toBe("前へ");
    expect(next.disabled).toBe(true);
    click(previous);
    expect(onPrevious).toHaveBeenCalledTimes(1);
  });
});
