import { TextField } from "@production-ready/ui";

import { profileContactInheritance, type ContactInheritance } from "@/lib/contact-inheritance";
import { t } from "@/lib/i18n";
import { useInheritedAnswerContact } from "@/lib/queries";

import { ContactInheritanceHelper, contactInheritancePlaceholder } from "./ContactInheritanceHelper";

/** 窓口の上書きの長さの上限（backend の `CONTACT_OVERRIDE_MAX_CHARS`。システム設定の窓口と同じ）。 */
export const CONTACT_OVERRIDE_MAX_CHARS = 200;

/** プロファイルの有効な窓口の 1 行（#2683）。システム設定を読めていなければ null。 */
export function profileEffectiveContactText({ effective }: ContactInheritance): string | null {
  if (!effective) return null;
  if (effective.source === "none") return t("searchAnswerProfiles.contact.effectiveNone");
  return t("searchAnswerProfiles.contact.effective", {
    contact: effective.contact,
    source: t(
      effective.source === "profile"
        ? "searchAnswerProfiles.contact.source.profile"
        : "searchAnswerProfiles.contact.source.system"
    ),
  });
}

/**
 * 回答の末尾の窓口の案内（#2400）。回答の注意書き（システム設定。#2405）の窓口を、このプロファイルの回答だけ
 * 上書きする。窓口の案内は共通の固定文を使う。
 * 上書きが空のときは継承するシステム設定の窓口を placeholder で示し、欄の下に有効な窓口と出どころを出す（#2683）。
 */
export function ContactGuidanceFieldset({
  contactOverride,
  disabled,
  onChange,
}: {
  contactOverride: string;
  disabled: boolean;
  onChange: (patch: { contact_override?: string }) => void;
}) {
  const system = useInheritedAnswerContact();
  const inheritance = profileContactInheritance(contactOverride, system.data);
  return (
    <fieldset
      className="space-y-3 rounded-lg border border-border p-4"
      data-testid="search-answer-profile-contact"
    >
      <legend className="px-1 text-sm font-semibold text-fg">{t("searchAnswerProfiles.contact.title")}</legend>
      <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.contact.helper")}</p>
      <div className="space-y-4 rounded-lg border border-border bg-surface-sunken p-3">
        <TextField
          id="search-answer-profile-contact-override"
          label={t("searchAnswerProfiles.contact.override")}
          value={contactOverride}
          onValueChange={(value) => onChange({ contact_override: value })}
          placeholder={contactInheritancePlaceholder(inheritance)}
          helper={
            <ContactInheritanceHelper
              helper={t("searchAnswerProfiles.contact.overrideHelper")}
              effective={profileEffectiveContactText(inheritance)}
              sameAs={inheritance.sameAs === "system" ? t("searchAnswerProfiles.contact.sameAsSystem") : null}
              loadError={system.isError}
              testId="search-answer-profile-contact"
            />
          }
          maxLength={CONTACT_OVERRIDE_MAX_CHARS}
          disabled={disabled}
          width="full"
        />
      </div>
    </fieldset>
  );
}
