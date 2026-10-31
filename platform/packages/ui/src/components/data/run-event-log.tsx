import { ArrowDownToLine, ArrowUpToLine, type LucideIcon } from "lucide-react";
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_FOCUS_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { EmptyState } from "../feedback/state-views";
import { Button } from "../ui/button";
import { DisclosureChevron } from "../ui/disclosure-chevron";
import { TEXT_ICON_SIZE } from "../ui/text-icon";
import { ListCount, type ListCountLabels } from "./list-count";
import { FilterChipGroup, FilterSelect } from "./list-filters";
import { formatRunDateTime, formatRunDuration } from "./run-history";
import { hasRunStepDetails, RunStepDetailsView, type RunStepDetails } from "./run-step-details";
import { StatusBadge, type StatusVariant } from "./status-badge";

/*
 * 実行のイベントの記録（#2386）。モデルが呼んだツール・承認の判断・成果物の保存などのイベントを、時間の順に
 * 1 イベント = 1 行で並べる（監査・イベントのタイムライン: GitHub Actions のログ、Datadog / Sentry のイベント、
 * Temporal の event history と同じ形）。
 *
 * - 行は「時刻・種類の印・要約・状態のバッジ・処理時間」だけ。詳細（イベントの種類のコード・ステップ ID・payload・
 *   警告）は行を開いて見る（`button[aria-expanded][aria-controls]` + `DisclosureChevron`。`RunTimeline` と同じ）。
 * - 一覧は表ではない行リストの高さ（`INFORMATION_LIST_SCROLL_CLASS`。md 未満 17.5rem・md 以上 28rem）で止め、
 *   内側だけを縦にスクロールする（件数でページを伸ばさない）。あふれたときだけ、スクロール領域を Tab で
 *   フォーカスでき（矢印キーで動かせる）、「最初へ」「最新へ」を出す。
 * - 最新の行を見ている（末尾にいる）間に増えたイベントは、末尾に付いて行く（ログの追尾）。途中を読んでいる間は動かさない。
 * - 種類（`category`）が 2 つ以上あれば、件数つきで絞り込める（4 つまでは `FilterChipGroup`、それ以上は `FilterSelect`）。
 * - 狭い置き場所（36rem 未満）では、時刻と処理時間を要約の下の 2 行目に置く（container query）。
 *
 * 製品は自分の記録（Agent の Run のイベント）をこの形に写すだけで、描き方は部品が持つ。3 製品の実行の詳細の
 * 「処理の詳細」の中に置く（UX 契約 run-history.md「詳細」）。
 */

export interface RunEventLogItem {
  id: string;
  /** 要約（翻訳済み。ツール名・イベントの文）。 */
  title: string;
  /** 補足（翻訳済み。要約の後ろに淡く出す）。 */
  summary?: string;
  /** 起きた時刻（ISO 8601）。 */
  at?: string | null;
  /** 種類（絞り込みのまとまり。例「ツール」「承認」）。 */
  category?: { id: string; label: string };
  /** 種類の印（lucide のアイコン）。 */
  icon?: LucideIcon;
  /** 状態のバッジ（翻訳済み。色だけにせず文言を出す）。 */
  status?: { variant: StatusVariant; label: string };
  /** 処理時間（ミリ秒）。 */
  durationMs?: number | null;
  /** 開いて見る詳細（見出しと値・行・コードの箱）。 */
  details?: RunStepDetails;
  /** 開いて見る自由な中身（警告など。`details` の下に出す）。 */
  children?: ReactNode;
  /** 最初から開く（失敗・警告のイベント）。 */
  defaultOpen?: boolean;
}

export interface RunEventLogLabels {
  /** 一覧の名前（スクロール領域・一覧の読み上げ名）。 */
  title: string;
  /** 種類の絞り込みの名前。 */
  filter: string;
  /** 絞り込まない選択肢。 */
  all: string;
  first: string;
  latest: string;
  empty: string;
  count?: Partial<ListCountLabels>;
  /** 詳細の長さを切ったときの印。 */
  detailsTruncated?: string;
}

