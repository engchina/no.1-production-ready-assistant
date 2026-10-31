import { readFileSync } from "node:fs";

import { Upload } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BUTTON_ICON_SIZE, Button } from "../src/components/ui/button";
import { SPINNER_STROKE_PX, Spinner, spinnerGeometry } from "../src/components/ui/spinner";

const readCss = (path: string) =>
  readFileSync(new URL(`../src/styles/tokens/${path}`, import.meta.url), "utf8");
const baseCss = readCss("base.css");
const colorsCss = readCss("colors.css");
const a11yCss = readCss("a11y.css");

/** `class="…"` から 1 要素分のクラス集合を取り出す。 */
function classesOf(html: string, marker: string) {
  const tag = html.slice(html.indexOf(marker));
  const start = tag.indexOf('class="');
  return tag.slice(start + 7, tag.indexOf('"', start + 7)).split(/\s+/);
}

function attr(html: string, marker: string, name: string) {
  const tag = html.slice(html.indexOf(marker));
  return tag.match(new RegExp(`${name}="([^"]*)"`))?.[1];
}

/**
 * `d` の円弧（`M x y a r r 0 0 1 dx dy` の繰り返し）を中心 (12, 12) の円の上で細かく分け、点の列にする。
 * 線の太さは一様なので、点の重心＝アーク（濃い筆画）の見た目の重心。
 */
function arcPoints(d: string) {
  const points: { x: number; y: number }[] = [];
  const re = /M([\d.-]+) ([\d.-]+)a([\d.]+) [\d.]+ 0 0 1 ?(-?[\d.]+) ?(-?[\d.]+)/g;
  for (const match of d.matchAll(re)) {
    const [x0, y0, r, dx, dy] = match.slice(1).map(Number);
    const start = Math.atan2(y0 - 12, x0 - 12);
    let end = Math.atan2(y0 + dy - 12, x0 + dx - 12);
    // sweep-flag 1 は SVG の座標で角度が増える向き（画面では時計回り）
    while (end <= start) end += 2 * Math.PI;
    for (let index = 0; index <= 360; index += 1) {
      const angle = start + ((end - start) * index) / 360;
      points.push({ x: 12 + r * Math.cos(angle), y: 12 + r * Math.sin(angle) });
    }
  }
  return points;
}

