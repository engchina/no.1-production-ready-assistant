// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Database } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  filterSearchableOptions,
  SEARCH_FIELD_DEBOUNCE_MS,
  SearchableMultiSelect,
  type SearchableMultiSelectProps,
  SearchableSelectField,
  type SearchableSelectFieldProps,
  type SearchableSelectOption,
} from "../src";

// #578: 数百件の選択肢を検索して選ぶ部品（単一・複数）。キーボード・IME・読み上げを確かめる。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const OPTIONS: SearchableSelectOption[] = Array.from({ length: 300 }, (_, index) => ({
  value: `kb-${index + 1}`,
  label: index === 0 ? "利用できるすべてのナレッジベースを対象にする、とても長い名前のナレッジベース" : `ナレッジベース-${index + 1}`,
  meta: `${index} 文書`,
  hideable: index % 2 === 1,
}));

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;

function type(input: HTMLInputElement, value: string, { composing = false } = {}) {
  act(() => {
    valueSetter.call(input, value);
    const event = new Event("input", { bubbles: true });
    Object.defineProperty(event, "isComposing", { value: composing });
    input.dispatchEvent(event);
  });
}

function keydown(target: Element, key: string, { composing = false, shiftKey = false } = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, shiftKey });
  Object.defineProperty(event, "isComposing", { value: composing });
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

