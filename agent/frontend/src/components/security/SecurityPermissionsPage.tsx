import { useMemo } from "react";
import { RolePermissionsPage } from "@production-ready/system-settings";

import { AGENT_LIST_DETAIL_STORAGE_PREFIX } from "@/components/EntityLayout";
import type { SecurityRole } from "@/lib/api";
import { t } from "@/lib/i18n";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { PERMISSIONS_API, agentPermissionTargets } from "@/lib/permission-targets";
import { securityApi } from "@/lib/security-api";
import { useAuth } from "./AuthProvider";

/**
 * 権限管理（「セキュリティ設定」のメニュー。#215 / #658）。画面の実体は platform の共通 RolePermissionsPage（#220）。
 * ロールごとの機能権限と、利用できるエージェントを設定する（検索・回答プロファイルの判定は RAG。#750）。
 */
export function SecurityPermissionsPage() {
  const { hasPermission } = useAuth();
  // 一覧の検索を毎 render 作り直さないよう、targets は mount 中は同じ配列を使う。
  const targets = useMemo(
    () =>
      agentPermissionTargets({
        agents: (query, signal) => securityApi.agentTargets(query, { signal }),
      }),
    [],
  );
  return (
    <RolePermissionsPage<SecurityRole>
      api={PERMISSIONS_API}
      canManage={hasPermission(MENU_PERMISSIONS.securityPermissions)}
      targets={targets}
      listDetailStoragePrefix={AGENT_LIST_DETAIL_STORAGE_PREFIX}
      messages={{
        title: t("nav.securityPermissions"),
        subtitle: t("security.permissions.subtitle"),
        listHint: t("security.permissions.listHint"),
        searchPlaceholder: t("security.permissions.searchPlaceholder"),
        formHint: t("security.permissions.formHint"),
        permissionsHint: t("security.permissions.permissionsHint"),
      }}
    />
  );
}
