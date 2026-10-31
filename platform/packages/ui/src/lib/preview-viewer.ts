// 文書プレビューのビューア（回転・拡大縮小・フィット・パン・ページ送り）の計算（#349）。
// 画面の部品（PreviewViewer）から分けて、単体テストで確かめられるようにする。
//
// 倍率の % は「横幅に合わせた大きさ」を 100% とする（denpyo-toroku-kun の画像レビューと同じ）。
// 全体表示は、ビューポートの実寸（ResizeObserver）から縦横どちらにも収まる大きさを求める。

export type PreviewFitMode = "fit-width" | "fit-page" | "zoom";

export const PREVIEW_ZOOM_STEPS = [25, 50, 75, 100, 125, 150, 200, 250, 300, 400] as const;
export const PREVIEW_MIN_ZOOM = PREVIEW_ZOOM_STEPS[0];
export const PREVIEW_MAX_ZOOM = PREVIEW_ZOOM_STEPS[PREVIEW_ZOOM_STEPS.length - 1];
/** 寸法が分かるまでの縦横比（A4 縦）。 */
export const PREVIEW_DEFAULT_ASPECT = 1 / 1.414;
/** PDF のページ画像を描く dpi の段階（倍率に応じて選ぶ）。 */
export const PREVIEW_DPI_STEPS = [96, 144, 216, 288] as const;

export function normalizeViewRotation(value: number): number {
  const rounded = Math.round(value / 90) * 90;
  return ((rounded % 360) + 360) % 360;
}

export type PreviewLayout = {
  /** 回転後の外枠（スクロール領域が占める大きさ）。 */
  frameWidth: number;
  frameHeight: number;
  /** 回転前の画像と強調の層（中央を基準に回す）。 */
  shellWidth: number;
  shellHeight: number;
  /** 横幅に合わせた大きさを 100 とした、現在の倍率。 */
  zoomPercent: number;
};

/**
 * ページの表示寸法を求める。
 *
 * - `aspect` は回転前のページの幅 / 高さ。90 / 270 度では外枠の縦横比が逆になる。
 * - 外枠の幅は、横幅: 利用できる幅、全体: 幅と高さの両方に収まる幅、倍率指定: 横幅 × %。
 * - 内側の層は回転前の向きの寸法（90 / 270 度は外枠の幅と高さを入れ替える）で、
 *   中央を基準に rotate すると外枠にちょうど収まる。画像と強調を同じ層に置くので、強調も一緒に回る。
 */
export function previewLayout({
  aspect,
  rotation,
  mode,
  zoomPercent,
  availableWidth,
  availableHeight,
}: {
  aspect: number | null | undefined;
  rotation: number;
  mode: PreviewFitMode;
  zoomPercent: number;
  availableWidth: number;
  availableHeight: number;
}): PreviewLayout {
  const pageAspect = aspect && Number.isFinite(aspect) && aspect > 0 ? aspect : PREVIEW_DEFAULT_ASPECT;
  const quarterTurn = normalizeViewRotation(rotation) % 180 !== 0;
  const displayAspect = quarterTurn ? 1 / pageAspect : pageAspect;
  const width = Math.max(1, availableWidth);
  const height = Math.max(1, availableHeight);
  const fitPageWidth = Math.min(width, height * displayAspect);
  const frameWidth =
    mode === "fit-width"
      ? width
      : mode === "fit-page"
        ? fitPageWidth
        : (width * clampZoom(zoomPercent)) / 100;
  const frameHeight = frameWidth / displayAspect;
  return {
    frameWidth,
    frameHeight,
    shellWidth: quarterTurn ? frameHeight : frameWidth,
    shellHeight: quarterTurn ? frameWidth : frameHeight,
    zoomPercent: (frameWidth / width) * 100,
  };
}

/**
 * ビューアの高さを決める縦横比（幅 / 高さ）。文書のうち最も縦長のページの値を使う（#559）。
 *
 * - ページごとに高さを変えると、ページ送りのたびに文書詳細の 2 ペインの高さが跳ねるため、文書で 1 つに決める。
 *   最も縦長のページが幅に合わせて丸ごと入るので、横長のページは上下に余白を残して収まる。
 * - 回転は含めない（利用者の一時的な操作で、ペインの高さを変えるほどではない。回した後は「全体を表示」で収まる）。
 * - 寸法が 1 つも分からないうち（ページ一覧の取得前・画像の読み込み前）は A4 縦を仮定する。
 */
