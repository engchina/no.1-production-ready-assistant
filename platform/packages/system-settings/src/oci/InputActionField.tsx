import type { ChangeEvent, InputHTMLAttributes, ReactNode } from "react";

import {
  Button,
  type ButtonProps,
  cn,
  FieldActionRow,
  FieldError,
  TextField,
  labeledFieldWidthClasses,
  type FieldWidth,
} from "@production-ready/ui";

/**
 * `FieldActionRow` に段の幅（`width`）の欄を置くときの指定（#1891）。`fit` で欄を段の幅のまま置き、操作を欄の
 * すぐ右に並べる。行の外枠は置いた枠（grid のセル・カード）の幅のままにし、補足・エラーの文はその幅で折り返す
 * （欄と操作の幅で折り返さない。#2063）。`full` か未指定は行の幅いっぱい（従来どおり）。
 */
export function fieldActionRowWidthClasses(width: FieldWidth | undefined): {
  row: string | undefined;
  footer: string | undefined;
  fit: boolean;
} {
  if (!width || width === "full") return { row: undefined, footer: undefined, fit: false };
  return { row: undefined, footer: labeledFieldWidthClasses(width).text, fit: true };
}

export interface InputActionFieldAction {
  label: ReactNode;
  ariaLabel?: string;
  icon?: ButtonProps["icon"];
  type?: ButtonProps["type"];
  variant?: ButtonProps["variant"];
  loading?: boolean;
  disabled?: boolean;
  onClick?: ButtonProps["onClick"];
  dataTestId?: string;
  className?: string;
}

export interface InputActionFieldProps {
  id: string;
  /** 翻訳済みのラベル（TextField の label）。 */
  label: string;
  value: string;
  onChange?: (value: string) => void;
  placeholder?: string;
  helper?: string;
  error?: string;
  actionError?: string;
  required?: boolean;
  requiredLabel?: string;
  readOnly?: boolean;
  disabled?: boolean;
  type?: InputHTMLAttributes<HTMLInputElement>["type"];
  autoComplete?: string;
  /** 入力欄の幅（値の種類で選ぶ。README §4「操作部品の高さと幅」）。操作は欄のすぐ右に置く。既定は行の幅いっぱい。 */
  width?: FieldWidth;
  className?: string;
  inputClassName?: string;
  inputTestId?: string;
  action: InputActionFieldAction;
}

/**
 * テキスト入力と右側の操作（取得・接続テスト）を 1 行に並べる欄（TextField + FieldActionRow）。
 * 高さは入力欄・ボタンとも md 36px（タッチ端末は 44px）。操作は入力欄の下端にそろい、375px では下に全幅（#613）。
 * 補足・エラーは FieldActionRow の footer に出す（欄の中に入れると操作の下端がずれるため）。#631 でネイティブの input から置き換えた。
 */
export function InputActionField({
  id,
  label,
  value,
  onChange,
  placeholder,
  helper,
  error,
  actionError,
  required,
  requiredLabel,
  readOnly = false,
  disabled = false,
  type = "text",
  autoComplete,
  width,
  className,
  inputClassName,
  inputTestId,
  action,
}: InputActionFieldProps) {
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const actionErrorId = `${id}-action-error`;
  const widthClasses = fieldActionRowWidthClasses(width);
  const inputDescribedBy =
    [helper ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ") || undefined;

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    if (readOnly || disabled) return;
    onChange?.(event.currentTarget.value);
  }

  return (
    <FieldActionRow
      className={cn(widthClasses.row, className)}
      fit={widthClasses.fit}
      actions={
        // 入力と同じ行の操作なので、入力欄と同じ md（README §4「操作部品の高さと幅」）。
        <Button
          type={action.type ?? "button"}
          variant={action.variant ?? "secondary"}
          size="md"
          className={action.className}
          aria-label={action.ariaLabel}
          aria-describedby={actionError ? actionErrorId : undefined}
          icon={action.icon}
          loading={action.loading}
          disabled={disabled || action.disabled}
          data-testid={action.dataTestId}
          onClick={action.onClick}
        >
          <span>{action.label}</span>
        </Button>
      }
      footer={
        helper || error || actionError ? (
          <div className={cn("space-y-1.5", widthClasses.footer)}>
            {helper ? (
              <p id={hintId} className="text-xs leading-relaxed text-fg-muted">
                {helper}
              </p>
            ) : null}
            <FieldError id={errorId} message={error} />
            <FieldError id={actionErrorId} message={actionError} />
          </div>
        ) : null
      }
    >
      <TextField
        id={id}
        label={label}
        type={type}
        value={value}
        readOnly={readOnly}
        disabled={disabled}
        required={required}
        requiredLabel={requiredLabel}
        aria-readonly={readOnly || undefined}
        // 補足・エラーは footer に出すので、TextField の helper / error ではなく aria で結び付ける。
        aria-invalid={Boolean(error)}
        aria-describedby={inputDescribedBy}
        autoComplete={autoComplete}
        placeholder={placeholder}
        width={width}
        data-testid={inputTestId}
        onChange={handleChange}
        inputClassName={cn(
          error && "border-danger-fg",
          readOnly && "cursor-default text-fg-muted",
          inputClassName
        )}
      />
    </FieldActionRow>
  );
}
