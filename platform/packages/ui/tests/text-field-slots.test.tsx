import { readFileSync } from "node:fs";

import { Search } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { Button } from "../src/components/ui/button";
import { SelectField } from "../src/components/ui/select-field";
import { Tabs } from "../src/components/ui/tabs";
import {
  clearTextField,
  hasTextValue,
  shouldClearOnEscape,
  TEXT_FIELD_LEADING_PADDING_CLASS,
  TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS,
  TextField,
  type TextFieldProps,
} from "../src/components/ui/text-field";
import { cn } from "../src/lib/utils";

// #384: TextField の先頭アイコン・後置スロット・クリアと、操作部品の角丸（--radius-control）。
// #374: 強制カラーモードで、選ばれていないタブに下線を出さない。

const noop = () => {};

function render(props: Partial<TextFieldProps> = {}) {
  return renderToStaticMarkup(<TextField id="q" label="検索" value="" onChange={noop} {...props} />);
}

/** 開始タグを取り出す（属性の並び順に依存しない検証のため）。 */
function openingTag(html: string, pattern: RegExp): string {
  const tag = (html.match(/<[a-z]+\b[^>]*>/g) ?? []).find((candidate) => pattern.test(candidate));
  if (!tag) throw new Error(`開始タグが見つかりません: ${pattern}`);
  return tag;
}

const classesOf = (tag: string) => tag.match(/class="([^"]*)"/)?.[1].replaceAll("&amp;", "&").split(" ") ?? [];
const input = (html: string) => openingTag(html, /^<input\b/);

describe("TextField の先頭アイコン（leadingIcon）", () => {
  it("アイコンが無いときは余白もスロットも足さない（既存の入力欄の見た目を変えない）", () => {
    const html = render();
    expect(classesOf(input(html))).not.toContain(TEXT_FIELD_LEADING_PADDING_CLASS);
    expect(classesOf(input(html))).not.toContain(TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS);
    expect(html).not.toContain("data-text-field-slot");
    expect(classesOf(input(html))).toContain("px-3");
  });

  it("16px のアイコンを読み上げずに出し、ポインタを透過して入力欄にフォーカスが入るようにする", () => {
    const html = render({ leadingIcon: Search });
    const icon = openingTag(html, /^<svg\b[^>]*data-text-field-slot="leading"/);
    expect(icon).toContain('aria-hidden="true"');
    expect(icon).toContain('width="16"');
    const iconClass = classesOf(icon);
    expect(iconClass).toEqual(expect.arrayContaining(["pointer-events-none", "absolute", "left-3", "top-1/2", "text-fg-muted"]));
    // disabled の入力欄ではアイコンも disabled の色（peer はアイコンより前に置く必要がある）。
    expect(iconClass).toContain("peer-disabled:text-fg-disabled");
    expect(html.indexOf("<input")).toBeLessThan(html.indexOf('data-text-field-slot="leading"'));
    expect(classesOf(input(html))).toContain("peer");
  });

  it("文字の開始位置 = アイコンの位置 + 16px + Button のアイコンと文字の間隔（トークンで表す）", () => {
    const cls = classesOf(input(render({ leadingIcon: Search })));
    expect(cls).toContain(TEXT_FIELD_LEADING_PADDING_CLASS);
    expect(TEXT_FIELD_LEADING_PADDING_CLASS).toBe("pl-[calc(var(--space-3)+var(--icon-md)+var(--button-gap))]");
  });

  it("labelHidden は見出しを読み上げだけにし、label と入力欄の結び付きは残す", () => {
    const html = render({ leadingIcon: Search, labelHidden: true });
    const label = openingTag(html, /^<label\b/);
    expect(label).toContain('for="q"');
    expect(classesOf(label)).toContain("sr-only");
    expect(classesOf(openingTag(render(), /^<label\b/))).not.toContain("sr-only");
  });
});

