// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ListCount, ListToolbar, listCountText, PagedDataTable, Pagination } from "../src";

// #1891: ページ番号を入れて移る（Enter・欄を離れたとき）、範囲の外は直前のページに戻して理由を出す。

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

function Harness({ totalPages = 13, onChange }: { totalPages?: number; onChange?: (page: number) => void }) {
  const [page, setPage] = useState(1);
  return (
    <Pagination
      page={page}
      totalPages={totalPages}
      range={{ start: (page - 1) * 10 + 1, end: page * 10, total: totalPages * 10 }}
      onPageChange={(next) => {
        onChange?.(next);
        setPage(next);
      }}
    />
  );
}

const input = () => host.querySelector<HTMLInputElement>("[data-pagination-input]")!;
const message = () => host.querySelector("[data-pagination-message]")!.textContent;
const status = () => host.querySelector("[data-pagination-status]")!.textContent;

function type(value: string) {
  const element = input();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function key(name: string, init: KeyboardEventInit = {}) {
  act(() => {
    input().dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...init }));
  });
}

function button(name: string) {
  return [...host.querySelectorAll("button")].find(
    (element) => element.getAttribute("aria-label") === name || element.textContent === name
  )!;
}

describe("Pagination のページ番号の入力（#1891）", () => {
  it("Enter で入れたページへ移り、入力欄と読み上げが今のページになる", () => {
    const onChange = vi.fn();
    act(() => root.render(<Harness onChange={onChange} />));
    type("7");
    key("Enter");
    expect(onChange).toHaveBeenCalledWith(7);
    expect(input().value).toBe("7");
    expect(status()).toBe("7 / 13 ページ");
    expect(host.textContent).toContain("61–70 / 130 件");
  });

  it("欄を離れたときにも移る（全角の数字も受け付ける）", () => {
    const onChange = vi.fn();
    act(() => root.render(<Harness onChange={onChange} />));
    type("１２");
    act(() => {
      input().dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
      input().dispatchEvent(new FocusEvent("blur"));
    });
    expect(onChange).toHaveBeenCalledWith(12);
  });

  it("範囲の外・数でない値は直前のページに戻し、理由を出す。次の入力で理由を消す", () => {
    const onChange = vi.fn();
    act(() => root.render(<Harness onChange={onChange} />));
    type("99");
    key("Enter");
    expect(onChange).not.toHaveBeenCalled();
    expect(input().value).toBe("1");
    expect(message()).toBe("1〜13 のページ番号を入力してください");
    expect(input().getAttribute("aria-describedby")).toContain(host.querySelector("[data-pagination-message]")!.id);
    type("2");
    expect(message()).toBe("");
    type("x");
    key("Enter");
    expect(input().value).toBe("1");
    expect(message()).toContain("1〜13");
  });

  it("Escape は入力を直前のページに戻す。IME の変換を確定する Enter では移らない", () => {
    const onChange = vi.fn();
    act(() => root.render(<Harness onChange={onChange} />));
    type("5");
    key("Escape");
    expect(input().value).toBe("1");
    type("5");
    key("Enter", { isComposing: true } as KeyboardEventInit);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("最初へ・最後へ・前へ・次へで移り、端のボタンは押せない", () => {
    act(() => root.render(<Harness />));
    expect(button("最初のページ").disabled).toBe(true);
    expect(button("前へ").disabled).toBe(true);
    act(() => button("最後のページ").click());
    expect(input().value).toBe("13");
    expect(button("最後のページ").disabled).toBe(true);
    expect(button("次へ").disabled).toBe(true);
    act(() => button("前へ").click());
    expect(input().value).toBe("12");
    act(() => button("最初のページ").click());
    expect(input().value).toBe("1");
  });

  it("disabled の間はすべての操作を止める", () => {
    act(() =>
      root.render(<Pagination page={2} totalPages={3} onPageChange={() => undefined} disabled />)
    );
    expect(input().disabled).toBe(true);
    expect([...host.querySelectorAll("button")].every((element) => element.disabled)).toBe(true);
  });
});

describe("PagedDataTable の表示件数（#1891）", () => {
  it("pageSizeOptions を渡すと表示件数の選択を出し、変えると 1 ページ目へ戻る", () => {
    const rows = Array.from({ length: 60 }, (_, index) => ({ id: index + 1 }));
    act(() =>
      root.render(
        <PagedDataTable
          columns={[{ key: "id", header: "ID", render: (row: { id: number }) => row.id }]}
          rows={rows}
          getRowKey={(row) => row.id}
          pageSizeOptions={[10, 25]}
        />
      )
    );
    // 既定は 25 件/ページ（#1952）。
    expect(host.textContent).toContain("1–25 / 60 件");
    expect(host.textContent).toContain("表示件数");
    type("3");
    key("Enter");
    expect(host.textContent).toContain("51–60 / 60 件");
  });
});

describe("PagedDataTable の pageSize の prop（ResultTable の外の選択欄。#1891）", () => {
  it("pageSizeOptions を渡さない一覧は、pageSize の prop が変わればその件数で描き直す", () => {
    const rows = Array.from({ length: 60 }, (_, index) => ({ id: index + 1 }));
    const table = (pageSize: number) => (
      <PagedDataTable
        columns={[{ key: "id", header: "ID", render: (row: { id: number }) => row.id }]}
        rows={rows}
        getRowKey={(row) => row.id}
        pageSize={pageSize}
        resetKey={pageSize}
      />
    );
    act(() => root.render(table(10)));
    expect(host.textContent).toContain("1–10 / 60 件");
    act(() => root.render(table(50)));
    expect(host.textContent).toContain("1–50 / 60 件");
    expect(host.querySelectorAll("tbody tr")).toHaveLength(50);
  });
});

describe("ListCount / ListToolbar の件数（#1891）", () => {
  it("絞り込みなしは「全 N 件」、絞り込み中は「N 件（全 M 件）」で、桁区切りと読み上げ", () => {
    expect(listCountText({ total: 1234 })).toBe("全 1,234 件");
    expect(listCountText({ total: 1234, matched: 12 })).toBe("12 件（全 1,234 件）");
    act(() => root.render(<ListCount total={3} />));
    const count = host.querySelector("[data-list-count]")!;
    expect(count.getAttribute("role")).toBe("status");
    expect(count.className).toContain("tnum");
  });

  it("ListToolbar の count は右側（操作の左）に出し、検索欄は lg の幅", () => {
    act(() =>
      root.render(
        <ListToolbar
          search={<input aria-label="検索" />}
          count={{ total: 120, matched: 8 }}
          actions={<button type="button">追加</button>}
        />
      )
    );
    const end = host.querySelector("[data-list-toolbar-end]")!;
    expect(end.textContent).toBe("8 件（全 120 件）追加");
    expect(host.querySelector("[data-list-toolbar-search]")!.className).toContain("@md:w-[var(--field-width-lg)]");
  });
});
