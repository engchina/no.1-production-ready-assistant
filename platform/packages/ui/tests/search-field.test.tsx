// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isImeComposing, isSubmitEnter, SEARCH_FIELD_DEBOUNCE_MS, SearchField, type SearchFieldProps } from "../src";

// #535: 一覧の絞り込みの共通の検索欄。入力に合わせて絞り込み（debounce）、Enter ですぐ反映、IME の変換中は絞り込まない。

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
  vi.useRealTimers();
});

/** 親が適用中の検索語を持つ、実際の使い方と同じ形。 */
function Harness({
  initial = "",
  onSearch,
  ...props
}: { initial?: string; onSearch: (value: string) => void } & Partial<SearchFieldProps>) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <SearchField
        id="q"
        label="名前で検索"
        clearLabel="検索語をクリア"
        {...props}
        value={value}
        onSearch={(next) => {
          onSearch(next);
          setValue(next);
        }}
      />
      <button type="button" data-testid="reset" onClick={() => setValue("")}>
        reset
      </button>
      <output data-testid="applied">{value}</output>
    </>
  );
}

function mount(node: React.ReactNode) {
  act(() => root.render(node));
  return host.querySelector("input") as HTMLInputElement;
}

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;

/** React の onChange に届くように、値をネイティブの setter で入れて input event を出す。 */
function type(input: HTMLInputElement, value: string, { composing = false } = {}) {
  act(() => {
    valueSetter.call(input, value);
    const event = new Event("input", { bubbles: true });
    Object.defineProperty(event, "isComposing", { value: composing });
    input.dispatchEvent(event);
  });
}

function composition(input: HTMLInputElement, name: "compositionstart" | "compositionend") {
  act(() => {
    input.dispatchEvent(new Event(name, { bubbles: true }));
  });
}

function keydown(input: HTMLInputElement, key: string, { composing = false } = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  Object.defineProperty(event, "isComposing", { value: composing });
  act(() => {
    input.dispatchEvent(event);
  });
  return event;
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe("SearchField の見た目と属性", () => {
  it("type=search・虫眼鏡・ラベルの結び付き・enterkeyhint=search を持ち、検索ボタンを置かない", () => {
    const input = mount(<Harness onSearch={vi.fn()} />);
    expect(input.type).toBe("search");
    expect(input.getAttribute("enterkeyhint")).toBe("search");
    expect(input.getAttribute("autocomplete")).toBe("off");
    expect(host.querySelector('label[for="q"]')?.textContent).toBe("名前で検索");
    expect(host.querySelector('svg[data-text-field-slot="leading"]')?.getAttribute("class")).toContain("lucide-search");
    // 値が空のときは消去ボタンもない（ボタンは reset だけ）。
    expect(host.querySelectorAll("button")).toHaveLength(1);
  });
});

describe("SearchField の debounce と Enter", () => {
  it("入力が止まってから 300ms 後に 1 回だけ絞り込む", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "a");
    type(input, "ab");
    advance(SEARCH_FIELD_DEBOUNCE_MS - 1);
    type(input, "abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS - 1);
    expect(onSearch).not.toHaveBeenCalled();
    advance(1);
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("abc");
  });

  it("Enter は debounce を待たずにすぐ反映し、フォームを送信しない（後から同じ値で再度呼ばない）", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "sales");
    const event = keydown(input, "Enter");
    expect(event.defaultPrevented).toBe(true);
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("sales");
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("前後の空白は落とし、確定した値が変わらなければ呼ばない（ページを空打ちで戻さない）", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "  abc ");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(onSearch).toHaveBeenLastCalledWith("abc");
    type(input, "abc  ");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(onSearch).toHaveBeenCalledTimes(1);
    // 親が value を "abc" に更新しても、入力中の文字（末尾の空白）を上書きしない。
    expect(input.value).toBe("abc  ");
  });

  it("debounceMs=0 なら入力のたびにすぐ呼ぶ", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} debounceMs={0} />);
    type(input, "x");
    expect(onSearch).toHaveBeenLastCalledWith("x");
  });

  it("formatInput は入力中の文字の見せ方を変える（IME の変換中は変えない）", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} formatInput={(value) => value.toUpperCase()} />);
    type(input, "sal");
    expect(input.value).toBe("SAL");
    composition(input, "compositionstart");
    type(input, "SALa", { composing: true });
    expect(input.value).toBe("SALa");
    composition(input, "compositionend");
    expect(input.value).toBe("SALA");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(onSearch).toHaveBeenLastCalledWith("SALA");
  });

  it("normalize で渡す値を変えられる（例: 所有者の接頭辞を大文字にする）", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} normalize={(value) => value.trim().toUpperCase()} />);
    type(input, "hr");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(onSearch).toHaveBeenLastCalledWith("HR");
    expect(input.value).toBe("hr");
  });
});

