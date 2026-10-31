import type { ReactNode } from "react";

import { ConfirmProvider as UiConfirmProvider } from "@production-ready/ui";
import { useConfirmNavigationKey } from "@production-ready/system-settings";

import { t } from "@/lib/i18n";

// useConfirm / 型は共有 UI パッケージをそのまま再公開。
export { useConfirm, type ConfirmOptions } from "@production-ready/ui";

/**
 * 確認ダイアログ Provider。共有 UI パッケージの ConfirmProvider に RAG の i18n（既定文言）を注入し、
 * ルートが変わったら（戻る・進むを含む）開いている確認をキャンセルする（UX 契約 messaging §3.5。NL2SQL と同じ。#800）。
 * 選択中のレシピなどを URL に書き戻すだけの置き換えではキャンセルしない（共通の useConfirmNavigationKey。#833）。
 * data router の route の中（main.tsx の Providers）で使う。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const navigationKey = useConfirmNavigationKey();
  return (
    <UiConfirmProvider
      labels={{ confirm: t("common.confirm"), cancel: t("common.cancel") }}
      navigationKey={navigationKey}
    >
      {children}
    </UiConfirmProvider>
  );
}
