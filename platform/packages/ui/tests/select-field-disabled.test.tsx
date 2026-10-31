// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SearchableSelectField, SelectField } from "../src";

// #631: 選択欄の無効な状態（SelectField / SearchableSelectField）と、製品のネイティブの <select> を置き換えるための
// labelHidden・data-testid・data-value を確かめる。

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
  document.body.innerHTML = "";
  vi.useRealTimers();
});

const OPTIONS = [
  { value: "apac", label: "アジア太平洋" },
  { value: "emea", label: "欧州" },
  { value: "us", label: "米国" },
];

function keydown(target: Element, key: string) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

function Harness({
  disabled,
  onChange,
  labelHidden,
}: {
  disabled: boolean;
  onChange?: (value: string) => void;
  labelHidden?: boolean;
}) {
  const [value, setValue] = useState("emea");
  return (
    <SelectField
      id="region"
      label="リージョン"
      value={value}
      options={OPTIONS}
      disabled={disabled}
      labelHidden={labelHidden}
      data-testid="region-select"
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    />
  );
}

function mount(props: Parameters<typeof Harness>[0]) {
  act(() => root.render(<Harness {...props} />));
  return host.querySelector<HTMLButtonElement>('button[role="combobox"]')!;
}

describe("SelectField の disabled", () => {
  it("ネイティブの disabled で、押しても・キーでも・typeahead でも開かず、値を変えない", () => {
    const onChange = vi.fn();
    const button = mount({ disabled: true, onChange });
    expect(button.disabled).toBe(true);
    // 無効な地と文字のトークン（TextField の disabled と同じ）。
    expect(button.className).toContain("disabled:bg-surface-disabled");
    expect(button.className).toContain("disabled:text-fg-disabled");
    expect(button.className).toContain("disabled:cursor-not-allowed");

    act(() => button.click());
    expect(button.getAttribute("aria-expanded")).toBe("false");
    for (const key of ["ArrowDown", "Enter", " ", "u"]) {
      keydown(button, key);
    }
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    expect(button.textContent).toContain("欧州");
  });

  it("有効なら開いて選べる。開いているあいだに無効になると一覧を閉じる", () => {
    const onChange = vi.fn();
    const button = mount({ disabled: false, onChange });
    expect(button.disabled).toBe(false);
    act(() => button.click());
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const listbox = document.querySelector('[role="listbox"]');
    expect(listbox).not.toBeNull();

    act(() => root.render(<Harness disabled onChange={onChange} />));
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[role="listbox"]')).toBeNull();
  });

  it("選択肢とボタンに値の data-value、ボタンに data-testid を出す（e2e が値で選ぶため）", () => {
    const button = mount({ disabled: false });
    expect(button.getAttribute("data-testid")).toBe("region-select");
    expect(button.getAttribute("data-value")).toBe("emea");
    act(() => button.click());
    const option = document.querySelector<HTMLElement>('[role="option"][data-value="us"]');
    expect(option?.textContent).toContain("米国");
    act(() => option!.click());
    expect(button.getAttribute("data-value")).toBe("us");
  });

  it("labelHidden はラベルを sr-only にし、読み上げ名は残す", () => {
    const button = mount({ disabled: false, labelHidden: true });
    const label = document.getElementById(button.getAttribute("aria-labelledby")!);
    expect(label?.textContent).toContain("リージョン");
    expect(label?.className).toContain("sr-only");
  });
});

describe("SearchableSelectField の disabled（SelectField とそろえる）", () => {
  function mountSearchable(disabled: boolean) {
    act(() =>
      root.render(
        <SearchableSelectField
          id="kb"
          label="ナレッジベース"
          value="emea"
          options={OPTIONS}
          disabled={disabled}
          onValueChange={() => {}}
        />
      )
    );
    return host.querySelector<HTMLButtonElement>("button#kb")!;
  }

  it("ネイティブの disabled で開かず、ホバーの地は有効なときだけ", () => {
    const button = mountSearchable(true);
    expect(button.disabled).toBe(true);
    expect(button.className).toContain("enabled:hover:bg-surface-hover");
    expect(button.className).toContain("disabled:bg-surface-disabled");
    act(() => button.click());
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[role="dialog"], [role="listbox"]')).toBeNull();
  });

  it("開いているあいだに無効になると閉じる", () => {
    vi.useFakeTimers();
    const button = mountSearchable(false);
    act(() => button.click());
    expect(button.getAttribute("aria-expanded")).toBe("true");
    mountSearchable(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });
});
