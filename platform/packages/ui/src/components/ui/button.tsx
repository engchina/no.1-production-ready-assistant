import { cva, type VariantProps } from "class-variance-authority";
import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, KeyboardEvent, MouseEvent, Ref } from "react";

import { cn } from "../../lib/utils";
import { Spinner } from "./spinner";
import { Tooltip, type TooltipPlacement } from "./tooltip";

/*
 * 枠線の方針（docs/design-system/README.md §6 罫線）:
 *   primary / danger  1px の透明枠線を残す（secondary と並べた時の 1px ズレ防止）
 *   secondary         --color-border-control（3:1 必須。入力欄と同じ線）
 *   ghost             枠なし（透明）
 * 高さ・余白・角丸は px トークン（ルート非依存）。
 */
export const buttonVariants = cva(
  [
    "inline-flex max-w-full min-w-[32px] cursor-pointer items-center justify-center gap-[var(--button-gap)] overflow-hidden whitespace-nowrap",
    "rounded-control border border-transparent text-sm font-medium leading-5 transition-colors",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
    "disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-disabled disabled:text-fg-disabled disabled:shadow-none",
    // loading 中はネイティブの disabled を付けず aria-disabled にする（フォーカスを保つ）。見た目は disabled と同じ。
    "aria-disabled:cursor-not-allowed aria-disabled:border-border aria-disabled:bg-surface-disabled aria-disabled:text-fg-disabled aria-disabled:shadow-none",
    "aria-pressed:border-accent-fg aria-pressed:bg-accent-muted aria-pressed:text-fg aria-pressed:shadow-[inset_0_0_0_1px_var(--color-accent-fg)]",
    "[&>span]:min-w-0 [&>span]:truncate [&>svg]:block [&>svg]:shrink-0",
    // 強制カラーモードでは塗りが消えるため、輪郭を必ず出す
    "forced-colors:border-[CanvasText]",
  ],
  {
    variants: {
      variant: {
        primary:
          "bg-accent-emphasis text-fg-on-accent hover:enabled:not-aria-disabled:bg-accent-emphasis-hover active:enabled:not-aria-disabled:bg-accent-emphasis-active forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none",
        secondary:
          "border-border-control bg-surface text-fg hover:enabled:not-aria-disabled:bg-surface-hover active:enabled:not-aria-disabled:bg-[color-mix(in_srgb,var(--color-fg)_12%,var(--color-surface))]",
        ghost:
          "bg-transparent text-fg hover:enabled:not-aria-disabled:bg-surface-hover active:enabled:not-aria-disabled:bg-[color-mix(in_srgb,var(--color-fg)_12%,var(--color-surface))]",
        danger:
          "bg-danger-emphasis text-fg-on-emphasis hover:enabled:not-aria-disabled:bg-[color-mix(in_srgb,var(--color-danger-emphasis)_88%,#000)] active:enabled:not-aria-disabled:bg-[color-mix(in_srgb,var(--color-danger-emphasis)_76%,#000)] forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none",
      },
      size: {
        sm: "h-[var(--button-height-sm)] min-h-[var(--button-height-sm)] px-[var(--button-padding-sm)]",
        md: "h-[var(--button-height-md)] min-h-[var(--button-height-md)] px-[var(--button-padding-md)]",
        lg: "h-[var(--button-height-lg)] min-h-[var(--button-height-lg)] px-[var(--button-padding-lg)]",
      },
      iconOnly: { true: "aspect-square min-w-0 px-0", false: "" },
      touchTarget: { true: "h-[var(--control-height-touch)] min-h-[var(--control-height-touch)]", false: "" },
      /** secondary / ghost を破壊的な操作に使うときの文字色（塗りの danger より控えめ）。 */
      tone: {
        default: "",
        danger: "text-danger-fg hover:enabled:not-aria-disabled:bg-danger-subtle hover:enabled:not-aria-disabled:text-danger-fg",
      },
    },
    compoundVariants: [{ variant: "secondary", tone: "danger", className: "border-danger-fg" }],
    defaultVariants: { variant: "primary", size: "md", iconOnly: false, touchTarget: false, tone: "default" },
  }
);

