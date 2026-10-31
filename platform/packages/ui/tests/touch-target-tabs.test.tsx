import { readFileSync } from "node:fs";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Switch } from "../src/components/ui/switch";
import { revealTabScrollLeft, Tabs, tabsScrollEdges } from "../src/components/ui/tabs";
import { ToggleChip } from "../src/components/ui/toggle-chip";

const read = (path: string) => readFileSync(new URL(`../src/styles/${path}`, import.meta.url), "utf8");
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("タッチ端末の当たり判定（#364）", () => {
  it("ToggleChip / Switch は pr-touch-target を持ち、擬似要素の基準になる relative を持つ", () => {
    for (const html of [
      renderToStaticMarkup(<ToggleChip selected={false}>ON</ToggleChip>),
      renderToStaticMarkup(<Switch checked={false} aria-label="通知" />),
    ]) {
      const className = html.match(/class="([^"]*)"/)?.[1].split(" ") ?? [];
      expect(className).toContain("pr-touch-target");
      expect(className).toContain("relative");
    }
  });

  it("見た目の大きさは変えない（チップは px-3 py-1 text-xs、スイッチは 44 × 24px のまま）", () => {
    const chip = renderToStaticMarkup(<ToggleChip selected>ON</ToggleChip>);
    expect(chip).toMatch(/class="[^"]*\bpx-3 py-1 text-xs\b/);
    expect(chip).not.toMatch(/min-h-|\bh-\[/);
    const toggle = renderToStaticMarkup(<Switch checked aria-label="通知" />);
    expect(toggle).toContain("h-[24px] min-h-[24px] w-[44px] min-w-[44px]");
  });

  it("当たり判定の拡張は pointer: coarse の中だけで、Button と同じ --control-height-touch を使う", () => {
    const css = stripComments(read("structure/touch-target.css"));
    const coarse = css.match(/@media \(pointer: coarse\) \{([\s\S]*)\}\s*\}\s*$/)?.[1] ?? "";
    expect(coarse).toMatch(/\.pr-touch-target::before \{[^}]*inset-block: min\(0px, calc\(\(100% - var\(--control-height-touch\)\) \/ 2\)\);/);
    expect(coarse).toMatch(/\.pr-touch-target::before \{[^}]*inset-inline: min\(0px, calc\(\(100% - var\(--control-height-touch\)\) \/ 2\)\);/);
    // 自分の見た目の範囲を隣の当たり判定より上に置く（重なっても見た目の上のタップは自分に届く）。
    expect(coarse).toMatch(/\.pr-touch-target::after \{[^}]*inset: -1px;[^}]*z-index: 1;/);
    // media の外（マウス環境）には擬似要素を作らない。
    const outside = css.replace(/@media \(pointer: coarse\) \{[\s\S]*\}\s*\}\s*$/, "");
    expect(outside).not.toContain(".pr-touch-target");
    // 色・背景を持たない（強制カラーモードでも何も描かない）。
    expect(css).not.toMatch(/background|border-color|box-shadow|outline/);
    expect(read("tokens/spacing.css")).toMatch(/--control-height-touch:\s*44px;/);
  });

  it("構造 CSS をエントリから読み込む（アプリの Tailwind が components レイヤーとして取り込む）", () => {
    const entry = read("tokens.css");
    expect(entry).toContain('@import "./structure/touch-target.css";');
    expect(entry).toContain('@import "./structure/tabs.css";');
  });
});

