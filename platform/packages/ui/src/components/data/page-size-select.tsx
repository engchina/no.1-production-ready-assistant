import { useId } from "react";

import { SelectField } from "../ui/select-field";
import type { ControlSize, FieldWidth } from "../ui/control-size";
import { PAGE_SIZE_OPTIONS } from "./page-size";

export { PAGE_SIZE_OPTIONS };

export interface PageSizeSelectLabels {
  /** 欄のラベル（既定「表示件数」）。 */
  label: string;
  /** 選択肢の文言（既定「{n} 件」）。 */
  option: (size: number) => string;
}

export const DEFAULT_PAGE_SIZE_SELECT_LABELS: PageSizeSelectLabels = {
  label: "表示件数",
  option: (size) => `${size} 件`,
};

export interface PageSizeSelectProps {
  id?: string;
  value: number;
  onValueChange: (size: number) => void;
  /** 選択肢（10 / 25 / 50 / 100。既定 25）。 */
  options?: readonly number[];
  labels?: Partial<PageSizeSelectLabels>;
  size?: ControlSize;
  /** 欄の幅（既定 `xs`。「100 件」が収まる数値の段。#613 / #1891）。 */
  width?: FieldWidth;
  /** 表の行など、ラベルを見せない所（読み上げには残る）。 */
  labelHidden?: boolean;
  disabled?: boolean;
  className?: string;
}

/**
 * 1 ページの件数（表示件数）の選択（#1266 / #1891）。`Pagination` / `OffsetPagination` / `PagedDataTable` に
 * `onPageSizeChange` を渡すと、ページ送りの左（件数の範囲の右）にこの部品が並ぶ。`ResultTable` も同じ部品。
 * 値（「100 件」）が収まる `xs` の幅（#613）。選択肢に無い値を渡されても、その値を選択肢に足して出す。
 */
export function PageSizeSelect({
  id,
  value,
  onValueChange,
  options = PAGE_SIZE_OPTIONS,
  labels: labelOverrides,
  size,
  width = "xs",
  labelHidden,
  disabled,
  className,
}: PageSizeSelectProps) {
  const reactId = useId();
  const labels = { ...DEFAULT_PAGE_SIZE_SELECT_LABELS, ...labelOverrides };
  const sizes = options.includes(value) ? options : [...options, value].sort((a, b) => a - b);
  return (
    <SelectField
      id={id ?? `page-size-${reactId}`}
      label={labels.label}
      value={String(value)}
      options={sizes.map((option) => ({ value: String(option), label: labels.option(option) }))}
      onValueChange={(next) => onValueChange(Number(next))}
      width={width}
      size={size}
      labelHidden={labelHidden}
      disabled={disabled}
      className={className}
    />
  );
}
