import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/utils";

/**
 * ToggleChip（セグメント化トグル / フィルタチップ）。
 * フィルタ・モード切替などの連動トグルに使う。状態は色だけでなく `aria-pressed` で伝える。
 * グループは呼び出し側で `role="group"` + `aria-label` を付け、`flex gap-1` 等で並べる。
 * タッチ端末（pointer: coarse）では、見た目の大きさを変えずに当たり判定だけを 44px 以上に広げる
 * （`pr-touch-target`。structure/touch-target.css、#364）。
 */
export function ToggleChip({
  selected,
  children,
  className,
  ...props
}: {
  selected: boolean;
  children: ReactNode;
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-pressed">) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className={cn(
        "pr-touch-target relative cursor-pointer rounded-full px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring forced-colors:border-[CanvasText]",
        selected
          ? "border border-transparent bg-accent-emphasis text-fg-on-accent forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none"
          : "border border-border-control bg-surface text-fg-muted hover:bg-surface-hover hover:text-fg",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
