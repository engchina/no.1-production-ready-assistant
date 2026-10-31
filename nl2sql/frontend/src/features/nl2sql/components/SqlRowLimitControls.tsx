import { useId } from "react";

import { FieldError, TextField } from "@production-ready/ui";
import { t } from "@/lib/i18n";

export const DEFAULT_SQL_ROW_LIMIT = 100;
export const MAX_SQL_ROW_LIMIT = 100000;

export function parseSqlRowLimit(value: string): number | null {
  const normalized = value.trim();
  if (!/^\d+$/.test(normalized)) return null;
  const rowLimit = Number(normalized);
  if (!Number.isInteger(rowLimit) || rowLimit < 1 || rowLimit > MAX_SQL_ROW_LIMIT) return null;
  return rowLimit;
}

export function RowLimitField({
  value,
  onChange,
  disabled = false,
  error = "",
  className = "",
  helper = t("queryResults.rowLimit.helper"),
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  error?: string;
  className?: string;
  helper?: string;
}) {
  const id = useId();
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;
  const describedBy = error ? `${helperId} ${errorId}` : helperId;

  return (
    // 件数は短い数値なので欄は xs の幅。補足は 1 行で読めるように（#433）、欄ではなく外枠（行のセル）の幅で出すため、
    // TextField の helper / error を使わず外に置いて aria-describedby でつなぐ（#613 / #631）。
    <div className={`grid w-full min-w-0 gap-1 ${className}`}>
      {/* 空や範囲外では実行できない（parseSqlRowLimit が null → 実行ボタン無効）ので必須として示す（#531）。 */}
      <TextField
        id={id}
        label={t("queryResults.rowLimit.label")}
        required
        type="number"
        min={1}
        max={MAX_SQL_ROW_LIMIT}
        step={1}
        inputMode="numeric"
        value={value}
        onValueChange={onChange}
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={Boolean(error)}
        inputClassName="aria-[invalid=true]:border-danger-fg"
        width="xs"
      />
      {/* sm 以上は 1 行。375px では折り返す（横スクロールの領域にするとキーボードで届かない。WCAG 2.1.1。#1671）。 */}
      <p id={helperId} className="text-xs leading-5 text-fg-muted sm:overflow-x-auto sm:whitespace-nowrap">
        {helper}
      </p>
      <FieldError id={errorId} message={error} />
    </div>
  );
}
