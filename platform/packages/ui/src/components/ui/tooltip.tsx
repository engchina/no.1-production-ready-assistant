import {
  cloneElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type FocusEvent,
  type PointerEvent,
  type ReactElement,
  type Ref,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";
import { resolveFloatingLayerZIndex } from "./floating-menu";
import { selectPortalContainer } from "./select-field";

/*
 * 共有の Tooltip（#372）。WAI-ARIA APG の Tooltip パターンに従う。
 *   - ホバーとキーボードのフォーカスで出す。遅延はホバーのときだけ（フォーカスはすぐ出す）
 *   - Escape で閉じる（フォーカスもポインタも動かさずに消せる。WCAG 1.4.13 dismissible）
 *   - ポインタをトリガーから吹き出しへ移しても消えない（WCAG 1.4.13 hoverable）
 *   - 補足の説明として `aria-describedby` で結び付ける。読み上げ名（aria-label）と同じ文言なら結び付けず、
 *     吹き出しは `aria-hidden` にする（二重に読み上げない）
 *   - 画面の端では上下を反転し、左右は画面の内側にずらす。Portal で描き、親の overflow に切られない
 *   - タッチ端末（pointer: coarse）では出さない（長押しの独自操作と競合し、指で吹き出しが隠れるため）
 *   - 動きは overlay-in の fade だけ。prefers-reduced-motion では base.css が一括で止める
 */

/** ホバーから表示までの待ち時間。ポインタが通り過ぎただけで出さない。 */
export const TOOLTIP_SHOW_DELAY_MS = 400;
/** トリガーを離れてから閉じるまでの猶予。この間に吹き出しへ移れば消えない（WCAG 1.4.13）。 */
export const TOOLTIP_HIDE_DELAY_MS = 100;
/** ホバーで開いた Tooltip が閉じてからこの時間内に隣のトリガーへ移ったら、待たずに出す（ツールバーをなぞるとき）。 */
export const TOOLTIP_SKIP_DELAY_MS = 300;
/** タッチ端末の判定。PR D（#364）の当たり判定の拡大と同じ条件。 */
export const TOOLTIP_COARSE_POINTER_QUERY = "(pointer: coarse)";

const TOOLTIP_GAP = 4;
const TOOLTIP_VIEWPORT_PADDING = 8;
/** `--z-popover` を読めないとき（テスト環境など）の既定値。elevation.css と同じ値。 */
const DEFAULT_POPOVER_Z_INDEX = 200;

export type TooltipPlacement = "top" | "bottom";
/**
 * 開いたきっかけ。`press` は押して開いたまま固定したもの（`InfoTip` のクリック・タップ・Enter / Space。#901）で、
 * ポインタが離れても・フォーカスが外れるまでは閉じない。
 */
export type TooltipOpenReason = "hover" | "focus" | "press";

type RectLike = Pick<DOMRect, "top" | "bottom" | "left" | "right" | "width">;

export interface TooltipLayoutInput {
  triggerRect: RectLike;
  tooltipWidth: number;
  tooltipHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  /** 空きがあれば出す側。既定は上。 */
  placement?: TooltipPlacement;
}

export interface TooltipLayout {
  /** 実際に出した側（入らなければ反対側へ反転する）。 */
  placement: TooltipPlacement;
  left: number;
  top: number;
}

/**
 * 吹き出しの位置。指定の側（既定は上）に入らず反対側なら入るときは反転し、どちらにも入らなければ広い側に出す。
 * 左右はトリガーの中央にそろえ、画面（左右 8px の余白）の外に出るときは内側にずらす。
 * 考え方は `computeFloatingMenuLayout`（#363）と同じ。DOM を読まない純粋関数（テストで反転の境界を確かめる）。
 */
export function computeTooltipLayout({
  triggerRect,
  tooltipWidth,
  tooltipHeight,
  viewportWidth,
  viewportHeight,
  placement: preferred = "top",
}: TooltipLayoutInput): TooltipLayout {
  const availableAbove = triggerRect.top - TOOLTIP_VIEWPORT_PADDING - TOOLTIP_GAP;
  const availableBelow = viewportHeight - TOOLTIP_VIEWPORT_PADDING - triggerRect.bottom - TOOLTIP_GAP;
  const fits = (side: TooltipPlacement) =>
    tooltipHeight <= (side === "top" ? availableAbove : availableBelow);
  const opposite: TooltipPlacement = preferred === "top" ? "bottom" : "top";
  const placement: TooltipPlacement = fits(preferred)
    ? preferred
    : fits(opposite)
      ? opposite
      : availableBelow > availableAbove
        ? "bottom"
        : "top";

  const unclampedTop =
    placement === "top"
      ? triggerRect.top - TOOLTIP_GAP - tooltipHeight
      : triggerRect.bottom + TOOLTIP_GAP;
  const top = Math.min(
    Math.max(TOOLTIP_VIEWPORT_PADDING, unclampedTop),
    Math.max(TOOLTIP_VIEWPORT_PADDING, viewportHeight - TOOLTIP_VIEWPORT_PADDING - tooltipHeight)
  );
  const centeredLeft = triggerRect.left + triggerRect.width / 2 - tooltipWidth / 2;
  const left = Math.min(
    Math.max(TOOLTIP_VIEWPORT_PADDING, centeredLeft),
    Math.max(TOOLTIP_VIEWPORT_PADDING, viewportWidth - TOOLTIP_VIEWPORT_PADDING - tooltipWidth)
  );
  return { placement, left: Math.round(left), top: Math.round(top) };
}

/** 吹き出しを `aria-describedby` で結び付けるか。読み上げ名と同じ文言なら結び付けない（二重に読み上げない）。 */
export function tooltipDescribesTrigger(content: string, accessibleName: unknown) {
  return typeof accessibleName !== "string" || content.trim() !== accessibleName.trim();
}

/** ホバーで開いた Tooltip が最後に閉じた時刻（全 Tooltip で共有。隣のトリガーへ移ったときに待たずに出す）。 */
let lastHoverCloseAt = Number.NEGATIVE_INFINITY;
/**
 * いま開いている Tooltip を閉じる関数（全 Tooltip で共有）。吹き出しは画面に 1 つだけ出す。ポインタを
 * 別のボタンに置いたままキーボードで移ったときなどに、ホバーの吹き出しとフォーカスの吹き出しを重ねない。
 */
let closeActiveTooltip: (() => void) | null = null;

export interface TooltipControllerOptions {
  onOpenChange: (open: boolean, reason: TooltipOpenReason | null) => void;
  /** タッチ端末か（true の間は出さない）。 */
  isCoarsePointer?: () => boolean;
  showDelayMs?: number;
  hideDelayMs?: number;
  skipDelayMs?: number;
}

export interface TooltipController {
  pointerEnterTrigger: (pointerType?: string) => void;
  pointerLeaveTrigger: () => void;
  pointerEnterTooltip: () => void;
  pointerLeaveTooltip: () => void;
  /** `focusVisible` はキーボードでのフォーカスか（マウスで押したときのフォーカスでは出さない）。 */
  focus: (focusVisible: boolean) => void;
  blur: () => void;
  /** トリガーを押したら閉じ、ポインタが離れるまで出さない（押した操作の結果を隠さない）。 */
  pointerDown: () => void;
  /** 開いていれば閉じて true を返す（呼び出し側はキーを握りつぶす）。 */
  escape: () => boolean;
  /**
   * 押して開閉する（`InfoTip` のクリック・タップ・Enter / Space。#901）。固定して開いていれば閉じ、
   * それ以外（閉じている・ホバーやフォーカスで開いている）は開いたまま固定する。タッチ端末でも開く。
   */
  press: () => void;
  /** 外側を押したときなどに閉じる（固定も外す）。開いていなければ何もしない。 */
  dismiss: () => void;
  isOpen: () => boolean;
  dispose: () => void;
}

/**
 * 開閉の状態機械。DOM にも React にも依存しない（タイマーだけ使う）ので、単体テストで遅延・Escape・
 * WCAG 1.4.13 の hoverable・タッチ端末を確かめられる。
 */
export function createTooltipController({
  onOpenChange,
  isCoarsePointer = () => false,
  showDelayMs = TOOLTIP_SHOW_DELAY_MS,
  hideDelayMs = TOOLTIP_HIDE_DELAY_MS,
  skipDelayMs = TOOLTIP_SKIP_DELAY_MS,
}: TooltipControllerOptions): TooltipController {
  let open = false;
  let reason: TooltipOpenReason | null = null;
  let hoveringTrigger = false;
  let hoveringTooltip = false;
  let focused = false;
  /** 押して開いたまま固定しているか（`press`）。固定中はポインタが離れても閉じない。 */
  let pinned = false;
  /** 押した・Escape で閉じた後は、ポインタが離れる（または改めてフォーカスする）まで出さない。 */
  let suppressed = false;
  let showTimer: ReturnType<typeof setTimeout> | undefined;
  let hideTimer: ReturnType<typeof setTimeout> | undefined;

  const clearShow = () => {
    clearTimeout(showTimer);
    showTimer = undefined;
  };
  const clearHide = () => {
    clearTimeout(hideTimer);
    hideTimer = undefined;
  };
  const setOpen = (next: boolean, nextReason: TooltipOpenReason | null) => {
    if (open === next && reason === nextReason) return;
    if (open && !next && reason === "hover") lastHoverCloseAt = Date.now();
    if (next && closeActiveTooltip !== close) {
      // 先に開いていた吹き出しを閉じてから、この吹き出しを開いているものとして覚える。
      closeActiveTooltip?.();
      closeActiveTooltip = close;
    } else if (!next && closeActiveTooltip === close) {
      closeActiveTooltip = null;
    }
    open = next;
    reason = next ? nextReason : null;
    onOpenChange(open, reason);
  };
  const close = () => {
    clearShow();
    clearHide();
    pinned = false;
    setOpen(false, null);
  };
  const closeIfIdle = () => {
    if (!open || pinned || hoveringTrigger || hoveringTooltip || focused) return;
    clearHide();
    hideTimer = setTimeout(() => {
      hideTimer = undefined;
      if (!pinned && !hoveringTrigger && !hoveringTooltip && !focused) setOpen(false, null);
    }, hideDelayMs);
  };

  return {
    pointerEnterTrigger(pointerType) {
      if (pointerType === "touch" || isCoarsePointer()) return;
      hoveringTrigger = true;
      clearHide();
      if (suppressed || open || showTimer) return;
      if (Date.now() - lastHoverCloseAt <= skipDelayMs) {
        setOpen(true, "hover");
        return;
      }
      showTimer = setTimeout(() => {
        showTimer = undefined;
        if (hoveringTrigger && !suppressed) setOpen(true, "hover");
      }, showDelayMs);
    },
    pointerLeaveTrigger() {
      hoveringTrigger = false;
      suppressed = false;
      clearShow();
      closeIfIdle();
    },
    pointerEnterTooltip() {
      if (!open) return;
      hoveringTooltip = true;
      clearHide();
    },
    pointerLeaveTooltip() {
      hoveringTooltip = false;
      closeIfIdle();
    },
    focus(focusVisible) {
      if (!focusVisible || isCoarsePointer()) return;
      focused = true;
      suppressed = false;
      clearShow();
      clearHide();
      if (pinned) return;
      setOpen(true, open && reason === "hover" ? "hover" : "focus");
    },
    blur() {
      focused = false;
      if (!open || hoveringTooltip) return;
      // 固定して開いたものも、フォーカスが外へ移ったら閉じる（吹き出しの中を押したときは残す）。
      if (pinned || !hoveringTrigger) close();
    },
    pointerDown() {
      suppressed = true;
      close();
    },
    escape() {
      if (!open) return false;
      suppressed = true;
      hoveringTooltip = false;
      close();
      return true;
    },
    press() {
      if (open && pinned) {
        suppressed = true;
        hoveringTooltip = false;
        close();
        return;
      }
      suppressed = false;
      clearShow();
      clearHide();
      pinned = true;
      setOpen(true, "press");
    },
    dismiss() {
      if (!open) return;
      hoveringTooltip = false;
      close();
    },
    isOpen: () => open,
    dispose() {
      clearShow();
      clearHide();
      if (closeActiveTooltip === close) closeActiveTooltip = null;
    },
  };
}

function subscribeNothing() {
  return () => {};
}

/** ブラウザで描いているか（サーバー描画と Node のテストでは Portal を作らない）。 */
export function useIsClient() {
  return useSyncExternalStore(
    subscribeNothing,
    () => typeof document !== "undefined",
    () => false
  );
}

export function isCoarsePointer() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(TOOLTIP_COARSE_POINTER_QUERY).matches
  );
}

