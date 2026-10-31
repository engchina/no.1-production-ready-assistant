import { RoleManagementPage } from "@production-ready/system-settings";

import { RAG_LIST_DETAIL_STORAGE_PREFIX } from "@/components/layout/EntityLayout";
import type { SecurityRole } from "@/lib/api";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { APP_ROUTES } from "@/lib/routes";
import { describeSecurityApiError, securityApi } from "@/lib/security-api";
import { useAuth } from "./AuthProvider";

/**
 * ロール管理。画面の実体は platform の共有パッケージ（#206）。
 * ロールへの権限と対象範囲の付与は RAG の権限管理画面で行うため、詳細に件数と導線だけを出す（共通画面の permissionSummary。#800）（#214）。
 */
export function SecurityRolesPage() {
  const { hasPermission } = useAuth();
  return (
    <RoleManagementPage<SecurityRole>
      api={securityApi}
      canManage={hasPermission(MENU_PERMISSIONS.securityRoles)}
      describeError={describeSecurityApiError}
      listDetailStoragePrefix={RAG_LIST_DETAIL_STORAGE_PREFIX}
      permissionSummary={{
        count: (role) => role.permissions?.length ?? 0,
        permissionsPath: APP_ROUTES.securityPermissions,
        canManagePermissions: hasPermission(MENU_PERMISSIONS.securityPermissions),
      }}
    />
  );
}
