// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatHistoryList, type ChatHistoryListProps } from "../src";

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
  { id: "c1", title: "売上の推移", meta: "10/4 9:00・2 件" },
  { id: "c2", title: "在庫の一覧", meta: "10/3 18:00・1 件", badge: <span data-testid="badge">実行中</span> },
];

function render(props: Partial<ChatHistoryListProps> = {}) {
  act(() =>
    root.render(
      <ChatHistoryList
        items={items}
        onSelect={() => undefined}
        labels={labels}
        testIds={{ loading: "loading", error: "error", list: "list" }}
        {...props}
      />
    )
  );
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

describe("ChatHistoryList", () => {
  it("行は名前・補足・状態を出し、開いている会話に aria-current を付けて選べる", () => {
    const onSelect = vi.fn();
    render({ currentId: "c2", onSelect });
    const list = host.querySelector('[data-testid="list"]')!;
    expect(list.getAttribute("aria-label")).toBe("会話の履歴");
    const buttons = list.querySelectorAll("button");
    expect(buttons[0].textContent).toContain("売上の推移");
    expect(buttons[0].textContent).toContain("10/4 9:00・2 件");
    expect(buttons[0].getAttribute("aria-current")).toBeNull();
    expect(buttons[1].getAttribute("aria-current")).toBe("true");
    expect(buttons[1].querySelector('[data-testid="badge"]')).not.toBeNull();
    act(() => buttons[0].click());
    expect(onSelect).toHaveBeenCalledWith(items[0]);
  });

  it("2 ページ以上なら一覧の下に共通の件数と前へ / 次へを出し、1 ページなら出さない（#1265）", () => {
    const onPageChange = vi.fn();
    render({
      testIds: { list: "list", pagination: "pager" },
      pagination: { type: "offset", offset: 0, limit: 10, total: 14, count: 2, onPageChange, ariaLabel: "会話の履歴のページ" },
    });
    const pager = host.querySelector('[data-testid="pager"]')!;
    expect(pager.getAttribute("aria-label")).toBe("会話の履歴のページ");
    expect(pager.textContent).toContain("1–2 / 14 件");
    expect(pager.textContent).toContain("1 / 2 ページ");
    const buttonNamed = (name: string) =>
      Array.from(pager.querySelectorAll("button")).find((button) => button.textContent === name)!;
    expect(buttonNamed("前へ").disabled).toBe(true);
    act(() => buttonNamed("次へ").click());
    expect(onPageChange).toHaveBeenCalledWith(2, 10);

    render({
      testIds: { list: "list", pagination: "pager" },
      pagination: { type: "offset", offset: 0, limit: 10, total: 2, count: 2, onPageChange },
    });
    expect(host.querySelector('[data-testid="pager"]')).toBeNull();
  });

  it("カーソルの API は前へ / 次へで隣のページだけに移る（#1265）", () => {
    const onPrevious = vi.fn();
    const onNext = vi.fn();
    render({
      testIds: { list: "list", pagination: "pager" },
      pagination: { type: "cursor", depth: 1, total: 27, count: 2, nextCursor: null, onPrevious, onNext },
    });
    const pager = host.querySelector('[data-testid="pager"]')!;
    expect(pager.textContent).toContain("26–27 / 27 件");
    expect(pager.textContent).toContain("2 / 2 ページ");
    const [prev, next] = Array.from(pager.querySelectorAll("button"));
    expect(next.disabled).toBe(true);
    act(() => prev.click());
    expect(onPrevious).toHaveBeenCalledTimes(1);
    expect(onNext).not.toHaveBeenCalled();
  });

  it("選べない間は行を押せない", () => {
    render({ disabled: true });
    const button = host.querySelector<HTMLButtonElement>('[data-testid="list"] button')!;
    expect(button.disabled).toBe(true);
  });

  it("行の操作と、その場の編集の中身を出せる", () => {
    render({
      currentId: "c1",
      renderActions: (item, current) => (
        <button type="button" data-testid={`rename-${item.id}`} data-current={String(current)}>
          名前を変更
        </button>
      ),
      renderEditor: (item) => (item.id === "c2" ? <input data-testid="editor" /> : null),
    });
    expect(host.querySelector('[data-testid="rename-c1"]')!.getAttribute("data-current")).toBe("true");
    expect(host.querySelector('[data-testid="rename-c2"]')).toBeNull();
    expect(host.querySelector('[data-testid="editor"]')).not.toBeNull();
  });

  it("対象を待っている間は経過時間を出さず一覧の形だけを出し、0 件の文を出さない", () => {
    render({ waiting: true, items: [], testIds: { skeleton: "skeleton", loading: "loading" } });
    expect(host.querySelector('[data-testid="skeleton"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="loading"]')).toBeNull();
    expect(host.textContent).not.toContain("まだ会話がありません");
  });

  it("読み込み中は文言と行の形の Skeleton、失敗は文と再試行、0 件は短い文を出す", () => {
    render({ loading: true });
    expect(host.querySelector('[data-testid="loading"]')!.textContent).toContain("会話を読み込んでいます");
    expect(host.querySelector('[data-skeleton="list"]')).not.toBeNull();

    const onRetry = vi.fn();
    // 要約に使える文が無い失敗は `labels.error` を出す（API の文があればそれを出す。ApiErrorBanner）。
    render({ error: {}, onRetry });
    const error = host.querySelector('[data-testid="error"]')!;
    expect(error.textContent).toContain("会話の一覧を読み込めませんでした。");
    const retry = Array.from(error.querySelectorAll("button")).find((b) => b.textContent === "再試行")!;
    act(() => retry.click());
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(host.querySelector('[data-testid="list"]')).toBeNull();

    render({ items: [] });
    expect(host.textContent).toContain("まだ会話がありません");
    expect(host.querySelector('[data-testid="list"]')).toBeNull();
  });
});