export function isFocusVisible(element: Element) {
  try {
    return element.matches(":focus-visible");
  } catch {
    return true;
  }
}

export function assignRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") ref(value);
  else if (ref && typeof ref === "object") (ref as { current: T | null }).current = value;
}

type TriggerProps = {
  ref?: Ref<HTMLElement>;
  "aria-label"?: string;
  "aria-describedby"?: string;
  onPointerEnter?: (event: PointerEvent<HTMLElement>) => void;
  onPointerLeave?: (event: PointerEvent<HTMLElement>) => void;
  onPointerDown?: (event: PointerEvent<HTMLElement>) => void;
  onFocus?: (event: FocusEvent<HTMLElement>) => void;
  onBlur?: (event: FocusEvent<HTMLElement>) => void;
};

export interface TooltipProps {
  /** 吹き出しの文言（翻訳済みの短い文。操作できる要素や書式は入れない）。 */
  content: string;
  /**
   * トリガー（フォーカスできる要素 1 つ。`Button` など）。ref とイベントを受け取れること。
   * `disabled` のボタンはフォーカスを受けないので、キーボードでは出ない（使えない理由は画面の文言で示す）。
   */
  children: ReactElement<TriggerProps>;
  /** 空きがあれば出す側。既定は上。入らなければ反転する。 */
  placement?: TooltipPlacement;
  /** true で出さない（`aria-describedby` の結び付けも外す）。 */
  disabled?: boolean;
  /**
   * false で説明として結び付けない（吹き出しは `aria-hidden`）。読み上げ名に同じ内容が既に入っているとき
   * （切り詰めた対象名の全文を見せる `RowTitleButton` など）に使う。既定は読み上げ名と文言が違えば結び付ける。
   */
  describe?: boolean;
}

