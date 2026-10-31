// @vitest-environment happy-dom
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatLayout } from "../src/components/chat/chat-layout";
import { useChatHistoryPanel } from "../src/components/chat/use-chat-history-panel";
import { TOAST_CLEARANCE_VAR, useToastClearance } from "../src/components/ui/use-toast-clearance";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// #2103: md 以上の通知（右下）を、画面の下端に固定された入力欄の上に出す。入力欄の上端から画面の下端までの高さを
// `--toast-clear-bottom` に書き、`<Toaster/>` の下端の距離はこれと `--toast-offset-bottom` の大きい方になる。

let container: HTMLDivElement;
let root: Root;
/** 測る要素の位置（画面の上端からの top / bottom）。 */
let box = { top: 760, bottom: 878 };

const clearance = () => document.documentElement.style.getPropertyValue(TOAST_CLEARANCE_VAR);

function Probe() {
  const ref = useRef<HTMLDivElement>(null);
  useToastClearance(ref);
  return <div ref={ref} data-testid="probe" />;
}

function Chat() {
  const history = useChatHistoryPanel({ inlineOpen: false, onInlineOpenChange: () => undefined });
  return (
    <ChatLayout
      history={history}
      historyTitle="会話の履歴"
      historyCloseLabel="会話の履歴を閉じる"
      historyContent={null}
      label="チャット"
      newConversation={{ label: "新しい会話", onClick: () => undefined }}
      logLabel="会話"
      composer={<textarea aria-label="質問" />}
    >
      <p>質問と回答</p>
    </ChatLayout>
  );
}

beforeEach(() => {
  box = { top: 760, bottom: 878 };
  vi.stubGlobal("innerHeight", 900);
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () => ({ ...box, x: 0, y: box.top, left: 0, right: 100, width: 100, height: box.bottom - box.top }) as DOMRect
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.documentElement.style.removeProperty(TOAST_CLEARANCE_VAR);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useToastClearance（#2103）", () => {
  it("下端に固定された入力欄の上端から画面の下端までの高さを書き、外れたら消す", () => {
    act(() => root.render(<Probe />));
    expect(clearance()).toBe("140px");
    act(() => root.render(<></>));
    expect(clearance()).toBe("");
  });

  it("画面の大きさが変わったら測り直す", async () => {
    act(() => root.render(<Probe />));
    box = { top: 700, bottom: 878 };
    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
    });
    expect(clearance()).toBe("200px");
  });

  it("画面の上半分・画面の外にある間は持ち上げない（決まった位置の通知と重ならない）", () => {
    box = { top: 100, bottom: 300 };
    act(() => root.render(<Probe />));
    expect(clearance()).toBe("");
    act(() => root.render(<></>));
    box = { top: 950, bottom: 1050 };
    act(() => root.render(<Probe />));
    expect(clearance()).toBe("");
  });

  it("ChatLayout の入力欄の領域が書く", () => {
    act(() => root.render(<Chat />));
    expect(document.querySelector('[data-testid="chat-composer-region"]')).not.toBeNull();
    expect(clearance()).toBe("140px");
    act(() => root.render(<></>));
    expect(clearance()).toBe("");
  });
});
