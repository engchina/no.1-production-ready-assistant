// @vitest-environment happy-dom
import { Wrench } from "lucide-react";
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Disclosure, DisclosureChevron } from "../src";

// #397: 開閉できる領域の共通部品。<details> を包み、開閉状態を必ず DisclosureChevron で示す。

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

function mount(node: React.ReactNode) {
  act(() => root.render(node));
  const details = host.querySelector("details") as HTMLDetailsElement;
  const summary = host.querySelector("summary") as HTMLElement;
  const chevron = () => summary.querySelector("svg[data-state]") as SVGElement;
  return { details, summary, chevron };
}

function click(element: HTMLElement) {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true });
  act(() => {
    element.dispatchEvent(event);
  });
  return event;
}

describe("DisclosureChevron", () => {
  it("折りたたみは右向き（-90deg）、展開は下向き。reduced-motion で回転を止め、読み上げない", () => {
    const collapsed = renderToStaticMarkup(<DisclosureChevron expanded={false} />);
    expect(collapsed).toContain('data-state="collapsed"');
    expect(collapsed).toMatch(/class="[^"]*(?:^|\s)-rotate-90(?:\s|")/);
    expect(collapsed).toContain("motion-reduce:transition-none");
    expect(collapsed).toContain('aria-hidden="true"');
    const expanded = renderToStaticMarkup(<DisclosureChevron expanded />);
    expect(expanded).toContain('data-state="expanded"');
    expect(expanded).toContain("rotate-0");
    expect(renderToStaticMarkup(<DisclosureChevron expanded="group" />)).toContain(
      "-rotate-90 group-open/disclosure:rotate-0"
    );
  });
});

describe("Disclosure", () => {
  it("見出しの行に先頭アイコン・見出し・meta・Chevron を置き、ブラウザの三角を消す", () => {
    const html = renderToStaticMarkup(
      <Disclosure summary="処理の詳細(診断)" icon={Wrench} meta={<span data-meta>3</span>} data-testid="diag">
        本文
      </Disclosure>
    );
    expect(html).toMatch(/^<details[^>]*data-testid="diag"/);
    expect(html).toContain('data-state="closed"');
    expect(html).toContain("[&amp;::-webkit-details-marker]:hidden");
    expect(html).toContain("list-none");
    const summary = html.slice(html.indexOf("<summary"), html.indexOf("</summary>"));
    expect(summary).toContain("処理の詳細(診断)");
    expect(summary).toContain("data-meta");
    expect(summary).toContain('data-state="collapsed"');
    // 見出しの行の高さはトークン（タッチ端末では --button-height-lg が 44px になる）
    expect(summary).toContain("min-h-[var(--button-height-lg)]");
    expect(summary).toContain("hover:bg-surface-hover");
    // フォーカスは枠の内側の outline（ring や outline-none を使わない）
    expect(summary).toContain("focus-visible:-outline-offset-2");
    expect(summary).not.toMatch(/focus(?:-visible)?:(?:ring|outline-none)/);
    // 先頭アイコンと Chevron の 2 つの svg。Chevron が最後。
    expect(summary.match(/<svg/g)).toHaveLength(2);
    expect(summary.lastIndexOf("<svg")).toBe(summary.indexOf('<svg', summary.indexOf("data-meta")));
  });

  it("card は開いたとき見出しと内容の間に区切り線を持つ。plain は枠を持たず Chevron を見出しの直後に置く", () => {
    const card = renderToStaticMarkup(<Disclosure summary="詳細" defaultOpen>本文</Disclosure>);
    const detailsTag = card.slice(0, card.indexOf(">") + 1);
    for (const token of ["rounded-md", "border", "border-border", "bg-surface"]) {
      expect(detailsTag).toMatch(new RegExp(`class="(?:[^"]* )?${token}(?: [^"]*)?"`));
    }
    expect(detailsTag).toContain('open=""');
    const content = card.slice(card.indexOf("</summary>"));
    expect(content).toMatch(/class="[^"]*border-t[^"]*"/);
    expect(content).toMatch(/class="[^"]*\bp-3\b[^"]*"/);
    const sunken = renderToStaticMarkup(<Disclosure summary="詳細" surface="sunken">本文</Disclosure>);
    expect(sunken).toContain("bg-surface-sunken");
    const danger = renderToStaticMarkup(<Disclosure summary="構成を解除" tone="danger">本文</Disclosure>);
    expect(danger).toContain("border-danger-border bg-danger-subtle text-danger-fg");
    expect(danger).toContain("hover:bg-current/5");
    expect(danger).not.toContain("hover:bg-surface-hover");
    const plain = renderToStaticMarkup(<Disclosure summary="根拠" variant="plain" size="sm">本文</Disclosure>);
    expect(plain).not.toContain("border-border");
    expect(plain).toContain("w-fit");
    expect(plain).toContain("text-xs");
    // 開閉の印は見出しの文字の大きさ（sm 12px）で、見出しの 1 行目の行の高さの箱に置く（#2371）。
    expect(plain).toContain('width="12"');
    expect(plain).toContain("pr-icon-line");
    expect(plain).toContain("items-start");
  });

  it("非受控: クリックで開閉し、Chevron と data-state が追従する", () => {
    const onOpenChange = vi.fn();
    const { details, summary, chevron } = mount(
      <Disclosure summary="詳細" onOpenChange={onOpenChange}>
        本文
      </Disclosure>
    );
    expect(details.open).toBe(false);
    expect(chevron().getAttribute("data-state")).toBe("collapsed");
    click(summary);
    expect(details.open).toBe(true);
    expect(details.getAttribute("data-state")).toBe("open");
    expect(chevron().getAttribute("data-state")).toBe("expanded");
    expect(onOpenChange).toHaveBeenLastCalledWith(true);
    click(summary);
    expect(details.open).toBe(false);
    expect(chevron().getAttribute("data-state")).toBe("collapsed");
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it("受控: クリックはブラウザの切り替えを止め（preventDefault）、onOpenChange だけを呼ぶ", () => {
    // happy-dom は preventDefault の前に details を切り替えるため、DOM の open ではなく
    // クリックの defaultPrevented と Chevron（部品の状態）で確かめる。ブラウザでの挙動は Playwright で確認する。
    const onOpenChange = vi.fn();
    const { summary, chevron } = mount(
      <Disclosure summary="詳細" open={false} onOpenChange={onOpenChange}>
        本文
      </Disclosure>
    );
    const event = click(summary);
    expect(event.defaultPrevented).toBe(true);
    expect(onOpenChange).toHaveBeenCalledWith(true);
    expect(chevron().getAttribute("data-state")).toBe("collapsed");
  });

  it("受控: 親の状態で開閉し、外から開いても Chevron が追従する", () => {
    let setOpenFromOutside: (open: boolean) => void = () => undefined;
    function Harness() {
      const [open, setOpen] = useState(false);
      setOpenFromOutside = setOpen;
      return (
        <Disclosure summary="詳細" open={open} onOpenChange={setOpen}>
          本文
        </Disclosure>
      );
    }
    const { details, summary, chevron } = mount(<Harness />);
    click(summary);
    expect(details.open).toBe(true);
    expect(chevron().getAttribute("data-state")).toBe("expanded");
    act(() => setOpenFromOutside(false));
    expect(details.open).toBe(false);
    expect(chevron().getAttribute("data-state")).toBe("collapsed");
  });

  it("入れ子: 外側が開いていても、内側の Chevron は内側の状態を示す", () => {
    act(() =>
      root.render(
        <Disclosure summary="外" defaultOpen>
          <Disclosure summary="内">本文</Disclosure>
        </Disclosure>
      )
    );
    const chevrons = host.querySelectorAll("summary svg[data-state]");
    expect(chevrons[0].getAttribute("data-state")).toBe("expanded");
    expect(chevrons[1].getAttribute("data-state")).toBe("collapsed");
  });

  it("ブラウザが開いたとき（ページ内検索など）は toggle で状態へ戻す", () => {
    const onOpenChange = vi.fn();
    const { details, chevron } = mount(
      <Disclosure summary="詳細" onOpenChange={onOpenChange}>
        本文
      </Disclosure>
    );
    act(() => {
      details.open = true;
      details.dispatchEvent(new Event("toggle"));
    });
    expect(onOpenChange).toHaveBeenCalledWith(true);
    expect(chevron().getAttribute("data-state")).toBe("expanded");
  });
});