export interface TooltipState {
  open: boolean;
  reason: TooltipOpenReason | null;
}

/** 開閉の状態機械を React の状態につなぐ（`Tooltip` と `InfoTip` で共有）。 */
export function useTooltipController(options: Pick<TooltipControllerOptions, "showDelayMs"> = {}) {
  const [state, setState] = useState<TooltipState>({ open: false, reason: null });
  const controllerRef = useRef<TooltipController | null>(null);
  if (!controllerRef.current) {
    controllerRef.current = createTooltipController({
      onOpenChange: (open, reason) => setState({ open, reason }),
      isCoarsePointer,
      ...options,
    });
  }
  const controller = controllerRef.current;
  useEffect(() => () => controller.dispose(), [controller]);
  return { controller, state };
}

export interface TooltipBubbleProps {
  id: string;
  open: boolean;
  /** 閉じている間も `hidden` で置いておき、`aria-describedby` の先にする。 */
  describes: boolean;
  reason: TooltipOpenReason | null;
  placement: TooltipPlacement;
  controller: TooltipController;
  triggerRef: RefObject<HTMLElement | null>;
  bubbleRef?: RefObject<HTMLDivElement | null>;
  content: string;
  /** 見た目の差分（`InfoTip` の長めの説明の幅・行間など）。 */
  className?: string;
  "data-testid"?: string;
}

