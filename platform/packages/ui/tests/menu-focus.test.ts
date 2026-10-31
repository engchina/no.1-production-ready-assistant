// @vitest-environment happy-dom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ObjectActionBar, restoreMenuTriggerFocus, type EntityAction } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** 操作の行（container）の中に、別の操作・トリガー・メニューの面を置く。 */
function setup() {
  const container = document.createElement("div");
  const sibling = document.createElement("button");
  sibling.textContent = "パスワードをリセット";
  const trigger = document.createElement("button");
  trigger.textContent = "その他の操作";
  const menu = document.createElement("div");
  const item = document.createElement("button");
  item.textContent = "無効化";
  menu.append(item);
  container.append(sibling, trigger, menu);
  const outside = document.createElement("button");
  outside.textContent = "ページの別の操作";
  document.body.append(container, outside);
  return { container, sibling, trigger, menu, item, outside };
}

describe("restoreMenuTriggerFocus（#1143）", () => {
  let frames: FrameRequestCallback[] = [];

  beforeEach(() => {
    frames = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  const flush = () => {
    for (const frame of frames.splice(0)) frame(0);
  };

  it("メニュー項目が消えて body にあるときはトリガーへ戻す", () => {
    const { trigger, menu, item } = setup();
    item.focus();
    restoreMenuTriggerFocus({ current: trigger }, { current: menu });
    item.remove();
    (document.activeElement as HTMLElement | null)?.blur();
    flush();
    expect(document.activeElement).toBe(trigger);
  });

  it("フォーカスがまだメニューの中にあるときはトリガーへ戻す", () => {
    const { trigger, menu, item } = setup();
    item.focus();
    restoreMenuTriggerFocus({ current: trigger }, { current: menu });
    flush();
    expect(document.activeElement).toBe(trigger);
  });

  it("閉じた直後に同じ行の別の操作へ移したフォーカスは奪わない", () => {
    const { trigger, menu, sibling } = setup();
    restoreMenuTriggerFocus({ current: trigger }, { current: menu });
    sibling.focus();
    flush();
    expect(document.activeElement).toBe(sibling);
  });

  it("閉じた直後にページの別の要素へ移したフォーカスは奪わない", () => {
    const { trigger, menu, outside } = setup();
    restoreMenuTriggerFocus({ current: trigger }, { current: menu });
    outside.focus();
    flush();
    expect(document.activeElement).toBe(outside);
  });
});

describe("ObjectActionBar のメニューを閉じた後のフォーカス（#1143）", () => {
  let frames: FrameRequestCallback[] = [];
  let root: Root | null = null;

  beforeEach(() => {
    frames = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
  });

  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  const noop = () => {};
  const actions: EntityAction[] = [
    { id: "edit", label: "編集", onSelect: noop },
    { id: "reset", label: "パスワードをリセット", onSelect: noop },
    { id: "disable", label: "無効化", onSelect: noop },
    { id: "delete", label: "削除", tone: "danger", onSelect: noop },
  ];

  function render() {
    const host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    act(() => root?.render(createElement(ObjectActionBar, { actions, ariaLabel: "sales.user" })));
    const button = (name: string) =>
      Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find((el) =>
        (el.getAttribute("aria-label") ?? el.textContent ?? "").includes(name)
      ) as HTMLButtonElement;
    return { button };
  }

  const flush = () =>
    act(() => {
      for (const frame of frames.splice(0)) frame(0);
    });

  it("Escape で閉じた直後に同じ行の別の操作へ移したフォーカスを奪わない", () => {
    const { button } = render();
    act(() => button("その他の操作").click());
    const menu = document.querySelector<HTMLElement>('[role="menu"]');
    expect(menu).not.toBeNull();
    act(() => {
      menu?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    const reset = button("パスワードをリセット");
    reset.focus();
    flush();
    expect(document.activeElement).toBe(reset);
  });

  it("Escape で閉じた後、ほかへ移していなければトリガーへ戻す", () => {
    const { button } = render();
    act(() => button("その他の操作").click());
    const menu = document.querySelector<HTMLElement>('[role="menu"]');
    act(() => {
      menu?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    flush();
    expect(document.activeElement).toBe(button("その他の操作"));
  });
});
