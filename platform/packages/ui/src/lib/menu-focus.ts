import type { RefObject } from "react";

/**
 * メニューを閉じたあとトリガーへフォーカスを戻す。
 *
 * 閉じた直後に利用者が別の要素へフォーカスを移していた場合は奪い返さない。
 * (メニュー項目は unmount 済みのため、復帰時点の activeElement は body か移動先になる。
 *  移動先を上書きすると、キーボード操作で次の行へ移った直後に前の行へ戻される。)
 *
 * 「まだメニューの中」と見なすのは、トリガーと `scopeRefs`（メニューの面）だけにする。
 * 操作の行全体（`ObjectActionBar` の container など）を渡すと、同じ行の別の操作へ移した
 * フォーカスまで奪い返してしまう（#1143）。
 */
export function restoreMenuTriggerFocus(
  triggerRef: RefObject<HTMLElement | null>,
  ...scopeRefs: readonly RefObject<HTMLElement | null>[]
) {
  window.requestAnimationFrame(() => {
    const active = document.activeElement;
    const focusMovedAway =
      active !== null &&
      active !== document.body &&
      active !== triggerRef.current &&
      !scopeRefs.some((ref) => ref.current?.contains(active));
    if (focusMovedAway) return;
    triggerRef.current?.focus({ preventScroll: true });
  });
}