describe("Tabs の続きのフェード（#364）", () => {
  it("入りきるときはどちらの端もフェードしない", () => {
    expect(tabsScrollEdges({ scrollLeft: 0, clientWidth: 800, scrollWidth: 800 })).toEqual({ start: false, end: false });
    // 小数の px の誤差ではフェードを出さない。
    expect(tabsScrollEdges({ scrollLeft: 0, clientWidth: 800, scrollWidth: 800.5 })).toEqual({ start: false, end: false });
  });

  it("スクロールできる方向の端だけをフェードする", () => {
    const size = { clientWidth: 320, scrollWidth: 600 };
    expect(tabsScrollEdges({ ...size, scrollLeft: 0 })).toEqual({ start: false, end: true });
    expect(tabsScrollEdges({ ...size, scrollLeft: 140 })).toEqual({ start: true, end: true });
    expect(tabsScrollEdges({ ...size, scrollLeft: 280 })).toEqual({ start: true, end: false });
    expect(tabsScrollEdges({ ...size, scrollLeft: 279.5 })).toEqual({ start: true, end: false });
  });

  it("選んだタブがフェードの外で見えていれば動かさず、隠れていればフェードの幅を空けて見せる", () => {
    const view = { clientWidth: 320, padding: 28 };
    // 見えている
    expect(revealTabScrollLeft({ ...view, scrollLeft: 0, tabLeft: 100, tabWidth: 60 })).toBe(0);
    // 右に隠れている → 右端にフェードの幅を空ける
    expect(revealTabScrollLeft({ ...view, scrollLeft: 0, tabLeft: 300, tabWidth: 60 })).toBe(300 + 60 + 28 - 320);
    // 右のフェードの下にある（一部だけ見えている）も隠れているとみなす
    expect(revealTabScrollLeft({ ...view, scrollLeft: 0, tabLeft: 250, tabWidth: 60 })).toBe(250 + 60 + 28 - 320);
    // 左に隠れている → 左端にフェードの幅を空ける
    expect(revealTabScrollLeft({ ...view, scrollLeft: 200, tabLeft: 180, tabWidth: 60 })).toBe(180 - 28);
    // 先頭のタブは 0 まで戻す
    expect(revealTabScrollLeft({ ...view, scrollLeft: 200, tabLeft: 0, tabWidth: 60 })).toBe(0);
  });

  it("初回の描画（SSR）ではフェードを出さず、スクロールバーの指定は pr-tabs-scroll が持つ", () => {
    const html = renderToStaticMarkup(
      <Tabs items={[{ id: "a", label: "概要" }, { id: "b", label: "履歴" }]} value="a" ariaLabel="表示" />
    );
    const tablist = html.match(/<div[^>]*role="tablist"[^>]*>/)?.[0] ?? "";
    expect(tablist).toMatch(/class="[^"]*\bpr-tabs-scroll relative\b/);
    expect(tablist).not.toContain("data-scroll-start");
    expect(tablist).not.toContain("data-scroll-end");
    // utility で隠すと components レイヤーの強制カラーモードの指定（細いスクロールバー）に勝ってしまう。
    expect(tablist).not.toContain("scrollbar-width");
  });

  it("フェードは data-scroll-* があるときだけ mask で付け、強制カラーモードでは外してスクロールバーを出す", () => {
    const css = stripComments(read("structure/tabs.css"));
    expect(css).toMatch(/\.pr-tabs-scroll \{[^}]*scroll-padding-inline: var\(--tab-fade-width\);[^}]*scrollbar-width: none;/);
    expect(css).toMatch(/\.pr-tabs-scroll:is\(\[data-scroll-start\], \[data-scroll-end\]\) \{[^}]*mask-image: linear-gradient\(/);
    expect(css).toMatch(/\.pr-tabs-scroll\[data-scroll-start\] \{\s*--pr-tabs-fade-start: var\(--tab-fade-width\);/);
    expect(css).toMatch(/\.pr-tabs-scroll\[data-scroll-end\] \{\s*--pr-tabs-fade-end: var\(--tab-fade-width\);/);
    // data-scroll-* の無い .pr-tabs-scroll 単体には mask を付けない（入りきるときの見た目は変えない）。
    expect(css.match(/\.pr-tabs-scroll \{[^}]*\}/)?.[0]).not.toContain("mask");
    expect(css).toMatch(/@media \(forced-colors: active\) \{[^{]*\{[^}]*mask-image: none;[^}]*scrollbar-width: thin;/);
    // 動き（transition / animation）を持たない。
    expect(css).not.toMatch(/transition|animation/);
    expect(read("tokens/spacing.css")).toMatch(/--tab-fade-width:\s*2rem;/);
  });
});
