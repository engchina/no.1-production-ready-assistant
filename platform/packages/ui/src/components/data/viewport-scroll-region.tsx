import { useLayoutEffect, useRef, useState, type HTMLAttributes } from "react";

import { INFORMATION_LIST_SCROLL_CLASS, INFORMATION_LIST_VISIBLE_ROWS, INFORMATION_TABLE_FOCUS_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";

/** 一覧の基準の高さを保ちつつ、広い画面では下の余白まで表示領域を伸ばす。 */
export function ViewportScrollRegion({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  const ref = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      const node = ref.current;
      if (!node) return;
      const rem = Number.parseFloat(getComputedStyle(document.documentElement).fontSize);
      if (!window.matchMedia("(min-width: 48rem)").matches) { setHeight(null); return; }
      // スクロール位置を空き高さと誤認しない（DataTable の主一覧と同じ基準）。
      let scroll = window.scrollY;
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) scroll += parent.scrollTop;
      }
      const available = window.innerHeight - node.getBoundingClientRect().top - scroll - 6 * rem;
      setHeight(Math.max(INFORMATION_LIST_VISIBLE_ROWS.desktop * INFORMATION_LIST_VISIBLE_ROWS.rowHeightRem, available / rem));
    };
    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (ref.current?.parentElement) observer?.observe(ref.current.parentElement);
    return () => { window.removeEventListener("resize", measure); observer?.disconnect(); };
  }, []);
  return <div {...props} ref={ref} tabIndex={0} className={cn(INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_FOCUS_CLASS, className)} style={height == null ? undefined : { maxHeight: `${height}rem` }}>{children}</div>;
}