/**
 * 吹き出しの本体（`Tooltip` と `InfoTip` で共有）。描く先（モーダルの中ならモーダル、それ以外は body）への
 * Portal・位置（`computeTooltipLayout`）・重なり順・Escape（吹き出しだけを閉じ、囲むモーダルに伝えない）を持つ。
 */
export function TooltipBubble({
  id,
  open,
  describes,
  reason,
  placement,
  controller,
  triggerRef,
  bubbleRef,
  content,
  className,
  "data-testid": testId,
}: TooltipBubbleProps) {
  const isClient = useIsClient();
  const ownRef = useRef<HTMLDivElement | null>(null);
  const tooltipRef = bubbleRef ?? ownRef;
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const [layout, setLayout] = useState<(TooltipLayout & { zIndex?: number }) | undefined>();

  // 描く先（モーダルの中ならモーダル、それ以外は body）。説明として結び付けるときは閉じていても置いておく。
  useLayoutEffect(() => {
    if (!open && !describes) return;
    setContainer(selectPortalContainer(triggerRef.current));
  }, [describes, open, triggerRef]);

  const updateLayout = useCallback(() => {
    const trigger = triggerRef.current;
    const tooltip = tooltipRef.current;
    if (!trigger || !tooltip) return;
    const rect = tooltip.getBoundingClientRect();
    const next = computeTooltipLayout({
      triggerRect: trigger.getBoundingClientRect(),
      tooltipWidth: Math.ceil(rect.width),
      tooltipHeight: Math.ceil(rect.height),
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      placement,
    });
    const zIndex = resolveFloatingLayerZIndex(trigger, "--z-popover", DEFAULT_POPOVER_Z_INDEX);
    setLayout(zIndex === undefined ? next : { ...next, zIndex });
  }, [placement, tooltipRef, triggerRef]);

  useLayoutEffect(() => {
    if (!open || !container) {
      setLayout(undefined);
      return undefined;
    }
    updateLayout();
    window.addEventListener("resize", updateLayout);
    window.addEventListener("scroll", updateLayout, true);
    return () => {
      window.removeEventListener("resize", updateLayout);
      window.removeEventListener("scroll", updateLayout, true);
    };
  }, [container, open, updateLayout]);

  // Escape は吹き出しだけを閉じ、囲むモーダル・メニューには伝えない（1 回目で文脈ごと閉じない）。
  useEffect(() => {
    if (!open) return undefined;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (controller.escape()) {
        event.stopPropagation();
        event.preventDefault();
      }
    }
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [controller, open]);

  if (!isClient || !container || !(open || describes)) return null;
  const style: CSSProperties = layout
    ? { left: layout.left, top: layout.top, ...(layout.zIndex === undefined ? {} : { zIndex: layout.zIndex }) }
    : { left: -9999, top: -9999 };

  return createPortal(
    <div
      ref={tooltipRef}
      id={id}
      role="tooltip"
      // 読み上げ名と同じ文言は、説明として結び付けず読み上げからも外す（二重に読み上げない）。
      aria-hidden={describes ? undefined : true}
      hidden={!open}
      data-surface="inverted"
      data-tooltip-placement={layout?.placement}
      data-tooltip-reason={open ? reason ?? undefined : undefined}
      data-testid={testId}
      className={cn(
        "animate-overlay-in fixed z-[var(--z-popover)] w-max max-w-[16rem] rounded-md border border-border bg-surface-overlay px-2 py-1 text-xs font-medium text-fg shadow-[var(--shadow-popover)] forced-colors:border-[CanvasText]",
        // ホバー・押して開いたときだけ吹き出しへポインタを移せる（WCAG 1.4.13）。フォーカスで開いたときは下の要素を押せるようにする。
        reason === "hover" || reason === "press" ? "pointer-events-auto" : "pointer-events-none",
        !layout && "opacity-0",
        className
      )}
      style={style}
      onPointerEnter={() => controller.pointerEnterTooltip()}
      onPointerLeave={() => controller.pointerLeaveTooltip()}
    >
      {content}
    </div>,
    container
  );
}

