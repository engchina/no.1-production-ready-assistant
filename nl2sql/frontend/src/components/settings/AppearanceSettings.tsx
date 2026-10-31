import { AppearanceSettingsPage } from "@production-ready/system-settings";

import { t } from "@/lib/i18n";

/**
 * 外観と証明書の設定（HTTPS の証明書。#1316）。画面の実体は platform の共有パッケージ（#95）。
 * 名前は NL2SQL の i18n から渡し、副題などそれ以外は platform の既定（3 製品で同じ）を使う。
 * 配色テーマの選択は #17 で廃止した（配色はライトの 1 種類だけ）。
 */
export function AppearanceSettings() {
  return <AppearanceSettingsPage messages={{ title: t("nav.settingsAppearance") }} />;
}
