import { ChevronLeft, ChevronRight, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

import {
  LIST_DETAIL_DEFAULT_LIST_WIDTH_REM,
  LIST_DETAIL_KEYBOARD_FAST_STEP_REM,
  LIST_DETAIL_KEYBOARD_STEP_REM,
  LIST_DETAIL_MAX_LIST_WIDTH_REM,
  LIST_DETAIL_MIN_LIST_HEIGHT_REM,
  LIST_DETAIL_MIN_LIST_WIDTH_REM,
  LIST_DETAIL_SPLIT_QUERY,
  LIST_DETAIL_STICKY_GAP_REM,
  LIST_DETAIL_STORAGE_PREFIX,
  clampListWidthRem,
  fitListColumn,
  listDetailStorageKey,
  maxListWidthForContainer,
  nextListItemIndex,
  parseListDetailStoredState,
  serializeListDetailStoredState,
  type ListDetailStoredState,
} from "../../lib/list-detail";
import { cn } from "../../lib/utils";
import { useMediaQuery } from "../chat/use-chat-history-panel";
import { Button } from "../ui/button";

/** 一覧 + 詳細の文言。製品の i18n で上書きできる。 */
export interface ListDetailLayoutLabels {
  /** 一覧の列の幅を変える分割線（role="separator"）の読み上げ名。 */
  resize: string;
  /** 分割線の操作の案内。 */
  resizeHint: string;
  /** 分割線の今の値の読み上げ（`{width}` は rem の幅）。 */
  widthValue: (widthRem: number) => string;
}

export const DEFAULT_LIST_DETAIL_LAYOUT_LABELS: ListDetailLayoutLabels = {
  resize: "一覧の幅",
  resizeHint: "ドラッグ・左右の矢印キーで一覧の幅を変更・ダブルクリックか Home で既定の幅に戻す",
  widthValue: (widthRem) => `${widthRem}rem`,
};

/** 一覧の項目（↑ / ↓ でフォーカスを移す先）。既定は共有の `RowTitleButton`。 */
export const LIST_DETAIL_ITEM_SELECTOR = "[data-row-title-button]";

/** 一覧と詳細を横に並べる幅（xl 以上）か。未満は 1 ペイン。`ListDetailLayout` と同じ判定。 */
export function useListDetailSplit(): boolean {
  return useMediaQuery(LIST_DETAIL_SPLIT_QUERY, true);
}

function readStoredState(key: string): ListDetailStoredState {
  if (typeof window === "undefined") return parseListDetailStoredState(null);
  try {
    return parseListDetailStoredState(window.localStorage.getItem(key));
  } catch {
    return parseListDetailStoredState(null);
  }
}

function rootFontSizePx() {
  if (typeof window === "undefined" || typeof document === "undefined") return 14;
  const size = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(size) && size > 0 ? size : 14;
}

/** 本文のスクロール領域（`AppShell` の main）。無ければ文書のスクロール。 */
function scrollContainerOf(element: HTMLElement | null): HTMLElement | null {
  let node = element?.parentElement ?? null;
  while (node) {
    const overflowY = window.getComputedStyle(node).overflowY;
    if ((overflowY === "auto" || overflowY === "scroll") && node.scrollHeight > node.clientHeight) return node;
    node = node.parentElement;
  }
  return (document.scrollingElement as HTMLElement | null) ?? null;
}

/** 本文のスクロール領域（overflow-y が auto / scroll の最も近い祖先。`AppShell` の main）。無ければ null（文書のスクロール）。 */
function scrollportOf(element: HTMLElement): HTMLElement | null {
  let node = element.parentElement;
  while (node && node !== document.body && node !== document.documentElement) {
    const overflowY = window.getComputedStyle(node).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return node;
    node = node.parentElement;
  }
  return null;
}

function cssPx(value: string) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** 測った一覧の列の高さと貼り付く位置（rem）。 */
interface ListColumnFit {
  heightRem: number;
  topRem: number;
}

/**
 * 2 ペインの一覧の列の高さと貼り付く位置を測る（#2330）。列の上端（本文の中の位置。スクロールに依らない）・
 * 置き場所の下の余白・上端に貼り付く `PageHeader` の高さから、初めの表示で列の下端が画面の中に収まる高さを決める。
 */
function measureListColumn(root: HTMLElement): ListColumnFit | null {
  const fontSize = rootFontSizePx();
  const scroller = scrollportOf(root);
  const rootRect = root.getBoundingClientRect();
  let viewportHeight: number;
  let columnTop: number;
  if (scroller) {
    viewportHeight = scroller.clientHeight;
    columnTop = rootRect.top - scroller.getBoundingClientRect().top - scroller.clientTop + scroller.scrollTop;
  } else {
    viewportHeight = document.documentElement.clientHeight || window.innerHeight;
    columnTop = rootRect.top + window.scrollY;
  }
  // 置き場所の下の余白（PageBody の下の padding など）。スクロール領域の外の枠は含めない。
  let bottomInset = cssPx(window.getComputedStyle(root).marginBottom);
  for (let node = root.parentElement; node; node = node.parentElement) {
    const style = window.getComputedStyle(node);
    bottomInset += cssPx(style.paddingBottom);
    if (node === scroller || node === document.documentElement) break;
    bottomInset += cssPx(style.borderBottomWidth);
  }
  // 上端に貼り付く見出し（lg 以上の PageHeader）の下に貼り付く。
  const headers = Array.from((scroller ?? document).querySelectorAll<HTMLElement>("[data-page-header]"));
  const stickyHeader = headers.find((header) => window.getComputedStyle(header).position === "sticky");
  const headerBottom = stickyHeader
    ? stickyHeader.getBoundingClientRect().height + cssPx(window.getComputedStyle(stickyHeader).top)
    : 0;
  const fit = fitListColumn({
    viewportHeight,
    columnTop,
    stickyTop: headerBottom + LIST_DETAIL_STICKY_GAP_REM * fontSize,
    bottomInset,
    minHeight: LIST_DETAIL_MIN_LIST_HEIGHT_REM * fontSize,
  });
  if (!fit) return null;
  // rem に直す（高さは切り捨て・位置は切り上げ。丸めで列の下端が画面の外へ 1px 出ないようにする）。
  return {
    heightRem: Math.floor((fit.height / fontSize) * 1000) / 1000,
    topRem: Math.ceil((fit.top / fontSize) * 1000) / 1000,
  };
}

/** 置き場所・見出し・本文の大きさが変わったときに測る要素（列の上端の位置と見える高さを動かすもの）。 */
function fitObservedElements(root: HTMLElement) {
  const scroller = scrollportOf(root);
  const elements: Element[] = [];
  for (let node = root.parentElement; node; node = node.parentElement) {
    elements.push(node);
    if (node === scroller || node === document.documentElement) break;
  }
  const header = (scroller ?? document).querySelector("[data-page-header]");
  if (header) elements.push(header);
  return elements;
}

function visibleItems(container: HTMLElement | null, selector: string) {
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(selector)).filter(
    (element) => !element.hasAttribute("disabled") && element.getClientRects().length > 0
  );
}

