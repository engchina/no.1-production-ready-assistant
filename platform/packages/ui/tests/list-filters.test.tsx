// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LIST_TOOLBAR_LABELS,
  FILTER_CHIP_MAX_OPTIONS,
  FilterChipGroup,
  FilterSelect,
  isFilterActive,
  ListToolbar,
  SearchableSelectField,
  SelectField,
} from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

function render(node: React.ReactNode) {
  act(() => root.render(node));
}

const KIND_OPTIONS = [
  { value: "all", label: "すべて" },
  { value: "search", label: "検索" },
  { value: "chat", label: "チャット" },
] as const;

type Kind = (typeof KIND_OPTIONS)[number]["value"];

describe("FilterSelect（#1933）", () => {
  it("ラベルをボタンの中に「種類: すべて」と出し、上のラベルは読み上げだけにする", () => {
    render(<FilterSelect<Kind> id="kind" label="種類" value="all" options={KIND_OPTIONS} onValueChange={() => {}} />);
    const button = host.querySelector<HTMLButtonElement>('button[role="combobox"]')!;
    expect(button.textContent).toBe("種類:すべて");
    // 読み上げ名はラベル（aria-labelledby）。ボタンの中のラベルの文字は aria-hidden。
    const labelId = button.getAttribute("aria-labelledby")!;
    const label = document.getElementById(labelId)!;
    expect(label.textContent).toBe("種類");
    expect(label.className).toContain("sr-only");
    expect(host.querySelector("[data-filter-select-label]")?.getAttribute("aria-hidden")).toBe("true");
    // 上のラベルの欄の目印を付けない（ツールバーが中心でそろえる）。
    expect(host.querySelector("[data-field-label]")).toBeNull();
    // 既定の幅は sm。
    expect(button.parentElement?.className).toContain("--field-width-sm");
  });

  it("既定（先頭の値）以外を選ぶと絞り込み中として強調する", () => {
    function Harness() {
      const [value, setValue] = useState<Kind>("all");
      return <FilterSelect<Kind> id="kind" label="種類" value={value} options={KIND_OPTIONS} onValueChange={setValue} />;
    }
    render(<Harness />);
    const root = () => host.querySelector<HTMLElement>("[data-filter-active]")!;
    expect(root().dataset.filterActive).toBe("false");
    const button = host.querySelector<HTMLButtonElement>('button[role="combobox"]')!;
    act(() => button.click());
    const option = document.querySelector<HTMLElement>('[role="option"][data-value="chat"]')!;
    act(() => option.click());
    expect(root().dataset.filterActive).toBe("true");
    expect(button.className).toContain("border-accent-fg");
    expect(host.querySelector("[data-filter-select-value]")?.className).toContain("font-semibold");
    expect(button.textContent).toBe("種類:チャット");
  });

  it("defaultValue で「すべて」の値を指定できる", () => {
    render(
      <FilterSelect<Kind>
        id="kind"
        label="種類"
        value="all"
        defaultValue="search"
        options={KIND_OPTIONS}
        onValueChange={() => {}}
      />
    );
    expect(host.querySelector<HTMLElement>("[data-filter-active]")!.dataset.filterActive).toBe("true");
  });

  it("isFilterActive は既定と違う値で true", () => {
    expect(isFilterActive("all", "all")).toBe(false);
    expect(isFilterActive("failed", "all")).toBe(true);
  });
});

describe("SelectField の上のラベルの目印（#1933）", () => {
  it("上にラベルを出す欄だけ data-field-label=top を付ける", () => {
    render(
      <>
        <SelectField id="a" label="状態" value="x" options={[{ value: "x", label: "X" }]} onValueChange={() => {}} />
        <SelectField
          id="b"
          label="状態"
          labelHidden
          value="x"
          options={[{ value: "x", label: "X" }]}
          onValueChange={() => {}}
        />
      </>
    );
    expect(host.querySelectorAll('[data-field-label="top"]')).toHaveLength(1);
  });
});

const STATUS_OPTIONS = [
  { value: "all", label: "すべて", count: 1234 },
  { value: "done", label: "完了", count: 12 },
  { value: "failed", label: "失敗" },
] as const;
type Status = (typeof STATUS_OPTIONS)[number]["value"];

