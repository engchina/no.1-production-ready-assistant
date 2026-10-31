import { TextField } from "@production-ready/ui";

import { guideContactInheritance, type ContactInheritance } from "@/lib/contact-inheritance";
import { t } from "@/lib/i18n";
import { useInheritedAnswerContact, useSearchAnswerProfile } from "@/lib/queries";

import { ContactInheritanceHelper, contactInheritancePlaceholder } from "./ContactInheritanceHelper";

/** 引き継ぎ先の長さの上限（backend の `SupportGuideHandoff.contact` の `_SHORT`）。 */
const HANDOFF_CONTACT_MAX_CHARS = 200;

/**
 * ガイドの有効な窓口の 1 行（#2683）。引き継ぎのとき、ガイドの窓口は回答の本文で案内し注意書きには窓口を出さない。
 * 空なら本文は「担当の窓口」と案内し、注意書きでプロファイル（無ければシステム設定）の窓口を案内する
 * （backend の `answer_notice.py` と `short_circuit_answer`）。読めていなければ null。
 */
export function guideEffectiveContactText({ effective }: ContactInheritance): string | null {
  if (!effective) return null;
  switch (effective.source) {
    case "guide":
      return t("supportGuides.contact.effectiveGuide", { contact: effective.contact });
    case "none":
      return t("supportGuides.contact.effectiveNone");
    default:
      return t("supportGuides.contact.effectiveInherited", {
        contact: effective.contact,
        source: t(
          effective.source === "profile" ? "supportGuides.contact.source.profile" : "supportGuides.contact.source.system"
        ),
      });
  }
}

function sameAsText(inheritance: ContactInheritance): string | null {
  if (inheritance.sameAs === "profile") return t("supportGuides.contact.sameAsProfile");
  if (inheritance.sameAs === "system") return t("supportGuides.contact.sameAsSystem");
  return null;
}

/**
 * 業務ガイドの「引き継ぎ先（窓口の名前）」。ガイドは 1 つの検索・回答プロファイルに属するので、空のときは
 * そのプロファイルの保存済みの窓口の上書き（回答に使う値）、無ければシステム設定の窓口を継承する（#2683）。
 */
export function SupportGuideHandoffContactField({
  id,
  searchAnswerProfileId,
  value,
  onChange,
}: {
  id: string;
  searchAnswerProfileId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const system = useInheritedAnswerContact();
  const profile = useSearchAnswerProfile(searchAnswerProfileId);
  // プロファイルを読めるまでは継承元が決まらないので、システム設定も出さない（出どころを取り違えない）。
  const inheritance = guideContactInheritance(
    value,
    profile.data?.config.contact_override,
    profile.data ? system.data : undefined
  );
  return (
    <TextField
      id={id}
      className="col-span-full"
      width="full"
      label={t("supportGuides.field.handoffContact")}
      value={value}
      maxLength={HANDOFF_CONTACT_MAX_CHARS}
      placeholder={contactInheritancePlaceholder(inheritance)}
      helper={
        <ContactInheritanceHelper
          helper={t("supportGuides.field.handoffContactHelp")}
          effective={guideEffectiveContactText(inheritance)}
          sameAs={sameAsText(inheritance)}
          loadError={system.isError || profile.isError}
          testId="support-guide-handoff-contact"
        />
      }
      onChange={(event) => onChange(event.target.value)}
    />
  );
}
