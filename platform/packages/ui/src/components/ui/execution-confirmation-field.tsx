import { Fragment, type ReactNode, useId } from "react";

import { surfaceTintFor } from "../../lib/surface-tint";
import { cn } from "../../lib/utils";

import { RequiredBadge } from "./required-badge";

/** 確認語欄の文言。製品は翻訳済みの文字列を渡す（未指定の項目は既定の日本語）。 */
export interface ExecutionConfirmationLabels {
  /** 見出し（入力欄のラベル）。例:「実行確認語」 */
  label: string;
  /** 必須のタグ。例:「必須」 */
  required: string;
  /** 入力条件の表示。`{phrase}` の位置に確認語を等幅で差し込む。例:「入力条件: {phrase}」 */
  expected: string;
  /** 状態のバッジ（未入力 / 不一致 / 確認済み）。 */
  pending: string;
  mismatch: string;
  confirmed: string;
}

export const DEFAULT_EXECUTION_CONFIRMATION_LABELS: ExecutionConfirmationLabels = {
  label: "実行確認語",
  required: "必須",
  expected: "入力条件: {phrase}",
  pending: "未入力",
  mismatch: "不一致",
  confirmed: "確認済み",
};

export type ExecutionConfirmationStatus = "pending" | "mismatch" | "confirmed";

/**
 * 確認語欄の状態。一致の判定は呼び出し側が行い（`confirmed`）、ここでは表示の区分だけを決める。
 * 空白だけの入力は「未入力」として扱う（入力前から danger 色にしない）。
 */
export function executionConfirmationStatus(value: string, confirmed: boolean): ExecutionConfirmationStatus {
  if (confirmed) return "confirmed";
  return value.trim() ? "mismatch" : "pending";
}

const STATUS_CLASS: Record<ExecutionConfirmationStatus, string> = {
  confirmed: "border-success-border bg-success-subtle text-success-fg",
  mismatch: "border-danger-border bg-danger-subtle text-danger-fg",
  pending: "border-border bg-surface text-fg-muted",
};


const PHRASE_TOKEN = "{phrase}";

/**
 * DB 識別子（`ADMIN_EXECUTE`・`OWNER.OBJECT`）を `.` / `_` / `$` / `#` の直後で優先的に折り返す。
 * 区切りのない長い区間だけ `overflow-wrap:anywhere` で折り返す。コピー・読み上げの文字は元のまま。
 */
