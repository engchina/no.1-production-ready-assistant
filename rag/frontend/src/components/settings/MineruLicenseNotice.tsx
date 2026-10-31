import { Banner } from "@production-ready/ui";
import { ExternalLink } from "lucide-react";

import type { MineruLicenseAcknowledgement } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";

/** MinerU の公式のリポジトリとライセンスの全文（#3359）。 */
export const MINERU_REPOSITORY_URL = "https://github.com/opendatalab/MinerU";
export const MINERU_LICENSE_URL = "https://github.com/opendatalab/MinerU/blob/master/LICENSE.md";

const LICENSE_POINTS: I18nKey[] = [
  "mineruLicense.point.threshold",
  "mineruLicense.point.attribution",
  "mineruLicense.point.termination",
  "mineruLicense.point.model",
];

/** 確認の記録の 1 行（「確認済み: 名前（日時）」）。未確認なら null。 */
export function mineruLicenseAcknowledgedText(
  acknowledgement: MineruLicenseAcknowledgement | undefined
): string | null {
  if (!acknowledgement?.acknowledged) return null;
  const at = formatDateTime(acknowledgement.acknowledged_at);
  return acknowledgement.acknowledged_by
    ? t("mineruLicense.acknowledgedBy", { name: acknowledgement.acknowledged_by, at })
    : t("mineruLicense.acknowledgedAt", { at });
}

/**
 * MinerU のライセンス（MinerU Open Source License）の主な条件と公式の全文への案内（#3359）。
 *
 * MinerU を選べる所（文書解析の使用エンジン・文書の処理レシピ）に出す。`acknowledge` を渡すと
 * 「MinerU のライセンスの条件を確認した」の checkbox を出す（文書解析の画面。保存で記録する）。
 */
export function MineruLicenseNotice({
  acknowledgement,
  acknowledge,
  compact = false,
  testId = "mineru-license-notice",
}: {
  acknowledgement?: MineruLicenseAcknowledgement;
  acknowledge?: {
    id: string;
    checked: boolean;
    disabled: boolean;
    /** 外せない理由など（checkbox の下に出す）。 */
    note?: string | null;
    onChange: (checked: boolean) => void;
  };
  /**
   * 準備中（ロードマップ）の間の短い案内（カードの下に 1 文と公式のリンクだけ。確認の checkbox は
   * 出さない）。主な条件の全文は README と配備の文書に書く。
   */
  compact?: boolean;
  testId?: string;
}) {
  if (compact) {
    return (
      <div data-testid="mineru-license-note" className="space-y-1 px-1 text-xs leading-relaxed text-fg-muted">
        <p>{t("mineruLicense.compact")}</p>
        <ul className="flex flex-wrap gap-x-3 gap-y-1">
          <li>
            <LicenseLink href={MINERU_REPOSITORY_URL} label={t("mineruLicense.link.repository")} />
          </li>
          <li>
            <LicenseLink href={MINERU_LICENSE_URL} label={t("mineruLicense.link.license")} />
          </li>
        </ul>
      </div>
    );
  }
  const acknowledgedText = mineruLicenseAcknowledgedText(acknowledgement);
  const noteId = acknowledge ? `${acknowledge.id}-note` : undefined;
  return (
    <div data-testid={testId}>
      <Banner severity={acknowledgement?.acknowledged ? "info" : "warning"} title={t("mineruLicense.title")}>
        <div className="space-y-2">
          <p>{t("mineruLicense.intro")}</p>
          <ul className="list-disc space-y-1 pl-5">
            {LICENSE_POINTS.map((key) => (
              <li key={key}>{t(key)}</li>
            ))}
          </ul>
          <p className="font-medium">{t("mineruLicense.confirmFullText")}</p>
          <ul className="flex flex-wrap gap-x-4 gap-y-1">
            <li>
              <LicenseLink href={MINERU_REPOSITORY_URL} label={t("mineruLicense.link.repository")} />
            </li>
            <li>
              <LicenseLink href={MINERU_LICENSE_URL} label={t("mineruLicense.link.license")} />
            </li>
          </ul>
          {acknowledge ? (
            <div className="space-y-1 border-t border-border pt-2">
              <label
                htmlFor={acknowledge.id}
                className="flex min-h-[var(--control-height-touch)] cursor-pointer items-center gap-2 font-medium text-fg"
              >
                {/* 確認の checkbox はネイティブ（共有の部品に checkbox は無い。docs/agents/ui.md）。 */}
                <input
                  id={acknowledge.id}
                  type="checkbox"
                  checked={acknowledge.checked}
                  disabled={acknowledge.disabled}
                  aria-describedby={noteId}
                  onChange={(event) => acknowledge.onChange(event.target.checked)}
                  className="h-4 w-4 shrink-0 rounded border-border accent-accent-emphasis"
                />
                {t("mineruLicense.acknowledge")}
              </label>
              <p id={noteId} className="text-xs text-fg-muted">
                {acknowledge.note ?? acknowledgedText ?? t("mineruLicense.acknowledgeHelper")}
              </p>
              {acknowledge.note && acknowledgedText ? (
                <p className="text-xs text-fg-muted">{acknowledgedText}</p>
              ) : null}
            </div>
          ) : acknowledgedText ? (
            <p className="text-xs text-fg-muted">{acknowledgedText}</p>
          ) : (
            <p className="font-medium">{t("mineruLicense.recipeUnacknowledged")}</p>
          )}
        </div>
      </Banner>
    </div>
  );
}

function LicenseLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-accent-fg underline underline-offset-2"
    >
      {label}
      <ExternalLink size={14} aria-hidden />
      <span className="sr-only">{t("mineruLicense.link.newTab")}</span>
    </a>
  );
}
