import type { ReactNode } from "react";
import { Banner, PageBody, PageHeader } from "@production-ready/ui";

import { t } from "@/lib/i18n";

/**
 * 画面は開けても（メニュー権限あり）、実データの閲覧に必要な capability が無いときの状態表示（#215）。
 * データを取りに行かず、ページ全体の状態として権限不足を Banner で説明する（UX 契約 messaging.md §3.3）。
 * backend は同じ規則で 403 を返すため、ここは表示を分かりやすくするだけで認可の境界ではない。
 */
export function CapabilityGate({
  allowed,
  title,
  requirement,
  children,
}: {
  allowed: boolean;
  /** ページの見出し（ナビと同じ名前）。 */
  title: string;
  /** 必要な権限の説明（i18n 済み）。 */
  requirement: string;
  children: ReactNode;
}) {
  if (allowed) return <>{children}</>;
  return (
    <>
      <PageHeader wide title={title} />
      <PageBody wide>
        <Banner severity="warning" title={t("capability.required.title")}>
          <span data-testid="capability-required">{t("capability.required.description", { requirement })}</span>
        </Banner>
      </PageBody>
    </>
  );
}