function composition(input: HTMLInputElement, name: "compositionstart" | "compositionend") {
  act(() => {
    input.dispatchEvent(new Event(name, { bubbles: true }));
  });
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

function click(element: Element) {
  act(() => {
    (element as HTMLElement).click();
  });
}

function focus(element: HTMLElement) {
  act(() => {
    element.focus();
  });
}

function options() {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'));
}

function MultiHarness({
  initial = [],
  onChange,
  ...props
}: { initial?: string[]; onChange?: (value: string[]) => void } & Partial<SearchableMultiSelectProps>) {
  const [value, setValue] = useState<string[]>(initial);
  return (
    <SearchableMultiSelect
      id="kb"
      label="ナレッジベース"
      options={OPTIONS}
      {...props}
      value={value}
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    />
  );
}

function mountMulti(props: Parameters<typeof MultiHarness>[0] = {}) {
  act(() => root.render(<MultiHarness {...props} />));
  return host.querySelector<HTMLInputElement>('input[role="combobox"]')!;
}

describe("filterSearchableOptions", () => {
  it("全角半角・大文字小文字を区別せず、空白で区切った語をすべて含む候補を残す", () => {
    const items = [
      { value: "a", label: "Sales Report", description: "営業" },
      { value: "b", label: "ｓａｌｅｓ 2026" },
      { value: "c", label: "人事" },
    ];
    expect(filterSearchableOptions(items, "SALES").map((item) => item.value)).toEqual(["a", "b"]);
    expect(filterSearchableOptions(items, "sales 営業").map((item) => item.value)).toEqual(["a"]);
    expect(filterSearchableOptions(items, "  ")).toBe(items);
  });
});

describe("SearchableMultiSelect", () => {
  it("検索欄は combobox で、ラベル・必須・一覧との結び付きを持つ。フォーカスで一覧を開く", () => {
    const input = mountMulti({ required: true });
    expect(input.getAttribute("aria-labelledby")).toBeTruthy();
    expect(document.getElementById(input.getAttribute("aria-labelledby")!)?.textContent).toContain("ナレッジベース");
    expect(input.getAttribute("aria-required")).toBe("true");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.getAttribute("aria-autocomplete")).toBe("list");
    expect(document.querySelector('[role="listbox"]')).toBeNull();

    focus(input);
    const listbox = document.querySelector('[role="listbox"]')!;
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(input.getAttribute("aria-controls")).toBe(listbox.id);
    expect(listbox.getAttribute("aria-multiselectable")).toBe("true");
    expect(listbox.getAttribute("aria-label")).toBe("ナレッジベース");
    expect(host.textContent).toContain("300 / 300 件");
  });

  it("入力が止まって 300ms で画面側を絞り込み、件数を読み上げる", () => {
    const onQueryChange = vi.fn();
    const input = mountMulti({ onQueryChange });
    focus(input);
    type(input, "-12");
    advance(SEARCH_FIELD_DEBOUNCE_MS - 1);
    expect(options()).toHaveLength(300);
    advance(1);
    // -12, -120〜-129 の 11 件
    expect(options()).toHaveLength(11);
    expect(onQueryChange).toHaveBeenLastCalledWith("-12");
    const status = host.querySelector('[role="status"][data-search-field-status]');
    expect(status?.textContent).toBe("11 / 300 件");
  });

  it("↑↓ で強調を動かし（aria-activedescendant）、Enter で選択を切り替える。選んでも一覧は開いたまま", () => {
    const onChange = vi.fn();
    const input = mountMulti({ onChange });
    focus(input);
    expect(input.getAttribute("aria-activedescendant")).toBe(options()[0].id);
    keydown(input, "ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe(options()[1].id);
    const enter = keydown(input, "Enter");
    expect(enter.defaultPrevented).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith(["kb-2"]);
    expect(options()[1].getAttribute("aria-selected")).toBe("true");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    keydown(input, "ArrowUp");
    keydown(input, "Enter");
    expect(onChange).toHaveBeenLastCalledWith(["kb-2", "kb-1"]);
    keydown(input, "Enter");
    expect(onChange).toHaveBeenLastCalledWith(["kb-2"]);
  });

  it("IME の変換中は絞り込まず、変換を確定する Enter・矢印では候補を操作しない", () => {
    const onChange = vi.fn();
    const onQueryChange = vi.fn();
    const input = mountMulti({ onChange, onQueryChange });
    focus(input);
    composition(input, "compositionstart");
    type(input, "なれ", { composing: true });
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onQueryChange).not.toHaveBeenCalled();
    const arrow = keydown(input, "ArrowDown", { composing: true });
    expect(arrow.defaultPrevented).toBe(false);
    keydown(input, "Enter", { composing: true });
    expect(onChange).not.toHaveBeenCalled();
    composition(input, "compositionend");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(onQueryChange).toHaveBeenLastCalledWith("なれ");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("Esc で一覧だけを閉じ（囲むダイアログへ伝えない）、閉じた後の Esc で検索語を消す", () => {
    const outer = vi.fn();
    const input = mountMulti();
    document.addEventListener("keydown", outer);
    focus(input);
    type(input, "abc");
    const first = keydown(input, "Escape");
    expect(first.defaultPrevented).toBe(true);
    expect(outer).not.toHaveBeenCalled();
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.value).toBe("abc");
    keydown(input, "Escape");
    expect(input.value).toBe("");
    document.removeEventListener("keydown", outer);
  });

  it("選択済みは chip で名前を切らずに出し、状態の文字と「外す」を持つ。一覧に無い値も外せる", () => {
    const onChange = vi.fn();
    mountMulti({
      initial: ["kb-1", "kb-archived", "kb-unknown"],
      onChange,
      selectedOptions: [{ value: "kb-archived", label: "旧規程", badge: "アーカイブ済み" }],
    });
    const chips = host.querySelector('ul[aria-label="選択中のナレッジベース"]')!;
    expect(chips.textContent).toContain(OPTIONS[0].label);
    expect(chips.querySelector("span")?.className).not.toContain("truncate");
    expect(chips.textContent).toContain("旧規程アーカイブ済み");
    expect(chips.textContent).toContain("kb-unknown");
    click(host.querySelector('[aria-label="旧規程 を選択から外す"]')!);
    expect(onChange).toHaveBeenLastCalledWith(["kb-1", "kb-unknown"]);
  });

  it("hideable な候補は既定で隠し（選択済みは残す）、チェックを外すと出す", () => {
    const input = mountMulti({
      initial: ["kb-2"],
      labels: { hideHideable: "空のKBを隠す", hiddenCount: (count) => `空 ${count} 件を非表示中` },
    });
    focus(input);
    // 奇数番目（kb-2, kb-4, …）が hideable。kb-2 は選択済みなので残る。
    expect(options()).toHaveLength(151);
    expect(host.textContent).toContain("空 149 件を非表示中");
    click(host.querySelector('input[type="checkbox"]')!);
    expect(options()).toHaveLength(300);
  });

  it("サーバー側の検索（remote）では画面側で絞り込まず、「さらに表示」と最後の候補からの ↓ で続きを読む", () => {
    const onLoadMore = vi.fn();
    const onQueryChange = vi.fn();
    const page = OPTIONS.slice(0, 50);
    const input = mountMulti({
      options: page,
      onQueryChange,
      remote: { total: 300, hasMore: true, loadingMore: false, searching: false, onLoadMore },
    });
    focus(input);
    expect(options()).toHaveLength(50);
    expect(host.textContent).toContain("50 / 300 件");
    type(input, "存在しない");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    // 絞り込みは呼び出し側（サーバー）が行う。
    expect(onQueryChange).toHaveBeenLastCalledWith("存在しない");
    expect(options()).toHaveLength(50);
    click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "さらに表示")!);
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    for (let step = 0; step < 49; step += 1) keydown(input, "ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe(options()[49].id);
    keydown(input, "ArrowDown");
    expect(onLoadMore).toHaveBeenCalledTimes(2);
  });

  it("「完了」で一覧を閉じて検索欄へ戻り、戻したことで開き直さない", () => {
    const input = mountMulti();
    focus(input);
    click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "完了")!);
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(input);
  });
});

