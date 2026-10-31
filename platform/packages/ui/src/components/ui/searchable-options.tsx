import { Check } from "lucide-react";
import type { MouseEvent } from "react";

import { cn } from "../../lib/utils";

/**
 * 検索できる選択部品（SearchableSelectField / SearchableMultiSelect、#578）が共有する型と部品。
 * 選択肢が数十〜数百件になる選択（RAG のナレッジベースなど）に使う。十数件までの固定の選択肢は SelectField。
 */

/** 選択肢。文言はすべて翻訳済みで渡す。 */
export interface SearchableSelectOption {
  value: string;
  /** 名前。切らずに折り返して全体を出す。 */
  label: string;
  /** 名前の下の補足（任意。1 行）。 */
  description?: string;
  /** 右端の補助情報（任意。例:「12 文書」）。数字の桁をそろえる。 */
  meta?: string;
  /** 名前の後ろの小さな目印（任意。例:「最多」「アーカイブ済み」）。色だけに頼らず文字で伝える。 */
  badge?: string;
  /** 画面側で絞り込むときに照合する文字（既定は label と description）。 */
  searchText?: string;
  /**
   * 既定で隠せる候補（例: 文書のない KB）。複数選択で `labels.hideHideable` を渡したときだけ、
   * 一覧の上の「〜を隠す」で隠す（選択済みは隠さない）。全件を手元に持つとき（remote なし）だけ働く。
   */
  hideable?: boolean;
}

/**
 * 候補をサーバー側で検索するとき（件数が多く全件を読まないとき）の状態。渡すと、部品は `options` を
 * 画面側で絞り込まず、返った順のまま出す。検索語は `onQueryChange` で受け取って問い合わせる。
 */
export interface SearchableSelectRemote {
  /** 検索語に一致する全件数。 */
  total: number;
  /** 続きのページがある。 */
  hasMore: boolean;
  /** 続きのページを読み込んでいる。 */
  loadingMore: boolean;
  /** 検索語を変えて取り直している（前の結果を出したまま）。 */
  searching: boolean;
  /** 続きのページを読み込む。 */
  onLoadMore: () => void;
}

/** 文言（翻訳済み）。既定は日本語（DEFAULT_SEARCHABLE_SELECT_LABELS）。 */
export interface SearchableSelectLabels {
  /** 検索欄のプレースホルダ。 */
  searchPlaceholder: string;
  /** 単一選択の検索欄の読み上げ名（ラベルは画面に出さない）。 */
  searchLabel: (label: string) => string;
  /** 検索語の消去ボタンの読み上げ名と Tooltip。 */
  clearSearch: string;
  /** 候補の件数（例:「50 / 300 件」）。 */
  count: (shown: number, total: number) => string;
  /** 検索語に一致する候補がない。 */
  noMatch: (query: string) => string;
  /** 候補がそもそもない。 */
  empty: string;
  /** サーバー側で検索している間。 */
  searching: string;
  /** 続きのページを読み込むボタン（複数選択）・読み込み中の表示（単一選択）。 */
  loadMore: string;
}

/** 複数選択だけで使う文言。 */
export interface SearchableMultiSelectLabels extends SearchableSelectLabels {
  /** 一覧の開閉ボタンの読み上げ名と Tooltip。 */
  toggleList: string;
  /** 選択中の件数。 */
  selectedCount: (count: number) => string;
  /** 選択済みの chip の一覧の読み上げ名。 */
  selectedList: (label: string) => string;
  /** chip の「外す」ボタンの読み上げ名。 */
  removeChip: (name: string) => string;
  /** 表示中の候補をまとめて選ぶ。 */
  selectAllVisible: string;
  /** 選択をすべて外す。 */
  clear: string;
  /** 一覧を閉じて検索欄へ戻る（選択は選ぶたびに反映済み）。 */
  done: string;
  /** hideable な候補を隠すチェックボックス（任意。渡したときだけ出す）。 */
  hideHideable?: string;
  /** 隠している件数（任意）。 */
  hiddenCount?: (count: number) => string;
}

export const DEFAULT_SEARCHABLE_SELECT_LABELS: SearchableMultiSelectLabels = {
  searchPlaceholder: "検索して選ぶ…",
  searchLabel: (label) => `${label}を検索`,
  clearSearch: "検索語をクリア",
  count: (shown, total) => `${shown} / ${total} 件`,
  noMatch: (query) => `「${query}」に一致する候補がありません。`,
  empty: "候補がありません。",
  searching: "検索しています…",
  loadMore: "さらに表示",
  toggleList: "一覧を開閉",
  selectedCount: (count) => `${count} 件選択中`,
  selectedList: (label) => `選択中の${label}`,
  removeChip: (name) => `${name} を選択から外す`,
  selectAllVisible: "表示中をすべて選択",
  clear: "クリア",
  done: "完了",
};

