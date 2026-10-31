import { ListFilter, X } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { FieldWidthScopeProvider } from "../ui/field-width-scope";
import { SideSheet } from "../ui/side-sheet";
import { ListCount, type ListCountProps } from "./list-count";
import { formatPaginationNumber } from "./pagination";

/**
 * `ListToolbar` の文言（#1933）。パッケージは i18n に依存しないため既定は日本語で、製品は i18n の文字列で上書きできる。
 */
export interface ListToolbarLabels {
  /** 検索語と絞り込みを既定に戻すボタン（例「条件をクリア」）。 */
  clearFilters: string;
  /** 狭い置き場所で絞り込みのシートを開くボタン（例「絞り込み」「絞り込み（2）」）。 */
  filtersButton: (activeCount: number) => string;
  /** 絞り込みのシートの見出し（例「絞り込み」）。 */
  filtersSheetTitle: string;
  /** 絞り込みのシートの閉じるボタンの名前（例「絞り込みを閉じる」）。 */
  filtersSheetClose: string;
  /** シートの末尾の、閉じて一覧を見るボタン（例「12 件を表示」。件数が無ければ「結果を表示」）。 */
  showResults: (matched: string | undefined) => string;
}

export const DEFAULT_LIST_TOOLBAR_LABELS: ListToolbarLabels = {
  clearFilters: "条件をクリア",
  filtersButton: (activeCount) => (activeCount > 0 ? `絞り込み（${activeCount}）` : "絞り込み"),
  filtersSheetTitle: "絞り込み",
  filtersSheetClose: "絞り込みを閉じる",
  showResults: (matched) => (matched === undefined ? "結果を表示" : `${matched} 件を表示`),
};

/** 置き場所がこの幅（rem）未満だと「狭い」（`@md` の container query と同じ 28rem）。 */
export const LIST_TOOLBAR_NARROW_REM = 28;

export interface ListToolbarProps {
  /**
   * 一覧の絞り込みの検索欄（`SearchField`）。ツールバーの**左端（先頭）**に置く。
   * 幅は部品が決める（`lg` 28rem で固定し、行を埋めない。置き場所が狭いと全幅）ので、SearchField に `width` を渡さない。
   */
  search?: ReactNode;
  /**
   * 検索欄のすぐ右に並べる絞り込み（#1933）。選択は `FilterSelect`（「種類: すべて」）、少数の排他の状態は
   * `FilterChipGroup`。欄の上にラベルを出す `SelectField` / `ToggleChip` は使わない（高さ・中心がずれる）。
   */
  filters?: ReactNode;
  /** 広い画面では二段目に置く絞り込み。狭い画面では filters と同じシートへまとめる。 */
  secondaryFilters?: ReactNode;
  /**
   * 既定と違う値の絞り込みの数（検索語は数えない。#1933）。狭い置き場所の「絞り込み（N）」に出し、
   * 1 以上（か `searchActive`）なら「条件をクリア」を出す。
   */
  activeFilterCount?: number;
  /** 検索語が入っている（「条件をクリア」を出す条件に入れる。#1933）。 */
  searchActive?: boolean;
  /**
   * 検索語とすべての絞り込みを既定に戻す（#1933）。渡すと、条件が付いているあいだ（`activeFilterCount` > 0 か
   * `searchActive`）だけ、絞り込みの後ろに「条件をクリア」を 1 か所出す。ページは 1 ページ目へ戻す。
   */
  onClearFilters?: () => void;
  /**
   * 狭い置き場所（28rem 未満。375px・横に並べたパネル）での絞り込みの出し方（#1933）。
   * - `wrap`（既定）: 検索欄の下に縦に積む。絞り込みが 2 つまでの一覧。
   * - `sheet`: 「絞り込み（N）」のボタンにまとめ、押すと右からのシートに絞り込みを出す。絞り込みが 3 つ以上の一覧。
   * 広い置き場所ではどちらも検索欄の右に 1 行で並べる。
   */
  filtersLayout?: "wrap" | "sheet";
  /**
   * 一覧の件数（`ListCount` の props）。右側の一覧への操作より前（左）に「全 N 件」「N 件（全 M 件）」を出す（#1891）。
   * 件数はここ 1 か所に出し、カードの見出しのバッジと二重にしない。
   */
  count?: ListCountProps;
  /** 件数のほかに右側に出す状態の文言（例:「選択中 3 件」）。件数には `count` を使う。 */
  summary?: ReactNode;
  /** 右端に置く一覧全体への操作（追加・一括操作など）。主操作は最後（右端）に置く。 */
  actions?: ReactNode;
  labels?: Partial<ListToolbarLabels>;
  className?: string;
  testId?: string;
}