describe("TextField のクリア（onClear）と後置スロット（trailing）", () => {
  it("値が空のときはクリアボタンを出さない", () => {
    const html = render({ onClear: noop, clearLabel: "検索語をクリア" });
    expect(html).not.toContain("<button");
    expect(classesOf(input(html))).not.toContain(TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS);
  });

  it("値があるときは入力欄の直後に、読み上げ名の付いた iconOnly のクリアボタンを出し、文字の右に余白を空ける", () => {
    const html = render({ value: "契約", onClear: noop, clearLabel: "検索語をクリア" });
    const button = openingTag(html, /^<button\b/);
    expect(button).toContain('type="button"');
    expect(button).toContain('aria-label="検索語をクリア"');
    expect(button).toContain('aria-controls="q"');
    // Tab 順は入力欄 → クリアボタン（DOM の順）。
    expect(html.indexOf("<input")).toBeLessThan(html.indexOf("<button"));
    // 枠線まで含めた右端。入力欄と同じ高さ（md 36px・タッチ端末 44px）の正方形で、外側の角だけ入力欄の角丸に合わせる。
    // 地は枠線の内側だけに塗る（ホバーの地を入力欄の枠線に重ねない）。
    const slot = openingTag(html, /data-text-field-slot="trailing"/);
    expect(classesOf(slot)).toEqual(expect.arrayContaining(["absolute", "inset-y-0", "right-0"]));
    expect(classesOf(slot)).not.toContain("inset-y-px");
    expect(classesOf(button)).toEqual(
      expect.arrayContaining(["h-full", "min-h-0", "aspect-square", "rounded-l-none", "rounded-r-control", "bg-clip-padding"])
    );
    expect(classesOf(input(html))).toContain(TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS);
  });

  it("無効・読み取り専用の入力欄にはクリアボタンを出さない", () => {
    expect(render({ value: "契約", onClear: noop, clearLabel: "クリア", disabled: true })).not.toContain("<button");
    expect(render({ value: "契約", onClear: noop, clearLabel: "クリア", readOnly: true })).not.toContain("<button");
  });

  it("trailing の要素を枠線の内側の右端に置き、クリアボタンはその後ろに並べる", () => {
    const html = render({ value: "契約", onClear: noop, clearLabel: "クリア", trailing: <span data-testid="count">12 件</span> });
    const slotStart = html.indexOf('data-text-field-slot="trailing"');
    expect(slotStart).toBeGreaterThan(html.indexOf("<input"));
    expect(html.indexOf('data-testid="count"')).toBeGreaterThan(slotStart);
    expect(html.indexOf("<button")).toBeGreaterThan(html.indexOf('data-testid="count"'));
    expect(classesOf(input(render({ trailing: <span>件</span> })))).toContain(TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS);
    expect(render({ trailing: null })).not.toContain("data-text-field-slot");
  });

  it("type=search のブラウザ既定のクリアボタンを出さない（共有のクリアボタンと二重にしない）", () => {
    const cls = classesOf(input(render({ type: "search" })));
    expect(cls).toContain("[&::-webkit-search-cancel-button]:appearance-none");
    expect(cls).toContain("[&::-webkit-search-decoration]:appearance-none");
  });

  it("Escape は、消す値があり onClear があるときだけ入力を消す（空・IME の変換中は消さない）", () => {
    expect(shouldClearOnEscape({ key: "Escape", value: "abc", hasClear: true })).toBe(true);
    expect(shouldClearOnEscape({ key: "Escape", value: "", hasClear: true })).toBe(false);
    expect(shouldClearOnEscape({ key: "Escape", value: "abc", hasClear: false })).toBe(false);
    expect(shouldClearOnEscape({ key: "Escape", value: "abc", hasClear: true, isComposing: true })).toBe(false);
    expect(shouldClearOnEscape({ key: "Enter", value: "abc", hasClear: true })).toBe(false);
    expect(hasTextValue(0)).toBe(true);
    expect(hasTextValue(undefined)).toBe(false);
  });

  it("クリアは値を消してから入力欄にフォーカスを戻す（消えるボタンからフォーカスを body へ落とさない）", () => {
    const calls: string[] = [];
    const onClear = vi.fn(() => calls.push("clear"));
    clearTextField(onClear, { focus: () => calls.push("focus") });
    expect(calls).toEqual(["clear", "focus"]);
    expect(() => clearTextField(noop, null)).not.toThrow();
  });
});

