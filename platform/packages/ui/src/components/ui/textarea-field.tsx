import { type CSSProperties, type ReactNode, type Ref, type TextareaHTMLAttributes, useId } from "react";

import { cn } from "../../lib/utils";

import { useFieldWidthScope } from "./field-width-scope";
import { FieldError } from "./field-error";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";
import { type FieldWidth, fieldControlClass, labeledFieldWidthClasses } from "./control-size";
import { usePlaceholderFit } from "./placeholder-fit";

/** 文字数の既定の表示。上限があれば「12 / 1,000」、無ければ「12 文字」。 */
export function defaultTextareaCount(count: number, maxLength?: number) {
  return maxLength !== undefined ? `${count.toLocaleString()} / ${maxLength.toLocaleString()}` : `${count.toLocaleString()} 文字`;
}

export type TextareaFieldProps = {
  id: string;
  /** 翻訳済みラベル。 */
  label: string;
  /** ラベルを画面に出さず読み上げだけにする（`sr-only`）。チャットの入力欄など、目的が周りから分かる場所だけに使う。 */
  labelHidden?: boolean;
  /** 翻訳済みの補足（任意）。 */
  helper?: ReactNode;
  /** 翻訳済みのエラー（任意）。指定時は aria-invalid と枠線の色が変わる。 */
  error?: string;
  /** 必須であることを aria-required と中立色の RequiredBadge「必須」で伝える。ネイティブの required 検証は行わない。 */
  required?: boolean;
  /** 必須バッジの文言。既定「必須」。条件付きの必須（例:「「違う」のとき必須」）だけ上書きする。 */
  requiredLabel?: string;
  /**
   * 必須を入力欄の aria-required で伝えるか（既定 true。タグは二重に読ませない）。条件付きの必須のように
   * 常に必須ではない欄は false にし、aria-required を付けずにタグ（`requiredLabel`）をラベルの一部として読ませる
   * （FieldLabel の同名の prop と同じ）。
   */
  requiredAnnouncedByControl?: boolean;
  className?: string;
  textareaClassName?: string;
  /** SQL・JSON・プロンプトの雛形など、等幅で見せる内容（`--font-mono`・12px）。 */
  monospace?: boolean;
  /**
   * 面。`code` は暗いコードの面（`data-surface="code"`。生成した SQL の表示など）で、等幅にし、
   * read-only でも面の地（コードの地）のままにする（`bg-surface-sunken` はコードの面で定義し直されないため）。
   */
  surface?: "default" | "code";
  /**
   * 幅（既定は親の幅いっぱい = `full`。文章・SQL・プロンプト）。短い文（コメント・理由など 1〜2 文）は `lg` にする
   * （#1891）。sm（640px）未満は全幅。
   */
  width?: FieldWidth;
  /** 高さの変更（既定 vertical = 縦だけ伸ばせる。読み取り専用のプレビューなどは none）。 */
  resize?: "vertical" | "none";
  /**
   * 文字数を右下に出す。`maxLength` があれば「現在 / 上限」。`true` は既定の表示（`defaultTextareaCount`）、
   * 関数を渡すと翻訳済みの文言にできる。読み上げは補足と同じく欄の説明（aria-describedby）として結び付ける。
   */
  showCount?: boolean | ((count: number, maxLength?: number) => string);
  onValueChange?: (value: string) => void;
  ref?: Ref<HTMLTextAreaElement>;
} & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id" | "required">;

/**
 * ラベル・補足・エラー・文字数付きの複数行の入力（#584）。見た目と API は TextField とそろえる
 * （枠線・角丸・フォーカス・disabled・read-only・必須のタグ・エラーの位置）。
 * 必須はネイティブの required 検証にしない（検証はアプリ側。TextField と同じ）。
 * 値の扱いはネイティブの textarea と同じ（`value` / `onChange`）。文字列だけ欲しい場合は `onValueChange`。
 */
export function TextareaField({
  id,
  label,
  labelHidden = false,
  helper,
  error,
  required,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  requiredAnnouncedByControl = true,
  className,
  textareaClassName,
  monospace = false,
  surface = "default",
  width,
  resize = "vertical",
  showCount = false,
  onValueChange,
  onChange,
  rows = 3,
  ref,
  ...props
}: TextareaFieldProps) {
  const widthScope = useFieldWidthScope();
  const reactId = useId();
  const hintId = `${id}-${reactId}-hint`;
  const errorId = `${id}-${reactId}-error`;
  const countId = `${id}-${reactId}-count`;
  const countText = showCount
    ? (typeof showCount === "function" ? showCount : defaultTextareaCount)(
        String(props.value ?? props.defaultValue ?? "").length,
        props.maxLength
      )
    : undefined;
  const describedBy =
    [helper ? hintId : "", countText ? countId : "", error ? errorId : "", props["aria-describedby"] ?? ""]
      .filter(Boolean)
      .join(" ") || undefined;
  const widthClasses = labeledFieldWidthClasses(width, widthScope, Boolean(helper || countText || error));
  const placeholderFit = usePlaceholderFit<HTMLTextAreaElement>(ref, props.placeholder, props.title);
  // 半端な行数（3.5 など。#2008）はネイティブの rows で表せないので、行の高さ（lh）から高さを決める。
  // 入りきらない次の行が半分見え、続きがあることが分かる。rows には切り上げた値を渡す（lh の無い環境の高さ）。
  const fractionalRows = !Number.isInteger(rows);

  return (
    <div className={cn("space-y-1.5", widthClasses.box, className)}>
      <label
        htmlFor={id}
        className={cn("flex items-center gap-2 text-sm font-medium text-fg", labelHidden && "sr-only")}
      >
        {label}
        {required && requiredLabel ? <RequiredBadge label={requiredLabel} aria-hidden={requiredAnnouncedByControl} /> : null}
      </label>
      <textarea
        ref={placeholderFit.ref}
        id={id}
        rows={Math.ceil(rows)}
        data-rows={fractionalRows ? rows : undefined}
        data-surface={surface === "code" ? "code" : undefined}
        aria-required={(required && requiredAnnouncedByControl) || undefined}
        aria-invalid={Boolean(error)}
        {...props}
        style={fractionalRows ? ({ ...props.style, "--textarea-rows": rows } as CSSProperties) : props.style}
        title={placeholderFit.title}
        data-placeholder-clipped={placeholderFit.clipped || undefined}
        aria-describedby={describedBy}
        onChange={(event) => {
          onChange?.(event);
          onValueChange?.(event.target.value);
        }}
        className={cn(
          fieldControlClass,
          "block py-2 leading-relaxed",
          widthClasses.control,
          resize === "none" ? "resize-none" : "resize-y",
          // 高さ = 行数 × 行の高さ + 上下の余白（py-2）+ 上下の枠線（border 1px）。
          fractionalRows && "h-[calc(var(--textarea-rows)*1lh+1rem+2px)]",
          (monospace || surface === "code") && "font-mono text-xs",
          surface === "code" && "read-only:bg-surface",
          error ? "border-danger-fg" : "border-border-control",
          textareaClassName
        )}
      />
      {helper || countText ? (
        <div className={cn("flex items-start justify-between gap-3", widthClasses.text)}>
          {helper ? (
            <p id={hintId} className="min-w-0 text-xs leading-relaxed text-fg-muted">
              {helper}
            </p>
          ) : null}
          {countText ? (
            <p id={countId} className="ml-auto shrink-0 text-xs leading-relaxed tabular-nums text-fg-muted">
              {countText}
            </p>
          ) : null}
        </div>
      ) : null}
      <FieldError id={errorId} message={error} className={widthClasses.text} />
    </div>
  );
}