export interface ListDetailLayoutProps {
  /**
   * 画面の識別子（`<feature>-<view>`。例: `table-management`）。一覧の幅と開閉の保存 key、
   * `data-testid`（`list-detail-<id>`・`-list`・`-detail`・`-resizer`・`-toggle`）に使う。
   */
  id: string;
  /** single は幅に関わらず一覧から全幅の詳細へ切り替える。既定は xl 以上で分割する。 */
  layout?: "responsive" | "single";
  /** 1 ペインの主一覧は table。枠・高さ・スクロールは DataTable が持つ（既定 panel）。 */
  listVariant?: "panel" | "table";
  /** 一覧の列の名前（翻訳済み。例:「テーブル一覧」）。列の読み上げ名と、開閉ボタンの名前・Tooltip に使う。 */
  listLabel: string;
  /** 一覧の列の上（スクロールしない）。検索・絞り込み・件数（`ListToolbar`）。 */
  listHeader?: ReactNode;
  /** 一覧の本体（行）。列の高さの中でスクロールする。`DataTable` は `className="min-h-0"` で列に合わせて縮む。 */
  list: ReactNode;
  /** 一覧の列の下（スクロールしない）。ページ送り・「さらに読み込む」。 */
  listFooter?: ReactNode;
  /** 詳細の列の名前（翻訳済み。例:「テーブルの詳細」）。 */
  detailLabel: string;
  /** 対象を選んでいるか（`useUrlSelection` の `selectedId !== null`）。1 ペインでは詳細を出すかを決める。 */
  hasSelection: boolean;
  /** 選んだ対象の詳細（読み込み中・失敗の表示も中に置く）。詳細はページと一緒にスクロールする。 */
  detail: ReactNode;
  /** 2 ペインで何も選んでいないときの詳細（`EmptyState`「一覧から〜を選んでください」）。 */
  emptyDetail: ReactNode;
  /** 詳細の列の上端の行の右（前へ / 次へ の `ListDetailPager` など）。 */
  detailToolbar?: ReactNode;
  /** ↑ / ↓ でフォーカスを移す一覧の項目の selector（既定は `RowTitleButton`）。 */
  itemSelector?: string;
  /** 幅と開閉を保存する localStorage key の前置き（製品ごと。既定 `production-ready.listDetail`）。 */
  storagePrefix?: string;
  labels?: Partial<ListDetailLayoutLabels>;
  className?: string;
}

