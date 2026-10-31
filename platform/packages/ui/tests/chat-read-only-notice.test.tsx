// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatReadOnlyNotice, DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

describe("ChatReadOnlyNotice", () => {
  it("入力欄を出さず、見出しと本文の info の案内を出す。停止は渡したときだけ出す", () => {
    act(() => root.render(<ChatReadOnlyNotice />));
    const notice = host.querySelector('[data-testid="chat-read-only-notice"]')!;
    expect(notice).not.toBeNull();
    expect(notice.textContent).toContain(DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS.title);
    expect(notice.textContent).toContain(DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS.message);
    expect(notice.querySelector('[data-surface-tint="info"]')).not.toBeNull();
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector('[data-testid="chat-read-only-stop"]')).toBeNull();
  });

  it("停止を押すと onStop を呼び、要求中はボタンを loading にする", () => {
    const onStop = vi.fn();
    act(() => root.render(<ChatReadOnlyNotice onStop={onStop} />));
    const stop = host.querySelector<HTMLButtonElement>('[data-testid="chat-read-only-stop"]')!;
    expect(stop.textContent).toContain(DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS.stop);
    act(() => stop.click());
    expect(onStop).toHaveBeenCalledTimes(1);

    act(() => root.render(<ChatReadOnlyNotice onStop={onStop} stopping />));
    expect(host.querySelector('[data-testid="chat-read-only-stop"]')!.getAttribute("aria-busy")).toBe("true");
  });

  it("testid の接頭辞・文言の上書き・案内の下の footer を受け取る", () => {
    act(() =>
      root.render(
        <ChatReadOnlyNotice
          testIdPrefix="sql-chat-read-only"
          labels={{ stop: "止める" }}
          onStop={() => undefined}
          footer={<p data-testid="footer">停止できませんでした。</p>}
        />
      )
    );
    expect(host.querySelector('[data-testid="sql-chat-read-only-notice"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="sql-chat-read-only-stop"]')!.textContent).toContain("止める");
    expect(host.querySelector('[data-testid="footer"]')).not.toBeNull();
  });
});
