import { AppearanceSettingsPage } from "@production-ready/system-settings";

import { t } from "@/lib/i18n";
import { useUiStore } from "@/lib/ui-store";

/**
 * 外観と証明書の設定（配色テーマ・HTTPS の証明書。#1316）。画面の実体は platform の共有パッケージ（#95）。
 * 名前と配色テーマの文言は Agent の i18n から渡し、副題などそれ以外は platform の既定（3 製品で同じ）を使う。
 */
export function AppearanceSettings() {
  const theme = useUiStore((state) => state.theme);
  const setTheme = useUiStore((state) => state.setTheme);
  return (
    <AppearanceSettingsPage
      theme={theme}
      onThemeChange={setTheme}
      messages={{
        title: t("nav.settingsAppearance"),
        themeLabel: t("appearance.theme.label"),
        themeHint: t("appearance.theme.hint"),
        themeLight: t("appearance.theme.light"),
        themeDark: t("appearance.theme.dark"),
        themeSystem: t("appearance.theme.system"),
      }}
    />
  );
}