/**
 * B 型（マスタ詳細の閲覧）の一覧 + 詳細（#2281。UX 契約 page-archetypes.md §1 B）。
 *
 * - xl 以上（2 ペイン）: 狭い一覧の列（既定 20rem・16〜32rem で幅を変えられ id ごとに保存）+ 広い詳細の列。
 *   一覧の列は sticky で画面の高さの中をスクロールし、詳細はページと一緒にスクロールする（入れ子のスクロールを作らない）。
 *   列の高さは「本文の見える高さ − 列の上端 − 置き場所の下の余白」で、詳細が短ければ本文に縦のスクロールを作らない。
 *   本文をスクロールしても高さは変えず、上端に貼り付く `PageHeader` の下に貼り付く（#2330）。置き場所の末尾に置く。
 *   詳細の上端の行の開閉ボタン（チャットの会話の履歴と同じ `PanelLeftClose` / `PanelLeftOpen`）で一覧を畳むと、詳細が全幅になる。
 * - xl 未満（1 ペイン）: 一覧だけを出し、選ぶと詳細だけを出す。戻る操作は画面の `PageHeader` の `back`
 *   （「一覧へ戻る」。`useUrlSelection` の `clear`）。一覧は描いたまま隠すので、検索・ページ・一覧の中のスクロールが残り、
 *   戻ると本文のスクロールの位置と、選んだ行へのフォーカスを戻す。
 * - ↑ / ↓ / Home / End で一覧の項目（`RowTitleButton`）の間をフォーカスが移り、Enter で選ぶ。
 * - 文言は翻訳済みを受け、業務の語彙を持たない。
 */
