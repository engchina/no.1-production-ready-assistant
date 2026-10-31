/**
 * 一覧の表示密度（3 製品で共通。NL2SQL の基準、#265）。
 *
 * 件数が多い一覧は DataTable の `stickyHeader` + `visibleRows` で表の中を縦スクロールにし、
 * ページ全体を伸ばさない。表示行数は md（48rem）未満 5 行・md 以上 8 行、行の最小高さは 3.5rem。
 * 製品で行数や高さの数値を書かず、ここの定数を使う。
 */
export const INFORMATION_LIST_VISIBLE_ROWS = {
  mobile: 5,
  desktop: 8,
  rowHeightRem: 3.5,
  headerHeightRem: 2.5,
} as const;

/** 共有 DataTable の visibleRows に渡す一覧の表示行数（md 未満 5 行・md 以上 8 行）。高さは表頭と行の実測で決まる。 */
export const INFORMATION_TABLE_VISIBLE_ROWS = {
  base: INFORMATION_LIST_VISIBLE_ROWS.mobile,
  md: INFORMATION_LIST_VISIBLE_ROWS.desktop,
} as const;

/** 画面幅によらず 5 行の固定高さにする一覧（対象グリッド等）。 */
export const INFORMATION_TABLE_FIXED_VISIBLE_ROWS = INFORMATION_LIST_VISIBLE_ROWS.mobile;

/** 表の行の最小高さ。1 行セルでも行の高さをそろえる（2 行セルで超えた分は visibleRows の実測が吸収する）。DataTable の `rowProps` の className に渡す。 */
export const INFORMATION_TABLE_ROW_CLASS = "h-[3.5rem]";

/** 表ではない行リスト（カードの行）の最小高さ。 */
export const INFORMATION_LIST_ROW_CLASS = "min-h-[3.5rem]";

/** 表ではない行リストを md 未満 5 行・md 以上 8 行の高さで縦スクロールにする。 */
export const INFORMATION_LIST_SCROLL_CLASS = "max-h-[17.5rem] overflow-auto [scrollbar-gutter:stable] md:max-h-[28rem]";

/** 画面幅によらず 5 行の高さで縦スクロールにする小さな行リスト。 */
export const INFORMATION_COMPACT_LIST_FIVE_ROW_SCROLL_CLASS = "h-56 max-h-56 overflow-auto [scrollbar-gutter:stable]";

/** Tab で到達できるスクロール領域のフォーカスの表示（outline に一本化、#355）。 */
export const INFORMATION_TABLE_FOCUS_CLASS =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring";