describe("FilterChipGroup（#1933）", () => {
  it("名前の付いた group に aria-pressed のチップを欄と同じ高さで並べ、件数は説明として読む", () => {
    render(
      <FilterChipGroup<Status> label="状態" value="done" options={STATUS_OPTIONS} onValueChange={() => {}} />
    );
    const group = host.querySelector('[role="group"]')!;
    expect(group.getAttribute("aria-label")).toBe("状態");
    const chips = Array.from(group.querySelectorAll("button"));
    expect(chips.map((chip) => chip.getAttribute("aria-pressed"))).toEqual(["false", "true", "false"]);
    for (const chip of chips) expect(chip.className).toContain("h-[var(--control-height-md)]");
    // 件数は桁区切り・等幅数字。名前は文言だけ（説明に件数）。
    const counts = Array.from(group.querySelectorAll("[data-filter-chip-count]"));
    expect(counts.map((count) => count.textContent)).toEqual(["1,234", "12"]);
    const describedBy = chips[1].getAttribute("aria-describedby")!;
    expect(document.getElementById(describedBy)?.textContent).toBe("12");
    expect(chips[2].hasAttribute("aria-describedby")).toBe(false);
  });

  it("選んでいないチップを押すと値を変え、選んでいるチップでは呼ばない", () => {
    const onValueChange = vi.fn();
    render(
      <FilterChipGroup<Status> label="状態" value="done" options={STATUS_OPTIONS} onValueChange={onValueChange} />
    );
    const chips = host.querySelectorAll<HTMLButtonElement>("button");
    act(() => chips[1].click());
    expect(onValueChange).not.toHaveBeenCalled();
    act(() => chips[2].click());
    expect(onValueChange).toHaveBeenCalledWith("failed");
  });

  it("size で高さを変えられ、上限の定数は 5", () => {
    render(
      <FilterChipGroup<Status> label="状態" value="all" size="sm" options={STATUS_OPTIONS} onValueChange={() => {}} />
    );
    expect(host.querySelector("button")!.className).toContain("h-[var(--control-height-sm)]");
    expect(FILTER_CHIP_MAX_OPTIONS).toBe(5);
  });
});

describe("ListToolbar の絞り込み（#1933）", () => {
  it("部品の縦の中心をそろえ、上のラベルの欄があるあいだだけ下端でそろえる", () => {
    render(<ListToolbar search={<input aria-label="検索" />} filters={<span />} />);
    const start = host.querySelector<HTMLElement>("[data-list-toolbar-start]")!;
    expect(start.className).toContain("@md:items-center");
    expect(start.className).toContain("@md:has-[[data-field-label=top]]:items-end");
    expect(start.className).not.toMatch(/(^|\s)@md:items-end/);
  });

  it("条件があるときだけ「条件をクリア」を絞り込みの後ろに 1 か所出す", () => {
    const onClear = vi.fn();
    render(
      <ListToolbar
        search={<input aria-label="検索" />}
        filters={<span data-testid="filters" />}
        onClearFilters={onClear}
        activeFilterCount={0}
      />
    );
    expect(host.querySelector("[data-list-toolbar-clear]")).toBeNull();

    render(
      <ListToolbar
        search={<input aria-label="検索" />}
        filters={<span data-testid="filters" />}
        onClearFilters={onClear}
        activeFilterCount={1}
      />
    );
    const clear = host.querySelector<HTMLButtonElement>("[data-list-toolbar-clear]")!;
    expect(clear.textContent).toBe(DEFAULT_LIST_TOOLBAR_LABELS.clearFilters);
    // 絞り込みの後ろ（同じ左の群れの中）。
    const start = host.querySelector("[data-list-toolbar-start]")!;
    expect(start.lastElementChild).toBe(clear);
    act(() => clear.click());
    expect(onClear).toHaveBeenCalledTimes(1);

    render(<ListToolbar search={<input aria-label="検索" />} onClearFilters={onClear} searchActive />);
    expect(host.querySelectorAll("[data-list-toolbar-clear]")).toHaveLength(1);
  });

  it("filtersLayout=sheet は広い置き場所では絞り込みをそのまま並べる", () => {
    render(
      <ListToolbar
        search={<input aria-label="検索" />}
        filters={<span data-testid="filters" />}
        filtersLayout="sheet"
        activeFilterCount={2}
      />
    );
    // happy-dom の幅は 0（測れない）なので狭いとみなさない。
    expect(host.querySelector("[data-list-toolbar]")?.getAttribute("data-list-toolbar-filters-layout")).toBe("inline");
    expect(host.querySelector('[data-testid="filters"]')).not.toBeNull();
  });

  it("filtersLayout=sheet は狭い置き場所で「絞り込み（N）」のボタンにまとめ、シートで開く", () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({ width: 343, height: 100, top: 0, left: 0, right: 343, bottom: 100, x: 0, y: 0, toJSON() {} });
    try {
      render(
        <ListToolbar
          search={<input aria-label="検索" />}
          filters={<span data-testid="filters" />}
          filtersLayout="sheet"
          activeFilterCount={2}
          onClearFilters={() => {}}
          count={{ total: 120, matched: 12 }}
          testId="runs-toolbar"
        />
      );
      const toolbar = host.querySelector("[data-list-toolbar]")!;
      expect(toolbar.getAttribute("data-list-toolbar-filters-layout")).toBe("sheet");
      const trigger = Array.from(host.querySelectorAll("button")).find((button) =>
        button.textContent?.includes("絞り込み")
      )!;
      expect(trigger.textContent).toBe(DEFAULT_LIST_TOOLBAR_LABELS.filtersButton(2));
      expect(trigger.getAttribute("aria-expanded")).toBe("false");
      // 絞り込みはシートの中にだけ描く（同じ id の欄を 2 つ描かない）。
      expect(document.querySelectorAll('[data-testid="filters"]')).toHaveLength(1);
      expect(host.querySelector('[data-testid="filters"]')).toBeNull();

      act(() => trigger.click());
      const sheet = document.querySelector<HTMLElement>('[data-testid="runs-toolbar-filters-sheet"]')!;
      expect(sheet.getAttribute("data-state")).toBe("open");
      expect(sheet.querySelector('[data-testid="filters"]')).not.toBeNull();
      const showResults = Array.from(sheet.querySelectorAll("button")).find(
        (button) => button.textContent === DEFAULT_LIST_TOOLBAR_LABELS.showResults("12")
      )!;
      expect(showResults).toBeDefined();
      act(() => showResults.click());
      expect(sheet.getAttribute("data-state")).toBe("closed");
    } finally {
      rect.mockRestore();
    }
  });

  it("二段目の絞り込みも狭い画面では同じシートへ移す", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 320, height: 40 } as DOMRect);
    try {
      render(<ListToolbar filters={<span data-testid="primary-filter" />} secondaryFilters={<span data-testid="secondary-filter" />} filtersLayout="sheet" testId="two-row-toolbar" />);
      const trigger = host.querySelector<HTMLButtonElement>("[data-list-toolbar-filters-trigger] button")!;
      act(() => trigger.click());
      const sheet = document.querySelector('[data-testid="two-row-toolbar-filters-sheet"]')!;
      expect(sheet.querySelector('[data-testid="primary-filter"]')).not.toBeNull();
      expect(sheet.querySelector('[data-testid="secondary-filter"]')).not.toBeNull();
      expect(document.querySelectorAll('[data-testid="secondary-filter"]')).toHaveLength(1);
    } finally { rect.mockRestore(); }
  });

  it("文言は labels で上書きできる", () => {
    render(
      <ListToolbar
        filters={<span />}
        onClearFilters={() => {}}
        activeFilterCount={1}
        labels={{ clearFilters: "Clear filters" }}
      />
    );
    expect(host.querySelector("[data-list-toolbar-clear]")?.textContent).toBe("Clear filters");
  });
});

