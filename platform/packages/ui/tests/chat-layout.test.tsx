// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatLayout } from "../src/components/chat/chat-layout";
import { useChatHistoryPanel } from "../src/components/chat/use-chat-history-panel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let wide = true;
const listeners = new Set<() => void>();

function stubViewport() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() {
      return wide;
    },
    media: query,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  }));
}

function setWide(next: boolean) {
  wide = next;
  act(() => listeners.forEach((listener) => listener()));
}

function Harness({
  onNew = () => undefined,
  title,
  titleLoading,
  testIdPrefix,
  caption,
  embedded,
  target,
}: {
  onNew?: () => void;
  title?: string | null;
  titleLoading?: boolean;
  testIdPrefix?: string;
  caption?: string | null;
  embedded?: boolean;
  target?: string;
}) {
  const [inlineOpen, setInlineOpen] = useState(false);
  const history = useChatHistoryPanel({ inlineOpen, onInlineOpenChange: setInlineOpen });
  return (
    <ChatLayout
      history={history}
      historyTitle="会話の履歴"
      historyCloseLabel="会話の履歴を閉じる"
      historyCaption={caption}
      historyContent={
        <button type="button" data-testid="item" onClick={history.closeSheet}>
          会話 1
        </button>
      }
      label="チャット"
      conversationTitle={title}
      targetSelector={target ? <button data-testid="target">{target}</button> : undefined}
      conversationTitleLoading={titleLoading}
      newConversation={{ label: "新しい会話", onClick: onNew }}
      logLabel="会話"
      composer={<textarea aria-label="質問" />}
      testIdPrefix={testIdPrefix}
      embedded={embedded}
    >
      <p>質問と回答</p>
    </ChatLayout>
  );
}

const $ = <T extends Element = HTMLElement>(selector: string) =>
  document.querySelector<T>(selector);
const toggle = (prefix = "chat") => $<HTMLButtonElement>(`[data-testid="${prefix}-history-toggle"]`)!;
const click = (target: Element) => act(() => (target as HTMLElement).click());

