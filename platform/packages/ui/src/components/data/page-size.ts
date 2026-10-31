/** 一覧のページングの既定の 1 ページの件数（25 件。#1952）。手書きの PAGE_SIZE を作らず、この定数を使う。 */
export const DEFAULT_PAGE_SIZE = 25;

/**
 * 1 ページの件数（表示件数）の既定の選択肢（#1891）。既定は 25 件（#1952）。
 * Carbon の advanced pagination・Material のデータ表と同じく、少ない段から 100 件までを選べる。
 */
export const PAGE_SIZE_OPTIONS = [10, DEFAULT_PAGE_SIZE, 50, 100] as const;
