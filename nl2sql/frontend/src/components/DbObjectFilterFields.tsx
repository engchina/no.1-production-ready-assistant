import {
  type FieldWidth,
  ListToolbar,
  type ListCountProps,
  SearchField,
  SelectField,
} from "@production-ready/ui";
import { useId, type ReactNode } from "react";

import { t } from "@/lib/i18n";
import { listCountLabels, listToolbarLabels } from "@/lib/pagination-labels";

export interface DbObjectFilterFieldProps {
  label: string;
  placeholder: string;
  /** 適用中の値。 */
  value: string;
  /** 値が確定したとき（入力が止まって 300ms・Enter・消去。IME の変換中は呼ばない。#535）。 */
  onChange: (value: string) => void;
  disabled?: boolean;
  /**
   * 欄の幅（#1891）。`ListToolbar` の `search` に入れる検索欄には渡さない（部品が lg で固定する）。
   * ツールバーの外に単独で置く検索欄は lg、所有者の接頭辞は名前なので md（既定）。
   */
  width?: FieldWidth;
  /**
   * ラベルを読み上げだけにする（既定 true）。一覧のツールバーの中では欄の上にラベルを置かない（#1933）ので、
   * 意味は placeholder と読み上げ名で伝える。
   */
  labelHidden?: boolean;
  testId?: string;
  className?: string;
}

/** 一覧のツールバーの「条件をクリア」と狭い置き場所の畳み方（`ListToolbar` の同名の props。#1933）。 */
export interface DbObjectToolbarFilterProps {
  /** 既定と違う値の絞り込みの数（検索語は数えない。所有者の接頭辞は数える）。 */
  activeFilterCount?: number;
  /** 検索語・所有者・絞り込みをすべて既定に戻す。渡すと条件があるあいだだけ「条件をクリア」を出す。 */
  onClearFilters?: () => void;
  /** 狭い置き場所の絞り込みの出し方。検索欄を含めて 3 つ以上の一覧は `sheet`。 */
  filtersLayout?: "wrap" | "sheet";
}

export interface DbObjectSearchOwnerFieldsProps extends DbObjectToolbarFilterProps {
  searchLabel: string;
  searchPlaceholder: string;
  searchValue: string;
  onSearchChange: (value: string) => void;
  ownerLabel: string;
  ownerPlaceholder: string;
  ownerValue: string;
  onOwnerChange: (value: string) => void;
  disabled?: boolean;
  /** 所有者の右に並べる絞り込み（`width` を渡した `DbManagementSelectField` など）。 */
  filters?: ReactNode;
  /**
   * 一覧の件数（`ListCount`。#1891）。絞り込みなしは `matched` を渡さない（「全 N 件」）。
   * 文言（`labels`）を省くと NL2SQL の i18n の文言を使う。
   */
  count?: ListCountProps;
  /** 件数の右に出す状態の文言（例:「選択中 3 件」）。 */
  summary?: ReactNode;
  /** 右端に置く一覧への操作。 */
  actions?: ReactNode;
  testId?: string;
  className?: string;
}

export interface DbManagementSelectOption<T extends string = string> {
  value: T;
  label: string;
}

export interface DbManagementSelectFieldProps<T extends string = string> {
  label: string;
  value: T;
  options: readonly DbManagementSelectOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  /** grid の外に単独で置くときの幅（#613）。検索・所有者と並べるツールバーでは短い列挙の sm。 */
  width?: FieldWidth;
  className?: string;
}

export function DbManagementSearchField({
  label,
  placeholder,
  value,
  onChange,
  disabled = false,
  width,
  labelHidden = true,
  testId,
  className = "",
}: DbObjectFilterFieldProps) {
  const id = useId();
  // 一覧の絞り込みは共有の SearchField（debounce・IME 対応・消去）で作る（#535）。
  return (
    <SearchField
      id={`db-object-search-${id}`}
      label={label}
      labelHidden={labelHidden}
      data-testid={testId}
      value={value}
      disabled={disabled}
      onSearch={onChange}
      clearLabel={t("common.clearSearch")}
      placeholder={placeholder}
      autoComplete="off"
      width={width}
      className={`min-w-0 ${className}`}
    />
  );
}