type ButtonVariantProps = VariantProps<typeof buttonVariants>;

/** ボタンのアイコンとスピナーの寸法（px）。sm / md / lg で共通（README §4「Button」、--icon-md）。 */
export const BUTTON_ICON_SIZE = 16;

/**
 * variant と tone の組み合わせ。`variant="danger"`（赤塗り）に `tone="danger"`（赤文字）を重ねると
 * 赤地に赤文字になり読めないため、型で禁止する。tone="danger" は secondary / ghost（と primary の既定）に使う。
 */
export type ButtonVariantToneProps =
  | { variant: "danger"; tone?: "default" | null }
  | { variant?: Exclude<ButtonVariantProps["variant"], "danger">; tone?: ButtonVariantProps["tone"] };

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  Pick<ButtonVariantProps, "size"> &
  ButtonVariantToneProps & {
    /** 先頭アイコン（lucide-react のコンポーネント。例: `icon={Upload}`）。子要素にアイコンを直接書かない。 */
    icon?: LucideIcon;
    /** 方向・開閉・外部リンクのみ（ChevronRight / ChevronDown / ExternalLink）。アイコンを 2 つ持たせない。 */
    trailingIcon?: LucideIcon;
    /**
     * アイコンだけのボタン。`aria-label` を必ず付ける。
     * 既定で `aria-label` と同じ文言の Tooltip をホバーとキーボードのフォーカスで出す（`tooltip` を参照）。
     */
    iconOnly?: boolean;
    /**
     * ホバーとキーボードのフォーカスで出す短い説明（翻訳済み）。`iconOnly` のときの既定は `aria-label` と同じ文言、
     * それ以外の既定は出さない。`false` で出さない。`aria-label` と違う文言（ショートカットキーを添える等）は
     * `aria-describedby` で説明として結び付き、同じ文言なら結び付けない（二重に読み上げない）。
     * HTML の `title` 属性は使わない（キーボード・タッチで出ず、表示まで長く待つ）。Tooltip を出すときは `title` を無視する。
     */
    tooltip?: string | false;
    /** Tooltip を出す側（既定は上。入らなければ反転する）。 */
    tooltipPlacement?: TooltipPlacement;
    /**
     * マウス環境でも 44px の高さにする場合だけ true（タッチ端末では sm / md / lg とも 44px になる）。
     * **製品では使わない**（adherence の lint が検出する。#613）。`packages/ui` の部品の、画面の端に置く
     * 閉じる・メニューのボタン（Toast・Banner・AppShell の上端のバー）だけが使う。同じ行の入力欄とは `size` でそろえる。
     */
    touchTarget?: boolean;
    /**
     * true で先頭アイコンがスピナーに置き換わる（ラベル・幅は変わらない）。`icon` と一緒に使う。
     * ネイティブの `disabled` は付けず、`aria-disabled="true"` と `aria-busy="true"` を付けてクリック・submit を止める。
     * フォーカス中のボタンに `disabled` を付けるとフォーカスが `body` へ外れるため（キーボード・支援技術の利用者が位置を失う）。
     */
    loading?: boolean;
    /** トグルボタンとして使う場合の押下状態（`aria-pressed`）。 */
    pressed?: boolean;
    ref?: Ref<HTMLButtonElement>;
  };

/**
 * loading 中のクリックを止める。既定動作（form の submit、Enter / Space の暗黙のクリックによる submit を含む）を
 * `preventDefault` で取り消し、呼び出し側の `onClick` を呼ばない。親への伝播も止め、行クリック等の二重起動を防ぐ。
 */
export function suppressLoadingClick(event: Pick<MouseEvent, "preventDefault" | "stopPropagation">) {
  event.preventDefault();
  event.stopPropagation();
}

/**
 * loading 中のキー操作。Enter / Space（ボタンを押すキー）は既定動作ごと止めて呼び出し側に渡さない。
 * Tab・Escape など移動や閉じる操作はそのまま渡す（フォーカスを保ったまま操作を続けられるように）。
 */