describe("SearchField の IME（日本語入力）", () => {
  it("変換中の入力では絞り込まず、変換を確定した値で絞り込む", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    composition(input, "compositionstart");
    type(input, "と", { composing: true });
    type(input, "とうきょう", { composing: true });
    type(input, "東京", { composing: true });
    advance(SEARCH_FIELD_DEBOUNCE_MS * 3);
    expect(onSearch).not.toHaveBeenCalled();
    // 変換中の値も入力欄には出る（制御された値が下書きを追う）。
    expect(input.value).toBe("東京");

    // 変換を確定する Enter では絞り込まない（フォームの既定動作も止めない）。
    const enter = keydown(input, "Enter", { composing: true });
    expect(enter.defaultPrevented).toBe(false);
    expect(onSearch).not.toHaveBeenCalled();

    composition(input, "compositionend");
    advance(SEARCH_FIELD_DEBOUNCE_MS - 1);
    expect(onSearch).not.toHaveBeenCalled();
    advance(1);
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("東京");
  });

  it("compositionstart で、待っていた絞り込みを取り消す", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS - 100);
    composition(input, "compositionstart");
    type(input, "abcか", { composing: true });
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onSearch).not.toHaveBeenCalled();
    composition(input, "compositionend");
    const enter = keydown(input, "Enter");
    expect(enter.defaultPrevented).toBe(true);
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("abcか");
  });

  it("isImeComposing / isSubmitEnter は Chrome・Firefox（isComposing）と Safari（keyCode 229）の確定の Enter を送信にしない", () => {
    expect(isSubmitEnter({ key: "Enter", keyCode: 13, nativeEvent: { isComposing: false } })).toBe(true);
    expect(isSubmitEnter({ key: "Enter", keyCode: 229, nativeEvent: { isComposing: true } })).toBe(false);
    expect(isSubmitEnter({ key: "Enter", keyCode: 229, nativeEvent: { isComposing: false } })).toBe(false);
    expect(isImeComposing({ key: "Enter", isComposing: true })).toBe(true);
    expect(isSubmitEnter({ key: "a", keyCode: 65 })).toBe(false);
  });
});

describe("SearchField の消去と外からの変更", () => {
  it("消去（×）は debounce を待たずに空で絞り込み、入力欄へフォーカスを戻す", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} initial="abc" />);
    expect(input.value).toBe("abc");
    const clear = host.querySelector('button[aria-label="検索語をクリア"]') as HTMLButtonElement;
    act(() => clear.click());
    expect(onSearch).toHaveBeenCalledWith("");
    expect(input.value).toBe("");
    expect(document.activeElement).toBe(input);
  });

  it("Escape でも消す", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} initial="abc" />);
    keydown(input, "Escape");
    expect(onSearch).toHaveBeenCalledWith("");
    expect(input.value).toBe("");
  });

  it("親が検索語を外から変えたら（条件のリセット）、入力欄を合わせ、待っている絞り込みを捨てる", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} initial="abc" />);
    type(input, "abcd");
    act(() => (host.querySelector('[data-testid="reset"]') as HTMLButtonElement).click());
    expect(input.value).toBe("");
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onSearch).not.toHaveBeenCalled();
  });

  it("入力欄が外れるとき（一覧 ⇄ 作成の切り替えなど）は、待っている絞り込みをその場で確定する", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} initial="sales" />);
    type(input, "");
    act(() => root.render(null));
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("");
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("ページを離れるとき（pagehide。再読込など）は、待っている絞り込みをその場で確定する（#3385）", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "abc");
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenLastCalledWith("abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS * 2);
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("確定済み・外れた後の pagehide では呼び直さない", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(onSearch).toHaveBeenCalledTimes(1);
    act(() => root.render(null));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("確定済みなら、外れるときに呼び直さない", () => {
    const onSearch = vi.fn();
    const input = mount(<Harness onSearch={onSearch} />);
    type(input, "abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    act(() => root.render(null));
    expect(onSearch).toHaveBeenCalledTimes(1);
  });
});

describe("SearchField の件数の読み上げ", () => {
  it("resultCountLabel は検索語があるときだけ role=status（aria-live=polite）に入れる", () => {
    const input = mount(<Harness onSearch={vi.fn()} resultCountLabel="12 件中 3 件" />);
    const status = host.querySelector('[role="status"]') as HTMLElement;
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.className).toContain("sr-only");
    expect(status.textContent).toBe("");
    type(input, "abc");
    advance(SEARCH_FIELD_DEBOUNCE_MS);
    expect(status.textContent).toBe("12 件中 3 件");
  });

  it("resultCountLabel を渡さなければ live region を置かない", () => {
    mount(<Harness onSearch={vi.fn()} />);
    expect(host.querySelector('[role="status"]')).toBeNull();
  });
});
