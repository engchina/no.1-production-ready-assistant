import { PageBody, PageHeader } from "@production-ready/ui";

import { CaCertificateCard } from "./CaCertificateCard";
import { APPEARANCE_MESSAGES, type AppearanceMessages } from "./messages";

export interface AppearanceSettingsPageProps {
  /** 既定の文言の上書き（各製品の i18n から渡す）。 */
  messages?: Partial<AppearanceMessages>;
  /** HTTPS の CA 証明書を配っているかの確認（テスト用。既定は /platform/ca.crt への HEAD）。 */
  probeCaCertificate?: (url: string) => Promise<boolean>;
}

/**
 * 外観と証明書（#95・#1316）。HTTPS の CA 証明書の取得。3製品共通。
 * 配色テーマの選択（ライト / ダーク / 自動）は #17 で廃止した（配色はライトの 1 種類だけ）。
 */
export function AppearanceSettingsPage({ messages, probeCaCertificate }: AppearanceSettingsPageProps) {
  const m = { ...APPEARANCE_MESSAGES, ...messages };
  return (
    <>
      <PageHeader wide title={m.title} subtitle={m.subtitle} />
      <PageBody wide className="grid gap-4">
        <CaCertificateCard messages={m} probe={probeCaCertificate} />
      </PageBody>
    </>
  );
}