export function loadingKeyDownHandler(onKeyDown?: (event: KeyboardEvent<HTMLButtonElement>) => void) {
  return (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      return;
    }
    onKeyDown?.(event);
  };
}

/**
 * RAG / NL2SQL / Agent 共通のボタン。4 バリアント × 3 サイズ（32 / 36 / 40px。入力欄・選択欄と同じ --control-height-*）。
 * 同じ行に並べる入力欄・選択欄とは同じ `size` にする（README §4「操作部品の高さと幅」、#613）。
 *
 * 非同期の操作を起こすボタンは必ず `icon` を持たせる（adherence の lint が検出する）。loading 中は先頭アイコンが
 * スピナーに置き換わるため幅が変わらない（アイコンが無いとスピナーの分だけ幅が広がる）。
 * ラベルは「実行中…」等に差し替えない。
 * loading 中は `aria-disabled` でクリックと submit を止め、フォーカスはボタンに残す（`disabled` prop はネイティブのまま）。
 * `type` の既定値はブラウザ既定（フォーム内では submit）のまま。既存フォームの暗黙 submit を壊さないため。
 */
export function Button({
  className,
  variant,
  size,
  tone,
  icon: Icon,
  trailingIcon: TrailingIcon,
  iconOnly = false,
  tooltip,
  tooltipPlacement,
  touchTarget = false,
  loading,
  pressed,
  disabled,
  children,
  ref,
  onClick,
  onKeyDown,
  onPointerDown,
  onMouseDown,
  ...props
}: ButtonProps) {
  // disabled が優先。loading だけのときはフォーカスを保つため aria-disabled にする。
  const busy = Boolean(loading) && !disabled;
  const ariaLabel = props["aria-label"];
  const tooltipText =
    tooltip === false
      ? undefined
      : tooltip || (iconOnly && typeof ariaLabel === "string" && ariaLabel.trim() ? ariaLabel : undefined);
  const button = (
    <button
      ref={ref}
      className={cn(
        buttonVariants({ variant, size, tone, iconOnly, touchTarget }),
        // 旧スタイル（子に直接アイコンを書く）互換: loading 中は子の svg を隠してスピナーと二重にしない
        loading && "[&>svg:not(.animate-spin)]:hidden",
        className
      )}
      aria-busy={loading || undefined}
      aria-pressed={pressed}
      {...props}
      // Tooltip を出すときは title を付けない（ブラウザの吹き出しと二重に出る）。
      title={tooltipText ? undefined : props.title}
      disabled={disabled || undefined}
      aria-disabled={busy ? true : props["aria-disabled"]}
      onClick={busy ? suppressLoadingClick : onClick}
      // 押下で処理を始める呼び出し側（onPointerDown で送信する検索ボタン等）も、loading 中は呼ばない。
      onPointerDown={busy ? undefined : onPointerDown}
      onMouseDown={busy ? undefined : onMouseDown}
      onKeyDown={busy ? loadingKeyDownHandler(onKeyDown) : onKeyDown}
    >
      {/*
        先頭スロットは sm / md / lg とも 16px（アイコンもスピナーも同じ寸法なので loading で幅が変わらない）。
        スピナーは全周トラック付き（回転してもシルエットが変わらない）。loading 中の地と文字は disabled と同じ
        （surface-disabled / fg-disabled。ライトで 2.82:1）なので、処理中を示すスピナーだけは fg-muted（4.39:1）にする。
      */}
      {loading ? (
        <Spinner size={BUTTON_ICON_SIZE} className="text-fg-muted" />
      ) : Icon ? (
        <Icon size={BUTTON_ICON_SIZE} aria-hidden />
      ) : null}
      {children}
      {TrailingIcon && !loading ? <TrailingIcon size={BUTTON_ICON_SIZE} aria-hidden /> : null}
    </button>
  );
  return tooltipText ? (
    <Tooltip content={tooltipText} placement={tooltipPlacement}>
      {button}
    </Tooltip>
  ) : (
    button
  );
}
