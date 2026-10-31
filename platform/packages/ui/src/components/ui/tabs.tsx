import { CircleAlert, type LucideIcon } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { cn } from "../../lib/utils";
import { nearestScrollTop } from "./select-field";

export interface TabItem {
  id: string;
  label: string;
  icon?: LucideIcon;
  /** 件数（等幅数字）。 */
  count?: number;
  /** 件数以外の短いバッジ（版数・状態など）。count と同じ見た目で出す。 */
  badge?: string;
  badgeTestId?: string;
  /** 表示ラベルより詳しい読み上げ名が必要な場合だけ指定する。 */
  ariaLabel?: string;
  disabled?: boolean;
  /**
   * 無効のときの理由（翻訳済み）。無効のタブにだけ HTML の `title` として付ける（#396）。
   * 無効のタブはフォーカスを受けず Tooltip を出せないため、ホバーと読み上げ（説明）で理由を伝える。
   * 画面の別の場所に同じ理由を書いているときは渡さない（二重に伝えない）。
   */
  disabledReason?: string;
  /**
   * タブの中に入力のエラーがある（#542）。ラベルの後ろに danger 色の `CircleAlert` を出し、
   * 読み上げでは `invalidLabel` を説明として伝える（色だけに頼らない）。フォームを複数のタブに分けたとき、
   * 選んでいないタブのエラーを見落とさないために使う。
   */
  invalid?: boolean;
  /** `invalid` のときの読み上げの説明（翻訳済み）。既定 `DEFAULT_TAB_INVALID_LABEL`。 */
  invalidLabel?: string;
}

/** エラーがあるタブの既定の説明。3 製品で同じ語にそろえるため `packages/ui` が持つ（#542）。 */
export const DEFAULT_TAB_INVALID_LABEL = "入力にエラーがあります";

export interface TabsProps {
  items: TabItem[];
  value: string;
  onChange?: (id: string) => void;
  /** tablist の aria-label（翻訳済み）。 */
  ariaLabel?: string;
  /** 同じ画面に Tabs を複数置くときだけ、TabPanel と揃えて指定する。 */
  idPrefix?: string;
  className?: string;
}

/**
 * ← → / Home / End に応じた移動先の tab id（WAI-ARIA Tabs パターン、無効タブは飛ばし端で循環）。
 * 対象外のキーなら null。
 */
export function nextTabId(items: TabItem[], value: string, key: string): string | null {
  const enabled = items.filter((item) => !item.disabled);
  if (enabled.length === 0) return null;
  const current = enabled.findIndex((item) => item.id === value);
  switch (key) {
    case "ArrowRight":
      return enabled[(current + 1) % enabled.length].id;
    case "ArrowLeft":
      return enabled[(current - 1 + enabled.length) % enabled.length].id;
    case "Home":
      return enabled[0].id;
    case "End":
      return enabled[enabled.length - 1].id;
    default:
      return null;
  }
}

/** スクロールの位置の丸めの誤差（小数の px）で端のフェードがちらつかないようにする余裕。 */
const SCROLL_EDGE_EPSILON = 1;

/**
 * 横にスクロールするタブの列で、どちらの方向に続きがあるか（その側の端をフェードする）。
 * 入りきるときはどちらも false（フェードを出さない）。
 */
export function tabsScrollEdges({
  scrollLeft,
  clientWidth,
  scrollWidth,
}: {
  scrollLeft: number;
  clientWidth: number;
  scrollWidth: number;
}): { start: boolean; end: boolean } {
  if (scrollWidth - clientWidth <= SCROLL_EDGE_EPSILON) return { start: false, end: false };
  return {
    start: scrollLeft > SCROLL_EDGE_EPSILON,
    end: scrollLeft + clientWidth < scrollWidth - SCROLL_EDGE_EPSILON,
  };
}

