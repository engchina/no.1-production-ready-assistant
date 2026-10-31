import {
  useCallback,
  useLayoutEffect,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";

const MENU_GAP = 4;
const MENU_VIEWPORT_PADDING = 8;
/** `--z-dropdown` を読めないとき（テスト環境など）の既定値。elevation.css と同じ値。 */
const DEFAULT_DROPDOWN_Z_INDEX = 850;

export type FloatingMenuPlacement = "top" | "bottom";
/** start / end は左右の端をトリガーに合わせる。stretch はトリガーと同じ幅にする（SelectField）。 */
export type FloatingMenuAlign = "start" | "end" | "stretch";
/**
 * 上下の空きを測る範囲。scroll-ancestor はトリガーを含むスクロール枠の内側（操作メニュー）、
 * viewport は画面全体（Portal で描くのでスクロール枠に切られない。SelectField）。
 */
export type FloatingMenuBoundary = "scroll-ancestor" | "viewport";

type FloatingMenuPosition = {
  /** 実際にそろえた端（start / end は画面に入らなければ反対側へ反転する）。 */
  align: FloatingMenuAlign;
  constrained: boolean;
  placement: FloatingMenuPlacement;
  style: CSSProperties;
};

type RectLike = Pick<DOMRect, "top" | "bottom" | "left" | "right" | "width">;

export interface FloatingMenuLayoutInput {
  align: FloatingMenuAlign;
  triggerRect: RectLike;
  /** メニューの幅（stretch のときはトリガーの幅を使うので無視する）。 */
  menuWidth: number;
  /** 中身をすべて出したときの高さ（枠線を含む）。 */
  naturalHeight: number;
  /** 空きが十分でも超えない高さ（SelectField の 16rem）。 */
  maxHeight?: number;
  viewportWidth: number;
  viewportHeight: number;
  /** 上下の空きを測る範囲。省略時は viewport。 */
  boundaryRect?: Pick<DOMRect, "top" | "bottom"> | null;
}

function isVerticalScrollable(element: HTMLElement) {
  const overflowY = window.getComputedStyle(element).overflowY;
  return (
    /(auto|scroll|overlay)/u.test(overflowY) && element.scrollHeight > element.clientHeight + 1
  );
}

function getScrollableAncestor(element: HTMLElement) {
  let current = element.parentElement;
  while (current && current !== document.body && current !== document.documentElement) {
    if (isVerticalScrollable(current)) return current;
    current = current.parentElement;
  }
  return null;
}

/**
 * 下に収まらず上のほうが広ければ上に反転し、どちらにも収まらなければ広い側で内部スクロールにする。
 * 左右は指定の端（start = 左端、end = 右端）にそろえ、画面に入らず反対の端なら入るときは反対の端にそろえる。
 * どちらの端でも入らなければ、画面の内側（左右 8px）にずらす。
 * DOM を読まない純粋関数（テストで反転の境界を確かめる）。
 */
export function computeFloatingMenuLayout({
  align,
  triggerRect,
  menuWidth: measuredMenuWidth,
  naturalHeight,
  maxHeight,
  viewportWidth,
  viewportHeight,
  boundaryRect,
}: FloatingMenuLayoutInput): FloatingMenuPosition {
  const menuWidth = align === "stretch" ? Math.round(triggerRect.width) : measuredMenuWidth;
  const menuHeight = maxHeight ? Math.min(naturalHeight, maxHeight) : naturalHeight;
  const maxViewportHeight = Math.max(0, viewportHeight - MENU_VIEWPORT_PADDING * 2);
  const boundaryTop = boundaryRect
    ? Math.max(MENU_VIEWPORT_PADDING, boundaryRect.top)
    : MENU_VIEWPORT_PADDING;
  const boundaryBottom = boundaryRect
    ? Math.min(viewportHeight - MENU_VIEWPORT_PADDING, boundaryRect.bottom)
    : viewportHeight - MENU_VIEWPORT_PADDING;
  const availableBelow = Math.max(0, boundaryBottom - triggerRect.bottom - MENU_GAP);
  const availableAbove = Math.max(0, triggerRect.top - boundaryTop - MENU_GAP);
  const fitsBelow = menuHeight <= availableBelow;
  const fitsAbove = menuHeight <= availableAbove;
  const placement: FloatingMenuPlacement =
    fitsBelow || (!fitsAbove && availableBelow >= availableAbove) ? "bottom" : "top";
  const availableInDirection = placement === "top" ? availableAbove : availableBelow;
  const renderedHeight = Math.max(
    0,
    Math.min(menuHeight, availableInDirection, maxViewportHeight)
  );
  // 中身より低く描く（= 内部スクロールになる）とき。maxHeight で頭打ちにした場合も含む。
  const constrained = renderedHeight + 1 < naturalHeight;

  const unclampedTop =
    placement === "top"
      ? triggerRect.top - MENU_GAP - renderedHeight
      : triggerRect.bottom + MENU_GAP;
  const top = Math.min(
    Math.max(MENU_VIEWPORT_PADDING, unclampedTop),
    Math.max(MENU_VIEWPORT_PADDING, viewportHeight - MENU_VIEWPORT_PADDING - renderedHeight)
  );
  const minLeft = MENU_VIEWPORT_PADDING;
  const maxLeft = Math.max(MENU_VIEWPORT_PADDING, viewportWidth - MENU_VIEWPORT_PADDING - menuWidth);
  const leftFor = (edge: FloatingMenuAlign) =>
    edge === "end" ? triggerRect.right - menuWidth : triggerRect.left;
  const fitsHorizontally = (value: number) => value >= minLeft && value <= maxLeft;
  const opposite: FloatingMenuAlign | null =
    align === "end" ? "start" : align === "start" ? "end" : null;
  // 例: 375px で操作が折り返し、左端に来たトリガーの end 揃えは左外に切れる → start 揃えにする（#363）。
  const resolvedAlign =
    opposite && !fitsHorizontally(leftFor(align)) && fitsHorizontally(leftFor(opposite))
      ? opposite
      : align;
  const left = Math.min(Math.max(minLeft, leftFor(resolvedAlign)), maxLeft);
  const horizontalOrigin = resolvedAlign === "end" ? "right" : "left";

  return {
    align: resolvedAlign,
    constrained,
    placement,
    style: {
      left: Math.round(left),
      ...(constrained ? { maxHeight: Math.floor(renderedHeight) } : {}),
      maxWidth: `calc(100vw - ${MENU_VIEWPORT_PADDING * 2}px)`,
      top: Math.round(top),
      ...(align === "stretch" ? { width: menuWidth } : {}),
      transformOrigin: `${placement === "top" ? "bottom" : "top"} ${horizontalOrigin}`,
    },
  };
}

/**
 * Portal で body に出したメニューの重なり順。トリガーが z-index を持つ層（モーダル・固定ヘッダー）の中にあれば、
 * その層より 1 つ上に出す（`--z-dropdown` のままだとモーダルの暗幕の下に隠れる）。層の外なら undefined（class の `--z-dropdown`）。
 */
export function floatingLayerZIndex(ancestorZIndexes: readonly number[], dropdownZIndex: number) {
  const top = ancestorZIndexes.length > 0 ? Math.max(...ancestorZIndexes) : Number.NEGATIVE_INFINITY;
  return top >= dropdownZIndex ? top + 1 : undefined;
}

/**
 * トリガーの祖先の z-index を読んで、Portal で描く層の重なり順を決める（`floatingLayerZIndex` の DOM 側）。
 * `baseVariable` は層の外で使う z-index のトークン（メニューは `--z-dropdown`、Tooltip は `--z-popover`）。
 */
export function resolveFloatingLayerZIndex(
  trigger: HTMLElement,
  baseVariable = "--z-dropdown",
  fallback = DEFAULT_DROPDOWN_Z_INDEX
) {
  const zIndexes: number[] = [];
  let current = trigger.parentElement;
  while (current && current !== document.documentElement) {
    const style = window.getComputedStyle(current);
    const zIndex = Number.parseInt(style.zIndex, 10);
    if (style.position !== "static" && Number.isFinite(zIndex)) zIndexes.push(zIndex);
    current = current.parentElement;
  }
  const baseZIndex = Number.parseInt(
    window.getComputedStyle(document.documentElement).getPropertyValue(baseVariable),
    10
  );
  return floatingLayerZIndex(zIndexes, Number.isFinite(baseZIndex) ? baseZIndex : fallback);
}

/**
 * トリガーの位置に合わせて fixed のメニューを置く。開いている間はスクロール（祖先のどれでも）とリサイズに追従する。
 * FloatingActionMenu（PageHeader・ObjectActionBar・RowActionMenu・FormActionBar の操作メニュー）と SelectField が共有する。
 */
export function useFloatingMenuPosition({
  align = "end",
  boundary = "scroll-ancestor",
  maxHeightRem,
  menuRef,
  open,
  triggerRef,
}: {
  align?: FloatingMenuAlign;
  boundary?: FloatingMenuBoundary;
  /** 空きが十分でも超えない高さ（rem）。 */
  maxHeightRem?: number;
  menuRef: RefObject<HTMLElement | null>;
  open: boolean;
  triggerRef: RefObject<HTMLElement | null>;
}) {
  const [position, setPosition] = useState<FloatingMenuPosition | undefined>();

  const updatePosition = useCallback((event?: Event | number) => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!open || !trigger || !menu) return;
    // メニュー自身の内部スクロールでは位置は変わらない。
    if (typeof event === "object" && event.target instanceof Node && menu.contains(event.target)) return;

    const triggerRect = trigger.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const menuStyle = window.getComputedStyle(menu);
    const menuBorderHeight =
      (Number.parseFloat(menuStyle.borderTopWidth) || 0) +
      (Number.parseFloat(menuStyle.borderBottomWidth) || 0);
    const rootFontSize =
      Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16;
    const scrollableAncestor =
      boundary === "scroll-ancestor" ? getScrollableAncestor(trigger) : null;
    const layout = computeFloatingMenuLayout({
      align,
      triggerRect,
      menuWidth: Math.ceil(menu.offsetWidth),
      naturalHeight: Math.ceil(Math.max(menuRect.height, menu.scrollHeight + menuBorderHeight)),
      maxHeight: maxHeightRem ? maxHeightRem * rootFontSize : undefined,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      boundaryRect: scrollableAncestor?.getBoundingClientRect(),
    });
    const zIndex = resolveFloatingLayerZIndex(trigger);
    setPosition(
      zIndex === undefined ? layout : { ...layout, style: { ...layout.style, zIndex } }
    );
  }, [align, boundary, maxHeightRem, menuRef, open, triggerRef]);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(undefined);
      return undefined;
    }

    updatePosition();
    const animationFrame = window.requestAnimationFrame(updatePosition);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, updatePosition]);

  return position;
}

