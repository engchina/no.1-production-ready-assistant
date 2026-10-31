import { useId, useRef, useState, useSyncExternalStore, type RefObject } from "react";

/**
 * 会話の履歴を本文の横にインラインで置く幅（Tailwind の lg）。未満はモーダルの `SideSheet` で開く
 * （UX 契約 page-archetypes.md §6、design-system README §4「SideSheet」。#664 / #889 / #1161）。
 */
export const CHAT_HISTORY_INLINE_QUERY = "(min-width: 1024px)";

/**
 * メディアクエリに合うか（`matchMedia` の変化に追従する）。`matchMedia` が無い環境（SSR・一部のテスト）では
 * `fallback` を返す。
 */
export function useMediaQuery(query: string, fallback = true): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
      const media = window.matchMedia(query);
      media.addEventListener("change", onChange);
      return () => media.removeEventListener("change", onChange);
    },
    () =>
      typeof window === "undefined" || typeof window.matchMedia !== "function"
        ? fallback
        : window.matchMedia(query).matches,
    () => fallback
  );
}

export interface UseChatHistoryPanelOptions {
  /** lg 以上のインラインのパネルを開いているか。製品の作業状態に残す（既定で閉じる。workspace-state.md）。 */
  inlineOpen: boolean;
  onInlineOpenChange: (open: boolean) => void;
  /** パネル・シートの要素の id（開閉ボタンの `aria-controls`）。省略時は `useId`。 */
  id?: string;
}

export interface ChatHistoryPanel {
  /** lg 以上（本文の横にインラインで置く）か。 */
  inline: boolean;
  /** 今の幅で履歴が開いているか（インラインはパネル、lg 未満はシート）。 */
  open: boolean;
  /** lg 以上のインラインのパネルを開いているか（作業状態の値）。 */
  inlineOpen: boolean;
  /** lg 未満のシートを開いているか（作業状態に残さない）。 */
  sheetOpen: boolean;
  /** 開閉ボタンの操作。今の幅に合わせてパネルかシートを切り替える。 */
  toggle: () => void;
  /** lg 未満のシートを閉じる（会話を選んだとき・Escape・scrim）。インラインのパネルは変えない。 */
  closeSheet: () => void;
  /** パネル・シートの id（開閉ボタンの `aria-controls`）。 */
  id: string;
  /** 開閉ボタン。シートを閉じたらここへフォーカスを戻す。 */
  toggleRef: RefObject<HTMLButtonElement | null>;
}

/**
 * チャットの会話の履歴の開閉（3 製品共通。#1161）。
 *
 * - lg 以上は本文の横のインラインのパネル。開閉は製品の作業状態に残す（`inlineOpen`）。
 * - lg 未満はモーダルの `SideSheet`。開閉は残さない（戻ったとき・再読込で画面を塞がない）。
 * - 幅が lg を越えて切り替わったら、シートを閉じる（インラインのパネルは作業状態のまま）。
 */
export function useChatHistoryPanel({
  inlineOpen,
  onInlineOpenChange,
  id,
}: UseChatHistoryPanelOptions): ChatHistoryPanel {
  const inline = useMediaQuery(CHAT_HISTORY_INLINE_QUERY);
  const generatedId = useId();
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  // 幅の区分が変わったらシートを閉じる（render の中で前の値と比べる。effect の 1 フレーム遅れを出さない）。
  const [previousInline, setPreviousInline] = useState(inline);
  if (previousInline !== inline) {
    setPreviousInline(inline);
    if (sheetOpen) setSheetOpen(false);
  }
  return {
    inline,
    open: inline ? inlineOpen : sheetOpen,
    inlineOpen,
    sheetOpen,
    toggle: () => {
      if (inline) onInlineOpenChange(!inlineOpen);
      else setSheetOpen((open) => !open);
    },
    closeSheet: () => setSheetOpen(false),
    id: id ?? generatedId,
    toggleRef,
  };
}
