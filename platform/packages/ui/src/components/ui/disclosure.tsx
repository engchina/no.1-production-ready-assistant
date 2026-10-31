import type { LucideIcon } from "lucide-react";
import {
  useState,
  type DetailsHTMLAttributes,
  type HTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type SyntheticEvent,
} from "react";

import { cn } from "../../lib/utils";
import { DisclosureChevron } from "./disclosure-chevron";
import { TEXT_ICON_SIZE } from "./text-icon";

export type DisclosureVariant = "card" | "plain";
export type DisclosureSurface = "surface" | "sunken";
export type DisclosureSize = "md" | "sm";
export type DisclosureTone = "neutral" | "warning" | "danger";

const TONE_CLASS: Record<Exclude<DisclosureTone, "neutral">, string> = {
  warning: "border-warning-border bg-warning-subtle text-warning-fg",
  danger: "border-danger-border bg-danger-subtle text-danger-fg",
};
const TONE_BORDER: Record<Exclude<DisclosureTone, "neutral">, string> = {
  warning: "border-warning-border",
  danger: "border-danger-border",
};

export interface DisclosureProps
  extends Omit<
    DetailsHTMLAttributes<HTMLDetailsElement>,
    "open" | "onToggle" | "title" | "children"
  > {
  /** 見出し（summary）の文言。押せる領域は見出しの行全体。 */
  summary: ReactNode;
  /** 見出しの先頭のアイコン（lucide-react、16px / sm は 14px）。意味を足すときだけ付ける。 */
  icon?: LucideIcon;
  /** 見出しの下の補足（card のみ）。 */
  description?: ReactNode;
  /** 見出しと Chevron の間に置く件数・状態バッジなど。操作できる要素（ボタン・リンク）は置かない。 */
  meta?: ReactNode;
  /**
   * 見出しの行の下に、見出しの行の全幅で出す内容（押せる見出しの中。開閉にかかわらず見える。#2426）。処理の経過の
   * 実行中の遅延・再接続の案内など、見出しに属する短い補足に使う。補足（`meta`）の列に押し込まず全幅で折り返す。
   * 操作できる要素は置かない。
   */
  summaryFooter?: ReactNode;
  /** 受控の開閉状態。渡すときは onOpenChange も渡す。 */
  open?: boolean;
  /** 非受控の初期状態。 */
  defaultOpen?: boolean;
  /** 開閉が変わったとき（クリック・Enter / Space・ページ内検索でブラウザが開いたとき）。 */
  onOpenChange?: (open: boolean) => void;
  /**
   * card（既定）: 枠線付きの面。開くと見出しと内容の間に区切り線を出す。
   * plain: 枠なし。見出しの直後に Chevron を置き、内容はその下に続ける（回答の根拠・表のセルの中など）。
   */
  variant?: DisclosureVariant;
  /** card の地。既定 surface。面の上に重ねるときは sunken。 */
  surface?: DisclosureSurface;
  /**
   * card の状態色（警告の一覧・危険な操作の区画）。面・枠線・文字・Chevron が状態色になる。
   * 内容だけを通常の面に置くときは contentClassName に `rounded-b-md bg-surface` を渡す。
   * 状態は色だけで伝えない（見出しの文言で伝える）。
   */
  tone?: DisclosureTone;
  /** 見出しの文字の大きさ。md = 14px、sm = 12px（密な表・補足の中）。 */
  size?: DisclosureSize;
  /** summary に渡す属性（data-testid など）。 */
  summaryProps?: HTMLAttributes<HTMLElement> & { [key: `data-${string}`]: string | undefined };
  /** summary の見た目の局所的な調整（tone 付きの面の hover を消す等）。 */
  summaryClassName?: string;
  /** 内容の領域の class。card の既定は区切り線 + `p-3`、plain は `pt-2`。 */
  contentClassName?: string;
  children?: ReactNode;
}

/**
 * Disclosure（開閉できる領域）。ネイティブの `<details>` / `<summary>` を包む（#397）。
 *
 * - 見出しの行全体が押せる。Enter / Space・ページ内検索での自動展開・読み上げの「展開 / 折りたたみ」は
 *   ブラウザ標準の `<details>` の意味をそのまま使う（APG Disclosure パターンと同じ状態が伝わる）。
 * - 開閉状態は必ず `DisclosureChevron`（折りたたみ = 右向き、展開 = 下向き。reduced-motion で回転を止める）で示す。
 *   ブラウザ標準の三角（`::marker`）は Chevron と二重になるため消す。
 * - Chevron の向きは CSS の祖先（`group-open`）ではなく部品の状態から決める。入れ子の Disclosure で
 *   外側が開いているだけで内側の Chevron が開いた向きになる誤りを防ぐため。
 * - 受控（`open` + `onOpenChange`）でも非受控（`defaultOpen`）でも、summary のクリックは部品が状態を切り替える
 *   （ブラウザの切り替えを止めて React の状態を正本にする）。ページ内検索でブラウザが開いたときは toggle で同期する。
 */