describe("TextField の候補（suggestions、#547）", () => {
  it("候補があるとき、入力欄を datalist と結び（role=combobox になる）、ブラウザの入力履歴を混ぜない", () => {
    const html = render({ suggestions: ["10_経理", "人事"] });
    const tag = input(html);
    const listId = tag.match(/list="([^"]+)"/)?.[1];

    expect(listId).toBeTruthy();
    expect(tag).toMatch(/autocomplete="off"/i);
    expect(openingTag(html, /^<datalist\b/)).toContain(`id="${listId}"`);
    expect(html).toContain('<option value="10_経理">');
    expect(html).toContain('<option value="人事">');
  });

  it("候補が無い（空を含む）ときは datalist を出さず、autocomplete も変えない", () => {
    for (const suggestions of [undefined, []]) {
      const html = render({ suggestions });

      expect(html).not.toContain("<datalist");
      expect(input(html)).not.toMatch(/\slist=|autocomplete=/i);
    }
  });

  it("呼び出し側の autoComplete を優先する", () => {
    expect(input(render({ suggestions: ["a"], autoComplete: "organization" }))).toMatch(
      /autocomplete="organization"/i
    );
  });
});

describe("操作部品の角丸（--radius-control）", () => {
  const read = (path: string) => readFileSync(new URL(`../src/styles/${path}`, import.meta.url), "utf8");

  it("入力欄・SelectField・Button が同じ rounded-control を使う", () => {
    expect(classesOf(input(render()))).toContain("rounded-control");
    const select = renderToStaticMarkup(
      <SelectField id="s" label="種類" value="a" options={[{ value: "a", label: "A" }]} onValueChange={noop} />
    );
    expect(classesOf(openingTag(select, /^<button\b[^>]*role="combobox"/))).toContain("rounded-control");
    expect(classesOf(openingTag(renderToStaticMarkup(<Button>保存</Button>), /^<button\b/))).toContain("rounded-control");
  });

  it("トークンは px（ルート非依存）で、Button・入力欄の別名はそれを参照する。utility として登録する", () => {
    const radius = read("tokens/radius.css");
    expect(radius).toMatch(/--radius-control:\s*6px;/);
    expect(radius).toMatch(/--button-radius:\s*var\(--radius-control\);/);
    expect(radius).toMatch(/--input-radius:\s*var\(--radius-control\);/);
    expect(read("tokens.css")).toMatch(/@theme inline \{[\s\S]*--radius-control:\s*var\(--radius-control\);/);
  });

  it("呼び出し側の角丸の指定で上書きできる（tailwind-merge が rounded-control を角丸として扱う）", () => {
    expect(cn("rounded-control", "rounded-none")).toBe("rounded-none");
    expect(cn("rounded-md", "rounded-control")).toBe("rounded-control");
  });
});

describe("Tabs の強制カラーモード（#374）", () => {
  const tabs = renderToStaticMarkup(
    <Tabs
      value="a"
      items={[
        { id: "a", label: "すべて" },
        { id: "b", label: "失敗" },
      ]}
    />
  );
  const tab = (id: string) => classesOf(openingTag(tabs, new RegExp(`^<button\\b[^>]*id="pr-tab-${id}"`)));

  it("選ばれていないタブの透明の下線を、強制カラーモードでは背景と同じ Canvas にする（CanvasText に塗られない）", () => {
    expect(tab("b")).toContain("border-transparent");
    expect(tab("b")).toContain("forced-colors:border-b-[Canvas]");
  });

  it("選んだタブは Highlight の下線で区別し、文字の太さと aria-selected でも伝える", () => {
    expect(tab("a")).toContain("forced-colors:aria-selected:border-b-[Highlight]");
    expect(tab("a")).toContain("aria-selected:font-semibold");
    expect(openingTag(tabs, /^<button\b[^>]*id="pr-tab-a"/)).toContain('aria-selected="true"');
    expect(openingTag(tabs, /^<button\b[^>]*id="pr-tab-b"/)).toContain('aria-selected="false"');
  });
});
