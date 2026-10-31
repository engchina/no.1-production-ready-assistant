// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LIST_PICKER_LABELS,
  ListPicker,
  ListToolbar,
  SEARCH_FIELD_DEBOUNCE_MS,
  type ListPickerGroup,
  type ListPickerItem,
  type ListPickerProps,
} from "../src";
import { prefixOffsets, rowIndexAt, scrollTopToReveal, visibleRange } from "../src/lib/list-window";

// #600: 大量の候補から複数を選ぶ一覧。キーボード（listbox）・IME・読み上げ・追加読み込み・仮想スクロールを確かめる。

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
  vi.useRealTimers();
});

function makeItems(count: number, prefix = "doc", groupKey?: string): ListPickerItem[] {
  return Array.from({ length: count }, (_, index) => {
    const name = `${prefix}-${String(index + 1).padStart(4, "0")}`;
    return { key: name, label: name, textValue: name, groupKey };
  });
}

/** 選択を持つ親（実際の使い方と同じ形）。 */
function Harness({
  onToggleSpy,
  initialSelected = [],
  ...props
}: Partial<ListPickerProps> & { onToggleSpy?: (key: string, selected: boolean) => void; initialSelected?: string[] }) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(initialSelected));
  return (
    <ListPicker
      id="picker"
      label="追加する文書の候補"
      items={[]}
      {...props}
      selectedKeys={selected}
      onToggle={(item, next) => {
        onToggleSpy?.(item.key, next);
        setSelected((current) => {
          const copy = new Set(current);
          if (next) copy.add(item.key);
          else copy.delete(item.key);
          return copy;
        });
      }}
      onSelectMany={(items) =>
        setSelected((current) => new Set([...current, ...items.map((item) => item.key)]))
      }
      onClearSelection={() => setSelected(new Set())}
      testId="picker"
    />
  );
}

function render(node: React.ReactNode) {
  act(() => root.render(node));
}

function key(target: Element, keyName: string, init: KeyboardEventInit = {}) {
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { key: keyName, bubbles: true, cancelable: true, ...init }));
  });
}

function focus(target: HTMLElement) {
  act(() => target.focus());
}

const listboxes = () => Array.from(host.querySelectorAll<HTMLElement>('[role="listbox"]'));
const options = () => Array.from(host.querySelectorAll<HTMLElement>('[role="option"]'));
const option = (name: string) =>
  options().find((node) => node.querySelector(`[id="${node.getAttribute("aria-labelledby")}"]`)?.textContent === name);

describe("list-window（仮想スクロールの計算）", () => {
  it("行の上端・位置の行・描く範囲・見せるための scrollTop を求める", () => {
    const offsets = prefixOffsets([10, 20, 20, 20, 30]);
    expect(offsets).toEqual([0, 10, 30, 50, 70, 100]);
    expect(rowIndexAt(offsets, -5)).toBe(0);
    expect(rowIndexAt(offsets, 29)).toBe(1);
    expect(rowIndexAt(offsets, 30)).toBe(2);
    expect(rowIndexAt(offsets, 1000)).toBe(4);
    expect(visibleRange(offsets, 30, 20, 0)).toEqual({ start: 2, end: 4 });
    // 全体の高さを超える scrollTop でも空にしない（末尾に寄せる）。
    expect(visibleRange(offsets, 5000, 20, 0)).toEqual({ start: 4, end: 5 });
    expect(visibleRange(prefixOffsets([]), 0, 100, 0)).toEqual({ start: 0, end: 0 });
    expect(scrollTopToReveal(offsets, 4, 0, 50)).toBe(50);
    expect(scrollTopToReveal(offsets, 0, 40, 50)).toBe(0);
    expect(scrollTopToReveal(offsets, 2, 20, 50)).toBe(20);
  });
});

