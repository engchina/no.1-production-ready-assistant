import { describe, expect, it } from "vitest";

import {
  PREVIEW_DEFAULT_ASPECT,
  PREVIEW_DPI_STEPS,
  anchoredScroll,
  clampZoom,
  normalizeViewRotation,
  previewDpiFor,
  previewHeightAspect,
  previewKeyAction,
  previewLayout,
  steppedZoom,
} from "../src/lib/preview-viewer";

const A4 = 595 / 842;

describe("previewLayout", () => {
  it("横幅に合わせる: 利用できる幅いっぱい、高さは縦横比から", () => {
    const layout = previewLayout({
      aspect: A4,
      rotation: 0,
      mode: "fit-width",
      zoomPercent: 100,
      availableWidth: 595,
      availableHeight: 400,
    });
    expect(layout.frameWidth).toBe(595);
    expect(layout.frameHeight).toBeCloseTo(842, 6);
    expect(layout.shellWidth).toBe(layout.frameWidth);
    expect(layout.shellHeight).toBeCloseTo(842, 6);
    expect(layout.zoomPercent).toBe(100);
  });

  it("全体を表示: 幅と高さの両方に収まる", () => {
    const layout = previewLayout({
      aspect: A4,
      rotation: 0,
      mode: "fit-page",
      zoomPercent: 100,
      availableWidth: 800,
      availableHeight: 421,
    });
    expect(layout.frameHeight).toBeCloseTo(421, 6);
    expect(layout.frameWidth).toBeCloseTo(297.5, 6);
    expect(layout.zoomPercent).toBeCloseTo((297.5 / 800) * 100, 6);
  });

  it("90 度回すと外枠の縦横が入れ替わり、内側の層は回転前の向きの寸法を保つ", () => {
    const layout = previewLayout({
      aspect: A4,
      rotation: 90,
      mode: "fit-width",
      zoomPercent: 100,
      availableWidth: 842,
      availableHeight: 500,
    });
    // 横向きになった A4 を幅 842 に合わせる → 高さ 595。
    expect(layout.frameWidth).toBe(842);
    expect(layout.frameHeight).toBeCloseTo(595, 6);
    // 内側（画像と強調）は縦長のまま 595x842 で、中央を基準に 90 度回すと外枠に収まる。
    expect(layout.shellWidth).toBeCloseTo(595, 6);
    expect(layout.shellHeight).toBe(842);
  });

  it("倍率は横幅に合わせた大きさを 100% とする", () => {
    const layout = previewLayout({
      aspect: 1,
      rotation: 180,
      mode: "zoom",
      zoomPercent: 250,
      availableWidth: 400,
      availableHeight: 400,
    });
    expect(layout.frameWidth).toBe(1000);
    expect(layout.shellWidth).toBe(1000);
    expect(layout.zoomPercent).toBe(250);
  });

  it("縦横比が分からないときは A4 縦、ビューポートが 0 でも崩れない", () => {
    const layout = previewLayout({
      aspect: null,
      rotation: 0,
      mode: "fit-page",
      zoomPercent: 100,
      availableWidth: 0,
      availableHeight: 0,
    });
    expect(layout.frameWidth).toBeGreaterThan(0);
    expect(Number.isFinite(layout.frameHeight)).toBe(true);
  });
});

describe("previewHeightAspect", () => {
  it("最も縦長のページの縦横比を使い、横長のページは高さを決めない", () => {
    expect(
      previewHeightAspect([
        { width: 792, height: 612 },
        { width: 612, height: 792 },
      ])
    ).toBeCloseTo(612 / 792, 6);
    expect(previewHeightAspect([{ width: 842, height: 595 }])).toBeCloseTo(842 / 595, 6);
  });

  it("寸法が分からない・0 のページは無視し、1 つも無ければ A4 縦を仮定する", () => {
    expect(previewHeightAspect([])).toBe(PREVIEW_DEFAULT_ASPECT);
    expect(previewHeightAspect([null, undefined, { width: 0, height: 100 }])).toBe(
      PREVIEW_DEFAULT_ASPECT
    );
    expect(previewHeightAspect([null, { width: 600, height: 800 }])).toBeCloseTo(0.75, 6);
  });
});

