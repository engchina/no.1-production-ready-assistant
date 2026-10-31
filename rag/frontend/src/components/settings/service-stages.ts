import { NAV_ITEMS, settingsSubtitleKey, type NavItem } from "@/components/layout/nav-config";
import type { ServiceCategory } from "@/lib/api";
import type { I18nKey } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

/**
 * サービスのカテゴリ（backend の `app/services/catalog.py` の `ServiceCategory`）と、その工程の設定画面
 * （サイドナビの「取込・検索・回答設定」の項目）の対応。サービス管理の工程の並び・名前・説明はサイドナビを正本にし、
 * ここでは対応だけを持つ（#638。権限管理の一覧をナビから作る #567 と同じ考え方）。
 */
export const SERVICE_CATEGORY_ROUTES = {
  preprocess: APP_ROUTES.settingsPreprocess,
  parser: APP_ROUTES.settingsParserAdapters,
} as const satisfies Record<ServiceCategory, string>;

export interface ServiceStage {
  category: ServiceCategory;
  /** サイドナビの表示名（`sidebarLabelKey` があればそれ。Sidebar.tsx と同じ規則）。 */
  labelKey: I18nKey;
  /** 対応する設定画面の説明（PageHeader の subtitle）。無い工程は null。 */
  descriptionKey: I18nKey | null;
}

/**
 * サービス管理の工程を、サイドナビの並び順で返す。ナビを並べ替え・改名すると、サービス管理の工程も追従する。
 * 権限では絞らない（サービス管理はそれ自体の権限で開く画面で、工程の設定画面を開けない利用者にも全工程を出す）。
 */
export function serviceStages(items: readonly NavItem[] = NAV_ITEMS): ServiceStage[] {
  const categoryByRoute = new Map<string, ServiceCategory>(
    Object.entries(SERVICE_CATEGORY_ROUTES).map(([category, route]) => [route, category as ServiceCategory])
  );
  return items.flatMap((item) => {
    const category = categoryByRoute.get(item.href);
    return category
      ? [
          {
            category,
            labelKey: item.sidebarLabelKey ?? item.labelKey,
            descriptionKey: settingsSubtitleKey(item),
          },
        ]
      : [];
  });
}
