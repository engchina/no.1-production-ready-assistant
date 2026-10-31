import { AppearanceSettingsPage } from "@production-ready/system-settings";

import { t } from "@/lib/i18n";
import { useUiStore } from "@/lib/ui-store";

/** 外観設定。画面の実体は platform の共有パッケージ（#95）。文言は NL2SQL の i18n から渡す。 */
export function AppearanceSettings() {
  const theme = useUiStore((state) => state.theme);
  const setTheme = useUiStore((state) => state.setTheme);
  return (
    <AppearanceSettingsPage
      theme={theme}
      onThemeChange={setTheme}
      messages={{
        title: t("nav.settingsAppearance"),
        subtitle: t("appearance.subtitle"),
        themeLabel: t("appearance.theme.label"),
        themeHint: t("appearance.theme.hint"),
        themeLight: t("appearance.theme.light"),
        themeDark: t("appearance.theme.dark"),
        themeSystem: t("appearance.theme.system"),
      }}
    />
  );
}