/**
 * タブを見せるための scrollLeft。すでにフェードの外で見えていれば動かさない。
 * padding はフェードの幅（scroll-padding-inline）。端のタブは 0 / 末尾まで寄せる（ブラウザが範囲に丸める）。
 */
export function revealTabScrollLeft({
  scrollLeft,
  clientWidth,
  tabLeft,
  tabWidth,
  padding = 0,
}: {
  scrollLeft: number;
  clientWidth: number;
  tabLeft: number;
  tabWidth: number;
  padding?: number;
}) {
  // SelectField の一覧の縦のスクロール（nearestScrollTop）と同じ計算を横に使う。
  return nearestScrollTop({ scrollTop: scrollLeft, viewportHeight: clientWidth, itemTop: tabLeft, itemHeight: tabWidth, padding });
}

/**
 * ビュー切替。**同じ対象の別の見方**に切り替えるときだけ使う。
 * データを絞り込むだけなら ToggleChip、別の画面に移るなら Sidebar。
 * 下線スタイル固定。PageHeader の `tabs` に渡すとヘッダー下端に吸い付く。
 * ペイン・カードの中の見方の切り替え（原本の処理前 / 処理後、表示する形式など）も Tabs + TabPanel で作る。
 * 枠の中にボタンを並べたセグメントを手書きしない（#396）。
 *
 * 入りきらないときは横にスクロールし、スクロールできる方向の端だけをフェードする（`data-scroll-start` /
 * `data-scroll-end` と structure/tabs.css、#364）。選択中のタブ（キーボード・クリック・呼び出し側の変更）は、
 * フェードに隠れない位置までタブの列の中だけでスクロールして見せる（ページ全体はスクロールしない）。
 */
