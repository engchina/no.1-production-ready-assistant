import { useId } from "react";

import { cn } from "../../lib/utils";
import { CONTROL_HEIGHT_CLASS, type ControlSize, type FieldWidth } from "../ui/control-size";
import { SelectField, type SelectFieldOption } from "../ui/select-field";
import { formatPaginationNumber } from "./pagination";

/*
 * 一覧の絞り込みの部品（#1933）。`ListToolbar` の `filters` に置く。
 * 規則は UX 契約 page-archetypes.md「一覧のツールバー」と design-system README §4「一覧の絞り込み」。
 *
 * - ツールバーの中の部品は検索欄と同じ高さ（`size` md 36px、タッチ端末 44px）で、縦の中心をそろえる。
 * - 欄の上にラベルを置かない。選択の絞り込みは `FilterSelect`（ボタンの中に「種類: すべて」）、
 *   少数の排他の状態は `FilterChipGroup`（チップの高さを欄にそろえ、件数を添えられる）。
 */

/** 絞り込みが掛かっているか（既定の値と違う値を選んでいるか）。`activeFilterCount` を数えるときに使う。 */
export function isFilterActive<T extends string>(value: T, defaultValue: T) {
  return value !== defaultValue;
}

export interface FilterSelectProps<T extends string> {
  id: string;
  /** 絞り込みの名前（翻訳済み。例「種類」）。ボタンの中の先頭に「種類: 」と出し、読み上げ名にもなる。 */
  label: string;
  value: T;
  options: readonly SelectFieldOption<T>[];
  onValueChange: (value: T) => void;
  /**
   * 絞り込まない値（「すべて」の値）。これ以外を選んでいると絞り込み中として値を強調する。
   * 既定は `options` の先頭の値。
   */
  defaultValue?: T;
  /**
   * 幅（既定 `sm` 12rem）。「ラベル: 値」の長さで選ぶ（短い状態・種類は `sm`、名前・長い選択肢は `md`、
   * 対象の選択は `lg`）。sm（640px）未満は全幅。
   */
  width?: Exclude<FieldWidth, "full">;
  /** 高さ（既定 md）。ツールバーの検索欄と同じ size にする。 */
  size?: ControlSize;
  disabled?: boolean;
  /** ボタン（combobox）に付ける `data-testid`。 */
  "data-testid"?: string;
  className?: string;
}

/**
 * 一覧のツールバーの選択の絞り込み（#1933）。ラベルをボタンの中に入れた `SelectField`（「種類: すべて」）。
 *
 * - 欄の上にラベルを出さないので、検索欄・状態のチップと同じ高さの 1 行にそろう（Primer の「Label: value」の
 *   ボタン・Carbon の inline の Dropdown と同じ形）。
 * - 読み上げ名はラベル（「種類」）、値はボタンの中の値の文字で読む（APG の select-only combobox）。
 * - 既定（`defaultValue`）以外を選んでいると、値を太字・アクセントの文字、枠をアクセントの線にする
 *   （`data-filter-active="true"`）。値の文字でも分かるので色だけで伝えない。
 * - キーボード・typeahead・一覧の位置は `SelectField` と同じ。
 */
export function FilterSelect<T extends string>({
  id,
  label,
  value,
  options,
  onValueChange,
  defaultValue,
  width = "sm",
  size = "md",
  disabled,
  "data-testid": testId,
  className,
}: FilterSelectProps<T>) {
  const resolvedDefault = defaultValue ?? options[0]?.value;
  return (
    <SelectField
      id={id}
      label={label}
      value={value}
      options={options}
      onValueChange={onValueChange}
      labelPlacement="inline"
      active={resolvedDefault !== undefined && isFilterActive(value, resolvedDefault)}
      width={width}
      size={size}
      disabled={disabled}
      data-testid={testId}
      className={className}
    />
  );
}

export interface FilterChipOption<T extends string = string> {
  value: T;
  /** チップの文言（翻訳済み）。読み上げ名にもなる。 */
  label: string;
  /** その状態の件数（任意）。チップの文言の後ろに等幅数字で出し、読み上げでは説明（aria-describedby）として読む。 */
  count?: number;
}

export interface FilterChipGroupProps<T extends string> {
  /** グループの名前（翻訳済み。例「状態」）。`role="group"` の `aria-label`。画面には出さない。 */
  label: string;
  value: T;
  options: readonly FilterChipOption<T>[];
  onValueChange: (value: T) => void;
  /** 高さ（既定 md）。ツールバーの検索欄と同じ size にする。 */
  size?: ControlSize;
  disabled?: boolean;
  "data-testid"?: string;
  className?: string;
}

/** `FilterChipGroup` で並べる選択肢の上限。これを超える状態・種類は `FilterSelect` にする（#1933）。 */
export const FILTER_CHIP_MAX_OPTIONS = 5;

/**
 * 少数の排他の状態の絞り込み（#1933）。「すべて / 完了 / 停止 / 失敗」のように 1 つを選ぶチップの並び。
 *
 * - チップの高さは欄と同じ（`size` md 36px、タッチ端末 44px）。以前の `ToggleChip`（約 26px）を並べると、
 *   ツールバーの中で検索欄より低く、下にずれて見えた。
 * - 選んだチップは塗り（アクセント）、`aria-pressed="true"`。塗りと枠の有無で、グレースケールでも分かる。
 * - 件数（`count`）を添えられる（「完了 12」。等幅数字、読み上げは説明として。名前は文言だけ）。
 * - 選択肢は `FILTER_CHIP_MAX_OPTIONS`（5）まで。それより多い・長い選択肢は `FilterSelect` にする。
 * - 置き場所が狭いと折り返す（隠さない）。
 */
export function FilterChipGroup<T extends string>({
  label,
  value,
  options,
  onValueChange,
  size = "md",
  disabled = false,
  "data-testid": testId,
  className,
}: FilterChipGroupProps<T>) {
  const idPrefix = useId();
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("flex min-w-0 flex-wrap items-center gap-1", className)}
      data-filter-chip-group=""
      data-testid={testId}
    >
      {options.map((option, index) => {
        const selected = option.value === value;
        const countId = option.count === undefined ? undefined : `${idPrefix}-count-${index}`;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            aria-describedby={countId}
            disabled={disabled}
            data-value={option.value}
            onClick={() => {
              if (!selected) onValueChange(option.value);
            }}
            className={cn(
              CONTROL_HEIGHT_CLASS[size],
              "inline-flex max-w-full shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-sm font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring forced-colors:border-[CanvasText]",
              "disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-disabled disabled:text-fg-disabled",
              selected
                ? "border-transparent bg-accent-emphasis text-fg-on-accent forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none"
                : "border-border-control bg-surface text-fg enabled:hover:bg-surface-hover"
            )}
          >
            <span className="min-w-0 truncate">{option.label}</span>
            {option.count === undefined ? null : (
              <span
                id={countId}
                className={cn("tnum text-xs font-normal", selected ? "text-fg-on-accent" : "text-fg-muted")}
                data-filter-chip-count=""
              >
                {formatPaginationNumber(Math.max(0, option.count))}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
