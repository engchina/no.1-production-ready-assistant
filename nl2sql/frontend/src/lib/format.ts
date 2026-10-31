/** 表示用フォーマッタ（ロケール ja-JP。日時はシステムの設定の時間帯。#3265）。 */

import { displayDateTimeFormat } from "@production-ready/ui";

const numberFormat = new Intl.NumberFormat("ja-JP");

/** 整数・件数のカンマ区切り。 */
export function formatNumber(value: number): string {
  return numberFormat.format(value);
}

const dateTimeFormat = displayDateTimeFormat({
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const dateTimeWithYearFormat = displayDateTimeFormat({
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

/** ISO 文字列を「MM/DD HH:mm」へ。未設定・無効値はダッシュ。 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return dateTimeFormat.format(date);
}

/** ISO 文字列を「YYYY/MM/DD HH:mm」へ。未設定・無効値はダッシュ。 */
export function formatDateTimeWithYear(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return dateTimeWithYearFormat.format(date);
}

/** バイト数を人間可読サイズへ。 */
export function formatBytes(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * Oracle 識別子（`OWNER.TABLE_NAME` 等）を折り返し候補位置で分割する。
 * `.` `_` `$` `#` の直後を候補にし、`<wbr>` を挟んで描画すると語の途中ではなく区切りで折り返す。
 * 区切りのない長い区間は描画側の `overflow-wrap:anywhere` で最後の手段として折り返す。
 */
export function identifierWrapSegments(value: string): string[] {
  // 区切りの連続を捕獲して分け、直前の区間に付け直す（1 回の走査で終わる。CodeQL の多項式 ReDoS を避ける）。
  const parts = value.split(/([._$#]+)/);
  const segments: string[] = [];
  for (let index = 0; index < parts.length; index += 2) {
    const segment = (parts[index] ?? "") + (parts[index + 1] ?? "");
    if (segment) segments.push(segment);
  }
  return segments.length > 0 ? segments : [value];
}
