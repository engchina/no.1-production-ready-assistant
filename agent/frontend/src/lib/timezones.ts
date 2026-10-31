/** 自動実行の時間帯の入力の検証（既定はシステムの設定の時間帯。`@production-ready/ui` の `displayTimeZone`。#3265）。 */
const OFFSET_TIMEZONE = /^[+-](?:[01]\d|2[0-3]):[0-5]\d$/;

/** 空欄は backend の既定を使う。地域名は夏時間を含むため時差に変換しない。 */
export function isValidTimezone(value: string): boolean {
  const timezone = value.trim();
  if (!timezone || OFFSET_TIMEZONE.test(timezone)) return true;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    return true;
  } catch {
    return false;
  }
}
