// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DataTable, RowTitleButton } from "../src";
import {
  isClampedOverflow,
  isPlainLeftClick,
  ROW_TITLE_TOOLTIP_MAX_CHARS,
  rowTitleTooltipText,
} from "../src/components/data/row-title-button";
import { TOOLTIP_SHOW_DELAY_MS } from "../src/components/ui/tooltip";

// #421: 一覧の行の題名のボタン（RAG / Agent の EntityLayout、NL2SQL・system-settings の手書きから共有化）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function openTag(html: string) {
  return html.slice(0, html.indexOf(">") + 1);
}

describe("RowTitleButton の見た目と属性", () => {
  it("type=button の文字だけのボタンで、タッチ端末の当たり判定（pr-touch-target）と押せる印を持つ", () => {
    const tag = openTag(renderToStaticMarkup(<RowTitleButton title="契約書レビュー" onClick={() => undefined} />));
    expect(tag).toContain('type="button"');
    expect(tag).toMatch(/class="[^"]*\bpr-touch-target\b[^"]*"/);
    expect(tag).toMatch(/class="[^"]*\brelative\b[^"]*"/);
    expect(tag).toContain("cursor-pointer");
    expect(tag).toContain("text-left");
    // フォーカスはグローバルの :focus-visible に任せる（ring も outline-none も書かない。#355）。
    expect(tag).not.toMatch(/focus(-visible)?:(ring|outline-none)/);
    expect(tag).not.toContain("aria-current");
  });

  it("current で aria-current=\"true\" を付ける（aria-pressed / aria-expanded は使わない）", () => {
    const tag = openTag(renderToStaticMarkup(<RowTitleButton title="A" current onClick={() => undefined} />));
    expect(tag).toContain('aria-current="true"');
    expect(tag).not.toContain("aria-pressed");
    expect(tag).not.toContain("aria-expanded");
  });

  it("対象名は既定で折り返し（切り詰めない）、ホバーで下線。補足は 12px の fg-muted", () => {
    const html = renderToStaticMarkup(<RowTitleButton title="名前" subtitle="kb-001" onClick={() => undefined} />);
    expect(html).toContain("[overflow-wrap:anywhere]");
    expect(html).not.toContain("line-clamp");
    expect(html).toContain("group-hover/row-title:underline");
    expect(html).toMatch(/<span[^>]* class="[^"]*text-xs[^"]*text-fg-muted[^"]*">kb-001<\/span>/);
  });

  it("maxLines で行数を切り詰める", () => {
    const html = renderToStaticMarkup(<RowTitleButton title="長い題名" maxLines={2} onClick={() => undefined} />);
    expect(html).toContain("line-clamp-2");
  });

  it("aria-label・data-*・disabled などのボタンの属性をそのまま渡す", () => {
    const tag = openTag(
      renderToStaticMarkup(
        <RowTitleButton
          title="A"
          aria-label="A を選ぶ"
          aria-describedby="comment-1"
          data-feedback-row-button="fb-1"
          disabled
          onClick={() => undefined}
        />
      )
    );
    expect(tag).toContain('aria-label="A を選ぶ"');
    expect(tag).not.toContain("aria-labelledby");
    expect(tag).toContain('aria-describedby="comment-1"');
    expect(tag).toContain('data-feedback-row-button="fb-1"');
    expect(tag).toContain("disabled");
    expect(tag).toContain("disabled:cursor-not-allowed");
  });

  it("Tooltip の文言は長すぎるとき先頭だけにする（Tooltip は短い文の部品。サロゲートペアを割らない）", () => {
    expect(rowTitleTooltipText("  短い題名 ")).toBe("短い題名");
    const long = "あ".repeat(ROW_TITLE_TOOLTIP_MAX_CHARS + 10);
    expect(rowTitleTooltipText(long)).toBe(`${"あ".repeat(ROW_TITLE_TOOLTIP_MAX_CHARS)}…`);
    expect(rowTitleTooltipText("𠮷𠮷𠮷", 2)).toBe("𠮷𠮷…");
  });

  it("href を渡すとリンク（<a href>）になり、ボタンだけの属性は渡さない（#583）", () => {
    const tag = openTag(
      renderToStaticMarkup(
        <RowTitleButton
          title="経理ビュー"
          href="/search-answer-profiles?id=bv-1"
          aria-label="経理ビュー を編集"
          current
          name="ignored"
          onClick={() => undefined}
        />
      )
    );
    expect(tag).toMatch(/^<a /);
    expect(tag).toContain('href="/search-answer-profiles?id=bv-1"');
    expect(tag).toContain('aria-label="経理ビュー を編集"');
    expect(tag).toContain('aria-current="true"');
    expect(tag).toContain("data-row-title-button");
    expect(tag).toContain("pr-touch-target");
    expect(tag).not.toContain("type=");
    expect(tag).not.toContain("name=");
    // リンクは :enabled を持たないので、ホバーの下線は group-enabled を付けない。
    const html = renderToStaticMarkup(<RowTitleButton title="A" href="/a" onClick={() => undefined} />);
    expect(html).toContain("group-hover/row-title:underline");
    expect(html).not.toContain("group-enabled/row-title");
  });

  it("href があっても disabled ならリンクにしない（押せないボタンのまま）", () => {
    const tag = openTag(renderToStaticMarkup(<RowTitleButton title="A" href="/a" disabled onClick={() => undefined} />));
    expect(tag).toMatch(/^<button /);
    expect(tag).not.toContain("href");
  });

  it("画面内で開くのは修飾キーの無い左クリックだけ（ほかは新しいタブ等のブラウザの既定）", () => {
    const click = { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, defaultPrevented: false };
    expect(isPlainLeftClick(click)).toBe(true);
    expect(isPlainLeftClick({ ...click, ctrlKey: true })).toBe(false);
    expect(isPlainLeftClick({ ...click, metaKey: true })).toBe(false);
    expect(isPlainLeftClick({ ...click, shiftKey: true })).toBe(false);
    expect(isPlainLeftClick({ ...click, altKey: true })).toBe(false);
    expect(isPlainLeftClick({ ...click, button: 1 })).toBe(false);
    expect(isPlainLeftClick({ ...click, defaultPrevented: true })).toBe(false);
  });

  it("切り詰めの判定は scrollHeight と clientHeight の差（1px の丸めは無視する）", () => {
    expect(isClampedOverflow({ scrollHeight: 60, clientHeight: 40 })).toBe(true);
    expect(isClampedOverflow({ scrollHeight: 41, clientHeight: 40 })).toBe(false);
    expect(isClampedOverflow({ scrollHeight: 40, clientHeight: 40 })).toBe(false);
  });
});

