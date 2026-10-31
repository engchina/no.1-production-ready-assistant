import {
  Card,
  CardContent,
  EmptyState,
  PageHeader,
  PageBody,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";

/** 共有 UI で雛形ページを描画する（skeleton。実装はバックエンド接続時に追加）。 */
export function PlaceholderPage({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <>
      <PageHeader wide title={title} subtitle={subtitle} />
      <PageBody wide>
        <Card>
          <CardContent className="pt-5">
            <EmptyState title={t("common.empty.title")} hint={t("common.empty.hint")} />
          </CardContent>
        </Card>
      </PageBody>
    </>
  );
}
