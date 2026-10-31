import { type LucideIcon, X } from "lucide-react";
import {
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  useCallback,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { cn } from "../../lib/utils";

import { useFieldWidthScope } from "./field-width-scope";
import { Button } from "./button";
import {
  CONTROL_MIN_HEIGHT_CLASS,
  type ControlSize,
  type FieldWidth,
  fieldControlClass,
  labeledFieldWidthClasses,
} from "./control-size";
import { FieldError } from "./field-error";
import { usePlaceholderFit } from "./placeholder-fit";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";

export { fieldControlClass };

/** 高さ（`ControlSize`）。既定 md（36px）。同じ行の Button と同じ size にする（#613）。 */
export type TextFieldSize = ControlSize;

/**
 * 先頭アイコンがあるときの左の余白 = アイコンの左の位置（px-3）+ アイコン（16px）+ Button のアイコンと文字の間隔（8px）。
 * 文字の開始位置を、アイコン付きの Button と同じ間隔にそろえる。
 */
export const TEXT_FIELD_LEADING_PADDING_CLASS = "pl-[calc(var(--space-3)+var(--icon-md)+var(--button-gap))]";
/** 後置スロットの幅を測る前（SSR・初回の描画）の右の余白。四角の iconOnly ボタン 1 つ分（入力欄の高さ）。 */
export const TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS = "pr-[var(--field-height)]";

/**
 * 入力欄の右端（`trailing` のスロット）に置く ghost の iconOnly ボタンの形（消去・一覧の開閉）。
 * スロットは入力欄の枠線まで含めた高さなので、ボタンは入力欄と同じ高さ（md 36px・タッチ端末 44px）の正方形になる。
 * 地は枠線の内側だけに塗り（bg-clip-padding）、ホバーの地が入力欄の枠線に重ならない。
 */
export const TEXT_FIELD_TRAILING_BUTTON_CLASS =
  "h-full min-h-0 rounded-l-none rounded-r-control bg-clip-padding";

/** 制御された value が空でないか（クリアボタンを出すか）。 */
export function hasTextValue(value: unknown) {
  if (value == null) return false;
  if (Array.isArray(value)) return value.length > 0;
  return String(value) !== "";
}

/**
 * Escape で入力を消すか。消す値があり、onClear があるときだけ（空のときは囲むダイアログ等の Escape に任せる）。
 * IME の変換中（isComposing）は変換の取り消しなので消さない。
 */
export function shouldClearOnEscape({
  key,
  value,
  hasClear,
  isComposing = false,
}: {
  key: string;
  value: unknown;
  hasClear: boolean;
  isComposing?: boolean;
}) {
  return hasClear && key === "Escape" && !isComposing && hasTextValue(value);
}

/**
 * 入力を消して入力欄にフォーカスを戻す。クリアボタンは値が空になると消えるため、
 * フォーカスを戻さないと body へ外れてキーボードの利用者が位置を失う。
 */
export function clearTextField(onClear: () => void, input: Pick<HTMLInputElement, "focus"> | null) {
  onClear();
  input?.focus();
}

export type TextFieldProps = {
  id: string;
  /** 翻訳済みラベル。 */
  label: string;
  /**
   * ラベルを画面に出さず読み上げだけにする（`sr-only`）。検索欄のように、アイコンとプレースホルダで目的が分かる場所だけに使う。
   * フォームの入力欄では使わない（プレースホルダをラベルの代わりにしない）。
   */
  labelHidden?: boolean;
  /** 翻訳済みの補足（任意）。ドキュメントへのリンクなどを含めてよい。 */
  helper?: ReactNode;
  /** 翻訳済みのエラー（任意）。指定時は aria-invalid と枠線の色が変わる。 */
  error?: string;
  /** 必須であることを aria-required と中立色の RequiredBadge「必須」で伝える。ネイティブの required 検証は行わない（検証はアプリ側）。 */
  required?: boolean;
  /** 必須バッジの文言。既定「必須」。条件付きの必須（例:「OCI 運用時必須」）だけ上書きする。 */
  requiredLabel?: string;
  className?: string;
  inputClassName?: string;
  /**
   * 高さ（既定 md = 36px）。同じ行に並べる Button と同じ size にする（lg の Button の隣は lg）。
   * タッチ端末では sm / md / lg とも 44px になる（Button と同じ）。
   */
  size?: TextFieldSize;
  /** 幅（入る値の長さで選ぶ。既定は親の幅いっぱい）。sm（640px）未満は全幅。 */
  width?: FieldWidth;
  /** 先頭のアイコン（lucide-react のコンポーネント。例: `leadingIcon={Search}`）。16px・読み上げない。 */
  leadingIcon?: LucideIcon;
  /** 末尾の任意の要素（単位・件数・ボタン）。値を消すボタンは `onClear` を使う。 */
  trailing?: ReactNode;
  /** 値を消す操作。渡すと、値があるときだけ末尾にクリアボタンを出し、Escape でも消す。`clearLabel` と一緒に渡す。 */
  onClear?: () => void;
  /** クリアボタンの読み上げ名と Tooltip（翻訳済み。例:「検索語をクリア」）。 */
  clearLabel?: string;
  onValueChange?: (value: string) => void;
  /**
   * 入力の候補（翻訳しないデータの値。例: 保存済みの分類の値）。渡すと、候補から選べて候補に無い値も
   * そのまま入力できる入力欄（ネイティブの `<datalist>`。読み上げは role=combobox）になる。#547
   */
  suggestions?: readonly string[];
  ref?: Ref<HTMLInputElement>;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "required" | "size" | "list">;

/**
 * ラベル・補足・エラー付きの 1 行入力。API は SelectField と揃える。
 * 必須はネイティブの required 検証にしない（未入力でも保存を許す画面があるため。SelectField と同じ）。
 * 値の扱いはネイティブ input と同じ（`value` / `onChange`）。文字列だけ欲しい場合は `onValueChange`。
 *
 * 検索欄などの「アイコン付きの入力欄」もこれで作る（#384。製品で `relative` + 絶対配置のアイコン + `pl-9` を手書きしない）。
 * - `leadingIcon`: 先頭の 16px のアイコン。読み上げず、ポインタを透過する（押すと下の入力欄にフォーカスが入る）。
 * - `onClear` + `clearLabel`: 値があるときだけ末尾に「クリア」の iconOnly ボタンを出す（入力欄の直後の Tab 順。
 *   押すと値を消して入力欄に戻る）。Escape でも消える（値があるときだけ。空なら Escape は囲むダイアログ等に渡す）。
 *   value を制御して使う。
 * - `trailing`: 末尾の任意の要素（単位・件数・ボタンなど）。枠線の内側の右端に置き、実際の幅の分だけ文字の右の余白を空ける。
 * - `suggestions`: 候補を選べる自由入力（editable combobox）。独自の listbox ではなくネイティブの `<datalist>` にする
 *   （キー操作・読み上げ・モバイルの候補表示・`color-scheme` によるダークテーマを OS / ブラウザに任せる。
 *   `type="date"` の日付選択と同じ扱い）。ブラウザの入力履歴を候補に混ぜないよう既定で `autoComplete="off"`。
 */
export function TextField({
  id,
  label,
  labelHidden = false,
  helper,
  error,
  required,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  className,
  inputClassName,
  size = "md",
  width,
  leadingIcon: LeadingIcon,
  trailing,
  onClear,
  clearLabel,
  onValueChange,
  suggestions,
  onChange,
  onKeyDown,
  type = "text",
  ref,
  ...props
}: TextFieldProps) {
  const widthScope = useFieldWidthScope();
  const reactId = useId();
  const hintId = `${id}-${reactId}-hint`;
  const errorId = `${id}-${reactId}-error`;
  const suggestionListId = suggestions?.length ? `${id}-${reactId}-suggestions` : undefined;
  const describedBy =
    [helper ? hintId : "", error ? errorId : "", props["aria-describedby"] ?? ""].filter(Boolean).join(" ") ||
    undefined;

  const inputRef = useRef<HTMLInputElement | null>(null);
  // placeholder が欄に収まらないときは目印（data-placeholder-clipped）と title の全文を付ける（#1891）。
  const placeholderFit = usePlaceholderFit<HTMLInputElement>(ref, props.placeholder, props.title);
  const placeholderRef = placeholderFit.ref;
  const setInputRef = useCallback(
    (node: HTMLInputElement | null) => {
      inputRef.current = node;
      placeholderRef(node);
    },
    [placeholderRef]
  );

  const showClear = Boolean(onClear) && !props.disabled && !props.readOnly && hasTextValue(props.value);
  const hasTrailing = showClear || (trailing !== undefined && trailing !== null && trailing !== false);

  // 後置スロットの実際の幅だけ文字の右の余白を空ける（件数・単位など幅が決まらない要素も入れられるように）。
  const trailingRef = useRef<HTMLDivElement | null>(null);
  const [trailingWidth, setTrailingWidth] = useState(0);
  useLayoutEffect(() => {
    const node = trailingRef.current;
    if (!hasTrailing || !node) {
      setTrailingWidth(0);
      return undefined;
    }
    const measure = () => setTrailingWidth(Math.ceil(node.getBoundingClientRect().width));
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasTrailing]);

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    onKeyDown?.(event);
    if (event.defaultPrevented || !onClear) return;
    if (
      shouldClearOnEscape({
        key: event.key,
        value: props.value,
        hasClear: true,
        isComposing: event.nativeEvent.isComposing,
      })
    ) {
      // 囲むダイアログ・メニューを閉じない（1 回目の Escape は入力を消すだけ）。
      event.preventDefault();
      event.stopPropagation();
      onClear();
    }
  }

  const widthClasses = labeledFieldWidthClasses(width, widthScope, Boolean(helper || error));

  return (
    // 段の幅（width）は入力欄に付け、ラベルと必須の印は入力欄の幅で折り返さない（#1746）。
    // data-field-label: 上にラベルを出している欄の目印（一覧のツールバーが、移行前の上のラベルの絞り込みを下端でそろえる。#1933）。
    <div className={cn("space-y-1.5", widthClasses.box, className)} data-field-label={labelHidden ? undefined : "top"}>
      <label
        htmlFor={id}
        className={cn("flex items-center gap-2 text-sm font-medium text-fg", labelHidden && "sr-only")}
      >
        {label}
        {required && requiredLabel ? <RequiredBadge label={requiredLabel} aria-hidden /> : null}
      </label>
      <div className={cn("relative", widthClasses.control)}>
        <input
          ref={setInputRef}
          id={id}
          type={type}
          aria-required={required || undefined}
          aria-invalid={Boolean(error)}
          list={suggestionListId}
          autoComplete={suggestionListId ? "off" : undefined}
          {...props}
          title={placeholderFit.title}
          data-placeholder-clipped={placeholderFit.clipped || undefined}
          aria-describedby={describedBy}
          onChange={(event) => {
            onChange?.(event);
            onValueChange?.(event.target.value);
          }}
          onKeyDown={handleKeyDown}
          className={cn(
            fieldControlClass,
            // 先頭アイコンの色を disabled に合わせるため peer にする（アイコンは入力欄の後ろに置く）。
            "peer",
            CONTROL_MIN_HEIGHT_CLASS[size],
            LeadingIcon && TEXT_FIELD_LEADING_PADDING_CLASS,
            hasTrailing && TEXT_FIELD_TRAILING_FALLBACK_PADDING_CLASS,
            error ? "border-danger-fg" : "border-border-control",
            inputClassName
          )}
          style={hasTrailing && trailingWidth > 0 ? { ...props.style, paddingRight: trailingWidth } : props.style}
        />
        {LeadingIcon ? (
          <LeadingIcon
            size={16}
            aria-hidden
            data-text-field-slot="leading"
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-muted peer-disabled:text-fg-disabled"
          />
        ) : null}
        {hasTrailing ? (
          <div
            ref={trailingRef}
            data-text-field-slot="trailing"
            // 入力欄の枠線まで含めた高さに置き、ボタンを入力欄と同じ高さ（md 36px・タッチ端末 44px）にする（SecretField と同じ）。
            // ボタンの枠線は透明で、地は枠線の内側だけに塗る（bg-clip-padding）ので、ホバーの地は入力欄の枠線に重ならない。
            className="absolute inset-y-0 right-0 flex items-center"
          >
            {trailing}
            {showClear ? (
              <Button
                type="button"
                variant="ghost"
                iconOnly
                icon={X}
                aria-label={clearLabel}
                aria-controls={id}
                // 押しても入力欄からフォーカスを外さない（blur で確定する検索欄が、消す前の値を確定しないように）。
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => clearTextField(onClear as () => void, inputRef.current)}
                // 入力欄の高さに合わせた正方形。外側の角だけ入力欄の角丸に合わせる。
                // ボタンの上・右・下の枠線は入力欄の枠線にちょうど重なる（強制カラーモードで CanvasText に塗られても
                // 入力欄の枠線と同じ位置なので二重にならず、左の区切りだけが増える）。
                className={TEXT_FIELD_TRAILING_BUTTON_CLASS}
              />
            ) : null}
          </div>
        ) : null}
        {suggestionListId ? (
          <datalist id={suggestionListId}>
            {suggestions?.map((suggestion) => <option key={suggestion} value={suggestion} />)}
          </datalist>
        ) : null}
      </div>
      {helper ? (
        <p id={hintId} className={cn("text-xs leading-relaxed text-fg-muted", widthClasses.text)}>
          {helper}
        </p>
      ) : null}
      <FieldError id={errorId} message={error} className={widthClasses.text} />
    </div>
  );
}
