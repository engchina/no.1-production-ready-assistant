import { RoleManagementPage } from "@production-ready/system-settings";

import { APP_ROUTES } from "@/lib/routes";
import { LIST_DETAIL_STORAGE_PREFIX } from "@/lib/ui-store";
import { useAuth } from "./AuthProvider";
import { securityApi } from "./api";
import { describeSecurityApiError } from "./describe-error";
import { MENU_PERMISSIONS } from "./menu-permissions";
import type { SecurityRole } from "./types";

/**
 * ロール管理。画面の実体は platform の共有パッケージ（#206）。
 * ロールへの権限付与は NL2SQL の権限管理画面で行うため、詳細に件数と導線だけを出す（共通画面の permissionSummary。#800）。
 */
export function SecurityRolesPage() {
  const { hasPermission } = useAuth();
  return (
    <RoleManagementPage<SecurityRole>
      api={securityApi}
      canManage={hasPermission(MENU_PERMISSIONS.securityRoles)}
      describeError={describeSecurityApiError}
      listDetailStoragePrefix={LIST_DETAIL_STORAGE_PREFIX}
      permissionSummary={{
        count: (role) => role.permissions?.length ?? 0,
        permissionsPath: APP_ROUTES.securityPermissions,
        canManagePermissions: hasPermission(MENU_PERMISSIONS.securityPermissions),
      }}
    />
  );
}