export const DEFAULT_RUN_EVENT_LOG_LABELS: RunEventLogLabels = {
  title: "イベントの記録",
  filter: "種類",
  all: "すべて",
  first: "最初へ",
  latest: "最新へ",
  empty: "イベントはまだありません",
};

/** チップで出す種類の数の上限（「すべて」を含めて 5 つまで。ui.md「一覧のツールバーの絞り込み」）。 */
const MAX_CHIP_CATEGORIES = 4;

const ALL = "__all__";

/** 時刻の表記（「09:12:05」）。日付を含む全体は `title` と詳細に出す。読めない値はそのまま返す。 */
export function formatRunEventTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const full = formatRunDateTime(iso, { seconds: true });
  const space = full.indexOf(" ");
  return space > 0 ? full.slice(space + 1) : full;
}

/** 種類の選択肢（最初に出た順・件数つき）。 */
export function runEventCategories(items: readonly RunEventLogItem[]): { id: string; label: string; count: number }[] {
  const categories = new Map<string, { id: string; label: string; count: number }>();
  for (const item of items) {
    if (!item.category) continue;
    const current = categories.get(item.category.id);
    if (current) current.count += 1;
    else categories.set(item.category.id, { ...item.category, count: 1 });
  }
  return [...categories.values()];
}

export interface RunEventLogProps {
  items: readonly RunEventLogItem[];
  labels?: Partial<RunEventLogLabels>;
  /** 時刻の詳細の表記（既定「2026/10/15 09:12:05」）。 */
  formatDateTime?: (iso: string) => string;
  /**
   * 部品の `data-testid`。行は `<testId>-event-<id>`、開閉のボタンは `…-toggle`、開いた詳細は `…-details`、
   * スクロール領域は `<testId>-scroll`。
   */
  testId?: string;
  className?: string;
}

