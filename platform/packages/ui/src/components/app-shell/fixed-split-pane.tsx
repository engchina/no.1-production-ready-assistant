import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

import {
  FIXED_SPLIT_DIVIDER_SIZE_PX,
  nextFixedSplitWideEnough,
  FIXED_SPLIT_STORAGE_PREFIX,
  FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX,
  FIXED_SPLIT_KEYBOARD_FAST_STEP_PX,
  FIXED_SPLIT_KEYBOARD_STEP_PX,
  adjustFixedSplitFraction,
  clampFixedSplitFractionToPaneWidths,
  fixedSplitFractionBounds,
  fixedSplitGridTemplateColumns,
  fixedSplitStateForFraction,
  fixedSplitStateForRatio,
  fixedSplitStorageKey,
  fixedSplitValueText,
  nextFixedSplitStateFromFraction,
  parseFixedSplitStorageValue,
  serializeFixedSplitState,
  type FixedSplitPaneState,
  type FixedSplitRatio,
} from "../../lib/fixed-split-pane";
import { cn } from "../../lib/utils";

/** 分割ペインの文言。製品の i18n で上書きできる。 */
export interface FixedSplitPaneLabels {
  /** divider（role="separator"）の読み上げ名。 */
  separator: string;
  /** divider の操作の案内。 */
  hint: string;
  equal: string;
  leftWide: string;
  rightWide: string;
}

export const DEFAULT_FIXED_SPLIT_PANE_LABELS: FixedSplitPaneLabels = {
  separator: "左右ペインの表示比率",
  hint: "ドラッグで調整・ダブルクリックで表示比率を切替",
  equal: "左右 1:1",
  leftWide: "左を広く表示",
  rightWide: "右を広く表示",
};

export interface FixedSplitPaneProps {
  splitId: string;
  left: ReactNode;
  right: ReactNode;
  className?: string;
  leftClassName?: string;
  rightClassName?: string;
  minLeftPaneWidthPx?: number;
  minRightPaneWidthPx?: number;
  /** 比率を保存する localStorage key の前置き（既定 `production-ready.fixedSplitPane`）。 */
  storagePrefix?: string;
  labels?: Partial<FixedSplitPaneLabels>;
}

// 保存が無い・壊れているときは、すべての画面で左右 1:1（50%）から始める（#1572）。
// 利用者が分割線を動かした位置は localStorage に残り、次からはそれを優先する。
function readStoredState(splitId: string, storagePrefix: string): FixedSplitPaneState {
  const fallbackState = fixedSplitStateForRatio("equal");
  if (typeof window === "undefined") return fallbackState;
  try {
    const value = window.localStorage.getItem(fixedSplitStorageKey(splitId, storagePrefix));
    return parseFixedSplitStorageValue(value, fallbackState);
  } catch {
    return fallbackState;
  }
}

function valueText(ratio: FixedSplitRatio, labels: FixedSplitPaneLabels) {
  if (ratio === "leftWide") return labels.leftWide;
  if (ratio === "rightWide") return labels.rightWide;
  return labels.equal;
}

