/**
 * 必須の入力欄の検証と、送信に失敗したときのフォーカス（UX 契約 messaging.md §3.2。#521）。
 * 検索・回答プロファイル・ナレッジベースの作成・編集フォーム、用語・同義語と回答ルール、回答プロンプトの版、FAQ などで使う（#541）。
 */

import { t } from "@/lib/i18n";

/** 前後の空白を除いて空なら `message` を返す（空白だけの入力も未入力として扱う）。 */
export function requiredTextError(value: string, message: string): string | null {
  return value.trim() ? null : message;
}

/** 検証エラーのある最初の欄の ID（欄の並び順で渡す）。無ければ null。 */
export function firstInvalidFieldId(
  fields: ReadonlyArray<readonly [id: string, error: string | null | undefined]>
): string | null {
  return fields.find(([, error]) => Boolean(error))?.[0] ?? null;
}

/** 送信に失敗したとき、最初の不正な欄へフォーカスを移す。エラーの欄があれば true（送信を止める）。 */
export function focusFirstInvalidField(
  fields: ReadonlyArray<readonly [id: string, error: string | null | undefined]>
): boolean {
  const id = firstInvalidFieldId(fields);
  if (id) document.getElementById(id)?.focus();
  return id !== null;
}

/**
 * 数値の欄（空は NaN）の検証。空は「〇〇を入力してください。」、範囲外・整数でないときは
 * 「〇〇は N 以上 M 以下の整数を入力してください。」を返す（UX 契約 messaging.md §3.2.1。#541）。
 * 空を 0 として扱わない。
 */
export function numberRangeError(
  value: number,
  rule: { label: string; min: number; max: number; integer?: boolean }
): string | null {
  const field = rule.label;
  if (Number.isNaN(value)) return t("validation.required", { field });
  const integer = rule.integer ?? true;
  if (Number.isFinite(value) && value >= rule.min && value <= rule.max && (!integer || Number.isInteger(value))) {
    return null;
  }
  return t(integer ? "validation.integerRange" : "validation.numberRange", {
    field,
    min: rule.min.toLocaleString("ja-JP"),
    max: rule.max.toLocaleString("ja-JP"),
  });
}