export function Disclosure({
  summary,
  icon: Icon,
  description,
  meta,
  summaryFooter,
  open: openProp,
  defaultOpen = false,
  onOpenChange,
  variant = "card",
  surface = "surface",
  tone = "neutral",
  size = "md",
  summaryProps,
  summaryClassName,
  contentClassName,
  className,
  children,
  ...props
}: DisclosureProps) {
  const [openState, setOpenState] = useState(defaultOpen);
  const controlled = openProp !== undefined;
  const open = controlled ? openProp : openState;
  const card = variant === "card";
  const toned = card && tone !== "neutral";
  // アイコン（先頭のアイコン・開閉の印）は見出しの文字の大きさにそろえる（sm 12px / md 14px。#2371）。
  const iconSize = TEXT_ICON_SIZE[size === "sm" ? "xs" : "sm"];

  function setOpen(next: boolean) {
    if (!controlled) setOpenState(next);
    onOpenChange?.(next);
  }

  function handleSummaryClick(event: MouseEvent<HTMLElement>) {
    summaryProps?.onClick?.(event);
    if (event.defaultPrevented) return;
    event.preventDefault();
    setOpen(!open);
  }

  function handleToggle(event: SyntheticEvent<HTMLDetailsElement>) {
    // ページ内検索（hidden until found）などでブラウザが開いたときだけ届く差分を、状態へ戻す。
    const next = event.currentTarget.open;
    if (next !== open) setOpen(next);
  }

  return (
    <details
      {...props}
      open={open}
      onToggle={handleToggle}
      data-state={open ? "open" : "closed"}
      // 状態色の面の上の補足（description）を 4.5:1 にする（tokens/colors.css。#1671）。
      data-surface-tint={toned ? tone : undefined}
      className={cn(
        "min-w-0",
        card && "rounded-md border",
        card &&
          (toned
            ? TONE_CLASS[tone as Exclude<DisclosureTone, "neutral">]
            : ["border-border", surface === "sunken" ? "bg-surface-sunken" : "bg-surface"]),
        className
      )}
    >
      <summary
        {...summaryProps}
        onClick={handleSummaryClick}
        className={cn(
          // アイコンは見出しの 1 行目の中心にそろえる（items-start + 行の高さの箱。折り返した見出し・補足のある見出しでも
          // 1 行目に合わせる）。1 行の見出しは、行の高さを引いた残りを上下の余白に分けて最小の高さの中央に置く（#2371）。
          "cursor-pointer list-none items-start transition-colors duration-150 motion-reduce:transition-none [&::-webkit-details-marker]:hidden",
          // 状態色の面では灰色の hover を重ねず、文字色（状態色）を薄く重ねる。
          toned ? "hover:bg-current/5" : "text-fg hover:bg-surface-hover",
          size === "sm" ? "text-xs" : "text-sm",
          card
            ? [
                "flex min-h-[var(--button-height-lg)] gap-2 px-3 py-[max(calc((var(--button-height-lg)-1lh)/2),0.5rem)] font-semibold",
                // 枠の内側に描く（枠線とフォーカスの輪郭を重ねない）。角は枠に合わせる。
                "rounded-md focus-visible:-outline-offset-2",
                open && "rounded-b-none",
              ]
            : "-mx-1 flex w-fit max-w-full min-h-[var(--button-height-sm)] gap-1.5 rounded-sm px-1 py-[max(calc((var(--button-height-sm)-1lh)/2),0px)] font-medium",
          // 見出しの下の全幅の行（summaryFooter）は折り返した 2 行目に置く（行の間は空けない）。
          summaryFooter != null && "flex-wrap gap-y-0",
          summaryClassName,
          summaryProps?.className
        )}
      >
        {Icon ? (
          <span className={cn("pr-icon-line", !toned && "text-fg-muted")}>
            <Icon size={iconSize} aria-hidden />
          </span>
        ) : null}
        {/* 補足（meta）・全幅の 2 行目があるときは見出しが残りの幅を取り（flex-1 は basis 0。折り返しで補足を 2 行目へ
            押し出さない）、補足を右端に置く（#2375 / #2426）。 */}
        <span className={cn("min-w-0 break-words", (card || meta || summaryFooter != null) && "flex-1")}>
          {summary}
          {card && description ? (
            <span className="mt-0.5 block text-xs font-normal leading-relaxed text-fg-muted">
              {description}
            </span>
          ) : null}
        </span>
        {/* 補足は見出しの 1 行目の高さで縦の中央に置く（2 行の補足〔経過時間と遅延の案内〕は上から並べる）。 */}
        {meta ? <span className="flex min-h-[1lh] shrink-0 items-center">{meta}</span> : null}
        <span className={cn("pr-icon-line", !toned && "text-fg-muted")}>
          <DisclosureChevron expanded={open} size={iconSize} />
        </span>
        {summaryFooter != null ? <span className="min-w-0 basis-full">{summaryFooter}</span> : null}
      </summary>
      <div
        className={cn(
          "min-w-0",
          card ? "border-t p-3" : "pt-2",
          card && (toned ? TONE_BORDER[tone as Exclude<DisclosureTone, "neutral">] : "border-border"),
          contentClassName
        )}
      >
        {children}
      </div>
    </details>
  );
}
