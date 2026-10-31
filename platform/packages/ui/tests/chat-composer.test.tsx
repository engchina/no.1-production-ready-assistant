// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatComposer, ChatComposerOption } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

function Harness({
  initial = "",
  running = false,
  submitBlocked = false,
  onSubmit,
  onStop,
}: {
  initial?: string;
  running?: boolean;
  submitBlocked?: boolean;
  onSubmit: () => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ChatComposer
      id="composer"
      value={value}
      onValueChange={setValue}
      onSubmit={onSubmit}
      onStop={onStop}
      running={running}
      submitBlocked={submitBlocked}
      label="質問"
      placeholder="質問を入力（Enter で送信、Shift+Enter で改行）"
      sendLabel="送信"
      stopLabel="停止"
      maxLength={100}
      sendTestId="send"
      options={
        <ChatComposerOption
          label="生成方法"
          labelDecorative
          info={{ label: "生成方法の説明", content: "SQL の作り方を選びます。", testId: "info" }}
          testId="option"
        >
          <span>select</span>
        </ChatComposerOption>
      }
      footer={<p data-testid="footer">通知</p>}
    />
  );
}

const textarea = () => host.querySelector<HTMLTextAreaElement>("#composer")!;
const send = () => host.querySelector<HTMLButtonElement>('[data-testid="send"]')!;
function keydown(init: KeyboardEventInit & { isComposing?: boolean }) {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  act(() => {
    textarea().dispatchEvent(event);
  });
  return event;
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("ChatComposer", () => {
  it("設定の行・入力欄と送信・通知の順に並び、入力欄は名前を読み上げだけで持つ", () => {
    act(() => root.render(<Harness onSubmit={vi.fn()} onStop={vi.fn()} />));
    const ids = Array.from(host.querySelectorAll("[data-testid]")).map((el) => el.getAttribute("data-testid"));
    expect(ids.indexOf("option")).toBeLessThan(ids.indexOf("send"));
    expect(ids.indexOf("send")).toBeLessThan(ids.indexOf("footer"));
    expect(host.querySelector('label[for="composer"]')!.textContent).toBe("質問");
    // 3.5 行（#2008）: ネイティブの rows は切り上げの 4、高さは行の高さ × 3.5（data-rows と --textarea-rows）。
    expect(textarea().getAttribute("rows")).toBe("4");
    expect(textarea().getAttribute("data-rows")).toBe("3.5");
    expect(textarea().style.getPropertyValue("--textarea-rows")).toBe("3.5");
    expect(textarea().getAttribute("maxlength")).toBe("100");
    // 中の選択欄が同じ名前を持つので、行の見える名前は読み上げから外す。
    expect(host.querySelector('[data-testid="option"] span[aria-hidden="true"]')!.textContent).toBe("生成方法");
  });

  it("Enter で送信し、Shift+Enter と IME の変換を確定する Enter では送らない", () => {
    const onSubmit = vi.fn();
    act(() => root.render(<Harness initial="売上" onSubmit={onSubmit} onStop={vi.fn()} />));
    expect(keydown({ key: "Enter", shiftKey: true }).defaultPrevented).toBe(false);
    expect(keydown({ key: "Enter", isComposing: true }).defaultPrevented).toBe(false);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(keydown({ key: "Enter" }).defaultPrevented).toBe(true);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("入力が空・送れない間・実行中の Enter は送らず、停止もしない", () => {
    const onSubmit = vi.fn();
    const onStop = vi.fn();
    act(() => root.render(<Harness initial="  " onSubmit={onSubmit} onStop={onStop} />));
    keydown({ key: "Enter" });
    act(() => root.render(<Harness initial="売上" submitBlocked onSubmit={onSubmit} onStop={onStop} />));
    keydown({ key: "Enter" });
    expect(send().getAttribute("aria-disabled")).toBe("true");
    act(() => root.unmount());
    root = createRoot(host);
    act(() => root.render(<Harness initial="売上" running onSubmit={onSubmit} onStop={onStop} />));
    keydown({ key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("前提の読み込み中（disabled）は書けず送れない。書いた文字は残す", () => {
    const onSubmit = vi.fn();
    function Disabled() {
      return (
        <ChatComposer
          id="composer"
          value="下書き"
          onValueChange={() => undefined}
          onSubmit={onSubmit}
          onStop={() => undefined}
          running={false}
          disabled
          label="質問"
          sendLabel="送信"
          stopLabel="停止"
          sendTestId="send"
        />
      );
    }
    act(() => root.render(<Disabled />));
    expect(textarea().disabled).toBe(true);
    expect(textarea().value).toBe("下書き");
    expect(send().getAttribute("aria-disabled")).toBe("true");
    act(() => send().click());
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("実行中は同じボタンが「停止」になり、押すと止める。入力欄は書ける", () => {
    const onStop = vi.fn();
    act(() => root.render(<Harness initial="" running onSubmit={vi.fn()} onStop={onStop} />));
    expect(send().getAttribute("data-state")).toBe("running");
    expect(textarea().disabled).toBe(false);
    act(() => send().click());
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});
