import {
  Children,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import { Banner } from "../ui/banner";

export interface ActionResultRegionProps {
  loading: boolean;
  operationKey: string | number;
  loadingLabel?: string;
  errorMessage?: string;
  errorAction?: ReactNode;
  children?: ReactNode;
  testId?: string;
  preserveHeight?: boolean;
  scrollPolicy?: "nearest-on-complete" | "none";
}

interface ActiveOperation {
  sequence: number;
  userScrolled: boolean;
}

const SCROLL_EPSILON = 2;

export function ActionResultRegion({
  loading,
  operationKey,
  errorMessage = "",
  errorAction,
  children,
  testId,
  preserveHeight = true,
  scrollPolicy = "nearest-on-complete",
}: ActionResultRegionProps) {
  const regionRef = useRef<HTMLElement | null>(null);
  const previousLoadingRef = useRef(false);
  const sequenceRef = useRef(0);
  const activeOperationRef = useRef<ActiveOperation | null>(null);
  const completedSequenceRef = useRef<number | null>(null);
  const [reservedMinHeight, setReservedMinHeight] = useState(0);

  const hasChildren = Children.count(children) > 0;
  const hasError = Boolean(errorMessage);
  const shouldRender = hasError || hasChildren || (loading && reservedMinHeight > 0);
  const style: CSSProperties | undefined =
    preserveHeight && loading && reservedMinHeight > 0
      ? { minHeight: reservedMinHeight }
      : undefined;

  useLayoutEffect(() => {
    const element = regionRef.current;
    if (!element || loading || !shouldRender) return;
    const nextHeight = Math.ceil(element.getBoundingClientRect().height);
    if (nextHeight > 0) setReservedMinHeight(nextHeight);
  }, [children, errorMessage, loading, shouldRender]);

  useEffect(() => {
    if (!loading || previousLoadingRef.current) {
      previousLoadingRef.current = loading;
      return undefined;
    }

    previousLoadingRef.current = true;
    sequenceRef.current += 1;
    const operation: ActiveOperation = { sequence: sequenceRef.current, userScrolled: false };
    activeOperationRef.current = operation;
    completedSequenceRef.current = null;

    const element = regionRef.current;
    const scroller = element ? getScrollableAncestor(element) : window;
    const initialScrollTop = getScrollTop(scroller);
    const handleScroll = () => {
      if (Math.abs(getScrollTop(scroller) - initialScrollTop) > SCROLL_EPSILON) {
        operation.userScrolled = true;
      }
    };

    addScrollListener(scroller, handleScroll);
    return () => {
      removeScrollListener(scroller, handleScroll);
      previousLoadingRef.current = false;
    };
  }, [loading, operationKey]);

  useEffect(() => {
    if (loading || !shouldRender || scrollPolicy === "none") return undefined;
    const operation = activeOperationRef.current;
    if (!operation || completedSequenceRef.current === operation.sequence) return undefined;
    completedSequenceRef.current = operation.sequence;
    if (operation.userScrolled) return undefined;

    const animationFrame = window.requestAnimationFrame(() => {
      const element = regionRef.current;
      if (!element || isRegionStartVisible(element)) return;
      element.scrollIntoView({
        block: "nearest",
        inline: "nearest",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    });
    return () => window.cancelAnimationFrame(animationFrame);
  }, [errorMessage, loading, scrollPolicy, shouldRender]);

  if (!shouldRender) return null;

  return (
    <section
      ref={regionRef}
      className="grid min-w-0 gap-3 scroll-mt-24"
      aria-busy={loading ? "true" : undefined}
      data-action-result-region=""
      data-testid={testId ? `${testId}-region` : undefined}
      style={style}
    >
      {loading ? null : hasError ? (
        <div data-testid={testId ? `${testId}-error` : undefined}>
          <Banner severity="danger" action={errorAction}>
            {errorMessage}
          </Banner>
        </div>
      ) : (
        children
      )}
    </section>
  );
}

function getScrollableAncestor(element: HTMLElement): HTMLElement | Window {
  let current = element.parentElement;
  while (current) {
    const style = window.getComputedStyle(current);
    if (
      /(auto|scroll|overlay)/u.test(style.overflowY) &&
      current.scrollHeight > current.clientHeight + 1
    ) {
      return current;
    }
    current = current.parentElement;
  }
  return window;
}

function getScrollTop(scroller: HTMLElement | Window) {
  return isWindowScroller(scroller) ? window.scrollY : scroller.scrollTop;
}

function addScrollListener(scroller: HTMLElement | Window, listener: () => void) {
  scroller.addEventListener("scroll", listener, { passive: true });
}

function removeScrollListener(scroller: HTMLElement | Window, listener: () => void) {
  scroller.removeEventListener("scroll", listener);
}

function isRegionStartVisible(element: HTMLElement) {
  const boundary = visibleBoundary(element);
  const rect = element.getBoundingClientRect();
  return rect.top >= boundary.top && rect.top <= boundary.bottom;
}

function visibleBoundary(element: HTMLElement) {
  const scroller = getScrollableAncestor(element);
  if (isWindowScroller(scroller)) {
    return { top: 0, bottom: window.innerHeight };
  }
  const rect = scroller.getBoundingClientRect();
  return {
    top: Math.max(0, rect.top),
    bottom: Math.min(window.innerHeight, rect.bottom),
  };
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function isWindowScroller(scroller: HTMLElement | Window): scroller is Window {
  return scroller === window;
}