function SingleHarness({
  initial = "",
  onChange,
  ...props
}: { initial?: string; onChange?: (value: string) => void } & Partial<SearchableSelectFieldProps>) {
  const [value, setValue] = useState(initial);
  return (
    <SearchableSelectField
      id="kb-filter"
      label="ナレッジベース"
      options={OPTIONS}
      {...props}
      value={value}
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    />
  );
}

function mountSingle(props: Parameters<typeof SingleHarness>[0] = {}) {
  act(() => root.render(<SingleHarness {...props} />));
  return host.querySelector<HTMLButtonElement>("#kb-filter")!;
}

function searchInput() {
  return document.querySelector<HTMLInputElement>('[role="dialog"] input[role="combobox"]')!;
}

describe("SearchableSelectField", () => {
  it("ボタンは選択中の名前を切らずに出し、ラベルと値の両方で読み上げる", () => {
    const button = mountSingle({ initial: "kb-1" });
    const [labelId, valueId] = button.getAttribute("aria-labelledby")!.split(" ");
    expect(document.getElementById(labelId)?.textContent).toBe("ナレッジベース");
    expect(document.getElementById(valueId)?.textContent).toBe(OPTIONS[0].label);
    expect(document.getElementById(valueId)?.className).not.toContain("truncate");
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("required はボタンに aria-required を付けず（role=button は持てない）、ラベルの「必須」のタグを読み上げる（#1595）", () => {
    const button = mountSingle({ initial: "kb-1", required: true });
    expect(button.hasAttribute("aria-required")).toBe(false);
    const [labelId] = button.getAttribute("aria-labelledby")!.split(" ");
    const label = document.getElementById(labelId)!;
    expect(label.textContent).toBe("ナレッジベース必須");
    expect(label.querySelector('[aria-hidden="true"]')).toBeNull();
  });

  it("閉じたボタンは値と開閉の印だけで虫眼鏡を持たず、虫眼鏡は開いたパネルの検索欄だけにある（#1771）", () => {
    const button = mountSingle({ placeholder: "ナレッジベースを検索して選択…" });
    // ボタンの svg は開閉の印（chevron）だけ。
    const icons = Array.from(button.querySelectorAll("svg"));
    expect(icons.map((icon) => icon.getAttribute("class"))).toEqual([expect.stringContaining("lucide-chevron-down")]);
    expect(button.querySelector("svg.lucide-search")).toBeNull();
    click(button);
    advance(20);
    const panel = document.querySelector<HTMLElement>("[data-searchable-select-popover]")!;
    expect(panel.querySelectorAll("svg.lucide-search")).toHaveLength(1);
    expect(searchInput().parentElement?.querySelector("svg.lucide-search")).not.toBeNull();
    expect(button.querySelector("svg.lucide-search")).toBeNull();
  });

  it("leadingIcon（対象の種類のアイコン）はボタンの先頭に読み上げない 16px のアイコンを出し、ボタンの名前を変えない（#635）", () => {
    const button = mountSingle({ leadingIcon: Database, placeholder: "データベースを選択…" });
    const icon = button.querySelector("svg");
    expect(icon?.getAttribute("aria-hidden")).toBe("true");
    expect(icon?.getAttribute("width")).toBe("16");
    // 先頭のアイコンは値の前（最後の svg は開閉の印）。
    expect(button.querySelectorAll("svg")).toHaveLength(2);
    const [, valueId] = button.getAttribute("aria-labelledby")!.split(" ");
    expect(document.getElementById(valueId)?.textContent).toBe("データベースを選択…");
  });

  it("選択中が候補（検索結果のページ）に無いときは selectedOption の名前を出す", () => {
    const button = mountSingle({
      initial: "kb-999",
      selectedOption: { value: "kb-999", label: "遠くのナレッジベース" },
    });
    expect(button.textContent).toContain("遠くのナレッジベース");
  });

  it("開くと検索欄へ移り、選択中を強調する。↓ と Enter で選んで閉じ、ボタンへ戻る", () => {
    const onChange = vi.fn();
    const button = mountSingle({ initial: "kb-3", onChange });
    click(button);
    advance(20);
    const input = searchInput();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe("ナレッジベース");
    expect(input.getAttribute("aria-label") ?? document.querySelector(`label[for="${input.id}"]`)?.textContent).toBe(
      "ナレッジベースを検索"
    );
    expect(input.getAttribute("aria-activedescendant")).toBe(options()[2].id);
    expect(options()[2].getAttribute("aria-selected")).toBe("true");
    keydown(input, "ArrowDown");
    const enter = keydown(input, "Enter");
    expect(enter.defaultPrevented).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith("kb-4");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("入力で絞り込み、IME の変換中は絞り込まず Enter でも選ばない", () => {
    const onChange = vi.fn();
    const button = mountSingle({ onChange });
    click(button);
    const input = searchInput();
    composition(input, "compositionstart");
    type(input, "ないれ", { composing: true });
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(options()).toHaveLength(300);
    keydown(input, "Enter", { composing: true });
    expect(onChange).not.toHaveBeenCalled();
    composition(input, "compositionend");
    type(input, "-25");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    // -25, -250〜-259
    expect(options()).toHaveLength(11);
  });

  it("Esc で閉じてボタンへ戻り、囲むダイアログへ伝えない。検索語は次に開いたとき残らない", () => {
    const outer = vi.fn();
    const button = mountSingle();
    document.addEventListener("keydown", outer);
    click(button);
    const input = searchInput();
    type(input, "-7");
    const escape = keydown(input, "Escape");
    expect(escape.defaultPrevented).toBe(true);
    expect(outer).not.toHaveBeenCalled();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(button);
    click(button);
    expect(searchInput().value).toBe("");
    expect(options()).toHaveLength(300);
    document.removeEventListener("keydown", outer);
  });

  it("Tab で閉じてボタンへ戻る（Shift+Tab はボタンに止まる）", () => {
    const button = mountSingle();
    click(button);
    const shiftTab = keydown(searchInput(), "Tab", { shiftKey: true });
    expect(shiftTab.defaultPrevented).toBe(true);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("サーバー側の検索（remote）では、最後の候補から ↓ で続きを読む", () => {
    listHeights({ scrollHeight: 600, clientHeight: 300 }); // 一覧があふれている（自動では読まない）
    const onLoadMore = vi.fn();
    const button = mountSingle({
      options: OPTIONS.slice(0, 3),
      remote: { total: 300, hasMore: true, loadingMore: false, searching: false, onLoadMore },
    });
    click(button);
    const input = searchInput();
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("3 / 300 件");
    keydown(input, "ArrowDown");
    keydown(input, "ArrowDown");
    expect(onLoadMore).not.toHaveBeenCalled();
    keydown(input, "ArrowDown");
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("読み込んだ候補が一覧の高さに満たず続きがあるときは、スクロールを待たずに続きを読む（#1708）", () => {
    listHeights({ scrollHeight: 150, clientHeight: 150 }); // 3 件で一覧がスクロールしない
    const onLoadMore = vi.fn();
    const remote = { total: null as number | null, hasMore: true, loadingMore: false, searching: false, onLoadMore };
    const button = mountSingle({ options: OPTIONS.slice(0, 3), remote: { ...remote, total: 300 } });
    expect(onLoadMore).not.toHaveBeenCalled(); // 開くまでは読まない
    click(button);
    expect(onLoadMore).toHaveBeenCalledTimes(1);

    // 読み込み中は待ち、読み終えて候補が増えても一覧に満たなければ、もう一度読む。
    act(() => root.render(<SingleHarness options={OPTIONS.slice(0, 3)} remote={{ ...remote, total: 300, loadingMore: true }} />));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    act(() => root.render(<SingleHarness options={OPTIONS.slice(0, 5)} remote={{ ...remote, total: 300 }} />));
    expect(onLoadMore).toHaveBeenCalledTimes(2);

    // 続きを読んでも候補が増えなければ、自動では繰り返さない。
    act(() => root.render(<SingleHarness options={OPTIONS.slice(0, 5)} remote={{ ...remote, total: 300, loadingMore: true }} />));
    act(() => root.render(<SingleHarness options={OPTIONS.slice(0, 5)} remote={{ ...remote, total: 300 }} />));
    expect(onLoadMore).toHaveBeenCalledTimes(2);

    // 続きが無ければ読まない。
    act(() => root.render(<SingleHarness options={OPTIONS.slice(0, 7)} remote={{ ...remote, total: 7, hasMore: false }} />));
    expect(onLoadMore).toHaveBeenCalledTimes(2);
  });

  it("検索中は自動で続きを読まない", () => {
    listHeights({ scrollHeight: 150, clientHeight: 150 });
    const onLoadMore = vi.fn();
    const button = mountSingle({
      options: OPTIONS.slice(0, 3),
      remote: { total: 300, hasMore: true, loadingMore: false, searching: true, onLoadMore },
    });
    click(button);
    expect(onLoadMore).not.toHaveBeenCalled();
  });

  it("選択肢が無いまま取得中は、一覧の先頭に左寄せの読み込み中の行と選択肢の形の Skeleton を出す（#2510）", () => {
    const button = mountSingle({
      options: [],
      remote: { total: 0, hasMore: false, loadingMore: false, searching: true, onLoadMore: vi.fn() },
    });
    click(button);
    const loading = document.querySelector('[data-testid="select-list-loading"]')!;
    expect(loading).not.toBeNull();
    expect(loading.querySelector(".pr-spinner")).not.toBeNull();
    expect(loading.querySelector('[data-skeleton="list"]')).not.toBeNull();
    expect(loading.className).not.toContain("text-center");
  });
});

/** happy-dom はレイアウトしないので、一覧の高さを決めて「あふれているか」を再現する。 */
function listHeights({ scrollHeight, clientHeight }: { scrollHeight: number; clientHeight: number }) {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(scrollHeight);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(clientHeight);
}

describe("チャットの対象のコンパクトな選択", () => {
  it("選択名は一行に収め、読み上げ名に欄名と必須を保持する", () => {
    act(() => root.render(<SearchableSelectField id="synthetic-target" label="対象" required labelPlacement="compact" size="sm" value="kb-1" options={OPTIONS} onValueChange={() => {}} />));
    const button = host.querySelector<HTMLButtonElement>("#synthetic-target")!;
    const ids = button.getAttribute("aria-labelledby")!.split(" ");
    expect(ids.map(id => document.getElementById(id)?.textContent).join(" ")).toContain("対象必須");
    expect(host.querySelector("[data-filter-select-label]")).toBeNull();
    expect(document.getElementById(ids[1])?.getAttribute("title")).toBe(OPTIONS[0].label);
    act(() => button.click());
    expect(document.querySelector("[role=dialog]")?.getAttribute("aria-label")).toBe("対象");
  });
});