export function ListDetailLayout({
  id,
  layout = "responsive",
  listVariant = "panel",
  listLabel,
  listHeader,
  list,
  listFooter,
  detailLabel,
  hasSelection,
  detail,
  emptyDetail,
  detailToolbar,
  itemSelector = LIST_DETAIL_ITEM_SELECTOR,
  storagePrefix = LIST_DETAIL_STORAGE_PREFIX,
  labels: labelOverrides,
  className,
}: ListDetailLayoutProps) {
  const labels = { ...DEFAULT_LIST_DETAIL_LAYOUT_LABELS, ...labelOverrides };
  const responsiveSplit = useListDetailSplit();
  const split = layout !== "single" && responsiveSplit;
  const storageKey = listDetailStorageKey(id, storagePrefix);
  const [stored, setStored] = useState<ListDetailStoredState>(() => readStoredState(storageKey));
  const [previousKey, setPreviousKey] = useState(storageKey);
  if (previousKey !== storageKey) {
    setPreviousKey(storageKey);
    setStored(readStoredState(storageKey));
  }
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLElement>(null);
  const detailRef = useRef<HTMLElement>(null);
  const generatedId = useId();
  const listId = `${generatedId}-list`;
  const hintId = `${generatedId}-resize-hint`;
  const [containerRem, setContainerRem] = useState(0);
  const [fit, setFit] = useState<ListColumnFit | null>(null);
  const [dragging, setDragging] = useState(false);
  const cleanupDragRef = useRef<(() => void) | null>(null);
  // 1 ペインで詳細へ入るときの、本文のスクロールの位置と一覧の中のフォーカス（戻ると戻す）。
  const returnRef = useRef<{ scrollTop: number; focus: HTMLElement | null } | null>(null);
  const lastListFocusRef = useRef<HTMLElement | null>(null);
  const previousSelectionRef = useRef(hasSelection);
  const previousSplitRef = useRef(split);
  // 選んでいる行（URL から直接開いた詳細から戻るときのフォーカスの戻り先。選択を外した描画では aria-current が消える）。
  const selectedItemRef = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    if (!hasSelection) return;
    const item = listRef.current?.querySelector<HTMLElement>(`${itemSelector}[aria-current="true"]`);
    if (item) selectedItemRef.current = item;
  });

  const collapsed = split && stored.collapsed;
  const maxWidthRem = maxListWidthForContainer(containerRem);
  const listWidthRem = clampListWidthRem(stored.listWidthRem, LIST_DETAIL_MIN_LIST_WIDTH_REM, maxWidthRem);

  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey, serializeListDetailStoredState(stored));
    } catch {
      // Storage が無効な環境では今のセッションの state だけで動かす。
    }
  }, [storageKey, stored]);

  useEffect(
    () => () => {
      cleanupDragRef.current?.();
      cleanupDragRef.current = null;
    },
    []
  );

  // 置き場所の幅（詳細に最小の幅を残すための一覧の最大の幅）。
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const update = () => setContainerRem(root.clientWidth / rootFontSizePx());
    update();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  const showList = split ? !collapsed : !hasSelection;

  // 2 ペインの一覧の列の高さ（#2330）: 見える高さ − 列の上端 − 下の余白。最初の描画の前に測り（跳ねを出さない）、
  // 置き場所・見出し・本文の大きさが変わったときだけ測り直す（本文のスクロールでは変えない）。
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!split || !showList || !root) return undefined;
    const update = () => {
      const next = measureListColumn(root);
      setFit((current) =>
        current && next && current.heightRem === next.heightRem && current.topRem === next.topRem ? current : next
      );
    };
    update();
    window.addEventListener("resize", update);
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(update);
      fitObservedElements(root).forEach((element) => observer?.observe(element));
    }
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [split, showList]);

  // 1 ペインの行き来: 詳細へ入るときは本文の先頭へ移って詳細へフォーカス、戻るときはスクロールと行へのフォーカスを戻す。
  useLayoutEffect(() => {
    const wasSelected = previousSelectionRef.current;
    const wasSplit = previousSplitRef.current;
    previousSelectionRef.current = hasSelection;
    previousSplitRef.current = split;
    if (split || wasSplit !== split) return;
    if (!wasSelected && hasSelection) {
      const scroller = scrollContainerOf(rootRef.current);
      // 戻ったら選んだ行へフォーカスを戻す（戻ると選択が外れるので、入る時点の選んだ行を覚える）。
      const selectedItem = listRef.current?.querySelector<HTMLElement>(`${itemSelector}[aria-current="true"]`) ?? null;
      returnRef.current = { scrollTop: scroller?.scrollTop ?? 0, focus: selectedItem ?? lastListFocusRef.current };
      const root = rootRef.current;
      if (scroller && root) {
        const top = root.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
        if (scroller.scrollTop > top) scroller.scrollTo({ top: Math.max(0, top - 16) });
      }
      detailRef.current?.focus({ preventScroll: true });
      return;
    }
    if (wasSelected && !hasSelection) {
      const saved = returnRef.current;
      returnRef.current = null;
      const scroller = scrollContainerOf(rootRef.current);
      if (saved && scroller) scroller.scrollTo({ top: saved.scrollTop });
      const lastSelected = selectedItemRef.current?.isConnected ? selectedItemRef.current : null;
      // 前へ / 次へ で移った後も、最後に選んでいた行へ戻す。
      const target = lastSelected ?? (saved?.focus?.isConnected ? saved.focus : null) ?? listRef.current;
      target?.focus({ preventScroll: true });
    }
  }, [hasSelection, itemSelector, split]);

  const setCollapsed = (next: boolean) => setStored((current) => ({ ...current, collapsed: next }));
  const setWidth = useCallback(
    (widthRem: number) =>
      setStored((current) => ({
        ...current,
        listWidthRem: clampListWidthRem(widthRem, LIST_DETAIL_MIN_LIST_WIDTH_REM, maxWidthRem),
      })),
    [maxWidthRem]
  );

  function onListKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement;
    if (!target.matches(itemSelector)) return;
    const items = visibleItems(listRef.current, itemSelector);
    const index = nextListItemIndex(event.key, items.indexOf(target), items.length);
    if (index === null) return;
    event.preventDefault();
    items[index]?.focus();
  }

  function onResizeKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? LIST_DETAIL_KEYBOARD_FAST_STEP_REM : LIST_DETAIL_KEYBOARD_STEP_REM;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setWidth(listWidthRem - step);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setWidth(listWidthRem + step);
    } else if (event.key === "Home" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setWidth(LIST_DETAIL_DEFAULT_LIST_WIDTH_REM);
    }
  }

  function onResizePointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture の無い環境では window の listener だけで続ける。
    }
    cleanupDragRef.current?.();
    const startX = event.clientX;
    const startWidth = listWidthRem;
    const fontSize = rootFontSizePx();
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    setDragging(true);
    const onMove = (move: globalThis.PointerEvent) => setWidth(startWidth + (move.clientX - startX) / fontSize);
    const finish = (updateState = true) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      if (updateState) setDragging(false);
      cleanupDragRef.current = null;
    };
    const onEnd = () => finish();
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
    cleanupDragRef.current = () => finish(false);
  }

  const showDetail = split || hasSelection;
  const tableList = !split && listVariant === "table";
  const style = {
    "--list-detail-list-width": `${listWidthRem}rem`,
    ...(split && fit
      ? { "--list-detail-list-height": `${fit.heightRem}rem`, "--list-detail-list-top": `${fit.topRem}rem` }
      : {}),
  } as CSSProperties;
  const toolbar = split || detailToolbar;

  return (
    <div
      ref={rootRef}
      className={cn(
        "grid min-w-0 items-start gap-4",
        split && !collapsed && "grid-cols-[var(--list-detail-list-width)_0.75rem_minmax(0,1fr)] gap-0",
        className
      )}
      style={style}
      data-testid={`list-detail-${id}`}
      data-list-detail-layout={split ? "split" : "single"}
      data-list-collapsed={collapsed ? "true" : "false"}
      data-list-detail-view={split ? "both" : hasSelection ? "detail" : "list"}
    >
      <section
        ref={listRef}
        id={listId}
        aria-label={listLabel}
        tabIndex={-1}
        className={cn(
          // 一覧の列: 画面の高さの中でスクロールし（sticky）、詳細はページと一緒にスクロールする。
          "min-w-0 outline-none",
          tableList
            ? "grid gap-3"
            : "sticky flex min-h-0 flex-col self-start overflow-hidden rounded-lg border border-border bg-surface shadow-sm",
          // 2 ペイン: 測った高さ（見える高さ − 列の上端 − 下の余白）と、貼り付く見出しの下の位置（#2330）。
          // 1 ペイン: 画面の高さ − 2rem（md 未満はナビの上端のバー 3.5rem の分だけ低くする）。
          split
            ? "top-[var(--list-detail-list-top,1rem)] max-h-[var(--list-detail-list-height,calc(100dvh-2rem))]"
            : !tableList && "top-4 max-h-[calc(100dvh-5.5rem)] md:max-h-[calc(100dvh-2rem)]",
          !showList && "hidden"
        )}
        data-testid={`list-detail-${id}-list`}
        onFocus={(event) => {
          if ((event.target as HTMLElement).matches(itemSelector)) lastListFocusRef.current = event.target as HTMLElement;
        }}
        onKeyDown={onListKeyDown}
      >
        {listHeader ? (
          <div className={tableList ? undefined : "shrink-0 border-b border-border p-3"} data-list-detail-header="">
            {listHeader}
          </div>
        ) : null}
        {/* 溝（scrollbar-gutter）は取らない: 行の表（DataTable）は自分のスクロール領域に溝を持つので、ここでも取ると
            表の右に 2 本分の空白が残る（#2282）。 */}
        <div className={tableList ? "min-w-0" : "flex min-h-0 shrink flex-col overflow-y-auto"} data-list-detail-body="">{list}</div>
        {/* 中身が何も描かない（1 ページの Pagination など）ときは、列の下の帯を出さない（empty:hidden。#2283）。 */}
        {listFooter ? (
          <div className={tableList ? "empty:hidden" : "shrink-0 border-t border-border px-3 py-2 empty:hidden"} data-list-detail-footer="">
            {listFooter}
          </div>
        ) : null}
      </section>
      {split && !collapsed ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={labels.resize}
          aria-describedby={hintId}
          aria-controls={listId}
          aria-valuemin={LIST_DETAIL_MIN_LIST_WIDTH_REM}
          aria-valuemax={Math.max(LIST_DETAIL_MIN_LIST_WIDTH_REM, Math.min(LIST_DETAIL_MAX_LIST_WIDTH_REM, maxWidthRem))}
          aria-valuenow={listWidthRem}
          aria-valuetext={labels.widthValue(listWidthRem)}
          tabIndex={0}
          className="group relative flex w-3 cursor-col-resize touch-none select-none justify-center self-stretch"
          data-dragging={dragging ? "true" : "false"}
          data-testid={`list-detail-${id}-resizer`}
          onDoubleClick={() => setWidth(LIST_DETAIL_DEFAULT_LIST_WIDTH_REM)}
          onKeyDown={onResizeKeyDown}
          onPointerDown={onResizePointerDown}
        >
          <span
            className="my-3 w-0.5 rounded-full bg-transparent transition-colors group-hover:bg-accent-fg group-focus-visible:bg-accent-fg group-data-[dragging=true]:bg-accent-fg motion-reduce:transition-none"
            aria-hidden="true"
          />
          <span id={hintId} className="sr-only">
            {labels.resizeHint}
          </span>
        </div>
      ) : null}
      <section
        ref={detailRef}
        aria-label={detailLabel}
        tabIndex={-1}
        className={cn(
          "min-w-0 rounded-lg border border-border bg-surface shadow-sm outline-none",
          split && !collapsed && "col-start-3",
          !showDetail && "hidden"
        )}
        data-testid={`list-detail-${id}-detail`}
      >
        {toolbar ? (
          <div className="flex min-h-12 items-center gap-2 border-b border-border px-3 py-2">
            {split ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                iconOnly
                icon={collapsed ? PanelLeftOpen : PanelLeftClose}
                aria-label={listLabel}
                aria-expanded={!collapsed}
                aria-controls={listId}
                data-testid={`list-detail-${id}-toggle`}
                onClick={() => setCollapsed(!collapsed)}
              />
            ) : null}
            <div className="ml-auto flex min-w-0 items-center gap-1">{detailToolbar}</div>
          </div>
        ) : null}
        <div className="grid min-w-0 gap-4 p-4">{hasSelection ? detail : emptyDetail}</div>
      </section>
    </div>
  );
}