describe("RowTitleButton の操作", () => {
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

  it.each([undefined, "/items?id=synthetic"])("補足を含む表示文と操作の説明を読み上げ名に含める（href=%s）", (href) => {
    act(() => root.render(<RowTitleButton title="合成の項目" subtitle={<span>合成の説明</span>} href={href} aria-label="詳細を開く" />));
    const control = host.querySelector("[data-row-title-button]")!;
    const labels = control.getAttribute("aria-labelledby")!.split(" ").map((id) => id === control.id ? control.getAttribute("aria-label") : document.getElementById(id)?.textContent);
    expect(labels).toEqual(["詳細を開く", "合成の項目", "合成の説明"]);
  });

  it("DataTable の行の中で押すと onClick だけが 1 回呼ばれ、行のクリック（onRowClick）は重ねて呼ばない", () => {
    const onClick = vi.fn();
    const onRowClick = vi.fn();
    act(() =>
      root.render(
        <DataTable<{ id: string; name: string }>
          columns={[
            {
              key: "name",
              header: "名前",
              rowHeader: true,
              render: (row) => <RowTitleButton title={row.name} current onClick={() => onClick(row.id)} />,
            },
          ]}
          rows={[{ id: "a", name: "A" }]}
          getRowKey={(row) => row.id}
          selectedRowKey="a"
          onRowClick={onRowClick}
        />
      )
    );
    const button = host.querySelector("button[data-row-title-button]") as HTMLButtonElement;
    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });
    expect(onClick).toHaveBeenCalledWith("a");
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onRowClick).not.toHaveBeenCalled();
    // 行と題名のボタンの両方が「現在の項目」を持つ（行は背景と左バー、ボタンは Tab で届いたときの読み上げ）。
    expect(button.closest("tr")?.getAttribute("aria-current")).toBe("true");
    expect(button.getAttribute("aria-current")).toBe("true");
  });

  it("リンクの形: 普通のクリックは既定の遷移を止めて onClick、Ctrl / ⌘ + クリックは止めない（新しいタブ）", () => {
    const onClick = vi.fn();
    const onRowClick = vi.fn();
    act(() =>
      root.render(
        <DataTable<{ id: string; name: string }>
          columns={[
            {
              key: "name",
              header: "名前",
              rowHeader: true,
              render: (row) => <RowTitleButton title={row.name} href={`/items?id=${row.id}`} onClick={() => onClick(row.id)} />,
            },
          ]}
          rows={[{ id: "a", name: "A" }]}
          getRowKey={(row) => row.id}
          onRowClick={onRowClick}
        />
      )
    );
    const link = host.querySelector("a[data-row-title-button]") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/items?id=a");
    const plain = new MouseEvent("click", { bubbles: true, cancelable: true });
    act(() => {
      link.dispatchEvent(plain);
    });
    expect(plain.defaultPrevented).toBe(true);
    expect(onClick).toHaveBeenCalledTimes(1);
    const withCtrl = new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true });
    act(() => {
      link.dispatchEvent(withCtrl);
    });
    expect(withCtrl.defaultPrevented).toBe(false);
    expect(onClick).toHaveBeenCalledTimes(1);
    // 行のクリック（onRowClick）はリンクのクリックを重ねて扱わない。
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("ref を渡せる（詳細を閉じたときにフォーカスを戻す先）", () => {
    const ref = { current: null as HTMLButtonElement | null };
    act(() => root.render(<RowTitleButton ref={ref} title="A" onClick={() => undefined} />));
    expect(ref.current?.tagName).toBe("BUTTON");
  });

  it("maxLines で実際に切り詰めたときだけ、ホバーで全文の Tooltip を出す（説明としては結び付けない）", () => {
    const heights = { scrollHeight: 60, clientHeight: 40 };
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => heights.scrollHeight);
    const client = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => heights.clientHeight);
    vi.useFakeTimers();
    const hover = (element: HTMLElement) => {
      act(() => {
        element.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }));
      });
      act(() => {
        vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
      });
    };
    const visibleTooltip = () => document.body.querySelector<HTMLElement>('[role="tooltip"]:not([hidden])');
    try {
      const title = "とても長い対象名".repeat(10);
      act(() =>
        root.render(<RowTitleButton title={title} maxLines={1} aria-label={`${title} を選ぶ`} onClick={() => undefined} />)
      );
      const button = host.querySelector("button") as HTMLButtonElement;
      expect(button.hasAttribute("aria-describedby")).toBe(false);
      hover(button);
      expect(visibleTooltip()?.textContent).toBe(title);
      expect(visibleTooltip()?.getAttribute("aria-hidden")).toBe("true");
      expect(button.hasAttribute("aria-describedby")).toBe(false);
      act(() => {
        button.dispatchEvent(new PointerEvent("pointerout", { bubbles: true, pointerType: "mouse" }));
      });
      act(() => {
        vi.advanceTimersByTime(1000);
      });

      // 切り詰めていなければ出さない。
      heights.scrollHeight = 40;
      act(() => root.render(<RowTitleButton title="短い名前" maxLines={1} onClick={() => undefined} />));
      hover(host.querySelector("button") as HTMLButtonElement);
      expect(visibleTooltip()).toBeNull();
    } finally {
      vi.useRealTimers();
      scroll.mockRestore();
      client.mockRestore();
    }
  });
});
