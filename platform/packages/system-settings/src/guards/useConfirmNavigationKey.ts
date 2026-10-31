import { useState } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

type ConfirmNavigation = { key: string; pathname: string };

/**
 * 共有 ConfirmProvider の `navigationKey` に渡す、画面遷移の識別子（UX 契約 messaging.md §3.5。#833）。
 *
 * 次のときだけ値が変わり、開いている確認をキャンセルする。
 * - ブラウザの戻る / 進む（POP）と、リンク・`navigate()` の遷移（PUSH）
 * - パスが変わる置き換え（REPLACE。作成後に詳細へ移るなど）
 *
 * 同じパスのままの REPLACE（画面が選択中のレシピ・ページ番号などの作業状態を URL に書き戻すだけ）では
 * 変えない。`useLocation().key` をそのまま渡すと、文書を開いた直後に「再実行」などの確認を開くと、
 * 後から届いた選択の書き戻しで確認が閉じていた（RAG の文書詳細。#833）。
 */
export function useConfirmNavigationKey(): string {
  const location = useLocation();
  const navigationType = useNavigationType();
  const [current, setCurrent] = useState<ConfirmNavigation>(() => ({
    key: location.key,
    pathname: location.pathname,
  }));
  const keepsScreen = navigationType === "REPLACE" && location.pathname === current.pathname;
  if (location.key !== current.key && !keepsScreen) {
    // 描画中に前の値から state を合わせる（React の「props の変化に合わせて state を調整する」形）。
    setCurrent({ key: location.key, pathname: location.pathname });
    return location.key;
  }
  return current.key;
}
