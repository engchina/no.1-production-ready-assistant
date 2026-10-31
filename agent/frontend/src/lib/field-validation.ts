/**
 * 入力欄の送信前の検証（UX 契約 messaging.md §3.2.1。#540 / #541）。
 * 文言は「〇〇を入力してください。」「〇〇は N 以上の数値を入力してください。」の型にそろえ、
 * 規則は backend（Pydantic の validator・設定の検証）と同じにする。画面の検証は先回りで、正本は backend。
 */
import { t } from "@/lib/i18n";

/** 英数字で終わるラベル（「Workspace root」）は、ほかの文言と同じく助詞の前に空白を入れる。 */
export function labelForMessage(label: string): string {
  return /[A-Za-z0-9)]$/.test(label) ? `${label} ` : label;
}

/** 文字の欄の未入力（空白だけも未入力）。 */
export function requiredTextError(value: string, label: string): string | null {
  return value.trim() ? null : t("validation.required", { field: labelForMessage(label) });
}

/** 選ぶ欄（選択・ラジオ）の未選択。 */
export function requiredSelectError(value: string, label: string): string | null {
  return value ? null : t("validation.select", { field: labelForMessage(label) });
}

export interface NumberRule {
  /** 欄のラベル（翻訳済み）。 */
  label: string;
  /** 整数だけを受け付ける。 */
  integer?: boolean;
  /** 下限（`exclusiveMin` のときは下限を含まない = 0 より大きい）。 */
  min?: number;
  exclusiveMin?: boolean;
  /** 上限（含む）。 */
  max?: number;
}

/**
 * 数値の欄の検証。空は 0 として扱わず「〇〇を入力してください。」にする（空の数値を 0 で保存しない。#540）。
 * 数値でない・範囲外は、規則に合わせて「〇〇は N 以上の整数を入力してください。」などを返す。
 */
export function numberFieldError(raw: string, rule: NumberRule): string | null {
  const field = labelForMessage(rule.label);
  if (!raw.trim()) return t("validation.required", { field });
  const value = Number(raw);
  const belowMin =
    rule.min !== undefined && (rule.exclusiveMin ? !(value > rule.min) : !(value >= rule.min));
  const aboveMax = rule.max !== undefined && !(value <= rule.max);
  const notInteger = rule.integer === true && !Number.isInteger(value);
  if (Number.isFinite(value) && !belowMin && !aboveMax && !notInteger) return null;
  const kind = rule.integer ? "integer" : "number";
  if (rule.exclusiveMin) {
    return rule.max !== undefined
      ? t("validation.number.positiveMax", { field, max: rule.max })
      : t("validation.number.positive", { field });
  }
  if (rule.min !== undefined && rule.max !== undefined) {
    return t(kind === "integer" ? "validation.integer.range" : "validation.number.range", {
      field,
      min: rule.min,
      max: rule.max,
    });
  }
  if (rule.min !== undefined) {
    return t(kind === "integer" ? "validation.integer.min" : "validation.number.min", {
      field,
      min: rule.min,
    });
  }
  return t(kind === "integer" ? "validation.integer.any" : "validation.number.any", { field });
}

/** 欄の並び順で [入力欄の id, エラー] を渡し、最初のエラーの欄へフォーカスする。エラーがあれば true。 */
export function focusFirstInvalidField(
  fields: ReadonlyArray<readonly [id: string, error: string | null | undefined]>
): boolean {
  const first = fields.find(([, error]) => Boolean(error));
  if (!first) return false;
  document.getElementById(first[0])?.focus();
  return true;
}

/** JSON の欄の検証。空は「〇〇を入力してください。」、解析できないときは「〇〇は有効な JSON で入力してください。」。 */
export function parseJsonField<T>(
  raw: string,
  label: string,
  options: { required?: boolean; expect?: "array" | "object" } = {}
): { ok: true; value: T | undefined } | { ok: false; error: string } {
  const field = labelForMessage(label);
  if (!raw.trim()) {
    return options.required ? { ok: false, error: t("validation.required", { field }) } : { ok: true, value: undefined };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, error: t("validation.json.invalid", { field }) };
  }
  if (options.expect === "array" && !Array.isArray(parsed)) {
    return { ok: false, error: t("validation.json.array", { field }) };
  }
  if (options.expect === "object" && (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))) {
    return { ok: false, error: t("validation.json.object", { field }) };
  }
  return { ok: true, value: parsed as T };
}
