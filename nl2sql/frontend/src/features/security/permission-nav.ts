import {
  arrangePermissionsByNav,
  permissionNavSections,
  type PermissionDefinition,
} from "@production-ready/system-settings";

import { NAV_SECTIONS } from "@/components/layout/nav-config";
import { t } from "@/lib/i18n";

/**
 * 権限管理の機能の一覧は、左のナビ（nav-config の NAV_SECTIONS）を正本にして、グループ・並び順・名前を
 * そろえる（#567）。ナビに無い権限（画面の中の操作を許可する capability の「Ontology」「参照権限」
 * 「管理権限」「実行権限」）は、ナビの後ろに backend のカタログのまま置く。
 */
export function arrangeNl2SqlPermissions(
  catalog: readonly PermissionDefinition[],
): PermissionDefinition[] {
  return arrangePermissionsByNav(catalog, permissionNavSections(NAV_SECTIONS, t));
}
