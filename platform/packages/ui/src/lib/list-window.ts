/**
 * 行の高さが決まっている一覧の仮想スクロールの計算（純粋ロジック。#600）。
 *
 * 大量の候補から選ぶ一覧（`ListPicker`）は、読み込んだ候補が数千件になっても、見えている範囲と前後の少しだけを描く。
 * 行の高さは種類（グループの見出し・候補の行）ごとに一定で、`prefixOffsets` で各行の上端を求めてから、
 * `visibleRange` でスクロール位置から描く範囲を求める。
 */

/** 各行の上端の位置（長さは行数 + 1。末尾は全体の高さ）。 */
export function prefixOffsets(heights: readonly number[]): number[] {
  const offsets = new Array<number>(heights.length + 1);
  offsets[0] = 0;
  for (let index = 0; index < heights.length; index += 1) {
    offsets[index + 1] = offsets[index] + Math.max(0, heights[index] ?? 0);
  }
  return offsets;
}

/** 位置 `y` を含む行の index（`offsets` は prefixOffsets の結果）。範囲外は端の行に寄せる。 */
export function rowIndexAt(offsets: readonly number[], y: number): number {
  const count = offsets.length - 1;
  if (count <= 0) return 0;
  if (!(y > 0)) return 0;
  let low = 0;
  let high = count - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (offsets[mid] <= y) low = mid;
    else high = mid - 1;
  }
  return low;
}

export interface VisibleRange {
  /** 描く最初の行（含む）。 */
  start: number;
  /** 描く最後の行の次（含まない）。 */
  end: number;
}

/**
 * スクロール位置から描く行の範囲を求める。
 * `overscan` は見えている範囲の上下に余分に描く高さ（px）。慣性スクロール・件数が減った直後など、
 * スクロール位置が全体の高さを超えていても空の範囲を返さないよう、末尾に寄せる。
 */
export function visibleRange(
  offsets: readonly number[],
  scrollTop: number,
  viewportHeight: number,
  overscan: number
): VisibleRange {
  const count = offsets.length - 1;
  if (count <= 0) return { start: 0, end: 0 };
  const total = offsets[count];
  const safeViewport = Math.max(0, viewportHeight);
  const maxScroll = Math.max(0, total - safeViewport);
  const top = Math.min(Math.max(0, Number.isFinite(scrollTop) ? scrollTop : 0), maxScroll);
  const start = rowIndexAt(offsets, top - overscan);
  const last = rowIndexAt(offsets, top + safeViewport + overscan);
  return { start, end: Math.min(count, last + 1) };
}

/**
 * 行 `index` が見える位置へ動かすための scrollTop（すでに見えていれば今の値）。
 * キーボードで選んだ行を、スクロール領域の中だけを動かして見せる（ページ全体は動かさない）。
 */
export function scrollTopToReveal(
  offsets: readonly number[],
  index: number,
  scrollTop: number,
  viewportHeight: number
): number {
  const count = offsets.length - 1;
  if (index < 0 || index >= count) return scrollTop;
  const top = offsets[index];
  const bottom = offsets[index + 1];
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewportHeight) return Math.max(0, bottom - viewportHeight);
  return scrollTop;
}