/** 要素の幅が `thresholdRem`（ルートの文字の大きさ基準）未満か。`enabled` が false の間は測らない。 */
function useNarrowContainer(enabled: boolean, thresholdRem: number) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!enabled || !element || typeof window === "undefined") {
      setNarrow(false);
      return;
    }
    const measure = () => {
      const rootFont = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 14;
      const width = element.getBoundingClientRect().width;
      // 幅 0（非表示のタブの中など）は狭いとみなさない。
      setNarrow(width > 0 && width < thresholdRem * rootFont);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled, thresholdRem]);
  return { ref, narrow };
}

/**
 * 一覧のツールバー（#600 / #1891 / #1933）。3 製品の一覧の上の「検索・絞り込み・件数・操作」の並びを 1 つにする。
 *
 * ```text
 * [🔍 検索欄（lg 28rem）] [種類: すべて ▾] [すべて|完了 12|失敗 3] [× 条件をクリア]  ……  [12 件（全 123 件）] [操作]
 * ```
 *
 * - **左**: 検索欄（先頭・`lg` 28rem）→ 絞り込み（`FilterSelect` / `FilterChipGroup`）→「条件をクリア」（条件があるときだけ）。
 * - **右**: 件数（`count` = `ListCount`）→ 一覧全体への操作。右端に寄せ、主操作は右端。
 * - **1 行の揃え（#1933）**: 部品はすべて同じ高さ（md 36px・タッチ端末 44px）で、縦の中心をそろえる。右の群れは 1 行目にそろえる。
 *   移行前の上にラベルを出す欄（`data-field-label="top"`）が入っているあいだだけ、下端でそろえる（段 2〜4 で無くす）。
 * - 幅はツールバー自身の幅（container query）で決める。42rem 以上は 1 行に並べ、入らない絞り込みは次の行へ折り返す。
 *   28rem 未満（375px・横に並べたパネル）は縦に積み、検索欄を先頭に全幅で置く。`filtersLayout="sheet"` なら、
 *   絞り込みを「絞り込み（N）」のボタンからシートで開く。
 *
 * 規則と根拠は UX 契約 page-archetypes.md「一覧のツールバー」。
 */
