// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ColumnBrowser, type ColumnBrowserColumn } from "../src";

// #2141: 階層の一覧を列で見るカラムブラウザ。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function columns(overrides: Partial<Record<0 | 1 | 2, Partial<ColumnBrowserColumn>>> = {}): ColumnBrowserColumn[] {
  return [
    {
      id: "c1",
      title: "大分類",
      count: "2 件",
      headerAction: <button type="button">追加1</button>,
      items: [
        { id: "a", label: "A", meta: "中分類 1", hasChildren: true },
        { id: "b", label: "B", hasChildren: true },
      ],
      selectedId: "a",
      ...overrides[0],
    },
    {
      id: "c2",
      title: "A の中分類",
      headerAction: <button type="button">追加2</button>,
      items: [{ id: "a1", label: "A1" }],
      ...overrides[1],
    },
    {
      id: "c3",
      title: "小分類",
      headerAction: <button type="button">追加3</button>,
      items: [],
      placeholder: "中分類を選ぶと表示します。",
      ...overrides[2],
    },
  ];
}

describe("ColumnBrowser の描画", () => {
  it("選んだ行は aria-current、下の段のある行は印、上の段を選んでいない列は案内だけ", () => {
    const html = renderToStaticMarkup(<ColumnBrowser columns={columns()} ariaLabel="分類の階層" />);
    expect(html).toContain('role="group" aria-label="分類の階層"');
    expect(html).toMatch(/aria-current="true"[^>]*>.*?A<\/span>/);
    expect(html).toContain("中分類 1");
    // 案内の列は項目と追加の操作を出さない。
    expect(html).toContain("中分類を選ぶと表示します。");
    expect(html).not.toContain("追加3");
    expect(html).toContain("追加2");
  });

  it("狭い置き場所では、案内の無い一番右の列だけを出す（他の列は広い置き場所だけ）", () => {
    const html = renderToStaticMarkup(<ColumnBrowser columns={columns()} ariaLabel="分類の階層" onBack={() => undefined} />);
    const sections = html.match(/<section[^>]*>/g) ?? [];
    expect(sections).toHaveLength(3);
    expect(sections[0]).toContain("hidden @3xl:flex");
    expect(sections[1]).toContain('data-active="true"');
    expect(sections[1]).not.toContain("hidden @3xl:flex");
    expect(sections[2]).toContain("hidden @3xl:flex");
  });

  it("activeColumnId で狭い置き場所の列を決める（案内の出ている列・知らない ID は使わない）", () => {
    const active = (activeColumnId: string) =>
      (renderToStaticMarkup(<ColumnBrowser columns={columns()} ariaLabel="分類の階層" activeColumnId={activeColumnId} />).match(
        /<section[^>]*>/g
      ) ?? []).findIndex((section) => section.includes('data-active="true"'));
    expect(active("c1")).toBe(0);
    expect(active("c3")).toBe(1);
    expect(active("unknown")).toBe(1);
  });

  it("列の中は一覧の表示密度の高さで縦にスクロールする", () => {
    const html = renderToStaticMarkup(<ColumnBrowser columns={columns()} ariaLabel="分類の階層" />);
    expect(html).toContain("overflow-auto");
    expect(html).toContain("min-h-[3.5rem]");
  });
});

describe("ColumnBrowser の操作", () => {
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

  const rowButtons = (column: number) =>
    Array.from(host.querySelectorAll<HTMLButtonElement>(`[data-column-browser-column="${column}"] [data-column-browser-row]`));

  it("行を押すと onSelect、戻るボタンは表示中の列の番号で onBack を呼ぶ", () => {
    const onSelect = vi.fn();
    const onBack = vi.fn();
    act(() =>
      root.render(
        <ColumnBrowser
          columns={columns({ 0: { onSelect } })}
          ariaLabel="分類の階層"
          backLabel={(index) => `戻る${index}`}
          onBack={onBack}
        />
      )
    );
    act(() => rowButtons(0)[1].click());
    expect(onSelect).toHaveBeenCalledWith("b");
    const back = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "戻る1");
    act(() => back?.click());
    expect(onBack).toHaveBeenCalledWith(1);
  });

  it("↑↓ で列の中、→ で右の列の先頭、← で左の列の選択中の行へ移る", () => {
    act(() => root.render(<ColumnBrowser columns={columns()} ariaLabel="分類の階層" />));
    const press = (key: string) =>
      act(() => {
        document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
      });
    rowButtons(0)[0].focus();
    press("ArrowDown");
    expect(document.activeElement).toBe(rowButtons(0)[1]);
    press("ArrowDown");
    expect(document.activeElement).toBe(rowButtons(0)[1]);
    press("ArrowRight");
    expect(document.activeElement).toBe(rowButtons(1)[0]);
    press("ArrowLeft");
    expect(document.activeElement).toBe(rowButtons(0)[0]);
  });
});