export function previewHeightAspect(
  pageSizes: ReadonlyArray<{ width?: number | null; height?: number | null } | null | undefined>
): number {
  const aspects = pageSizes
    .map((size) =>
      size?.width && size.height && size.width > 0 && size.height > 0
        ? size.width / size.height
        : null
    )
    .filter((aspect): aspect is number => aspect != null && Number.isFinite(aspect));
  return aspects.length > 0 ? Math.min(...aspects) : PREVIEW_DEFAULT_ASPECT;
}

export function clampZoom(value: number): number {
  if (!Number.isFinite(value)) return 100;
  return Math.min(PREVIEW_MAX_ZOOM, Math.max(PREVIEW_MIN_ZOOM, value));
}

/** 現在の倍率から、一段大きい（小さい）段階を返す。端では同じ値を返す。 */
export function steppedZoom(currentPercent: number, direction: "in" | "out"): number {
  const current = clampZoom(currentPercent);
  if (direction === "in") {
    return PREVIEW_ZOOM_STEPS.find((step) => step > current + 0.5) ?? PREVIEW_MAX_ZOOM;
  }
  const smaller = PREVIEW_ZOOM_STEPS.filter((step) => step < current - 0.5);
  return smaller.length > 0 ? smaller[smaller.length - 1] : PREVIEW_MIN_ZOOM;
}

/**
 * 表示する大きさに足りる dpi を選ぶ（ページ画像の再取得を減らすため段階にする）。
 * `displayWidth` は CSS px の回転前の幅、`pageWidthPt` は PDF のページ幅（pt）。
 */
export function previewDpiFor(
  displayWidth: number,
  pageWidthPt: number | null | undefined,
  devicePixelRatio = 1
): number {
  const ratio = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1;
  if (!pageWidthPt || !(pageWidthPt > 0) || !(displayWidth > 0)) return PREVIEW_DPI_STEPS[1];
  const needed = (displayWidth * ratio * 72) / pageWidthPt;
  return PREVIEW_DPI_STEPS.find((dpi) => dpi >= needed) ?? PREVIEW_DPI_STEPS[PREVIEW_DPI_STEPS.length - 1];
}

export type PreviewKeyAction =
  | "zoom-in"
  | "zoom-out"
  | "fit-page"
  | "fit-width"
  | "rotate-right"
  | "rotate-left"
  | "previous-page"
  | "next-page"
  | "first-page"
  | "last-page"
  | "focus-highlight";

/**
 * ビューアにフォーカスがあるときのキー操作。矢印キーとスペースはスクロール（パン）のまま残す。
 * Ctrl / Meta / Alt 付きはブラウザの操作（Ctrl + +/- の画面拡大など）を妨げないため扱わない。
 */
export function previewKeyAction(event: {
  key: string;
  shiftKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
}): PreviewKeyAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  switch (event.key) {
    case "+":
    case "=":
      return "zoom-in";
    case "-":
    case "_":
      return "zoom-out";
    case "0":
      return "fit-page";
    case "w":
    case "W":
      return "fit-width";
    case "r":
      return event.shiftKey ? "rotate-left" : "rotate-right";
    case "R":
      return event.shiftKey ? "rotate-left" : "rotate-right";
    case "PageDown":
      return "next-page";
    case "PageUp":
      return "previous-page";
    case "Home":
      return "first-page";
    case "End":
      return "last-page";
    case "h":
    case "H":
      return "focus-highlight";
    default:
      return null;
  }
}

/** 拡大縮小の前後で、ビューポート内の同じ点（既定は中央）を画面上の同じ位置に保つスクロール量。 */
export function anchoredScroll({
  scrollLeft,
  scrollTop,
  pointX,
  pointY,
  previousWidth,
  previousHeight,
  nextWidth,
  nextHeight,
}: {
  scrollLeft: number;
  scrollTop: number;
  pointX: number;
  pointY: number;
  previousWidth: number;
  previousHeight: number;
  nextWidth: number;
  nextHeight: number;
}): { left: number; top: number } {
  const ratioX = previousWidth > 0 ? (scrollLeft + pointX) / previousWidth : 0;
  const ratioY = previousHeight > 0 ? (scrollTop + pointY) / previousHeight : 0;
  return {
    left: Math.max(0, ratioX * nextWidth - pointX),
    top: Math.max(0, ratioY * nextHeight - pointY),
  };
}