describe("steppedZoom / clampZoom", () => {
  it("段階で拡大・縮小し、端で止まる", () => {
    expect(steppedZoom(100, "in")).toBe(125);
    expect(steppedZoom(100, "out")).toBe(75);
    expect(steppedZoom(137, "in")).toBe(150);
    expect(steppedZoom(137, "out")).toBe(125);
    expect(steppedZoom(400, "in")).toBe(400);
    expect(steppedZoom(25, "out")).toBe(25);
    // 全体表示（例えば 37%）からの一段は、その倍率の前後の段階。
    expect(steppedZoom(37.2, "in")).toBe(50);
    expect(steppedZoom(37.2, "out")).toBe(25);
  });

  it("範囲外と数値でない値を丸める", () => {
    expect(clampZoom(10)).toBe(25);
    expect(clampZoom(900)).toBe(400);
    expect(clampZoom(Number.NaN)).toBe(100);
  });
});

describe("normalizeViewRotation", () => {
  it("90 度単位で 0〜270 に正規化する", () => {
    expect(normalizeViewRotation(-90)).toBe(270);
    expect(normalizeViewRotation(360)).toBe(0);
    expect(normalizeViewRotation(450)).toBe(90);
    expect(normalizeViewRotation(-540)).toBe(180);
  });
});

describe("previewDpiFor", () => {
  it("表示する大きさに足りる段階の dpi を選ぶ", () => {
    // A4 幅 595pt を 595 CSS px で表示: 72dpi 相当 → 最小段階。
    expect(previewDpiFor(595, 595, 1)).toBe(PREVIEW_DPI_STEPS[0]);
    // 高解像度の画面（2 倍）では 144dpi。
    expect(previewDpiFor(595, 595, 2)).toBe(144);
    // 大きく拡大すると上限。
    expect(previewDpiFor(5000, 595, 2)).toBe(288);
    expect(previewDpiFor(0, 595)).toBe(144);
    expect(previewDpiFor(500, null)).toBe(144);
  });
});

describe("previewKeyAction", () => {
  it("キーを操作に対応させる", () => {
    expect(previewKeyAction({ key: "+" })).toBe("zoom-in");
    expect(previewKeyAction({ key: "=" })).toBe("zoom-in");
    expect(previewKeyAction({ key: "-" })).toBe("zoom-out");
    expect(previewKeyAction({ key: "0" })).toBe("fit-page");
    expect(previewKeyAction({ key: "w" })).toBe("fit-width");
    expect(previewKeyAction({ key: "r" })).toBe("rotate-right");
    expect(previewKeyAction({ key: "R", shiftKey: true })).toBe("rotate-left");
    expect(previewKeyAction({ key: "PageDown" })).toBe("next-page");
    expect(previewKeyAction({ key: "PageUp" })).toBe("previous-page");
    expect(previewKeyAction({ key: "Home" })).toBe("first-page");
    expect(previewKeyAction({ key: "End" })).toBe("last-page");
    expect(previewKeyAction({ key: "h" })).toBe("focus-highlight");
  });

  it("矢印キー（パン）とブラウザの操作（Ctrl / Meta / Alt 付き）は扱わない", () => {
    expect(previewKeyAction({ key: "ArrowDown" })).toBeNull();
    expect(previewKeyAction({ key: " " })).toBeNull();
    expect(previewKeyAction({ key: "+", ctrlKey: true })).toBeNull();
    expect(previewKeyAction({ key: "0", metaKey: true })).toBeNull();
    expect(previewKeyAction({ key: "r", altKey: true })).toBeNull();
  });
});

describe("anchoredScroll", () => {
  it("拡大縮小の前後で基準の点を同じ位置に保つ", () => {
    // 幅 1000 の内容を scrollLeft 200 で見ていて、中央（x=150）を基準に 2 倍にする。
    expect(
      anchoredScroll({
        scrollLeft: 200,
        scrollTop: 0,
        pointX: 150,
        pointY: 100,
        previousWidth: 1000,
        previousHeight: 800,
        nextWidth: 2000,
        nextHeight: 1600,
      })
    ).toEqual({ left: 550, top: 100 });
    // 縮小して左端を超える場合は 0 に止める。
    expect(
      anchoredScroll({
        scrollLeft: 0,
        scrollTop: 0,
        pointX: 300,
        pointY: 0,
        previousWidth: 1000,
        previousHeight: 800,
        nextWidth: 400,
        nextHeight: 300,
      })
    ).toEqual({ left: 0, top: 0 });
  });
});
