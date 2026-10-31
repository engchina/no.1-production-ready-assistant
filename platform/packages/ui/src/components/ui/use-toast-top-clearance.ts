import { useLayoutEffect, type RefObject } from "react";

/** 上端の通知がバーからはみ出す分を確保し、戻る操作や本文に重ねない。 */
export const TOAST_TOP_CLEARANCE_VAR = "--toast-top-clearance";

export function useToastTopClearance(ref: RefObject<HTMLElement | null>, enabled: boolean) {
  useLayoutEffect(() => {
    const element = ref.current;
    if (!enabled || !element) return;
    const root = document.documentElement;
    const bar = document.querySelector<HTMLElement>("[data-nav-drawer-bar]");
    if (!bar) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      // 登場アニメーションの transform に影響されない外側の領域を測る。
      const overflow = Math.max(0, Math.ceil(element.getBoundingClientRect().bottom - bar.getBoundingClientRect().bottom));
      if (overflow > 0) root.style.setProperty(TOAST_TOP_CLEARANCE_VAR, `calc(${overflow}px + var(--space-2))`);
      else root.style.removeProperty(TOAST_TOP_CLEARANCE_VAR);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(element);
    observer?.observe(bar);
    window.addEventListener("resize", schedule);
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      root.style.removeProperty(TOAST_TOP_CLEARANCE_VAR);
    };
  }, [ref, enabled]);
}