function identifierSegments(value: string): string[] {
  // 区切りの連続を捕獲して分け、直前の区間に付け直す（1 回の走査で終わる。CodeQL の多項式 ReDoS を避ける）。
  const parts = value.split(/([._$#]+)/);
  const segments: string[] = [];
  for (let index = 0; index < parts.length; index += 2) {
    const segment = (parts[index] ?? "") + (parts[index + 1] ?? "");
    if (segment) segments.push(segment);
  }
  return segments.length > 0 ? segments : [value];
}

export interface ExecutionConfirmationFieldProps {
  value: string;
  onChange: (value: string) => void;
  /** 入力が確認語と一致しているか（判定は呼び出し側。例: `value.trim() === phrase`）。 */
  confirmed: boolean;
  /** 入力条件として示す確認語（識別子として等幅で出す）。 */
  expectedLabel: string;
  /** 入力欄のプレースホルダ。既定は `expectedLabel`。 */
  placeholder?: string;
  /** 入力欄の下の説明（翻訳済み）。不一致のときだけ danger 色になる。 */
  helper: ReactNode;
  disabled?: boolean;
  /** 入力欄の id。既定は自動生成（e2e やラベルから引くときだけ渡す）。 */
  id?: string;
  labels?: Partial<ExecutionConfirmationLabels>;
  /** 確認語の直下（区切り線の下）に描画する実行 / キャンセル等の操作。primary / danger → secondary の順で渡す。 */
  actions?: ReactNode;
  className?: string;
}

/**
 * 破壊的な操作の「実行確認語」の入力欄（type-to-confirm）。
 *
 * - 面は中立（`--color-surface-sunken`）。入力前は danger 色を使わず、ラベルは `--color-fg`、説明は `--color-fg-muted`。
 * - 一致しない語を入力したときだけ `aria-invalid="true"` にし、説明文と状態バッジを danger 色にする。
 * - 状態（未入力 / 不一致 / 確認済み）は文字のバッジでも示し（色だけに頼らない）、`aria-live="polite"` で読み上げる。
 * - 入力欄は 44px（タッチ端末でも押しやすい高さ）。フォーカス中は枠線を danger 色にする。
 */
export function ExecutionConfirmationField({
  value,
  onChange,
  confirmed,
  expectedLabel,
  placeholder,
  helper,
  disabled = false,
  id: idProp,
  labels: labelOverrides,
  actions,
  className,
}: ExecutionConfirmationFieldProps) {
  const labels = { ...DEFAULT_EXECUTION_CONFIRMATION_LABELS, ...labelOverrides };
  const generatedId = useId();
  const id = idProp ?? generatedId;
  const helperId = `${id}-helper`;
  const status = executionConfirmationStatus(value, confirmed);
  const mismatch = status === "mismatch";
  // 「入力条件: {phrase}」の確認語の部分だけを等幅で出すため、前後に分ける。
  const tokenIndex = labels.expected.indexOf(PHRASE_TOKEN);
  const expectedPrefix = tokenIndex === -1 ? `${labels.expected} ` : labels.expected.slice(0, tokenIndex);
  const expectedSuffix = tokenIndex === -1 ? "" : labels.expected.slice(tokenIndex + PHRASE_TOKEN.length);

  return (
    <div
      className={cn("grid min-w-0 gap-2 rounded-md border border-border bg-surface-sunken p-3", className)}
      data-testid="execution-confirmation-field"
      data-confirmation-status={status}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        {/* 入力前から danger 色にしない（操作前のエラー表示に見えるため）。強調は状態バッジと入力欄のフォーカス色で行う。 */}
        <label htmlFor={id} className="text-sm font-semibold text-fg">
          {labels.label}
          {/* 入力側の required / aria-required で必須を伝えるので、バッジは二重に読み上げない。 */}
          <RequiredBadge label={labels.required} aria-hidden className="ml-2 align-middle" />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <span className="max-w-full rounded-md bg-surface px-2 py-1 font-sans text-xs text-fg">
            {expectedPrefix}
            {/* 確認語は識別子なので、入力する文字を読み違えないよう等幅で示す。 */}
            <span className="font-mono font-semibold [overflow-wrap:anywhere]">
              {identifierSegments(expectedLabel).map((segment, index) => (
                <Fragment key={index}>
                  {index > 0 && <wbr />}
                  {segment}
                </Fragment>
              ))}
            </span>
            {expectedSuffix}
          </span>
          <span
            data-surface-tint={surfaceTintFor(STATUS_CLASS[status])}
            className={cn(
              "inline-flex min-h-6 items-center rounded-full border px-2 py-0.5 text-xs font-semibold",
              STATUS_CLASS[status]
            )}
            aria-live="polite"
          >
            {labels[status]}
          </span>
        </div>
      </div>
      <input
        id={id}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        // 下の操作行（実行・取消）は lg なので、入力欄も lg（40px、タッチ端末は 44px。#613）。
        className="h-[var(--control-height-lg)] w-full rounded-control border border-border-control bg-surface px-3 py-2 text-sm outline-none transition-colors placeholder:text-fg-muted focus:border-danger-fg disabled:cursor-not-allowed disabled:bg-surface-hover disabled:text-fg-disabled"
        placeholder={placeholder ?? expectedLabel}
        disabled={disabled}
        required
        aria-required="true"
        aria-describedby={helperId}
        aria-invalid={mismatch ? "true" : undefined}
        autoCapitalize="off"
        autoCorrect="off"
        autoComplete="off"
        spellCheck={false}
      />
      <p id={helperId} className={cn("break-words text-xs leading-5", mismatch ? "text-danger-fg" : "text-fg-muted")}>
        {helper}
      </p>
      {actions ? (
        <div className="flex min-w-0 flex-col gap-[8px] border-t border-border pt-3 sm:flex-row sm:flex-wrap sm:items-center">
          {actions}
        </div>
      ) : null}
    </div>
  );
}