const toUpperCase = (value: string) => value.toUpperCase();

/** 所有者の接頭辞の正規化（前後の空白を落として大文字にする）。 */
export const normalizeOwnerPrefix = (value: string) => value.trim().toUpperCase();

export function DbOwnerPrefixFilterField({
  label,
  placeholder,
  value,
  onChange,
  disabled = false,
  width = "md",
  labelHidden = true,
  testId,
  className = "",
}: DbObjectFilterFieldProps) {
  const id = useId();
  // 所有者の接頭辞も一覧の絞り込み。入力中の文字を大文字で見せ、確定した値も大文字にして渡す（Oracle の所有者名は大文字）。
  return (
    <SearchField
      id={`db-owner-prefix-${id}`}
      label={label}
      labelHidden={labelHidden}
      data-testid={testId}
      value={value}
      disabled={disabled}
      onSearch={onChange}
      normalize={normalizeOwnerPrefix}
      formatInput={toUpperCase}
      clearLabel={t("common.clearInput")}
      placeholder={placeholder}
      autoCapitalize="characters"
      autoComplete="off"
      spellCheck={false}
      width={width}
      className={`min-w-0 ${className}`}
    />
  );
}

export function DbManagementSelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false,
  width,
  className = "",
}: DbManagementSelectFieldProps<T>) {
  const id = useId();
  // 検索・所有者（SearchField）と同じ行に並べる選択欄。共有の SelectField で同じ高さ（md）・枠線・角丸にそろえる（#613 / #631）。
  return (
    <SelectField<T>
      id={`db-management-select-${id}`}
      label={label}
      value={value}
      options={options}
      onValueChange={onChange}
      disabled={disabled}
      width={width}
      className={`min-w-0 ${className}`}
    />
  );
}

/**
 * DB オブジェクトの一覧の検索・所有者の接頭辞・絞り込み・件数を、共通の `ListToolbar` で並べる（#600 / #1891）。
 * 左に検索欄（部品が lg で固定）→ 所有者（md）→ 絞り込み（`FilterSelect`）→「条件をクリア」、右に件数（`ListCount`）と
 * 一覧への操作。欄の上にラベルを置かず、部品の縦の中心をそろえる（#1933）。
 */
export function DbObjectSearchOwnerFields({
  searchLabel,
  searchPlaceholder,
  searchValue,
  onSearchChange,
  ownerLabel,
  ownerPlaceholder,
  ownerValue,
  onOwnerChange,
  disabled = false,
  filters,
  count,
  summary,
  actions,
  testId,
  className = "",
  activeFilterCount = 0,
  onClearFilters,
  filtersLayout,
}: DbObjectSearchOwnerFieldsProps) {
  return (
    <ListToolbar
      className={className}
      testId={testId}
      activeFilterCount={activeFilterCount + (ownerValue.trim() ? 1 : 0)}
      searchActive={Boolean(searchValue.trim())}
      onClearFilters={onClearFilters}
      filtersLayout={filtersLayout}
      labels={listToolbarLabels()}
      search={
        <DbManagementSearchField
          label={searchLabel}
          placeholder={searchPlaceholder}
          value={searchValue}
          disabled={disabled}
          onChange={onSearchChange}
        />
      }
      filters={
        <>
          <DbOwnerPrefixFilterField
            label={ownerLabel}
            placeholder={ownerPlaceholder}
            value={ownerValue}
            disabled={disabled}
            onChange={onOwnerChange}
          />
          {filters}
        </>
      }
      count={count ? { labels: listCountLabels(), ...count } : undefined}
      summary={summary}
      actions={actions}
    />
  );
}