/**
 * アイコンだけのボタンなどに、ホバーとキーボードのフォーカスで短い説明を出す。
 * アイコンだけの `Button`（`iconOnly`）は既定で `aria-label` と同じ文言を出すので、通常は直接使わない。
 * 操作や欄の補足の説明（文で読むもの）は `InfoTip` を使う（#901）。
 */
export function Tooltip({ content, children, placement = "top", disabled = false, describe = true }: TooltipProps) {
  const id = useId();
  const triggerRef = useRef<HTMLElement | null>(null);
  const { controller, state } = useTooltipController();
  const describes = !disabled && describe && tooltipDescribesTrigger(content, children.props["aria-label"]);
  const open = state.open && !disabled;

  useEffect(() => {
    if (disabled && controller.isOpen()) controller.escape();
  }, [controller, disabled]);

  const childProps = children.props;
  const trigger = cloneElement(children, {
    ref: (node: HTMLElement | null) => {
      triggerRef.current = node;
      assignRef(childProps.ref, node);
    },
    "aria-describedby": describes
      ? [childProps["aria-describedby"], id].filter(Boolean).join(" ")
      : childProps["aria-describedby"],
    onPointerEnter: (event: PointerEvent<HTMLElement>) => {
      childProps.onPointerEnter?.(event);
      if (!disabled) controller.pointerEnterTrigger(event.pointerType);
    },
    onPointerLeave: (event: PointerEvent<HTMLElement>) => {
      childProps.onPointerLeave?.(event);
      controller.pointerLeaveTrigger();
    },
    onPointerDown: (event: PointerEvent<HTMLElement>) => {
      childProps.onPointerDown?.(event);
      controller.pointerDown();
    },
    onFocus: (event: FocusEvent<HTMLElement>) => {
      childProps.onFocus?.(event);
      if (!disabled) controller.focus(isFocusVisible(event.currentTarget));
    },
    onBlur: (event: FocusEvent<HTMLElement>) => {
      childProps.onBlur?.(event);
      controller.blur();
    },
  });

  return (
    <>
      {trigger}
      <TooltipBubble
        id={id}
        open={open}
        describes={describes}
        reason={state.reason}
        placement={placement}
        controller={controller}
        triggerRef={triggerRef}
        content={content}
      />
    </>
  );
}