beforeEach(() => {
  wide = true;
  listeners.clear();
  stubViewport();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe("ChatLayout", () => {
  it("会話の領域は上端の行・role=log の会話の欄・入力欄の領域の 3 段で、名前を持つ", () => {
    act(() => root.render(<Harness />));
    const section = $('section[aria-label="チャット"]')!;
    expect(section.getAttribute("data-testid")).toBe("chat-panel");
    const log = section.querySelector('[role="log"]')!;
    expect(log.getAttribute("aria-label")).toBe("会話");
    expect(log.getAttribute("data-testid")).toBe("chat-conversation");
    expect(log.textContent).toBe("質問と回答");
    expect(section.querySelector('[data-testid="chat-composer-region"] textarea')).not.toBeNull();
    // lg 未満は高さを制限して、長い会話でページを伸ばさない。
    expect(section.className).toContain("h-[70dvh]");
  });

  it("期間の補足: lg 以上は見出しの行、lg 未満はシートの本文の先頭に出し、パネルの説明にする（#2340）", () => {
    act(() => root.render(<Harness caption="最近 90 日" />));
    click(toggle());
    const aside = $('aside[aria-label="会話の履歴"]')!;
    const caption = $('[data-testid="chat-history-caption"]')!;
    expect(caption.textContent).toBe("最近 90 日");
    expect(caption.className).toContain("text-fg-muted");
    expect(aside.querySelector("h2")!.parentElement!.contains(caption)).toBe(true);
    expect(aside.getAttribute("aria-describedby")).toBe(caption.id);

    act(() => root.render(<Harness caption={null} />));
    expect($('[data-testid="chat-history-caption"]')).toBeNull();
    expect($('aside[aria-label="会話の履歴"]')!.hasAttribute("aria-describedby")).toBe(false);

    setWide(false);
    act(() => root.render(<Harness caption="最近 30 日" />));
    click(toggle());
    const dialog = $('[role="dialog"]')!;
    expect(dialog.querySelector('[data-testid="chat-history-caption"]')!.textContent).toBe("最近 30 日");
  });

  it("testid の接頭辞で既存の spec の testid を残す", () => {
    act(() => root.render(<Harness testIdPrefix="sql-chat" />));
    expect($('[data-testid="sql-chat-history"]')).not.toBeNull();
    expect($('[data-testid="sql-chat-panel"]')).not.toBeNull();
    expect($('[data-testid="sql-chat-conversation"]')).not.toBeNull();
    expect(toggle("sql-chat")).not.toBeNull();
  });

  it("lg 以上: 履歴は既定で閉じ、開くと 20rem の列のパネルになる。閉じても aria-controls の先を残す", () => {
    act(() => root.render(<Harness />));
    const aside = $('aside[aria-label="会話の履歴"]')!;
    expect(aside.className).toContain("hidden");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(toggle().getAttribute("aria-controls")).toBe(aside.id);

    click(toggle());
    expect(aside.className).not.toContain("hidden");
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect($("[data-chat-layout]")!.className).toContain("lg:grid-cols-[20rem_minmax(0,1fr)]");
    expect($('[role="dialog"]')).toBeNull();
  });

  it("lg 未満: 履歴はモーダルのシートで開き、会話を選ぶと閉じて開閉ボタンへフォーカスを戻す", () => {
    wide = false;
    act(() => root.render(<Harness />));
    expect($("aside")).toBeNull();
    click(toggle());
    const dialog = $('[role="dialog"]')!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(toggle().getAttribute("aria-controls")).toBe(dialog.id);

    click($('[data-testid="item"]')!);
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(toggle());
  });

  it("lg 未満でシートを開いたまま lg 以上に広げると、シートを閉じてインラインのパネルの状態に戻る", () => {
    wide = false;
    act(() => root.render(<Harness />));
    click(toggle());
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    setWide(true);
    // インラインのパネルは作業状態のまま（閉じている）。
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect($('aside[aria-label="会話の履歴"]')!.className).toContain("hidden");
    // 狭くし直してもシートは開いたまま戻らない。
    setWide(false);
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
  });

  it("会話の名前: 名前があれば見出し、読み込み中は Skeleton、会話が無ければ何も出さない", () => {
    act(() => root.render(<Harness title="売上の推移" />));
    expect($('[data-testid="chat-conversation-title"]')!.textContent).toBe("売上の推移");
    act(() => root.render(<Harness title={null} titleLoading />));
    expect($('[data-testid="chat-conversation-title"]')).toBeNull();
    expect($('section [aria-hidden="true"].animate-pulse')).not.toBeNull();
    act(() => root.render(<Harness title={null} />));
    expect($('section [aria-hidden="true"].animate-pulse')).toBeNull();
  });

  it("「最新へ」は届いたときだけ会話の欄の外（log の外）に出て、押すと末尾へ戻す", () => {
    const onClick = vi.fn();
    function WithLatest({ visible }: { visible: boolean }) {
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
          latest={{ visible, label: "最新のメッセージへ", onClick }}
          composer={null}
        >
          <p>回答</p>
        </ChatLayout>
      );
    }
    act(() => root.render(<WithLatest visible={false} />));
    expect($('[data-testid="chat-latest"]')).toBeNull();
    act(() => root.render(<WithLatest visible />));
    const button = $<HTMLButtonElement>('[data-testid="chat-latest"]')!;
    expect(button.textContent).toBe("最新のメッセージへ");
    expect($('[role="log"]')!.contains(button)).toBe(false);
    click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("embedded: スクロールするページの中では、どの幅でも全体の高さを 70dvh に固定する（#2515）", () => {
    act(() => root.render(<Harness embedded />));
    const layout = $("[data-chat-layout]")!;
    expect(layout.hasAttribute("data-chat-layout-embedded")).toBe(true);
    expect(layout.className).toContain("h-[70dvh]");
    expect(layout.className).not.toContain("lg:flex-1");
    // 会話の領域は全体の高さの中で伸び、lg 以上で高さを外さない。
    const section = $('section[aria-label="チャット"]')!;
    expect(section.className).not.toContain("lg:h-auto");
  });

  it("「新しい会話」は上端の行の右端の 1 か所", () => {
    const onNew = vi.fn();
    act(() => root.render(<Harness onNew={onNew} />));
    const buttons = Array.from(document.querySelectorAll("button")).filter(
      (button) => button.textContent === "新しい会話"
    );
    expect(buttons).toHaveLength(1);
    click(buttons[0]);
    expect(onNew).toHaveBeenCalledTimes(1);
  });
});

it("対象の選択は会話名と新しい会話と同じツールバーに置く", () => {
  act(() => root.render(<Harness title="会話 A" target="対象 A" />));
  const target = container.querySelector('[data-testid="target"]')!;
  const toolbar = target.closest("[data-chat-toolbar]")!;
  expect(toolbar.textContent).toContain("会話 A");
  expect(toolbar.textContent).toContain("新しい会話");
  expect(target.closest("[data-chat-target-selector]")).not.toBeNull();
  expect(target.closest('[role="log"]')).toBeNull();
});
