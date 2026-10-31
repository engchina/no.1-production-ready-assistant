/*
 * 画面の日時の表示の時間帯（#3265）。3 製品の画面は、日時をブラウザの時間帯ではなく、システムの
 * 設定の時間帯（共通の設定 `PLATFORM_TIMEZONE`。`GET /api/auth/me` の `timezone`）で表示する。
 * `@production-ready/system-settings` の `AuthProvider` が利用者を受け取ったときに
 * `setDisplayTimeZone` で設定し、書式の関数（`formatDisplayDateTime` / `displayDateTimeParts`）が読む。
 * 認証の確認の後にしか日時を出さないため、表示の前に時間帯が決まっている。
 */

/** 設定を受け取る前・読めない値のときの時間帯（backend の既定 `pr_backend_core.timezones` と同じ）。 */
export const DEFAULT_DISPLAY_TIME_ZONE = "UTC";

const LOCALE = "ja-JP";

let currentTimeZone = DEFAULT_DISPLAY_TIME_ZONE;
const formatters = new Map<string, Intl.DateTimeFormat>();

/** IANA の地域名として `Intl` が受け付けるか（`Asia/Tokyo`・`UTC`）。 */
export function isSupportedTimeZone(value: string): boolean {
  if (!value.trim()) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value.trim() });
    return true;
  } catch {
    return false;
  }
}

/** 表示の時間帯を設定する。空・読めない値は既定（`UTC`。#3274）にする。 */
export function setDisplayTimeZone(value: string | null | undefined): void {
  const next = value && isSupportedTimeZone(value) ? value.trim() : DEFAULT_DISPLAY_TIME_ZONE;
  if (next === currentTimeZone) return;
  currentTimeZone = next;
  formatters.clear();
}

/** 今の表示の時間帯（IANA の地域名）。API に時間帯を渡すとき（Agent の利用状況など）にも使う。 */
export function displayTimeZone(): string {
  return currentTimeZone;
}

function formatterFor(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify(options);
  let formatter = formatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(LOCALE, { ...options, timeZone: currentTimeZone });
    formatters.set(key, formatter);
  }
  return formatter;
}

function toDate(value: Date | string | number): Date | null {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * 日時を表示の時間帯で書く（`Intl.DateTimeFormat("ja-JP", options)` に `timeZone` を足したもの）。
 * `options` に `timeZone` は渡さない（表示の時間帯に従う）。読めない値は `invalid`（既定「—」）。
 */
export function formatDisplayDateTime(
  value: Date | string | number,
  options: Omit<Intl.DateTimeFormatOptions, "timeZone">,
  invalid = "—",
): string {
  const date = toDate(value);
  return date ? formatterFor(options).format(date) : invalid;
}

const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const calendarFormatters = new Map<string, Intl.DateTimeFormat>();

/**
 * 暦の日付（`YYYY-MM-DD`。集計の日など、瞬間ではない値）を書く。時間帯で日付を動かさない。
 * `YYYY-MM-DD` でない値は日時として `formatDisplayDateTime` で書く。読めない値は `invalid`。
 */
export function formatDisplayCalendarDate(
  value: string,
  options: Omit<Intl.DateTimeFormatOptions, "timeZone">,
  invalid = "—",
): string {
  const match = CALENDAR_DATE.exec(value.trim());
  if (!match) return formatDisplayDateTime(value, options, invalid);
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(Date.UTC(year, month - 1, day));
  // 13 月・40 日などの繰り上がりは読めない値とする。
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return invalid;
  const key = JSON.stringify(options);
  let formatter = calendarFormatters.get(key);
  if (!formatter) {
    // 暦の日付は UTC の 0 時として作り、同じ UTC で書く（端末・表示の時間帯で前後の日にしない）。
    formatter = new Intl.DateTimeFormat(LOCALE, { ...options, timeZone: "UTC" });
    calendarFormatters.set(key, formatter);
  }
  return formatter.format(date);
}

export interface DisplayDateTimeFormat {
  format: (value: Date | string | number) => string;
}

/**
 * `new Intl.DateTimeFormat("ja-JP", options)` の代わり（module の定数に置ける）。書くたびに今の表示の
 * 時間帯を使う（時間帯を設定する前に作っても、設定の後の表示は設定の時間帯）。読めない値は「—」。
 */
export function displayDateTimeFormat(options: Omit<Intl.DateTimeFormatOptions, "timeZone">): DisplayDateTimeFormat {
  return { format: (value) => formatDisplayDateTime(value, options) };
}

/** 日付と時刻（「2026/10/25 13:45:55」。`toLocaleString("ja-JP")` と同じ形）。 */
export const DISPLAY_DATE_TIME_SECONDS: Omit<Intl.DateTimeFormatOptions, "timeZone"> = {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
};

/** 時刻（「13:45:55」。`toLocaleTimeString("ja-JP")` と同じ形）。 */
export const DISPLAY_TIME_SECONDS: Omit<Intl.DateTimeFormatOptions, "timeZone"> = {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
};

export interface DisplayDateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const PARTS_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
  hourCycle: "h23",
};

/** 表示の時間帯の年・月・日・時・分・秒（固定の形の文字列・ファイル名に使う）。読めない値は null。 */
export function displayDateTimeParts(value: Date | string | number = new Date()): DisplayDateTimeParts | null {
  const date = toDate(value);
  if (!date) return null;
  const parts: Record<string, number> = {};
  for (const part of formatterFor(PARTS_OPTIONS).formatToParts(date)) {
    if (part.type !== "literal") parts[part.type] = Number(part.value);
  }
  return {
    year: parts.year ?? 0,
    month: parts.month ?? 0,
    day: parts.day ?? 0,
    // `h23` でも 0 時を 24 と返す実装があるため 0 にそろえる。
    hour: (parts.hour ?? 0) % 24,
    minute: parts.minute ?? 0,
    second: parts.second ?? 0,
  };
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** ファイル名に付ける表示の時間帯の時刻（「20261025-134555」）。 */
export function displayFileTimestamp(value: Date | string | number = new Date()): string {
  const parts = displayDateTimeParts(value) ?? displayDateTimeParts(new Date());
  if (!parts) return "";
  return `${parts.year}${pad2(parts.month)}${pad2(parts.day)}-${pad2(parts.hour)}${pad2(parts.minute)}${pad2(parts.second)}`;
}
