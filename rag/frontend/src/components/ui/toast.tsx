import { Toaster as UiToaster } from "@production-ready/ui";

import { t } from "@/lib/i18n";

/**
 * Toast 表示領域。共有 UI パッケージの Toaster に RAG の i18n（閉じるラベル・領域の読み上げ名）を注入するラッパ。
 */
export function Toaster() {
  return <UiToaster dismissLabel={t("common.dismiss")} regionLabel={t("common.notifications")} />;
}