describe("ListPicker", () => {
  it("読み上げ: 複数選択の listbox と、選択の状態・位置・説明を持つ選択肢", () => {
    const items = makeItems(3);
    items[1] = { ...items[1], disabled: true, disabledReason: "追加済み" };
    render(<Harness items={items} total={3} initialSelected={["doc-0001"]} />);

    const [listbox] = listboxes();
    expect(listbox.getAttribute("aria-multiselectable")).toBe("true");
    expect(listbox.getAttribute("aria-label")).toBe("追加する文書の候補");
    expect(listbox.tabIndex).toBe(0);
    const hint = document.getElementById(listbox.getAttribute("aria-describedby") ?? "");
    expect(hint?.textContent).toBe(DEFAULT_LIST_PICKER_LABELS.keyboardHint);

    const first = option("doc-0001")!;
    expect(first.getAttribute("aria-checked")).toBe("true");
    expect(first.getAttribute("aria-posinset")).toBe("1");
    expect(first.getAttribute("aria-setsize")).toBe("3");
    const second = option("doc-0002")!;
    expect(second.getAttribute("aria-checked")).toBe("false");
    expect(second.getAttribute("aria-disabled")).toBe("true");
    expect(document.getElementById(second.getAttribute("aria-describedby") ?? "")?.textContent).toBe("追加済み");
    expect(host.querySelector('[data-testid="picker-footer"]')?.textContent).toContain("3 / 3 件を表示、選択 1 件");
  });

  it("キーボード: ↓↑ / Home / End で位置を動かし、Space と Enter で選択を切り替える。選べない候補は切り替わらない", () => {
    const items = makeItems(5);
    items[2] = { ...items[2], disabled: true, disabledReason: "追加済み" };
    const spy = vi.fn();
    render(<Harness items={items} onToggleSpy={spy} />);
    const [listbox] = listboxes();

    focus(listbox);
    const activeName = () => {
      const id = listboxes()[0].getAttribute("aria-activedescendant");
      const node = id ? document.getElementById(id) : null;
      return node?.querySelector(`[id="${node.getAttribute("aria-labelledby")}"]`)?.textContent;
    };
    // フォーカスが入ると、見えている最初の候補が位置になる。
    expect(activeName()).toBe("doc-0001");
    key(listbox, "ArrowDown");
    expect(activeName()).toBe("doc-0002");
    key(listbox, " ");
    expect(spy).toHaveBeenLastCalledWith("doc-0002", true);
    expect(option("doc-0002")!.getAttribute("aria-checked")).toBe("true");
    key(listbox, "Enter");
    expect(spy).toHaveBeenLastCalledWith("doc-0002", false);

    key(listbox, "ArrowDown");
    expect(activeName()).toBe("doc-0003");
    spy.mockClear();
    key(listbox, " ");
    expect(spy).not.toHaveBeenCalled();

    key(listbox, "End");
    expect(activeName()).toBe("doc-0005");
    key(listbox, "ArrowDown");
    expect(activeName()).toBe("doc-0005");
    key(listbox, "Home");
    expect(activeName()).toBe("doc-0001");
    key(listbox, "ArrowUp");
    expect(activeName()).toBe("doc-0001");
  });

  it("IME: 変換を確定する Enter（isComposing / keyCode 229）では選択を切り替えない", () => {
    const spy = vi.fn();
    render(<Harness items={makeItems(2)} onToggleSpy={spy} />);
    const [listbox] = listboxes();
    focus(listbox);
    key(listbox, "Enter", { isComposing: true } as KeyboardEventInit);
    key(listbox, "Enter", { keyCode: 229 } as KeyboardEventInit);
    expect(spy).not.toHaveBeenCalled();
    key(listbox, "Enter");
    expect(spy).toHaveBeenCalledWith("doc-0001", true);
  });

  it("グループ: 見出しと一括操作を listbox の外に置き、端の ↓ で次のグループの listbox へ移る", async () => {
    const onSelectAll = vi.fn(() => Promise.resolve());
    const groups: ListPickerGroup[] = [
      { key: "APP", label: "APP", textValue: "APP", countLabel: "選択 0 / 全 2 件", onSelectAll, onClearAll: vi.fn() },
      { key: "SH", label: "SH", textValue: "SH" },
    ];
    render(
      <Harness
        items={[...makeItems(2, "APP.T", "APP"), ...makeItems(2, "SH.T", "SH")]}
        groups={groups}
      />
    );
    const boxes = listboxes();
    expect(boxes.map((box) => box.getAttribute("aria-label"))).toEqual([
      "追加する文書の候補: APP",
      "追加する文書の候補: SH",
    ]);
    // 一括操作のボタンは listbox の子にしない（listbox の子は選択肢だけ）。
    for (const box of boxes) expect(box.querySelector("button")).toBeNull();
    const selectAll = host.querySelector<HTMLButtonElement>('[aria-label="APP をすべて選択"]')!;
    expect(host.querySelector('[data-testid="picker-group-APP-heading"]')?.textContent).toContain("選択 0 / 全 2 件");
    await act(async () => selectAll.click());
    expect(onSelectAll).toHaveBeenCalledTimes(1);
    // グループの見出しがない SH は一括操作を出さない。
    expect(host.querySelector('[aria-label="SH をすべて選択"]')).toBeNull();

    focus(boxes[0]);
    key(boxes[0], "ArrowDown");
    key(boxes[0], "ArrowDown");
    expect(document.activeElement).toBe(listboxes()[1]);
    expect(listboxes()[0].getAttribute("aria-activedescendant")).toBeNull();
    const activeId = listboxes()[1].getAttribute("aria-activedescendant");
    expect(activeId && document.getElementById(activeId)?.textContent).toContain("SH.T-0001");
  });

  it("仮想スクロール: 3,000 件でも見えている範囲の行だけを描き、End で末尾の行を描いて見せる", () => {
    render(<Harness items={makeItems(3000)} total={3000} />);
    const count = options().length;
    expect(count).toBeGreaterThan(0);
    expect(count).toBeLessThan(60);
    const [listbox] = listboxes();
    focus(listbox);
    key(listbox, "End");
    const id = listboxes()[0].getAttribute("aria-activedescendant");
    const active = id ? document.getElementById(id) : null;
    expect(active?.textContent).toContain("doc-3000");
    expect(active?.getAttribute("aria-posinset")).toBe("3000");
    expect(active?.getAttribute("aria-setsize")).toBe("3000");
    expect(host.querySelector<HTMLElement>('[data-testid="picker-scroll-region"]')!.scrollTop).toBeGreaterThan(0);
  });

  it("検索: 入力に合わせて（debounce）サーバーの検索語を変え、IME の変換中は変えない。件数を読み上げる", () => {
    vi.useFakeTimers();
    const onSearch = vi.fn();
    function SearchHarness() {
      const [q, setQ] = useState("");
      return (
        <Harness
          items={makeItems(q ? 1 : 3)}
          total={q ? 1 : 3}
          search={{
            label: "追加する文書を検索",
            value: q,
            onSearch: (next) => {
              onSearch(next);
              setQ(next);
            },
          }}
        />
      );
    }
    render(<SearchHarness />);
    const input = host.querySelector<HTMLInputElement>('input[type="search"]')!;
    // 検索欄はツールバーの先頭（左）。
    expect(host.querySelector('[data-list-toolbar-start] [data-list-toolbar-search] input')).toBe(input);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      input.dispatchEvent(new Event("compositionstart", { bubbles: true }));
      setter.call(input, "ぶん");
      const event = new Event("input", { bubbles: true });
      Object.defineProperty(event, "isComposing", { value: true });
      input.dispatchEvent(event);
    });
    act(() => vi.advanceTimersByTime(SEARCH_FIELD_DEBOUNCE_MS * 2));
    expect(onSearch).not.toHaveBeenCalled();
    act(() => {
      setter.call(input, "文書");
      input.dispatchEvent(new Event("compositionend", { bubbles: true }));
    });
    act(() => vi.advanceTimersByTime(SEARCH_FIELD_DEBOUNCE_MS));
    expect(onSearch).toHaveBeenCalledWith("文書");
    expect(host.querySelector('[role="status"][data-search-field-status]')?.textContent).toBe("1 件が一致しました");
  });

  it("0 件: 検索語があれば「検索語をクリア」を出し、無ければ候補が無い案内を出す", () => {
    const onSearch = vi.fn();
    render(<Harness items={[]} total={0} search={{ label: "検索", value: "zzz", onSearch }} />);
    expect(host.textContent).toContain(DEFAULT_LIST_PICKER_LABELS.noResultsTitle);
    const clear = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "検索語をクリア")!;
    act(() => clear.click());
    expect(onSearch).toHaveBeenCalledWith("");
    render(<Harness items={[]} total={0} />);
    expect(host.textContent).toContain(DEFAULT_LIST_PICKER_LABELS.emptyTitle);
  });

  it("追加読み込み: 続きがあれば「さらに読み込む」、失敗は再試行付きで出す", () => {
    const onLoadMore = vi.fn();
    render(<Harness items={makeItems(50)} total={3000} hasMore onLoadMore={onLoadMore} />);
    expect(host.querySelector('[data-testid="picker-footer"]')?.textContent).toContain("50 / 3,000 件を表示、選択 0 件");
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="picker-footer-load-more"]')!.click());
    expect(onLoadMore).toHaveBeenCalledTimes(1);

    render(<Harness items={makeItems(50)} total={3000} hasMore onLoadMore={onLoadMore} loadMoreError="読み込めませんでした" />);
    expect(host.querySelector('[data-testid="picker-footer-load-more"]')).toBeNull();
    const retry = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "再試行")!;
    act(() => retry.click());
    expect(onLoadMore).toHaveBeenCalledTimes(2);
  });

  it("一括選択と「選択中だけ表示」: 読み込みの範囲の外の選択も確かめられる", () => {
    const items = makeItems(3);
    function SelectedHarness() {
      const [selected, setSelected] = useState<Set<string>>(new Set(["old-0001"]));
      const selectedItems = [
        { key: "old-0001", label: "old-0001", textValue: "old-0001" },
        ...items.filter((item) => selected.has(item.key)),
      ].filter((item) => selected.has(item.key));
      return (
        <ListPicker
          id="picker"
          label="候補"
          items={items}
          selectedKeys={selected}
          selectedItems={selectedItems}
          onToggle={(item, next) =>
            setSelected((current) => {
              const copy = new Set(current);
              if (next) copy.add(item.key);
              else copy.delete(item.key);
              return copy;
            })
          }
          onSelectMany={(targets) => setSelected((current) => new Set([...current, ...targets.map((t) => t.key)]))}
          onClearSelection={() => setSelected(new Set())}
          testId="picker"
        />
      );
    }
    render(<SelectedHarness />);
    const button = (name: string) =>
      Array.from(host.querySelectorAll("button")).find((node) => node.textContent === name)!;
    act(() => button("表示中をすべて選択").click());
    expect(options().every((node) => node.getAttribute("aria-checked") === "true")).toBe(true);
    const chip = host.querySelector<HTMLButtonElement>('[data-testid="picker-show-selected"]')!;
    expect(chip.textContent).toBe("選択中だけ表示（4）");
    act(() => chip.click());
    expect(chip.getAttribute("aria-pressed")).toBe("true");
    expect(options().map((node) => node.textContent)).toContain("old-0001");
    act(() => button("選択をすべて解除").click());
    expect(host.textContent).toContain(DEFAULT_LIST_PICKER_LABELS.selectedEmpty);
  });

  it("100 行までは全部描き（ページ内検索・読み上げで届く）、超えると見えている行だけを描く", () => {
    render(<Harness items={makeItems(100)} />);
    expect(options()).toHaveLength(100);
    render(<Harness items={makeItems(101)} />);
    expect(options().length).toBeLessThan(60);
  });

  it("無効（保存中・fieldset の disabled）の間は選択を切り替えない", () => {
    const spy = vi.fn();
    render(<Harness items={makeItems(2)} onToggleSpy={spy} disabled />);
    const [listbox] = listboxes();
    expect(listbox.getAttribute("aria-disabled")).toBe("true");
    expect(listbox.tabIndex).toBe(-1);
    expect(option("doc-0001")!.getAttribute("aria-disabled")).toBe("true");
    act(() => option("doc-0001")!.click());
    expect(spy).not.toHaveBeenCalled();

    render(
      <fieldset disabled>
        <Harness items={makeItems(2)} onToggleSpy={spy} />
      </fieldset>
    );
    act(() => option("doc-0001")!.click());
    expect(spy).not.toHaveBeenCalled();
  });

  it("読み込み中は経過時間と、行の形の Skeleton を出す", () => {
    render(<Harness items={[]} loading />);
    expect(host.querySelector('[data-testid="picker-loading"]')?.getAttribute("aria-label")).toBe(
      DEFAULT_LIST_PICKER_LABELS.loading
    );
    expect(host.querySelector('[data-skeleton="list"]')).not.toBeNull();
    expect(listboxes()).toHaveLength(0);
  });
});

describe("ListToolbar", () => {
  it("検索欄を先頭（左）に、件数と操作を右に置く", () => {
    render(
      <ListToolbar
        search={<input aria-label="検索" />}
        filters={<span data-testid="filters" />}
        summary="12 件"
        actions={<button type="button">追加</button>}
      />
    );
    const start = host.querySelector("[data-list-toolbar-start]")!;
    const end = host.querySelector("[data-list-toolbar-end]")!;
    expect(start.firstElementChild?.hasAttribute("data-list-toolbar-search")).toBe(true);
    expect(start.querySelector('[data-testid="filters"]')).not.toBeNull();
    expect(end.textContent).toBe("12 件追加");
    // DOM の順（= Tab の順）も検索が先。
    expect(start.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