describe("Spinner", () => {
  it("全周トラックと 180 度対称の 2 本のアーク（90 度 × 2）で構成する", () => {
    const html = renderToStaticMarkup(<Spinner />);
    // 閉じた円のトラック（回転しても外形が変わらない）
    expect(html).toContain('<circle class="pr-spinner-track" cx="12" cy="12" r="8.5"');
    // 固定の opacity ではなく、トークン（--color-spinner-track）で色を付ける
    expect(html).not.toContain("opacity=");
    // 12 時→3 時と 6 時→9 時の 2 本（1 つの path）。#1180
    expect(html).toContain('class="pr-spinner-arc" d="M12 3.5a8.5 8.5 0 0 1 8.5 8.5M12 20.5a8.5 8.5 0 0 1-8.5-8.5"');
    expect(html).toContain('viewBox="0 0 24 24"');
  });

  it.each([14, 16, 20, 24])(
    "%ipx でもアークの見た目の重心は中心にあり、回転角によらず動かない（#1180）",
    (size) => {
      const html = renderToStaticMarkup(<Spinner size={size} />);
      const d = attr(html, '<path class="pr-spinner-arc"', "d") ?? "";
      const points = arcPoints(d);
      const { radius } = spinnerGeometry(size);
      // 2 本 × 90 度（解析の確認: 点はすべて半径 radius の円の上）
      expect(points).toHaveLength(2 * 361);
      for (const point of points) {
        expect(Math.hypot(point.x - 12, point.y - 12)).toBeCloseTo(radius, 3);
      }
      const centroid = {
        x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
        y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
      };
      // 重心が中心なら、回転した点の列の重心も中心のまま（回転は中心のまわりの線形変換）。
      // 旧形（270 度の 1 本）は中心から 2.55（viewBox 単位）＝16px で 1.7px 外れ、回転で上下・左右に揺れて見えた。
      expect(Math.abs(centroid.x - 12)).toBeLessThan(0.01);
      expect(Math.abs(centroid.y - 12)).toBeLessThan(0.01);
    }
  );

  it.each([
    [14, 3.429, 8.286],
    [16, 3, 8.5],
    [20, 2.4, 8.8],
    [24, 2, 9],
  ])("%ipx でも線の実寸は 2px、外縁の半径は 10（viewBox 単位）", (size, strokeWidth, radius) => {
    expect(spinnerGeometry(size)).toEqual({ strokeWidth, radius });
    // 実寸 = viewBox 上の線幅 × size / 24
    expect((strokeWidth * size) / 24).toBeCloseTo(SPINNER_STROKE_PX, 2);
    expect(radius + strokeWidth / 2).toBeCloseTo(10, 2);

    const html = renderToStaticMarkup(<Spinner size={size} />);
    expect(attr(html, "<svg", "stroke-width")).toBe(String(strokeWidth));
    expect(attr(html, "<circle", "r")).toBe(String(radius));
    expect(html).toContain(`a${radius} ${radius} 0 0 1 ${radius} ${radius}`);
    expect(html).toContain(`a${radius} ${radius} 0 0 1-${radius}-${radius}`);
  });

  it("回転しない固定の正方形の箱の中で、内側の svg だけを回す（#1180）", () => {
    const html = renderToStaticMarkup(<Spinner size={14} />);
    // 外側の箱: 寸法を固定し、回転は内側の svg だけ
    expect(html.startsWith("<span")).toBe(true);
    const box = classesOf(html, "<span");
    expect(box).toContain("pr-spinner");
    expect(box).not.toContain("animate-spin");
    expect(attr(html, "<span", "style")).toBe("width:14px;height:14px");
    expect(attr(html, "<span", "aria-hidden")).toBe("true");
    // 内側の svg: 回る
    expect(classesOf(html, "<svg")).toContain("animate-spin");
    expect(attr(html, "<svg", "width")).toBe("14");
    expect(attr(html, "<svg", "height")).toBe("14");
    expect(html.match(/<svg/g)).toHaveLength(1);
  });

  it("既定 16px・animate-spin・motion-reduce でも回転を止めない（#440）", () => {
    const html = renderToStaticMarkup(<Spinner />);
    expect(html).toContain('width="16"');
    expect(html).toContain('height="16"');
    const classes = classesOf(html, "<svg");
    expect(classes).toContain("animate-spin");
    expect(classes).not.toContain("motion-reduce:animate-none");
    expect(html).toContain('aria-hidden="true"');
  });

  it("size と className を上書きできる（className は箱に付け、色は currentColor で svg に伝わる）", () => {
    const html = renderToStaticMarkup(<Spinner size={14} className="text-accent-fg" />);
    expect(html).toContain('width="14"');
    expect(classesOf(html, "<span")).toContain("text-accent-fg");
    expect(attr(html, "<svg", "stroke")).toBe("currentColor");
  });
});