export function RunEventLog({ items, labels: labelsInput, formatDateTime, testId, className }: RunEventLogProps) {
  const labels = { ...DEFAULT_RUN_EVENT_LOG_LABELS, ...labelsInput };
  const idPrefix = useId();
  const categories = useMemo(() => runEventCategories(items), [items]);
  const [filter, setFilter] = useState<string>(ALL);
  // 選んでいた種類のイベントが無くなったら「すべて」に戻す。
  const activeFilter = categories.some((category) => category.id === filter) ? filter : ALL;
  const visible = useMemo(
    () => (activeFilter === ALL ? items : items.filter((item) => item.category?.id === activeFilter)),
    [items, activeFilter]
  );
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const scrollRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [edge, setEdge] = useState<{ top: boolean; bottom: boolean }>({ top: true, bottom: true });
  // 末尾を見ているか（増えたイベントに付いて行くか）。最初の描画では動かさない。
  const followRef = useRef(false);

  const empty = items.length === 0;

  const measure = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return;
    const max = node.scrollHeight - node.clientHeight;
    const nextOverflowing = max > 1;
    const top = node.scrollTop <= 1;
    const bottom = node.scrollTop >= max - 1;
    followRef.current = bottom;
    setOverflowing((current) => (current === nextOverflowing ? current : nextOverflowing));
    setEdge((current) => (current.top === top && current.bottom === bottom ? current : { top, bottom }));
  }, []);

  useLayoutEffect(() => {
    const node = scrollRef.current;
    const list = listRef.current;
    if (!node) return undefined;
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => measure());
    observer.observe(node);
    if (list) observer.observe(list);
    return () => observer.disconnect();
  }, [measure, empty]);

  // 末尾を見ている間に増えたイベントは末尾に付いて行く（ログの追尾）。
  const previousCount = useRef(visible.length);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node && visible.length > previousCount.current && followRef.current) {
      node.scrollTop = node.scrollHeight;
    }
    previousCount.current = visible.length;
    measure();
  }, [visible.length, measure]);

  const scrollToEdge = (where: "first" | "latest") => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = where === "first" ? 0 : node.scrollHeight;
    measure();
  };

  if (empty) {
    return (
      <div className={className} data-run-event-log="" data-testid={testId}>
        <EmptyState title={labels.empty} />
      </div>
    );
  }

  const showFilter = categories.length > 1;
  const filterOptions = [
    { value: ALL, label: labels.all, count: items.length },
    ...categories.map((category) => ({ value: category.id, label: category.label, count: category.count })),
  ];

  return (
    <div className={cn("@container grid min-w-0 gap-2", className)} data-run-event-log="" data-testid={testId}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        {showFilter ? (
          categories.length <= MAX_CHIP_CATEGORIES ? (
            <FilterChipGroup
              label={labels.filter}
              size="sm"
              value={activeFilter}
              options={filterOptions}
              onValueChange={setFilter}
              data-testid={testId ? `${testId}-filter` : undefined}
            />
          ) : (
            <FilterSelect
              id={`${idPrefix}-filter`}
              label={labels.filter}
              size="sm"
              value={activeFilter}
              options={filterOptions.map(({ value, label }) => ({ value, label }))}
              onValueChange={setFilter}
              data-testid={testId ? `${testId}-filter` : undefined}
            />
          )
        ) : (
          <span />
        )}
        <div className="flex min-w-0 flex-wrap items-center justify-end gap-x-2 gap-y-1">
          <ListCount
            total={items.length}
            matched={activeFilter === ALL ? undefined : visible.length}
            labels={labels.count}
            live={false}
            testId={testId ? `${testId}-count` : undefined}
          />
          {overflowing ? (
            <>
              <Button
                variant="ghost"
                size="sm"
                icon={ArrowUpToLine}
                disabled={edge.top}
                onClick={() => scrollToEdge("first")}
                data-testid={testId ? `${testId}-first` : undefined}
              >
                {labels.first}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                icon={ArrowDownToLine}
                disabled={edge.bottom}
                onClick={() => scrollToEdge("latest")}
                data-testid={testId ? `${testId}-latest` : undefined}
              >
                {labels.latest}
              </Button>
            </>
          ) : null}
        </div>
      </div>
      {/* 内側だけを縦にスクロールする領域。あふれたときだけ Tab で届き、矢印キーで動かせる（#2386）。 */}
      <div
        ref={scrollRef}
        role="region"
        aria-label={labels.title}
        tabIndex={overflowing ? 0 : undefined}
        onScroll={measure}
        className={cn(
          "relative min-w-0 rounded-md border border-border",
          INFORMATION_LIST_SCROLL_CLASS,
          INFORMATION_TABLE_FOCUS_CLASS
        )}
        data-run-event-log-scroll=""
        data-overflowing={overflowing ? "true" : "false"}
        data-testid={testId ? `${testId}-scroll` : undefined}
      >
        <ol ref={listRef} className="grid min-w-0 divide-y divide-border" aria-label={labels.title}>
          {visible.map((item, index) => (
            <RunEventRow
              key={item.id}
              item={item}
              regionId={`${idPrefix}-event-${index}`}
              open={expanded[item.id] ?? Boolean(item.defaultOpen)}
              onToggle={() =>
                setExpanded((current) => ({ ...current, [item.id]: !(current[item.id] ?? Boolean(item.defaultOpen)) }))
              }
              formatDateTime={formatDateTime}
              truncatedLabel={labels.detailsTruncated}
              testId={testId ? `${testId}-event-${item.id}` : undefined}
            />
          ))}
        </ol>
      </div>
    </div>
  );
}

/*
 * 行の格子。狭い置き場所は「印・要約・バッジ」/「・時刻・処理時間」の 2 行、36rem 以上は
 * 「印・時刻・要約・バッジ・処理時間」の 1 行（同じ DOM を grid-template-areas で並べ替える）。
 */