export function FixedSplitPane({
  splitId,
  left,
  right,
  className,
  leftClassName,
  rightClassName,
  minLeftPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX,
  minRightPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX,
  storagePrefix = FIXED_SPLIT_STORAGE_PREFIX,
  labels: labelOverrides,
}: FixedSplitPaneProps) {
  const labels = { ...DEFAULT_FIXED_SPLIT_PANE_LABELS, ...labelOverrides };
  const rootRef = useRef<HTMLDivElement | null>(null);
  const hintId = useId();
  const dragStartXRef = useRef(0);
  const dragStartFractionRef = useRef(0.5);
  const dragAvailableWidthRef = useRef(1);
  const cleanupDragRef = useRef<(() => void) | null>(null);
  const previousBodyCursorRef = useRef("");
  const previousBodyUserSelectRef = useRef("");
  const [splitState, setSplitState] = useState<FixedSplitPaneState>(() =>
    readStoredState(splitId, storagePrefix)
  );
  const [isDragging, setIsDragging] = useState(false);
  const [rootWidth, setRootWidth] = useState(0);
  // 横並びにできる幅か。境目の近くで幅が揺れても横並びと縦積みを往復しないよう、横並びから
  // 縦積みへ戻すのは境目より少し狭くなったときだけにする（nextFixedSplitWideEnough。#1222）。
  const [wideEnough, setWideEnough] = useState(false);
  const splitThreshold = minLeftPaneWidthPx + FIXED_SPLIT_DIVIDER_SIZE_PX + minRightPaneWidthPx;
  const [isDesktopViewport, setIsDesktopViewport] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia("(min-width: 1280px)").matches
  );

  useEffect(() => {
    setSplitState(readStoredState(splitId, storagePrefix));
  }, [splitId, storagePrefix]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(fixedSplitStorageKey(splitId, storagePrefix), serializeFixedSplitState(splitState));
    } catch {
      // Storage が無効な環境では現在セッション内の state だけで動かす。
    }
  }, [splitId, splitState, storagePrefix]);

  useEffect(() => {
    return () => {
      cleanupDragRef.current?.();
      cleanupDragRef.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const updateWidth = () => {
      const width = root.clientWidth;
      setRootWidth(width);
      setWideEnough((current) => nextFixedSplitWideEnough(current, width, splitThreshold));
    };
    updateWidth();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateWidth);
      return () => window.removeEventListener("resize", updateWidth);
    }

    const observer = new ResizeObserver(updateWidth);
    observer.observe(root);
    return () => observer.disconnect();
  }, [splitThreshold]);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(min-width: 1280px)");
    const updateViewport = () => setIsDesktopViewport(mediaQuery.matches);
    updateViewport();
    mediaQuery.addEventListener("change", updateViewport);
    return () => mediaQuery.removeEventListener("change", updateViewport);
  }, []);

  const availableWidth = useCallback(() => {
    const rootWidth = rootRef.current?.clientWidth ?? 0;
    return Math.max(rootWidth - FIXED_SPLIT_DIVIDER_SIZE_PX, 1);
  }, []);

  const measuredAvailableWidth = Math.max(rootWidth - FIXED_SPLIT_DIVIDER_SIZE_PX, 1);
  const splitLayout = isDesktopViewport && wideEnough;
  const constrainedLeftFraction =
    rootWidth > 0
      ? clampFixedSplitFractionToPaneWidths(
          splitState.leftFraction,
          measuredAvailableWidth,
          minLeftPaneWidthPx,
          minRightPaneWidthPx
        )
      : splitState.leftFraction;
  const fractionBounds = fixedSplitFractionBounds(
    measuredAvailableWidth,
    minLeftPaneWidthPx,
    minRightPaneWidthPx
  );

  const style = useMemo(
    () =>
      ({
        "--fixed-split-columns": fixedSplitGridTemplateColumns(constrainedLeftFraction),
      }) as CSSProperties,
    [constrainedLeftFraction]
  );

  const applyDragDocumentState = useCallback(() => {
    if (typeof document === "undefined") return;
    previousBodyCursorRef.current = document.body.style.cursor;
    previousBodyUserSelectRef.current = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const resetDragDocumentState = useCallback(() => {
    if (typeof document === "undefined") return;
    document.body.style.cursor = previousBodyCursorRef.current;
    document.body.style.userSelect = previousBodyUserSelectRef.current;
  }, []);

  const setRatio = useCallback((ratio: FixedSplitRatio) => {
    setSplitState(fixedSplitStateForRatio(ratio));
  }, []);

  const cycleRatio = useCallback(() => {
    setSplitState((current) => nextFixedSplitStateFromFraction(current.leftFraction));
  }, []);

  const adjustFraction = useCallback(
    (deltaPx: number) => {
      setSplitState((current) =>
        fixedSplitStateForFraction(
          adjustFixedSplitFraction(
            current.leftFraction,
            deltaPx,
            availableWidth(),
            minLeftPaneWidthPx,
            minRightPaneWidthPx
          )
        )
      );
    },
    [availableWidth, minLeftPaneWidthPx, minRightPaneWidthPx]
  );

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture 非対応の環境では window listener だけで継続する。
    }

    cleanupDragRef.current?.();
    dragStartXRef.current = event.clientX;
    dragStartFractionRef.current = constrainedLeftFraction;
    dragAvailableWidthRef.current = availableWidth();
    applyDragDocumentState();
    setIsDragging(true);

    const handlePointerMove = (moveEvent: globalThis.PointerEvent) => {
      const deltaPx = moveEvent.clientX - dragStartXRef.current;
      const nextFraction = adjustFixedSplitFraction(
        dragStartFractionRef.current,
        deltaPx,
        dragAvailableWidthRef.current,
        minLeftPaneWidthPx,
        minRightPaneWidthPx
      );
      setSplitState(fixedSplitStateForFraction(nextFraction));
    };

    const finishDrag = (shouldUpdateState = true) => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerEnd);
      window.removeEventListener("pointercancel", handlePointerEnd);
      resetDragDocumentState();
      if (shouldUpdateState) setIsDragging(false);
      cleanupDragRef.current = null;
    };

    const handlePointerEnd = () => {
      finishDrag();
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerEnd);
    window.addEventListener("pointercancel", handlePointerEnd);
    cleanupDragRef.current = () => finishDrag(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      cycleRatio();
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      adjustFraction(-(event.shiftKey ? FIXED_SPLIT_KEYBOARD_FAST_STEP_PX : FIXED_SPLIT_KEYBOARD_STEP_PX));
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      adjustFraction(event.shiftKey ? FIXED_SPLIT_KEYBOARD_FAST_STEP_PX : FIXED_SPLIT_KEYBOARD_STEP_PX);
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      setRatio("equal");
    }
  }

  return (
    <div
      className={cn("fixed-split-pane", className)}
      ref={rootRef}
      style={style}
      data-testid={`fixed-split-pane-${splitId}`}
      data-split-ratio={fixedSplitValueText(splitState.ratio)}
      data-split-left-fraction={constrainedLeftFraction.toFixed(4)}
      data-split-layout={splitLayout ? "split" : "stacked"}
    >
      <div
        className={cn("fixed-split-pane__panel fixed-split-pane__panel--left min-w-0", leftClassName)}
        data-split-pane-side="left"
        data-testid={`fixed-split-pane-${splitId}-left`}
      >
        {left}
      </div>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={labels.separator}
        aria-describedby={hintId}
        aria-valuemin={Math.ceil(fractionBounds.minFraction * 100)}
        aria-valuemax={Math.floor(fractionBounds.maxFraction * 100)}
        aria-valuenow={Math.round(constrainedLeftFraction * 100)}
        aria-valuetext={`${valueText(splitState.ratio, labels)} ${Math.round(constrainedLeftFraction * 100)}%`}
        tabIndex={0}
        className="fixed-split-pane__divider"
        data-dragging={isDragging ? "true" : "false"}
        data-testid={`fixed-split-pane-${splitId}-divider`}
        onDoubleClick={cycleRatio}
        onKeyDown={handleKeyDown}
        onPointerDown={handlePointerDown}
      >
        <span className="fixed-split-pane__line" aria-hidden="true" />
        <span className="fixed-split-pane__grip" aria-hidden="true">
          <span className="fixed-split-pane__dot" />
          <span className="fixed-split-pane__dot" />
          <span className="fixed-split-pane__dot" />
        </span>
        <span id={hintId} className="sr-only">
          {labels.hint}
        </span>
      </div>
      <div
        className={cn("fixed-split-pane__panel fixed-split-pane__panel--right min-w-0", rightClassName)}
        data-split-pane-side="right"
        data-testid={`fixed-split-pane-${splitId}-right`}
      >
        {right}
      </div>
    </div>
  );
}
