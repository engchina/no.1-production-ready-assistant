import { Info } from "lucide-react";
import { useEffect, useId, useRef, type ButtonHTMLAttributes } from "react";

import { cn } from "../../lib/utils";
import {
  isFocusVisible,
  TooltipBubble,
  useTooltipController,
  type TooltipPlacement,
} from "./tooltip";

/*
 * InfoTip（#901）。操作・欄の補足の説明を常設せず、ラベルの横の info アイコンから吹き出しで出す。
 *
 * 業界の指針（README §4「`InfoTip`」）:
 *   - Carbon: 補足の説明は Tooltip（hover / focus）か Toggletip（押して開閉・Esc・外側で閉じる）。
 *     作業に欠かせない情報・入力の条件は隠さない（常設の helper text / hint に書く。GOV.UK の hint も同じ）
 *   - Inclusive Components「Tooltips & Toggletips」: info アイコンのボタン。hover だけではタッチで読めない
 *   - WCAG 1.4.13（dismissible / hoverable / persistent）・2.1.1（キーボード）・2.5.8（当たり判定 24px）
 * これらを合わせて、ホバー・キーボードのフォーカス・押す（クリック・タップ・Enter / Space）のどれでも開く。
 *   - ホバー: 短く待って（150ms）出し、ポインタが離れたら閉じる。吹き出しへ移しても消えない
 *   - フォーカス（:focus-visible）: すぐ出し、フォーカスが外れたら閉じる
 *   - 押す: 開いたまま固定し、もう一度押す・Escape・外側を押す・フォーカスを外すで閉じる（タッチ端末はこれだけ）
 *   - 画面に出す吹き出しは Tooltip と合わせて 1 つだけ（#655）
 *   - 説明は `aria-describedby` で結び付け、閉じている間も読み上げで読める（開閉の状態は読み上げない）
 */

/** ホバーから表示までの待ち時間。アイコンは説明を出すためだけにあるので Tooltip（400ms）より短い。 */
export const INFO_TIP_SHOW_DELAY_MS = 150;
/** アイコンの寸法（README §3 のアイコン寸法の 16px）。 */
export const INFO_TIP_ICON_SIZE = 16;

export interface InfoTipProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "content" | "aria-label" | "type"> {
  /** 説明の文（翻訳済み。1〜3 文。リンク・ボタンなど操作できる要素は入れない）。 */
  content: string;
  /**
   * アイコンのボタンの読み上げ名（翻訳済み）。何の説明かが分かる名前にする（例「回答するモデルの説明」）。
   * 説明の文は `aria-describedby` で読まれる。
   */
  label: string;
  /** 空きがあれば出す側。既定は上。入らなければ反転する。 */
  placement?: TooltipPlacement;
  /**
   * 吹き出しの id。説明の対象の欄（`SelectField` の `describedBy` など）からも同じ説明を結び付けるときに渡す。
   * 省略時は自動で振る。
   */
  contentId?: string;
  /** 吹き出しの `data-testid`（E2E 用）。ボタンの `data-testid` はそのまま渡す。 */
  contentTestId?: string;
}

/**
 * ラベルの横に置く info アイコン（`lucide-react` の `Info`、16px）。押すと補足の説明を吹き出しで出す。
 * 作業に欠かせない情報（必須の条件・エラー・結果）には使わない（常設のまま出す）。
 */
export function InfoTip({
  content,
  label,
  placement = "top",
  contentId,
  contentTestId,
  className,
  onClick,
  onPointerEnter,
  onPointerLeave,
  onFocus,
  onBlur,
  disabled,
  ...props
}: InfoTipProps) {
  const generatedId = useId();
  const id = contentId ?? generatedId;
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const { controller, state } = useTooltipController({ showDelayMs: INFO_TIP_SHOW_DELAY_MS });
  const pinned = state.open && state.reason === "press";

  // 押して開いている間は、外側を押したら閉じる（クリックでフォーカスが移らないブラウザ・タッチ端末でも閉じる）。
  useEffect(() => {
    if (!pinned) return undefined;
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node | null;
      if (target && (triggerRef.current?.contains(target) || bubbleRef.current?.contains(target))) return;
      controller.dismiss();
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [controller, pinned]);

  useEffect(() => {
    if (disabled && controller.isOpen()) controller.dismiss();
  }, [controller, disabled]);

  return (
    <>
      <button
        {...props}
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-describedby={[props["aria-describedby"], id].filter(Boolean).join(" ")}
        disabled={disabled}
        data-info-tip=""
        data-state={state.open ? "open" : "closed"}
        className={cn(
          // 見た目は 24px（WCAG 2.5.8 の当たり判定）。タッチ端末では見た目を変えずに当たり判定だけを 44px にする（#364）。
          // アイコン（16px）の周りに 4px の余白があるので、フォーカスの outline は offset 0 で円に沿わせ、隣のラベルに重ねない。
          "pr-touch-target relative inline-flex h-[24px] w-[24px] shrink-0 cursor-pointer items-center justify-center rounded-full align-middle text-fg-muted transition-colors focus-visible:outline-offset-0 hover:bg-surface-hover hover:text-fg data-[state=open]:text-fg disabled:cursor-not-allowed disabled:opacity-50 forced-colors:text-[ButtonText]",
          className
        )}
        onPointerEnter={(event) => {
          onPointerEnter?.(event);
          if (!disabled) controller.pointerEnterTrigger(event.pointerType);
        }}
        onPointerLeave={(event) => {
          onPointerLeave?.(event);
          controller.pointerLeaveTrigger();
        }}
        onFocus={(event) => {
          onFocus?.(event);
          if (!disabled) controller.focus(isFocusVisible(event.currentTarget));
        }}
        onBlur={(event) => {
          onBlur?.(event);
          controller.blur();
        }}
        onClick={(event) => {
          onClick?.(event);
          if (!event.defaultPrevented && !disabled) controller.press();
        }}
      >
        <Info size={INFO_TIP_ICON_SIZE} aria-hidden />
      </button>
      <TooltipBubble
        id={id}
        open={state.open && !disabled}
        describes
        reason={state.reason}
        placement={placement}
        controller={controller}
        triggerRef={triggerRef}
        bubbleRef={bubbleRef}
        content={content}
        data-testid={contentTestId}
        // 文で読む説明なので、Tooltip（12px / 500・16rem）より広く、行間を空け、太さは本文の 400 にする。
        className="max-w-[min(20rem,calc(100vw-1rem))] px-3 py-2 font-normal leading-relaxed"
      />
    </>
  );
}