const ROW_GRID_CLASS = cn(
  "grid w-full min-w-0 items-start gap-x-2 gap-y-0.5 px-3 py-2 text-left",
  "grid-cols-[auto_minmax(0,1fr)_auto] [grid-template-areas:'mark_main_badge'_'._time_dur']",
  "@xl:grid-cols-[auto_4.5rem_minmax(0,1fr)_auto_4.5rem] @xl:items-center @xl:[grid-template-areas:'mark_time_main_badge_dur']"
);

function RunEventRow({
  item,
  regionId,
  open,
  onToggle,
  formatDateTime = (iso) => formatRunDateTime(iso, { seconds: true }),
  truncatedLabel,
  testId,
}: {
  item: RunEventLogItem;
  regionId: string;
  open: boolean;
  onToggle: () => void;
  formatDateTime?: (iso: string) => string;
  truncatedLabel?: string;
  testId?: string;
}) {
  const details = hasRunStepDetails(item.details) ? item.details : undefined;
  const hasDetails = details !== undefined || item.children != null;
  const Icon = item.icon;
  const time = formatRunEventTime(item.at);
  const cells = (
    <>
      <span className="flex items-center gap-1 leading-5 text-fg-muted [grid-area:mark]">
        {hasDetails ? (
          <span className="pr-icon-line">
            <DisclosureChevron expanded={open} size={TEXT_ICON_SIZE.sm} />
          </span>
        ) : (
          <span className="inline-block w-3.5" aria-hidden="true" />
        )}
        {Icon ? (
          <span className="pr-icon-line">
            <Icon size={TEXT_ICON_SIZE.sm} aria-hidden="true" />
          </span>
        ) : null}
      </span>
      <span className="whitespace-nowrap text-xs leading-5 text-fg-muted tnum [grid-area:time]">
        {item.at ? (
          <time dateTime={item.at} title={formatDateTime(item.at)}>
            {time}
          </time>
        ) : null}
      </span>
      {/* 要約。開ける行は狭い置き場所で 2 行・広い置き場所で 1 行に切り詰め、全文は開いた詳細で読む。 */}
      <span
        className={cn(
          "min-w-0 text-sm leading-5 [grid-area:main] [overflow-wrap:anywhere]",
          hasDetails ? "line-clamp-2 @xl:line-clamp-1" : "break-words"
        )}
      >
        <span className="font-medium text-fg">{item.title}</span>
        {item.summary ? <span className="text-fg-muted">{` — ${item.summary}`}</span> : null}
      </span>
      <span className="flex justify-end [grid-area:badge]">
        {item.status ? <StatusBadge variant={item.status.variant} label={item.status.label} /> : null}
      </span>
      <span className="whitespace-nowrap text-right text-xs leading-5 text-fg-muted tnum [grid-area:dur]">
        {item.durationMs != null ? formatRunDuration(item.durationMs) : null}
      </span>
    </>
  );
  return (
    <li className="min-w-0" data-run-event={item.id} data-testid={testId}>
      {hasDetails ? (
        <button
          type="button"
          className={cn(
            ROW_GRID_CLASS,
            "cursor-pointer hover:bg-surface-hover focus-visible:-outline-offset-2"
          )}
          data-surface-tint="neutral"
          aria-expanded={open}
          aria-controls={regionId}
          onClick={onToggle}
          data-testid={testId ? `${testId}-toggle` : undefined}
        >
          {cells}
        </button>
      ) : (
        <div className={ROW_GRID_CLASS}>{cells}</div>
      )}
      {hasDetails ? (
        <div
          id={regionId}
          hidden={!open}
          className="mx-3 mb-2 ml-9 grid min-w-0 gap-2 rounded-md border border-border bg-surface-sunken p-3 text-sm"
          data-testid={testId ? `${testId}-details` : undefined}
        >
          {open ? (
            <>
              {details ? (
                <RunStepDetailsView details={details} labels={truncatedLabel ? { truncated: truncatedLabel } : undefined} />
              ) : null}
              {item.children}
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
