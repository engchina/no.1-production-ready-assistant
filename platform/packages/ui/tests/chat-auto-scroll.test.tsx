// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useChatAutoScroll } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let log: HTMLDivElement | null = null;
let api: ReturnType<typeof useChatAutoScroll>;

/** happy-dom はレイアウトしないので、会話の欄の寸法を決め打ちにする（高さ 400・内容 scrollHeight）。 */
let contentHeight = 1000;
function stubMetrics(element: HTMLDivElement) {
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => 400 });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => contentHeight });
  element.scrollTo = ((options: ScrollToOptions) => {
    element.scrollTop = Math.min(options.top ?? 0, contentHeight - 400);
    element.dispatchEvent(new Event("scroll"));
  }) as typeof element.scrollTo;
}

function Harness({ contentKey, resetKey }: { contentKey: unknown; resetKey?: unknown }) {
  api = useChatAutoScroll({ contentKey, resetKey });
  return (
    <div
      ref={(element) => {
        if (element && element !== log) {
          log = element;
          stubMetrics(element);
        }
        api.logRef(element);
      }}
    />
  );
}

/** happy-dom は大きさの変化を知らせないので、ResizeObserver を差し替えて `resize()` で呼ぶ。 */
const resizeCallbacks = new Set<() => void>();
class FakeResizeObserver {
  private readonly notify: () => void;
  constructor(callback: ResizeObserverCallback) {
    this.notify = () => callback([], this as unknown as ResizeObserver);
  }
  observe() {
    resizeCallbacks.add(this.notify);
  }
  unobserve() {}
  disconnect() {
    resizeCallbacks.delete(this.notify);
  }
}

function resize() {
  act(() => {
    for (const callback of resizeCallbacks) callback();
  });
}

function render(contentKey: unknown, resetKey?: unknown) {
  act(() => root.render(<Harness contentKey={contentKey} resetKey={resetKey} />));
}

function userScrollTo(top: number) {
  act(() => {
    log!.scrollTop = top;
    log!.dispatchEvent(new Event("scroll"));
  });
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  resizeCallbacks.clear();
  contentHeight = 1000;
  log = null;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("useChatAutoScroll", () => {
  it("会話を開くと末尾から読み始め、末尾を見ている間は新しい内容に合わせて末尾へ追う", () => {
    render(1, "c1");
    expect(log!.scrollTop).toBe(600);
    contentHeight = 1500;
    render(2, "c1");
    expect(log!.scrollTop).toBe(1100);
    expect(api.showLatest).toBe(false);
  });

  it("上を読んでいる間は引き戻さず「最新へ」を出し、末尾へ戻ると消す", () => {
    render(1, "c1");
    userScrollTo(100);
    contentHeight = 1500;
    render(2, "c1");
    expect(log!.scrollTop).toBe(100);
    expect(api.showLatest).toBe(true);

    act(() => api.scrollToLatest());
    expect(log!.scrollTop).toBe(1100);
    expect(api.showLatest).toBe(false);
  });

  it("利用者が末尾まで戻ったら「最新へ」を消し、また追う", () => {
    render(1, "c1");
    userScrollTo(0);
    contentHeight = 1200;
    render(2, "c1");
    expect(api.showLatest).toBe(true);
    userScrollTo(790); // 末尾から 10px（しきい値の内）
    expect(api.showLatest).toBe(false);
    contentHeight = 1600;
    render(3, "c1");
    expect(log!.scrollTop).toBe(1200);
  });

  it("描いた後に高さが変わっても、末尾を見ていて利用者が操作していなければ末尾に留める (#2479)", () => {
    render(1, "c1");
    expect(log!.scrollTop).toBe(600);
    // フォントの読み込みなどで、内容の値は変わらずに高さだけが伸びる。
    contentHeight = 1019;
    resize();
    expect(log!.scrollTop).toBe(619);
  });

  it("利用者が会話の欄を操作した後は、高さが変わっても引き戻さない (#2479)", () => {
    render(1, "c1");
    act(() => log!.dispatchEvent(new Event("pointerdown")));
    contentHeight = 1300;
    resize();
    expect(log!.scrollTop).toBe(600);
    // 「最新へ」・新しい内容で末尾へ動かしたら、また追う。
    act(() => api.scrollToLatest());
    contentHeight = 1400;
    resize();
    expect(log!.scrollTop).toBe(1000);
  });

  it("自分で末尾へ動かした scroll が高さの伸びた後に届いても、末尾を追うのをやめない (#3245)", () => {
    render(1, "c1");
    expect(log!.scrollTop).toBe(600);
    // 内容が伸びた後に、前に末尾へ動かしたときの scroll が届く（scroll は非同期。scrollTop は動かしたときのまま）。
    contentHeight = 1443;
    act(() => log!.dispatchEvent(new Event("scroll")));
    expect(log!.scrollTop).toBe(1043);
    expect(api.showLatest).toBe(false);
    // その後の高さの変化にも追う。
    contentHeight = 1500;
    resize();
    expect(log!.scrollTop).toBe(1100);
  });

  it("末尾を追っている間はブラウザの scroll anchoring を止め、利用者が操作したら戻す (#3245)", () => {
    render(1, "c1");
    expect(log!.style.overflowAnchor).toBe("none");
    act(() => log!.dispatchEvent(new Event("pointerdown")));
    expect(log!.style.overflowAnchor).toBe("");
    act(() => api.scrollToLatest());
    expect(log!.style.overflowAnchor).toBe("none");
    userScrollTo(100);
    expect(log!.style.overflowAnchor).toBe("");
  });

  it("上を読んでいる間は、高さが変わっても引き戻さない (#2479)", () => {
    render(1, "c1");
    userScrollTo(100);
    contentHeight = 1300;
    resize();
    expect(log!.scrollTop).toBe(100);
  });

  it("別の会話を開いたら、上を読んでいても末尾から読み始める", () => {
    render(1, "c1");
    userScrollTo(0);
    render(1, "c2");
    expect(log!.scrollTop).toBe(600);
    expect(api.showLatest).toBe(false);
  });
});