/** 照合用の正規化（全角半角・大文字小文字を区別しない）。 */
export function normalizeSearchableText(text: string) {
  return text.normalize("NFKC").toLocaleLowerCase();
}

/**
 * 画面側の絞り込み（部分一致）。検索語は空白で区切った語をすべて含む候補だけを残す。
 * 検索語が空なら options をそのまま返す。
 */
export function filterSearchableOptions<T extends SearchableSelectOption>(
  options: readonly T[],
  query: string
): readonly T[] {
  const terms = normalizeSearchableText(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return options;
  return options.filter((option) => {
    const text = normalizeSearchableText(
      option.searchText ?? `${option.label} ${option.description ?? ""}`
    );
    return terms.every((term) => text.includes(term));
  });
}

/** 強調中の候補を ↑↓ で動かした次の位置（端で止める。候補がなければ -1）。 */
export function moveActiveIndex(current: number, delta: number, count: number) {
  if (count <= 0) return -1;
  if (current < 0) return delta > 0 ? 0 : count - 1;
  return Math.min(count - 1, Math.max(0, current + delta));
}

/** 候補の要素の id（aria-activedescendant）。値は任意の文字を含み得るので位置で作る。 */
export function searchableOptionId(listboxId: string, index: number) {
  return `${listboxId}-option-${index}`;
}

/** 選択肢の 1 行（listbox の option）。単一選択はチェック、複数選択はチェックボックスの印を出す。 */
export function SearchableOptionRow({
  id,
  option,
  selected,
  active,
  multiple,
  onSelect,
  onHover,
}: {
  id: string;
  option: SearchableSelectOption;
  selected: boolean;
  active: boolean;
  multiple: boolean;
  onSelect: () => void;
  onHover: () => void;
}) {
  return (
    <li
      id={id}
      role="option"
      aria-selected={selected}
      data-active={active || undefined}
      // 淡青の面の上の補足（description / meta）を 4.5:1 にする（tokens/colors.css。#1671）。
      data-surface-tint={selected || active ? "accent" : undefined}
      // 押してもフォーカスは検索欄に残す（aria-activedescendant で強調を伝える）。
      onMouseDown={(event: MouseEvent) => event.preventDefault()}
      // pointerdown で選ぶとタッチで一覧をスクロールしただけで選ばれるため、click で選ぶ。
      onClick={onSelect}
      onMouseMove={onHover}
      className={cn(
        "flex min-h-[var(--control-height-touch)] cursor-pointer items-center gap-2.5 px-3 py-2 text-sm text-fg transition-colors",
        selected && "bg-accent-muted",
        active && "bg-accent-subtle",
        selected && active && "bg-accent-muted",
        // 強制カラーモードでは地の色が消えるため、強調を枠線で示す。
        active && "forced-colors:outline forced-colors:outline-1 forced-colors:-outline-offset-1"
      )}
    >
      {/* 選択の印は名前の 1 行目の中心にそろえる（折り返した名前・補足のある行でも 1 行目。#2371）。印と名前を 1 つの
          items-start のまとまりにし、まとまりを行の縦の中央に置く。 */}
      <span className="flex min-w-0 flex-1 items-start gap-2.5">
        <span className="pr-icon-line">
          {multiple ? (
            <span
              aria-hidden
              className={cn(
                "flex size-4 shrink-0 items-center justify-center rounded-sm border",
                selected
                  ? "border-accent-emphasis bg-accent-emphasis text-fg-on-accent"
                  : "border-border-control bg-surface"
              )}
            >
              {selected ? <Check size={14} strokeWidth={3} /> : null}
            </span>
          ) : (
            <Check
              size={16}
              aria-hidden
              className={cn("shrink-0 text-accent-fg-strong", selected ? "opacity-100" : "opacity-0")}
            />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className={cn("[overflow-wrap:anywhere]", selected && "font-medium text-accent-fg-strong")}>
            {option.label}
          </span>
          {option.badge ? (
            <span
              data-surface-tint="info"
              className="ml-1.5 inline-block rounded-sm bg-info-subtle px-1 align-middle text-xs font-medium text-accent-fg"
            >
              {option.badge}
            </span>
          ) : null}
          {option.description ? (
            <span className="mt-0.5 block truncate text-xs text-fg-muted">{option.description}</span>
          ) : null}
        </span>
      </span>
      {option.meta ? <span className="tnum shrink-0 text-xs text-fg-muted">{option.meta}</span> : null}
    </li>
  );
}
