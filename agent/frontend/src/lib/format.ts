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

/** ISO 文字列を「MM/DD HH:mm」へ。未設定・無効値はダッシュ。 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return dateTimeFormat.format(date);
}