describe("ツールバーの中の欄の幅は container で切り替える（#1933）", () => {
  it("ListToolbar の絞り込みの欄は @md（container 28rem）、外の欄は sm（viewport 640px）で段の幅になる", () => {
    render(
      <>
        <ListToolbar
          filters={
            <>
              <FilterSelect<Kind> id="in-kind" label="種類" value="all" options={KIND_OPTIONS} onValueChange={() => {}} />
              <SelectField
                id="in-owner"
                label="所有者"
                labelHidden
                width="md"
                value="x"
                options={[{ value: "x", label: "X" }]}
                onValueChange={() => {}}
              />
            </>
          }
        />
        <SelectField id="out" label="外" width="md" value="x" options={[{ value: "x", label: "X" }]} onValueChange={() => {}} />
      </>
    );
    const control = (id: string) => document.getElementById(id)!.parentElement!.className;
    expect(control("in-kind")).toContain("@md:w-[var(--field-width-sm)]");
    expect(control("in-kind")).not.toContain("sm:w-[var(--field-width-sm)]");
    expect(control("in-owner")).toContain("@md:w-[var(--field-width-md)]");
    expect(control("out")).toContain("sm:w-[var(--field-width-md)]");
    expect(control("out")).not.toContain("@md:");
  });
});


describe("SearchableSelectField の inline のラベル（#1933）", () => {
  const KB_OPTIONS = [
    { value: "ALL", label: "すべて" },
    { value: "kb-1", label: "とても長い名前のナレッジベース" },
  ];

  it("ラベルをボタンの中に出し、上のラベルの目印を付けない。名前は 1 行で切り全文を title に出す", () => {
    render(
      <SearchableSelectField
        id="kb"
        label="ナレッジベース"
        value="kb-1"
        options={KB_OPTIONS}
        onValueChange={() => {}}
        labelPlacement="inline"
        active
      />
    );
    const button = host.querySelector<HTMLButtonElement>("button#kb")!;
    expect(button.textContent).toBe("ナレッジベース:とても長い名前のナレッジベース");
    expect(host.querySelector("[data-field-label]")).toBeNull();
    expect(host.querySelector("label")?.className).toContain("sr-only");
    expect(host.querySelector("[data-filter-select-label]")?.getAttribute("aria-hidden")).toBe("true");
    const value = host.querySelector<HTMLElement>("[data-filter-select-value]")!;
    expect(value.title).toBe("とても長い名前のナレッジベース");
    expect(value.className).toContain("truncate");
    expect(host.querySelector('[data-filter-active="true"]')).not.toBeNull();
    expect(button.className).toContain("border-accent-fg");
  });

  it("既定の top は今までどおり上のラベルの目印を持つ", () => {
    render(<SearchableSelectField id="kb" label="ナレッジベース" value="ALL" options={KB_OPTIONS} onValueChange={() => {}} />);
    expect(host.querySelector('[data-field-label="top"]')).not.toBeNull();
    expect(host.querySelector("[data-filter-select-label]")).toBeNull();
  });
});