/** 「前へ / 次へ」の文言。 */
export interface ListDetailPagerLabels {
  previous: string;
  next: string;
}

export const DEFAULT_LIST_DETAIL_PAGER_LABELS: ListDetailPagerLabels = { previous: "前へ", next: "次へ" };

export interface ListDetailPagerProps {
  /** 前の項目へ。無い（先頭）ときは null。 */
  onPrevious: (() => void) | null;
  /** 次の項目へ。無い（末尾・今のページの外）ときは null。 */
  onNext: (() => void) | null;
  labels?: Partial<ListDetailPagerLabels>;
  disabled?: boolean;
  testId?: string;
}

/**
 * 詳細・シートの見出しの行に置く「前へ / 次へ」（#2281）。一覧の今の並び（今のページ）の前後の項目へ移る。
 * アイコンだけのボタン（名前と Tooltip は「前へ」「次へ」）。前後が無ければ押せない。
 */
export function ListDetailPager({ onPrevious, onNext, labels: labelOverrides, disabled, testId }: ListDetailPagerProps) {
  const labels = { ...DEFAULT_LIST_DETAIL_PAGER_LABELS, ...labelOverrides };
  return (
    <div className="flex items-center gap-1" role="group" aria-label={`${labels.previous} / ${labels.next}`} data-testid={testId}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        iconOnly
        icon={ChevronLeft}
        aria-label={labels.previous}
        disabled={disabled || !onPrevious}
        data-testid={testId ? `${testId}-previous` : undefined}
        onClick={() => onPrevious?.()}
      />
      <Button
        type="button"
        variant="ghost"
        size="sm"
        iconOnly
        icon={ChevronRight}
        aria-label={labels.next}
        disabled={disabled || !onNext}
        data-testid={testId ? `${testId}-next` : undefined}
        onClick={() => onNext?.()}
      />
    </div>
  );
}

/** 一覧の今の並びの中の前後の項目（`ListDetailPager` に渡す）。選んでいる項目が並びに無ければ両方 null。 */
export function adjacentListItems<T>(items: readonly T[], selectedKey: string | null, getKey: (item: T) => string) {
  const index = selectedKey === null ? -1 : items.findIndex((item) => getKey(item) === selectedKey);
  if (index < 0) return { previous: null, next: null } as { previous: T | null; next: T | null };
  return {
    previous: index > 0 ? items[index - 1] : null,
    next: index < items.length - 1 ? items[index + 1] : null,
  } as { previous: T | null; next: T | null };
}
