import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  PageBody,
  PageHeader,
  type ThemePreference,
} from "@production-ready/ui";

import { CaCertificateCard } from "./CaCertificateCard";
import { APPEARANCE_MESSAGES, type AppearanceMessages } from "./messages";

const THEME_OPTIONS: Array<{ value: ThemePreference; label: keyof AppearanceMessages }> = [
  { value: "light", label: "themeLight" },
  { value: "dark", label: "themeDark" },
  { value: "system", label: "themeSystem" },
];

export interface AppearanceSettingsPageProps {
  /** 現在のテーマ選好（各製品の `useUiStore((s) => s.theme)`）。 */
  theme: ThemePreference;
  /** テーマ選好を変更する（各製品の `useUiStore((s) => s.setTheme)`）。反映は `initTheme` が行う。 */
  onThemeChange: (theme: ThemePreference) => void;
  /** 既定の文言の上書き（各製品の i18n から渡す）。 */
  messages?: Partial<AppearanceMessages>;
  /** HTTPS の CA 証明書を配っているかの確認（テスト用。既定は /platform/ca.crt への HEAD）。 */
  probeCaCertificate?: (url: string) => Promise<boolean>;
}

/**
 * 外観と証明書（#95・#1316）。配色テーマ（ライト / ダーク / 自動）と、HTTPS の CA 証明書の取得。3製品共通。
 */
export function AppearanceSettingsPage({ theme, onThemeChange, messages, probeCaCertificate }: AppearanceSettingsPageProps) {
  const m = { ...APPEARANCE_MESSAGES, ...messages };
  return (
    <>
      <PageHeader wide title={m.title} subtitle={m.subtitle} />
      <PageBody wide className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>{m.themeLabel}</CardTitle>
            <CardDescription>{m.themeHint}</CardDescription>
          </CardHeader>
          <CardContent>
            <div
              role="group"
              aria-label={m.themeLabel}
              className="flex flex-wrap gap-2"
              data-testid="appearance-theme-toggle"
            >
              {THEME_OPTIONS.map((option) => (
                <Button
                  type="button"
                  variant="secondary"
                  size="md"
                  key={option.value}
                  aria-pressed={theme === option.value}
                  onClick={() => onThemeChange(option.value)}
                >
                  {m[option.label]}
                </Button>
              ))}
            </div>
          </CardContent>
        </Card>
        <CaCertificateCard messages={m} probe={probeCaCertificate} />
      </PageBody>
    </>
  );
}
