import type { ReactNode } from "react";

import { t } from "@/lib/i18n";
import type { ContactInheritance } from "@/lib/contact-inheritance";

/** 空の欄の placeholder（継承する窓口を薄く示す。値は欄にコピーしない。#2683）。読めていなければ出さない。 */
export function contactInheritancePlaceholder(inheritance: ContactInheritance): string | undefined {
  const { inherited } = inheritance;
  if (!inherited) return undefined;
  if (inherited.source === "none") return t("contactInheritance.placeholderNone");
  return t("contactInheritance.placeholder", { contact: inherited.contact });
}

/**
 * 窓口の欄の補足（`TextField` の `helper`。欄の aria-describedby に入る）。欄の説明の後に、有効な窓口と出どころ、
 * 継承する窓口と同じ値のときの案内（作業は止めない）を続ける（#2683）。
 */
export function ContactInheritanceHelper({
  helper,
  effective,
  sameAs,
  loadError,
  testId,
}: {
  helper: ReactNode;
  /** 有効な窓口の 1 行（読めていなければ null）。 */
  effective: string | null;
  /** 継承する窓口と同じ値のときの案内（無ければ null）。 */
  sameAs: string | null;
  loadError: boolean;
  testId: string;
}) {
  return (
    <>
      {helper}
      {effective ? (
        <span className="mt-1 block text-fg" data-testid={`${testId}-effective`}>
          {effective}
        </span>
      ) : null}
      {sameAs ? (
        <span className="mt-1 block" data-testid={`${testId}-same`}>
          {sameAs}
        </span>
      ) : null}
      {loadError ? (
        <span className="mt-1 block" data-testid={`${testId}-load-error`}>
          {t("contactInheritance.loadError")}
        </span>
      ) : null}
    </>
  );
}
