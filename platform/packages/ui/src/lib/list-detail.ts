// ListDetailLayout（B 型の一覧 + 詳細。#2281）の定数と、保存・幅の計算の純粋関数。

/**
 * 一覧と詳細を横に並べる幅（Tailwind の xl = 1280px）。未満は 1 ペイン（一覧 → 詳細 → 「一覧へ戻る」）。
 * Material 3 の list-detail（2 ペインは expanded の幅だけ）に合わせ、`FixedSplitPane` と同じ境目にする。
 */
export const LIST_DETAIL_SPLIT_QUERY = "(min-width: 1280px)";

/** 一覧の列の既定の幅（rem）。チャットの会話の履歴の列（20rem）と同じ。 */
export const LIST_DETAIL_DEFAULT_LIST_WIDTH_REM = 20;
/** 一覧の列の最小の幅（rem）。名前と補足 1 行が読める幅。 */
export const LIST_DETAIL_MIN_LIST_WIDTH_REM = 16;
/** 一覧の列の最大の幅（rem）。これより広い一覧は詳細の幅を奪う。 */
export const LIST_DETAIL_MAX_LIST_WIDTH_REM = 32;
/** 詳細の列に残す最小の幅（rem）。一覧を広げても詳細がこれより狭くならない。 */
export const LIST_DETAIL_MIN_DETAIL_WIDTH_REM = 30;
/** 幅を変えるキーの 1 回の量（rem）。Shift と押すと `LIST_DETAIL_KEYBOARD_FAST_STEP_REM`。 */
export const LIST_DETAIL_KEYBOARD_STEP_REM = 1;
export const LIST_DETAIL_KEYBOARD_FAST_STEP_REM = 4;
/** 幅と開閉を保存する localStorage key の既定の前置き。製品は `storagePrefix` で上書きできる。 */
export const LIST_DETAIL_STORAGE_PREFIX = "production-ready.listDetail";

export interface ListDetailStoredState {
  /** 一覧の列の幅（rem）。 */
  listWidthRem: number;
  /** 一覧を畳んでいるか（畳むと詳細が全幅）。 */
  collapsed: boolean;
}

export const LIST_DETAIL_DEFAULT_STATE: ListDetailStoredState = {
  listWidthRem: LIST_DETAIL_DEFAULT_LIST_WIDTH_REM,
  collapsed: false,
};

export function listDetailStorageKey(id: string, prefix = LIST_DETAIL_STORAGE_PREFIX) {
  return `${prefix}.${id}`;
}

/** 一覧の列の幅を範囲に収める（0.25rem 単位に丸める）。数でない値は既定の幅。 */
export function clampListWidthRem(
  value: number,
  minRem = LIST_DETAIL_MIN_LIST_WIDTH_REM,
  maxRem = LIST_DETAIL_MAX_LIST_WIDTH_REM
) {
  if (!Number.isFinite(value)) return Math.min(maxRem, Math.max(minRem, LIST_DETAIL_DEFAULT_LIST_WIDTH_REM));
  const rounded = Math.round(value * 4) / 4;
  return Math.min(maxRem, Math.max(minRem, rounded));
}

/**
 * 置き場所の幅（rem）から、一覧の列に使える最大の幅を決める。詳細に `LIST_DETAIL_MIN_DETAIL_WIDTH_REM` を残し、
 * 最小より小さくはしない（狭すぎる置き場所は 1 ペインになる）。
 */
export function maxListWidthForContainer(
  containerRem: number,
  minRem = LIST_DETAIL_MIN_LIST_WIDTH_REM,
  maxRem = LIST_DETAIL_MAX_LIST_WIDTH_REM,
  minDetailRem = LIST_DETAIL_MIN_DETAIL_WIDTH_REM
) {
  if (!Number.isFinite(containerRem) || containerRem <= 0) return maxRem;
  return Math.max(minRem, Math.min(maxRem, containerRem - minDetailRem));
}

/** 保存の値を読む。壊れた値・範囲の外の幅は既定・範囲の端に直す。 */
export function parseListDetailStoredState(value: string | null): ListDetailStoredState {
  if (!value) return { ...LIST_DETAIL_DEFAULT_STATE };
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!parsed || typeof parsed !== "object") return { ...LIST_DETAIL_DEFAULT_STATE };
    const record = parsed as Record<string, unknown>;
    const width =
      typeof record.listWidthRem === "number" ? clampListWidthRem(record.listWidthRem) : LIST_DETAIL_DEFAULT_LIST_WIDTH_REM;
    return { listWidthRem: width, collapsed: record.collapsed === true };
  } catch {
    return { ...LIST_DETAIL_DEFAULT_STATE };
  }
}

export function serializeListDetailStoredState(state: ListDetailStoredState) {
  return JSON.stringify({ listWidthRem: clampListWidthRem(state.listWidthRem), collapsed: state.collapsed });
}

/** ↑ / ↓ / Home / End で一覧の中のフォーカスを移す先の位置。移さないキーは null。 */
export function nextListItemIndex(key: string, currentIndex: number, count: number): number | null {
  if (count <= 0) return null;
  if (key === "ArrowDown") return currentIndex < 0 ? 0 : Math.min(count - 1, currentIndex + 1);
  if (key === "ArrowUp") return currentIndex < 0 ? 0 : Math.max(0, currentIndex - 1);
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/**
 * 2 ペインの一覧の列の最小の高さ（rem）。画面が低く、列の上の内容が多くても、行を数行は読める高さを残す
 * （これより低い画面では本文がスクロールする）。
 */
export const LIST_DETAIL_MIN_LIST_HEIGHT_REM = 20;
/** 一覧の列が貼り付く位置の、上端に貼り付く見出し（`PageHeader`）の下の間隔（rem）。 */
export const LIST_DETAIL_STICKY_GAP_REM = 1;

/** 一覧の列の高さを決める寸法（すべて px。本文のスクロール領域の中の値）。 */
export interface ListColumnFitInput {
  /** 本文のスクロール領域の見える高さ。 */
  viewportHeight: number;
  /** 一覧 + 詳細の上端の位置（スクロール領域の内容の先頭から。スクロールの量に依らない）。 */
  columnTop: number;
  /** 一覧の列が貼り付く位置（スクロール領域の上端から。上端に貼り付く見出しの高さ + 間隔）。 */
  stickyTop: number;
  /** 一覧 + 詳細の下に続く余白（置き場所の下の padding・border の合計）。 */
  bottomInset: number;
  /** 最小の高さ。 */
  minHeight: number;
}

/**
 * 2 ペインの一覧の列の高さ（px、整数に切り捨て）と貼り付く位置（px）。高さは「見える高さ − 列の上端 − 下の余白」で、
 * 初めの表示で列の下端（ページ送り・「さらに読み込む」）が画面の中に収まり、本文に縦のスクロールを作らない（#2330）。
 * 本文をスクロールしても高さは変えない（貼り付いた後も伸びない）。貼り付いた位置から画面の下端を越えないよう上限を掛け、
 * `minHeight` より低くはしない。寸法が取れない（見える高さが 0・数でない）ときは null（既定の CSS のまま）。
 */
export function fitListColumn({ viewportHeight, columnTop, stickyTop, bottomInset, minHeight }: ListColumnFitInput) {
  const values = [viewportHeight, columnTop, stickyTop, bottomInset, minHeight];
  if (!values.every(Number.isFinite) || viewportHeight <= 0) return null;
  const available = viewportHeight - Math.max(columnTop, stickyTop) - Math.max(0, bottomInset);
  return { height: Math.max(Math.floor(minHeight), Math.floor(available)), top: Math.max(0, stickyTop) };
}