describe("Spinner の CSS（トークン・reduced-motion）", () => {
  it("トラックはアークと同じ色を透かすトークン（ライト 30% / ダーク 35%）", () => {
    expect(colorsCss).toMatch(
      /--color-spinner-track: light-dark\(\s*color-mix\(in srgb, currentColor 30%, transparent\),\s*color-mix\(in srgb, currentColor 35%, transparent\)\s*\);/
    );
    // color-scheme を切り替える要素すべてで宣言し直すブロックの中に置く（Lightning CSS 変換後もダーク値になる）
    const scoped = colorsCss.slice(colorsCss.indexOf(":root, [data-theme], [data-surface] {"));
    expect(scoped.slice(0, scoped.indexOf("\n}\n"))).toContain("--color-spinner-track:");
    expect(baseCss).toMatch(/\.pr-spinner-track\s*\{\s*stroke:\s*var\(--color-spinner-track\);/);
    // 強制カラーモードでは透過色を使わずシステム色にする
    expect(a11yCss).toMatch(/@media \(forced-colors: active\)[\s\S]*--color-spinner-track: GrayText;/);
  });

  it("reduced-motion でも回転を続ける（一括無効化より強い詳細度で 1s に戻す。#440）", () => {
    const reduced = baseCss.slice(baseCss.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/svg\.animate-spin\s*\{\s*animation-duration: 1s !important;/);
    expect(baseCss).not.toContain("pr-spinner-breathe");
  });

  it("base.css: 箱は固定の正方形で、回転を周りのレイアウト・スクロールの領域に出さない（#1180）", () => {
    const rule = baseCss.match(/\.pr-spinner\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(rule).toMatch(/display:\s*inline-block/);
    expect(rule).toMatch(/flex:\s*none/);
    expect(rule).toMatch(/line-height:\s*0/);
    expect(rule).toMatch(/vertical-align:\s*middle/);
    // 寸法・レイアウト・描画を箱の中に閉じる（回転した正方形の角が外へ出ない）
    expect(rule).toMatch(/contain:\s*strict/);
    expect(baseCss).toMatch(/\.pr-spinner\s*>\s*svg\s*\{[^}]*display:\s*block/);
  });

  it("base.css が回転原点を図形中心へ固定し合成レイヤーで回す", () => {
    expect(baseCss).toMatch(/svg\.animate-spin\s*\{[^}]*transform-box:\s*view-box/);
    expect(baseCss).toMatch(/svg\.animate-spin\s*\{[^}]*transform-origin:\s*50%\s*50%/);
    expect(baseCss).toMatch(/svg\.animate-spin\s*\{[^}]*will-change:\s*transform/);
  });
});

describe("Button loading spinner", () => {
  it.each(["sm", "md", "lg"] as const)(
    "size=%s でもスピナーはアイコンと同じ 16px（loading で先頭スロットの寸法が変わらない）",
    (size) => {
      expect(BUTTON_ICON_SIZE).toBe(16);
      const idle = renderToStaticMarkup(
        <Button size={size} icon={Upload}>
          実行
        </Button>
      );
      const busy = renderToStaticMarkup(
        <Button size={size} icon={Upload} loading>
          実行
        </Button>
      );
      expect(attr(idle, "<svg", "width")).toBe("16");
      expect(attr(busy, "<svg", "width")).toBe("16");
      expect(attr(busy, "<svg", "height")).toBe("16");
      expect(classesOf(busy, "<svg")).toContain("animate-spin");
      // loading 中は 1 つだけ（アイコンとスピナーを重ねない）
      expect(busy.match(/<svg/g)).toHaveLength(1);
    }
  );

  it("loading 中のスピナーは fg-muted（disabled の文字色 fg-disabled では地に対して 3:1 に届かない）", () => {
    const html = renderToStaticMarkup(
      <Button icon={Upload} loading>
        実行
      </Button>
    );
    expect(classesOf(html, '<span class="pr-spinner')).toContain("text-fg-muted");
  });

  it("loading 中は children 側の先頭アイコンを隠して無効化する", () => {
    const html = renderToStaticMarkup(<Button loading>実行</Button>);
    // renderToStaticMarkup は class 内の & > を HTML エスケープする
    expect(classesOf(html, "<button")).toContain(
      "[&amp;&gt;svg:not(.animate-spin)]:hidden"
    );
    // loading 中はネイティブの disabled ではなく aria-disabled（フォーカスを保つ。#355）
    expect(html).toContain('aria-disabled="true"');
  });
});
