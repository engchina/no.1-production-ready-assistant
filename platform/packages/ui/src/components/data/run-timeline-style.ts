/** 処理の経過と軌跡の調査で共有する時間条と文字の仕様。 */
export const RUN_TIME_TRACK = "relative h-2 min-w-0 rounded-full bg-surface-sunken";
export const RUN_TIME_FILL = "absolute inset-y-0 min-w-px rounded-full forced-colors:bg-[CanvasText]";
export const RUN_TIME_COLOR = {
  done: "bg-accent-emphasis", running: "bg-info-emphasis", waiting: "bg-warning-emphasis",
  failed: "bg-danger-emphasis", skipped: "bg-fg-subtle", pending: "bg-fg-subtle",
};
export const runTimelineNameClass = (size: "sm" | "md") => size === "sm" ? "text-xs" : "text-sm";
