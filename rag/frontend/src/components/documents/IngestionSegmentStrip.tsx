import { useRef, useState, type KeyboardEvent } from "react";
import { Circle, CircleCheck, CircleDot, CircleMinus, CircleX, type LucideIcon } from "lucide-react";
import { Tooltip } from "@production-ready/ui";

import type { IngestionParseSegmentStatus } from "@/lib/api";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import {
  segmentStatusLabelKey,
  type SegmentStripView,
} from "./DocumentWorkspace.logic";

/** 区画にアイコンを入れる段の数の上限。これより多いと区画が細くなるため、凡例と読み上げで示す。 */
const STRIP_ICON_MAX_SEGMENTS = 24;
/** 区画の間を細くする段の数。 */
const STRIP_DENSE_SEGMENTS = 60;

const STATUS_ICON: Record<IngestionParseSegmentStatus, LucideIcon> = {
  SUCCEEDED: CircleCheck,
  RUNNING: CircleDot,
  QUEUED: Circle,
  FAILED: CircleX,
  CANCELLED: CircleMinus,
};

const STATUS_CLASS: Record<IngestionParseSegmentStatus, string> = {
  SUCCEEDED: "border-success-border bg-success-subtle text-success-fg",
  RUNNING: "border-info-border bg-info-subtle text-info-fg",
  QUEUED: "border-border bg-surface text-fg-muted",
  FAILED: "border-danger-border bg-danger-subtle text-danger-fg",
  CANCELLED: "border-border bg-surface-sunken text-fg-muted",
};

const STATUS_ICON_CLASS: Record<IngestionParseSegmentStatus, string> = {
  SUCCEEDED: "text-success-fg",
  RUNNING: "text-info-fg",
  QUEUED: "text-fg-muted",
  FAILED: "text-danger-fg",
  CANCELLED: "text-fg-muted",
};

/**
 * 段の帯（#2001）。PDF の段（ページの範囲）ごとの小さな区画を並べ、完了・処理中・待機中・失敗・取消を
 * 色・アイコン・読み上げで区別する。区画は 1 つの Tab の止まり先にまとめ（段が多い文書で Tab が
 * 増えない）、← → / Home / End で区画を移る。hover / focus でページの範囲と状態を吹き出しに出す。
 * 凡例は状態ごとの段の数で、色だけに頼らない。
 */
export function IngestionSegmentStrip({ view }: { view: SegmentStripView }) {
  const [activeIndex, setActiveIndex] = useState(() => firstActiveIndex(view));
  const itemRefs = useRef<(HTMLLIElement | null)[]>([]);
  const index = Math.min(activeIndex, view.items.length - 1);
  const showIcons = view.items.length <= STRIP_ICON_MAX_SEGMENTS;

  function move(next: number) {
    const bounded = Math.max(0, Math.min(view.items.length - 1, next));
    setActiveIndex(bounded);
    itemRefs.current[bounded]?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLOListElement>) {
    const keys: Record<string, number> = {
      ArrowRight: index + 1,
      ArrowDown: index + 1,
      ArrowLeft: index - 1,
      ArrowUp: index - 1,
      Home: 0,
      End: view.items.length - 1,
    };
    if (!(event.key in keys)) return;
    event.preventDefault();
    move(keys[event.key]);
  }

  return (
    <div className="space-y-1.5" data-testid="ingestion-segment-strip">
      <ol
        aria-label={view.summary}
        onKeyDown={onKeyDown}
        className={cn(
          "flex w-full min-w-0",
          view.items.length > STRIP_DENSE_SEGMENTS ? "gap-px" : "gap-0.5"
        )}
      >
        {view.items.map((item, itemIndex) => {
          const Icon = STATUS_ICON[item.status];
          return (
            <Tooltip key={item.segmentId} content={item.label} describe={false}>
              <li
                ref={(element) => {
                  itemRefs.current[itemIndex] = element;
                }}
                tabIndex={itemIndex === index ? 0 : -1}
                aria-label={item.label}
                data-status={item.status}
                data-testid="ingestion-segment-strip-item"
                onFocus={() => setActiveIndex(itemIndex)}
                className={cn(
                  "flex h-5 min-w-0 flex-1 items-center justify-center overflow-hidden rounded-sm border",
                  STATUS_CLASS[item.status]
                )}
              >
                {showIcons ? <Icon size={14} aria-hidden className="shrink-0" /> : null}
              </li>
            </Tooltip>
          );
        })}
      </ol>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted" aria-hidden>
        {view.counts.map(({ status, count }) => {
          const Icon = STATUS_ICON[status];
          return (
            <li key={status} className="tnum inline-flex items-center gap-1">
              <Icon size={14} aria-hidden className={STATUS_ICON_CLASS[status]} />
              {t("flow.segments.strip.count", { status: t(segmentStatusLabelKey(status)), count })}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** 最初に Tab で止まる区画（処理中 → 失敗 → 待機中 → 先頭の順）。 */
function firstActiveIndex(view: SegmentStripView): number {
  for (const status of ["RUNNING", "FAILED", "QUEUED"] as const) {
    const found = view.items.findIndex((item) => item.status === status);
    if (found >= 0) return found;
  }
  return 0;
}