/**
 * 操作メニューを開くボタン・メニューの読み上げ名（#582）。「その他の操作」だけだと、ページ（PageHeader）と
 * カード（ObjectActionBar）・フォーム（FormActionBar）のボタンが同じ名前で並び、読み上げ・音声操作で区別できない。
 * 見えている文言を先頭に置き（WCAG 2.5.3 Label in Name）、括弧で対象（操作のグループの読み上げ名）を足す。
 */
export function contextualMenuLabel(label: string, context?: string) {
  const target = context?.trim();
  return target ? `${label}（${target}）` : label;
}

export function FloatingActionMenu({
  align,
  ariaLabel,
  children,
  className,
  id,
  menuRef,
  onKeyDown,
  open,
  triggerRef,
}: {
  align?: "start" | "end";
  /** メニューの読み上げ名（翻訳済み）。 */
  ariaLabel?: string;
  children: ReactNode;
  className?: string;
  id: string;
  menuRef: RefObject<HTMLDivElement | null>;
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  open: boolean;
  triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const position = useFloatingMenuPosition({ align, open, triggerRef, menuRef });

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      ref={menuRef}
      id={id}
      role="menu"
      aria-label={ariaLabel}
      data-floating-menu-align={position?.align}
      data-floating-menu-constrained={position?.constrained ? "true" : undefined}
      data-floating-menu-placement={position?.placement}
      className={cn(
        "fixed z-[var(--z-dropdown)] grid gap-1 rounded-md border border-border bg-surface-raised p-1 text-sm shadow-[var(--shadow-popover)]",
        position?.constrained && "overflow-y-auto overscroll-contain [scrollbar-gutter:stable]",
        !position && "opacity-0",
        className
      )}
      style={position?.style ?? { left: -9999, top: -9999 }}
      onKeyDown={onKeyDown}
    >
      {children}
    </div>,
    document.body
  );
}
