// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ChatHistoryList,
  ConfirmProvider,
  DEFAULT_CHAT_HISTORY_ACTION_LABELS,
  useChatHistoryActions,
  useToastStore,
  type UseChatHistoryActionsOptions,
} from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

const labels = {
  list: "会話の履歴",
  loading: "会話を読み込んでいます",
  error: "会話の一覧を読み込めませんでした。",
  retry: "再試行",
  empty: "まだ会話がありません",
};
const items = [
  { id: "c1", title: "売上の推移" },
  { id: "c2", title: "在庫の一覧" },
];

function Harness(options: Omit<UseChatHistoryActionsOptions, "onRename" | "onDelete"> & Partial<UseChatHistoryActionsOptions>) {
  const actions = useChatHistoryActions({
    onRename: options.onRename ?? (async () => undefined),
    onDelete: options.onDelete ?? (async () => undefined),
    ...options,
  });
  return (
    <ChatHistoryList
      items={items}
      onSelect={() => undefined}
      labels={labels}
      renderActions={actions.renderActions}
      renderEditor={actions.renderEditor}
    />
  );
}

function render(options: Partial<UseChatHistoryActionsOptions> = {}) {
  act(() =>
    root.render(
      <ConfirmProvider>
        <Harness {...options} />
      </ConfirmProvider>
    )
  );
}

function button(label: string): HTMLButtonElement {
  const found = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
    (element) => element.getAttribute("aria-label") === label || element.textContent === label
  );
  if (!found) throw new Error(`button not found: ${label}`);
  return found;
}

function input(): HTMLInputElement {
  return host.querySelector<HTMLInputElement>("input")!;
}

function type(value: string) {
  const element = input();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function pressEnter() {
  act(() => {
    input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  useToastStore.setState({ toasts: [] });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("useChatHistoryActions", () => {
  it("名前の変更は行をその場の欄にし、空の名前は保存せず、前後の空白を除いて保存する", async () => {
    const onRename = vi.fn(async () => undefined);
    render({ onRename });
    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.rename("売上の推移")).click());
    expect(input().value).toBe("売上の推移");
    expect(input().maxLength).toBe(80);

    type("   ");
    pressEnter();
    await flush();
    expect(onRename).not.toHaveBeenCalled();
    expect(host.textContent).toContain("会話名を入力してください。");

    type("  今月の売上  ");
    pressEnter();
    await flush();
    expect(onRename).toHaveBeenCalledWith("c1", "今月の売上");
    expect(host.querySelector("input")).toBeNull();
  });

  it("canRename が false の行は名前の変更を出さず、削除だけを出す（他の利用者の会話。#3244）", () => {
    render({ canRename: (item) => item.id !== "c2" });
    expect(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.rename("売上の推移"))).not.toThrow();
    expect(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.rename("在庫の一覧"))).toThrow();
    expect(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.delete("在庫の一覧"))).not.toThrow();
    expect(host.querySelector("input")).toBeNull();
  });

  it("名前を保存できなければ欄を残して理由を出し、Esc で取り消せる", async () => {
    render({ onRename: async () => Promise.reject(new Error("会話が見つかりません。")) });
    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.rename("在庫の一覧")).click());
    type("新しい名前");
    pressEnter();
    await flush();
    expect(host.textContent).toContain("会話が見つかりません。");
    act(() => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(host.querySelector("input")).toBeNull();
  });

  it("削除は確認のダイアログを通し、確定したら前処理・削除・Toast の順に行う", async () => {
    const calls: string[] = [];
    const onBeforeDelete = vi.fn(() => calls.push("before"));
    const onDelete = vi.fn(async () => {
      calls.push("delete");
    });
    render({ onBeforeDelete, onDelete });

    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.delete("売上の推移")).click());
    const dialog = document.querySelector('[role="alertdialog"]')!;
    expect(dialog.textContent).toContain("会話を削除しますか？");
    expect(dialog.textContent).toContain("「売上の推移」を会話の履歴から削除します。");
    act(() => button("キャンセル").click());
    await flush();
    expect(onDelete).not.toHaveBeenCalled();

    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.delete("売上の推移")).click());
    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.deleteConfirm).click());
    await flush();
    expect(onDelete).toHaveBeenCalledWith("c1");
    expect(calls).toEqual(["before", "delete"]);
    expect(useToastStore.getState().toasts.map((toast) => toast.message)).toContain("会話を削除しました。");
  });

  it("削除できなければ API の文を Toast で出す", async () => {
    render({ onDelete: async () => Promise.reject(new Error("回答の作成中の会話は削除できません。")) });
    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.delete("在庫の一覧")).click());
    act(() => button(DEFAULT_CHAT_HISTORY_ACTION_LABELS.deleteConfirm).click());
    await flush();
    const toasts = useToastStore.getState().toasts;
    expect(toasts.at(-1)?.message).toBe("回答の作成中の会話は削除できません。");
    expect(toasts.at(-1)?.tone).toBe("danger");
  });
});