export function Tabs({ items, value, onChange, ariaLabel, idPrefix = "pr", className }: TabsProps) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const listRef = useRef<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ start: false, end: false });

  const updateEdges = useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    const next = tabsScrollEdges(list);
    setEdges((current) => (current.start === next.start && current.end === next.end ? current : next));
  }, []);

  const reveal = useCallback((id: string) => {
    const list = listRef.current;
    const tab = refs.current[id];
    if (!list || !tab) return;
    const scrollLeft = revealTabScrollLeft({
      scrollLeft: list.scrollLeft,
      clientWidth: list.clientWidth,
      // タブの列は relative なので offsetLeft はスクロールの内容の左端からの距離になる。
      tabLeft: tab.offsetLeft,
      tabWidth: tab.offsetWidth,
      padding: Number.parseFloat(window.getComputedStyle(list).scrollPaddingInlineStart) || 0,
    });
    if (scrollLeft !== list.scrollLeft) list.scrollLeft = scrollLeft;
  }, []);

  // 選択中のタブを見せる（初回の表示と、value が変わったとき）。scroll イベントでフェードも更新される。
  useLayoutEffect(() => {
    reveal(value);
    updateEdges();
  }, [value, reveal, updateEdges]);

  // タブの列の幅（画面の resize・サイドバーの開閉）と、タブの中身（件数・ラベル）が変わったときにフェードを出し分ける。
  useEffect(() => {
    updateEdges();
    const list = listRef.current;
    if (!list || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateEdges);
    observer.observe(list);
    for (const tab of Array.from(list.children)) observer.observe(tab);
    return () => observer.disconnect();
  }, [items, updateEdges]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const next = nextTabId(items, value, event.key);
    if (next === null) return;
    event.preventDefault();
    onChange?.(next);
    // ブラウザの既定のスクロール（タブがフェードの下に残ることがある・ページまで動く）ではなく、
    // タブの列の中だけでフェードの外まで見せる。
    refs.current[next]?.focus({ preventScroll: true });
    reveal(next);
  }

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={handleKeyDown}
      onScroll={updateEdges}
      data-scroll-start={edges.start || undefined}
      data-scroll-end={edges.end || undefined}
      className={cn(
        // PageHeader 内ではヘッダーの full-bleed な罫線が境界になるため自前の線を出さない。
        // スクロールバーを隠す指定・フェード・scroll-padding は pr-tabs-scroll（structure/tabs.css）が持つ。
        "pr-tabs-scroll relative flex items-stretch gap-5 overflow-x-auto shadow-[inset_0_-1px_0_var(--color-border)] [header_&]:shadow-none",
        className
      )}
    >
      {items.map((item) => {
        const selected = item.id === value;
        const Icon = item.icon;
        const badge = item.count ?? item.badge;
        const badgeId = `${idPrefix}-tab-${item.id}-badge`;
        const invalidId = `${idPrefix}-tab-${item.id}-invalid`;
        const describedBy = [badge == null ? "" : badgeId, item.invalid ? invalidId : ""].filter(Boolean).join(" ");
        return (
          <button
            key={item.id}
            ref={(node) => {
              refs.current[item.id] = node;
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${item.id}`}
            aria-selected={selected}
            aria-label={item.ariaLabel}
            aria-describedby={describedBy || undefined}
            data-invalid={item.invalid || undefined}
            aria-controls={`${idPrefix}-panel-${item.id}`}
            tabIndex={selected ? 0 : -1}
            disabled={item.disabled}
            title={item.disabled ? item.disabledReason : undefined}
            onClick={() => onChange?.(item.id)}
            className={cn(
              "group inline-flex h-[var(--tab-height)] shrink-0 cursor-pointer items-center gap-2 whitespace-nowrap border-b-2 border-transparent px-1 text-sm font-medium text-fg-muted transition-colors",
              "hover:enabled:border-border-strong hover:enabled:text-fg",
              "aria-selected:border-accent-emphasis aria-selected:font-semibold aria-selected:text-accent-fg-strong aria-selected:hover:border-accent-emphasis aria-selected:hover:text-accent-fg-strong",
              "disabled:cursor-not-allowed disabled:text-fg-disabled",
              "focus-visible:rounded-sm focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring",
              // 強制カラーモードでは透明の枠線も system color（CanvasText）に塗られ、選ばれていないタブにも下線が出る（#374）。
              // 選ばれていないタブは背景と同じ Canvas にして消し、選んだタブだけを Highlight の下線で示す（文字の太さと aria-selected も残る）。
              "forced-colors:border-b-[Canvas] forced-colors:aria-selected:border-b-[Highlight]"
            )}
          >
            {Icon ? <Icon size={16} className="shrink-0" aria-hidden /> : null}
            <span>{item.label}</span>
            {badge == null ? null : (
              <span
                id={badgeId}
                data-testid={item.badgeTestId}
                // 選んだタブの淡青の地（accent-muted）と、選んでいない灰色の地（surface-hover。fg-muted は 4.39:1）の
                // どちらでも件数を 4.5:1 にする（tokens/colors.css。#1671）。
                data-surface-tint="accent"
                className="tnum inline-flex min-w-6 items-center justify-center rounded-full bg-surface-hover px-1.5 text-xs font-normal text-fg-muted group-aria-selected:bg-accent-muted group-aria-selected:text-accent-fg-strong"
              >
                {badge}
              </span>
            )}
            {item.invalid ? (
              <>
                <CircleAlert size={14} className="shrink-0 text-danger-fg" aria-hidden />
                <span id={invalidId} className="sr-only">
                  {item.invalidLabel ?? DEFAULT_TAB_INVALID_LABEL}
                </span>
              </>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

/** Tabs の中身。`id` は Tabs の item.id と一致させる。選択中のときだけ描画する。 */
export function TabPanel({
  id,
  value,
  idPrefix = "pr",
  className,
  children,
}: {
  id: string;
  value: string;
  idPrefix?: string;
  className?: string;
  children?: ReactNode;
}) {
  if (id !== value) return null;
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={`${idPrefix}-tab-${id}`}
      tabIndex={0}
      className={cn("min-w-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring", className)}
    >
      {children}
    </div>
  );
}
