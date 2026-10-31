import { UserManagementPage } from "@production-ready/system-settings";

import { RAG_LIST_DETAIL_STORAGE_PREFIX } from "@/components/layout/EntityLayout";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { describeSecurityApiError, securityApi } from "@/lib/security-api";
import { useAuth } from "./AuthProvider";

/** ユーザー管理。画面の実体は platform の共有パッケージ（#206）。 */
export function SecurityUsersPage() {
  const { hasPermission, user } = useAuth();
  return (
    <UserManagementPage
      api={securityApi}
      canManage={hasPermission(MENU_PERMISSIONS.securityUsers)}
      currentUserUuid={user?.user_uuid ?? null}
      describeError={describeSecurityApiError}
      listDetailStoragePrefix={RAG_LIST_DETAIL_STORAGE_PREFIX}
    />
  );
}
