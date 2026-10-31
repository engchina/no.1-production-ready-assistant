import { Eye, EyeOff } from "lucide-react";
import { type ReactNode, type Ref, useState } from "react";

import { StatusBadge } from "../data/status-badge";
import { cn } from "../../lib/utils";

import { useFieldWidthScope } from "./field-width-scope";
import { Button } from "./button";
import { FieldError } from "./field-error";
import { FormStatus } from "./form-status";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";
import {
  CONTROL_MIN_HEIGHT_CLASS,
  type ControlSize,
  type FieldWidth,
  fieldControlClass,
  labeledFieldWidthClasses,
} from "./control-size";

/** 保存済みの値を削除する指定（保存済みの値があるときだけ表示する）。 */
export interface SecretFieldClearOption {
  /** 翻訳済みの文言（例:「保存済みパスワードを削除する」）。 */
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

export interface SecretFieldProps {
  id: string;
  /** 翻訳済みラベル。 */
  label: string;
  /** 入力中の新しい値。保存済みの値は画面に出さないので、通常は空文字から始める。 */
  value: string;
  onValueChange: (value: string) => void;
  /** サーバーに保存済みの値があるか。バッジ・削除の指定の表示に使う。 */
  hasSavedSecret: boolean;
  /** 保存済みのバッジの文言（例:「保存済み」）。 */
  savedLabel: string;
  /** 未設定のバッジの文言（例:「未設定」）。 */
  notSetLabel: string;
  /** 表示の切り替えボタンの aria-label（隠れているとき。例:「API key を表示」）。 */
  showLabel: string;
  /** 表示の切り替えボタンの aria-label（表示しているとき。例:「API key を隠す」）。 */
  hideLabel: string;
  /** 表示するか（制御する場合）。省略すると内部の状態で切り替える。 */
  visible?: boolean;
  /** 表示の切り替えが押されたとき。引数は切り替え後の値。 */
  onVisibleChange?: (visible: boolean) => void;
  /** 保存済みの値をサーバーから取り出している間 true。切り替えボタンがスピナーになり押せなくなる。 */
  revealPending?: boolean;
  /** revealPending 中の切り替えボタンの aria-label（例:「DB パスワードを取得中」）。 */
  revealPendingLabel?: string;
  /** 保存済みの値を取り出せなかったときの文言。 */
  revealError?: string | null;
  /** 翻訳済みの補足。 */
  helper?: ReactNode;
  /** 翻訳済みのエラー。指定時は aria-invalid と枠線の色が変わる。 */
  error?: string;
  placeholder?: string;
  /** 必須であることを aria-required と RequiredBadge で伝える（ネイティブの required 検証も付ける）。 */
  required?: boolean;
  /** 必須バッジの文言。既定「必須」。条件付きの必須だけ上書きする。 */
  requiredLabel?: string;
  disabled?: boolean;
  /** 保存済みの値を削除する指定。hasSavedSecret のときだけ入力欄の下に出し、指定中は入力欄を無効にする。 */
  clearOption?: SecretFieldClearOption;
  autoComplete?: string;
  /** 高さ（既定 md = 36px）。同じ行の Button と同じ size にする（#613）。 */
  size?: ControlSize;
  /** 幅（既定は親の幅いっぱい）。sm（640px）未満は全幅（#613）。 */
  width?: FieldWidth;
  className?: string;
  ref?: Ref<HTMLInputElement>;
}

/**
 * 保存済みの値を表示しない secret の入力欄（API key・パスワード・token）。
 *
 * - ラベルの行に「保存済み / 未設定」を StatusBadge（アイコン付き。色だけに頼らない）で出す。
 * - 入力欄は TextField と同じ高さ・枠線・フォーカス表示で、既定はマスクする。右端の表示の切り替えは共有 Button。
 * - 保存済みの値の削除は、入力欄の直下のチェックボックスで指定する（指定中は入力欄と切り替えを無効にする）。
 * - 削除の指定や保存済みの値の取り出しは呼び出し側が持つ（このコンポーネントは API を知らない）。
 */
export function SecretField({
  id,
  label,
  value,
  onValueChange,
  hasSavedSecret,
  savedLabel,
  notSetLabel,
  showLabel,
  hideLabel,
  visible: visibleProp,
  onVisibleChange,
  revealPending = false,
  revealPendingLabel,
  revealError,
  helper,
  error,
  placeholder,
  required = false,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  disabled = false,
  clearOption,
  autoComplete = "off",
  size = "md",
  width,
  className,
  ref,
}: SecretFieldProps) {
  const widthScope = useFieldWidthScope();
  const [visibleState, setVisibleState] = useState(false);
  const visible = visibleProp ?? visibleState;
  const clearing = hasSavedSecret && Boolean(clearOption?.checked);
  const inputDisabled = disabled || clearing;

  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const revealErrorId = `${id}-reveal-error`;
  const describedBy =
    [helper ? hintId : "", error ? errorId : "", revealError ? revealErrorId : ""]
      .filter(Boolean)
      .join(" ") || undefined;

  const toggleLabel = revealPending
    ? (revealPendingLabel ?? showLabel)
    : visible
      ? hideLabel
      : showLabel;

  function toggleVisible() {
    const next = !visible;
    if (visibleProp === undefined) setVisibleState(next);
    onVisibleChange?.(next);
  }

  const widthClasses = labeledFieldWidthClasses(width, widthScope, Boolean(helper || error || revealError));

  return (
    // 段の幅（width）は入力欄に付け、ラベルと必須の印は入力欄の幅で折り返さない（#1746）。
    <div className={cn("min-w-0 space-y-1.5", widthClasses.box, className)}>
      {/* 隣の TextField とラベル行の高さ（20px）をそろえ、2 列の入力欄の上端を一致させる。 */}
      <div className="flex min-h-5 flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="flex items-center gap-2 text-sm font-medium text-fg">
          {label}
          {required && requiredLabel ? <RequiredBadge label={requiredLabel} aria-hidden /> : null}
        </label>
        <StatusBadge
          className="py-0"
          variant={hasSavedSecret ? "success" : "neutral"}
          label={hasSavedSecret ? savedLabel : notSetLabel}
        />
      </div>
      <div className={cn("relative", widthClasses.control)}>
        <input
          ref={ref}
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          disabled={inputDisabled}
          required={required || undefined}
          aria-required={required || undefined}
          aria-invalid={Boolean(error)}
          aria-describedby={describedBy}
          autoComplete={autoComplete}
          spellCheck={false}
          placeholder={placeholder}
          onChange={(event) => onValueChange(event.target.value)}
          className={cn(fieldControlClass, CONTROL_MIN_HEIGHT_CLASS[size], "pr-11", error ? "border-danger-fg" : "border-border-control")}
        />
        {/* 入力欄の直後に置く（`#id + button` で引ける）。高さは入力欄に合わせる。
            無効時の地と枠は入力欄が示すので、ボタン側では重ねない（loading 中の aria-disabled も同じ）。 */}
        <Button
          type="button"
          variant="ghost"
          iconOnly
          icon={visible ? EyeOff : Eye}
          loading={revealPending}
          // 保存済みの値の取得中（revealPending）は、呼び出し側が欄を disabled にしても切り替えボタンは
          // ネイティブの disabled にしない（loading の aria-disabled でフォーカスを保つ。#355 / #835）。
          disabled={inputDisabled && !revealPending}
          aria-label={toggleLabel}
          onClick={toggleVisible}
          className="absolute inset-y-0 right-0 h-full min-h-0 rounded-l-none disabled:border-transparent disabled:bg-transparent aria-disabled:border-transparent aria-disabled:bg-transparent"
        />
      </div>
      {helper ? (
        <p id={hintId} className={cn("text-xs leading-relaxed text-fg-muted", widthClasses.text)}>
          {helper}
        </p>
      ) : null}
      <FieldError id={errorId} message={error} className={widthClasses.text} />
      {revealError ? (
        <div id={revealErrorId} className={widthClasses.text}>
          <FormStatus tone="danger" className="text-xs" message={revealError} />
        </div>
      ) : null}
      {hasSavedSecret && clearOption ? (
        <label
          htmlFor={`${id}-clear`}
          className={cn(
            "flex items-start gap-3 rounded-md border border-border bg-surface-sunken px-4 py-3 text-sm transition-colors",
            widthClasses.text,
            disabled ? "cursor-not-allowed text-fg-disabled" : "cursor-pointer text-fg hover:bg-info-subtle"
          )}
        >
          <input
            id={`${id}-clear`}
            type="checkbox"
            checked={clearOption.checked}
            disabled={disabled}
            onChange={(event) => clearOption.onCheckedChange(event.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-[var(--color-accent-emphasis)] disabled:cursor-not-allowed"
          />
          <span>{clearOption.label}</span>
        </label>
      ) : null}
    </div>
  );
}