export function ListToolbar({
  search,
  filters,
  secondaryFilters,
  activeFilterCount = 0,
  searchActive = false,
  onClearFilters,
  filtersLayout = "wrap",
  count,
  summary,
  actions,
  labels: labelOverrides,
  className,
  testId,
}: ListToolbarProps) {
  const labels = { ...DEFAULT_LIST_TOOLBAR_LABELS, ...labelOverrides };
  const hasFilters = Boolean(filters) || Boolean(secondaryFilters);
  const { ref, narrow } = useNarrowContainer(filtersLayout === "sheet" && hasFilters, LIST_TOOLBAR_NARROW_REM);
  const [sheetOpen, setSheetOpen] = useState(false);
  const sheetId = useId();
  const sheetMode = filtersLayout === "sheet" && narrow && hasFilters;

  // 広くなったら（回転・パネルの幅の変更）シートを閉じる。絞り込みはツールバーの中に戻る。
  useEffect(() => {
    if (!sheetMode) setSheetOpen(false);
  }, [sheetMode]);

  const filtersActive = activeFilterCount > 0 || searchActive;
  const clearButton =
    onClearFilters && filtersActive ? (
      <Button
        type="button"
        variant="ghost"
        size="md"
        icon={X}
        onClick={onClearFilters}
        // 縦に積む狭い置き場所でも行いっぱいに伸ばさない（文字の幅のボタン）。
        className="self-start @md:self-auto"
        data-list-toolbar-clear=""
      >
        {labels.clearFilters}
      </Button>
    ) : null;

  const hasStart = Boolean(search) || hasFilters || Boolean(clearButton);
  const hasEnd = Boolean(count) || Boolean(summary) || Boolean(actions);
  if (!hasStart && !hasEnd) return null;

  const matchedText = count ? formatPaginationNumber(Math.max(0, count.matched ?? count.total)) : undefined;

  return (
    <div
      ref={ref}
      className={cn("@container min-w-0", className)}
      data-list-toolbar=""
      data-list-toolbar-filters-layout={sheetMode ? "sheet" : "inline"}
      data-testid={testId}
    >
      <div
        className={cn(
          "flex min-w-0 flex-col gap-2 @2xl:flex-row @2xl:flex-wrap @2xl:gap-x-6",
          // 右の群れ（件数・操作）は 1 行目（検索欄の行）にそろえる。上のラベルの欄があるあいだは下端（移行前の形）。
          "@2xl:items-start @2xl:has-[[data-field-label=top]]:items-end"
        )}
      >
        {hasStart ? (
          <div
            className={cn(
              "flex min-w-0 flex-col gap-2 @md:flex-row @md:flex-wrap @md:gap-x-3",
              // 同じ高さの部品の縦の中心をそろえる（#1933）。上のラベルの欄があるあいだは入力欄の下端でそろえる。
              "@md:items-center @md:has-[[data-field-label=top]]:items-end"
            )}
            data-list-toolbar-start=""
          >
            {search ? (
              <div
                className="min-w-0 @md:w-[var(--field-width-lg)] @md:max-w-full @md:shrink-0"
                data-list-toolbar-search=""
              >
                {search}
              </div>
            ) : null}
            {sheetMode ? (
              <div className="flex flex-wrap items-center gap-2" data-list-toolbar-filters-trigger="">
                <Button
                  type="button"
                  variant="secondary"
                  size="md"
                  icon={ListFilter}
                  aria-haspopup="dialog"
                  aria-expanded={sheetOpen}
                  aria-controls={sheetId}
                  onClick={() => setSheetOpen(true)}
                >
                  {labels.filtersButton(activeFilterCount)}
                </Button>
                {clearButton}
              </div>
            ) : (
              // 絞り込みの欄の幅の段は、画面ではなくツールバーの幅（container。28rem）で切り替える（#1933）。
              // 狭い分割のペインでも、検索欄（全幅）と絞り込みの欄の幅がそろう。
              <FieldWidthScopeProvider scope="container">
                {filters}
                {secondaryFilters ? (
                  <div className="flex min-w-0 flex-wrap items-center gap-3 basis-full" data-list-toolbar-secondary-filters="">
                    {secondaryFilters}
                    {clearButton}
                  </div>
                ) : clearButton}
              </FieldWidthScopeProvider>
            )}
          </div>
        ) : null}
        {hasEnd ? (
          <div
            className="flex min-w-0 flex-wrap items-center gap-2 @2xl:ml-auto @2xl:min-h-[var(--control-height-md)] @2xl:justify-end"
            data-list-toolbar-end=""
          >
            {count ? <ListCount {...count} /> : null}
            {summary ? <div className="min-w-0 text-xs text-fg-muted tnum">{summary}</div> : null}
            {actions}
          </div>
        ) : null}
      </div>
      {sheetMode ? (
        <SideSheet
          open={sheetOpen}
          onClose={() => setSheetOpen(false)}
          title={labels.filtersSheetTitle}
          closeLabel={labels.filtersSheetClose}
          side="right"
          id={sheetId}
          data-testid={testId ? `${testId}-filters-sheet` : undefined}
        >
          <div className="flex flex-col gap-3 pb-4" data-list-toolbar-sheet-filters="">
            {/* シートの中は @container の外なので、container の基準の欄は全幅になる。 */}
            <FieldWidthScopeProvider scope="container">{filters}{secondaryFilters}</FieldWidthScopeProvider>
          </div>
          <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button type="button" variant="primary" size="md" onClick={() => setSheetOpen(false)}>
              {labels.showResults(matchedText)}
            </Button>
            {clearButton}
          </div>
        </SideSheet>
      ) : null}
    </div>
  );
}
