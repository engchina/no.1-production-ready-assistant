// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SearchableSelectField, SelectField } from "../src";

// #647: 任意の欄で未選択へ戻す選択肢（emptyOptionLabel）と、欄の外の説明への aria-describedby（describedBy）。

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
});

const OPTIONS = [
  { value: "apac", label: "アジア太平洋" },
  { value: "emea", label: "欧州" },
  { value: "us", label: "米国" },
];

function keydown(target: Element, key: string) {
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  });
}

function Harness({
  initial = "",
  required,
  options = OPTIONS,
  onChange,
}: {
  initial?: string;
  required?: boolean;
  options?: typeof OPTIONS;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SelectField
      id="region"
      label="リージョン"
      value={value}
      options={options}
      placeholder="リージョンを選択"
      emptyOptionLabel="未選択"
      required={required}
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    />
  );
}

function mount(props: Parameters<typeof Harness>[0] = {}) {
  act(() => root.render(<Harness {...props} />));
  return host.querySelector<HTMLButtonElement>('button[role="combobox"]')!;
}

function optionsInList() {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'));
}

describe("SelectField の emptyOptionLabel（任意の欄で未選択へ戻す）", () => {
  it("一覧の先頭に空の値の選択肢を出し、未選択のあいだボタンは placeholder を控えめの色で出す", () => {
    const button = mount();
    expect(button.getAttribute("data-value")).toBe("");
    expect(button.textContent).toBe("リージョンを選択");
    expect(button.querySelector("span")?.className).toContain("text-fg-muted");

    act(() => button.click());
    const options = optionsInList();
    expect(options.map((option) => option.textContent)).toEqual(["未選択", "アジア太平洋", "欧州", "米国"]);
    expect(options[0].getAttribute("data-value")).toBe("");
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    // 未選択の選択肢を強調して開く（ネイティブの select と同じく、選択中の選択肢から始める）。
    expect(button.getAttribute("aria-activedescendant")).toBe(options[0].id);
  });

  it("値を選んだ後に「未選択」を選ぶと onValueChange(\"\") を呼び、ボタンは placeholder に戻る", () => {
    const onChange = vi.fn();
    const button = mount({ initial: "emea", onChange });
    expect(button.textContent).toBe("欧州");

    act(() => button.click());
    act(() => optionsInList()[0].click());
    expect(onChange).toHaveBeenLastCalledWith("");
    expect(button.getAttribute("data-value")).toBe("");
    expect(button.textContent).toBe("リージョンを選択");
  });

  it("キーボード: Home で「未選択」を強調し Enter で選ぶ。↑↓ は未選択を含めて巡る", () => {
    const onChange = vi.fn();
    const button = mount({ initial: "emea", onChange });
    keydown(button, "ArrowDown");
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(button.getAttribute("aria-activedescendant")).toBe(optionsInList()[2].id);

    keydown(button, "Home");
    expect(button.getAttribute("aria-activedescendant")).toBe(optionsInList()[0].id);
    keydown(button, "ArrowUp");
    // 先頭から ↑ で末尾へ巡る（既存の動きと同じ）。
    expect(button.getAttribute("aria-activedescendant")).toBe(optionsInList()[3].id);
    keydown(button, "ArrowDown");
    expect(button.getAttribute("aria-activedescendant")).toBe(optionsInList()[0].id);
    keydown(button, "Enter");
    expect(onChange).toHaveBeenLastCalledWith("");
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("typeahead は未選択の選択肢があっても値の選択肢の位置を正しく引く", () => {
    const onChange = vi.fn();
    const button = mount({ onChange });
    keydown(button, "欧");
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const options = optionsInList();
    expect(button.getAttribute("aria-activedescendant")).toBe(options[2].id);
    expect(options[2].textContent).toBe("欧州");
    keydown(button, "Enter");
    expect(onChange).toHaveBeenLastCalledWith("emea");
  });

  it("required の欄には出さない（必須の欄を未選択へ戻す操作は要らない）", () => {
    const button = mount({ required: true, initial: "emea" });
    act(() => button.click());
    expect(optionsInList().map((option) => option.getAttribute("data-value"))).toEqual(["apac", "emea", "us"]);
  });

  it("options に空の値が既にあるときは重ねて足さず、その選択肢のラベルをボタンに出す", () => {
    const button = mount({ options: [{ value: "", label: "すべて" }, ...OPTIONS] });
    expect(button.textContent).toBe("すべて");
    act(() => button.click());
    expect(optionsInList().map((option) => option.textContent)).toEqual(["すべて", "アジア太平洋", "欧州", "米国"]);
  });

  it("値の選択肢が 0 件のあいだ（候補の取得中など）は「未選択」だけの一覧を開かない", () => {
    const button = mount({ options: [] });
    act(() => button.click());
    keydown(button, "ArrowDown");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[role="listbox"]')).toBeNull();
  });

  it("値の型が空の値を含まないときは型エラーにする", () => {
    type Region = "apac" | "emea";
    const element = (
      <SelectField<Region>
        id="typed"
        label="型"
        value="apac"
        options={[{ value: "apac", label: "アジア太平洋" }]}
        onValueChange={() => {}}
        // @ts-expect-error 空の値を持てない型では emptyOptionLabel を渡せない
        emptyOptionLabel="未選択"
      />
    );
    expect(element).toBeTruthy();
  });
});

describe("describedBy（欄の外の説明への aria-describedby）", () => {
  it("SelectField: helper・error の id の後ろに外の説明の id を足す", () => {
    act(() =>
      root.render(
        <>
          <SelectField
            id="dataset"
            label="種類"
            value="emea"
            options={OPTIONS}
            helper="補足"
            error="エラー"
            describedBy="dataset-description"
            onValueChange={() => {}}
          />
          <p id="dataset-description">説明</p>
        </>
      )
    );
    const button = host.querySelector<HTMLButtonElement>('button[role="combobox"]')!;
    const ids = button.getAttribute("aria-describedby")!.split(" ");
    expect(ids).toHaveLength(3);
    expect(document.getElementById(ids[0])?.textContent).toBe("補足");
    expect(document.getElementById(ids[1])?.textContent).toContain("エラー");
    expect(ids[2]).toBe("dataset-description");
  });

  it("SelectField: helper・error が無ければ外の説明の id だけ。どれも無ければ属性を付けない", () => {
    act(() =>
      root.render(
        <SelectField id="a" label="A" value="emea" options={OPTIONS} describedBy="x y" onValueChange={() => {}} />
      )
    );
    expect(host.querySelector("#a")?.getAttribute("aria-describedby")).toBe("x y");
    act(() => root.render(<SelectField id="a" label="A" value="emea" options={OPTIONS} onValueChange={() => {}} />));
    expect(host.querySelector("#a")?.hasAttribute("aria-describedby")).toBe(false);
  });

  it("SearchableSelectField も同じく結ぶ", () => {
    act(() =>
      root.render(
        <SearchableSelectField
          id="kb"
          label="ナレッジベース"
          value="emea"
          options={OPTIONS}
          helper="補足"
          describedBy="kb-description"
          onValueChange={() => {}}
        />
      )
    );
    const ids = host.querySelector("#kb")!.getAttribute("aria-describedby")!.split(" ");
    expect(ids).toHaveLength(2);
    expect(document.getElementById(ids[0])?.textContent).toBe("補足");
    expect(ids[1]).toBe("kb-description");
  });
});
