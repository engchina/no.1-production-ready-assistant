import {
  RolePermissionsPage,
  type RolePermissionTargetSection,
  type RolePermissionsApi,
} from "@production-ready/system-settings";

import { t } from "@/lib/i18n";
import { LIST_DETAIL_STORAGE_PREFIX } from "@/lib/ui-store";
import { useAuth } from "./AuthProvider";
import { securityApi } from "./api";
import { MENU_PERMISSIONS } from "./menu-permissions";
import { arrangeNl2SqlPermissions } from "./permission-nav";
import type { SecurityRole } from "./types";

/** 業務プロファイル管理権限を持つロールは、すべての業務プロファイルを利用できる（個別の ID は保存しない）。 */
const PROFILE_MANAGE_PERMISSION = "nl2sql.profiles.manage";
/** 業務プロファイル利用権限の key。要素 ID とテスト ID は `security-roles-profile-access-*` になる。 */
const PROFILE_ACCESS_KEY = "profile-access";

function normalizedRole(role: SecurityRole): SecurityRole {
  return {
    ...role,
    allowed_profile_ids: role.allowed_profile_ids ?? [],
    data_entitlements: role.data_entitlements ?? [],
    permissions: role.permissions ?? [],
  };
}

const PERMISSIONS_API: RolePermissionsApi<SecurityRole> = {
  roles: (includeArchived, options) =>
    securityApi.roles(includeArchived, options).then((rows) => rows.map(normalizedRole)),
  // 機能の一覧は左のナビのグループ・並び順・名前にそろえる（#567）。
  permissions: (options) => securityApi.permissions(options).then(arrangeNl2SqlPermissions),
  // 機能権限と業務プロファイル利用権限を NL2SQL の保存 API（PUT /permissions）へ送る。
  save: (role, draft) =>
    securityApi
      .updateRolePermissions({
        role_id: role.role_id,
        version: role.version,
        permissions: draft.permissions,
        // 全件が対象のとき（SYSTEM_ADMIN・業務プロファイル管理権限）は共通画面が空の一覧を渡す。
        allowed_profile_ids: draft.targets[PROFILE_ACCESS_KEY] ?? [],
      })
      .then(normalizedRole),
};

/** ロールごとの業務プロファイル利用権限。候補の取得に失敗しても、ロール一覧は警告付きで表示する。 */
const PROFILE_ACCESS_TARGET: RolePermissionTargetSection<SecurityRole> = {
  key: PROFILE_ACCESS_KEY,
  messages: {
    title: t("security.roles.profileAccess"),
    hint: t("security.roles.profileAccessHint"),
    all: t("security.roles.profileAccessAll"),
    count: t("security.roles.profileAccessCount"),
    searchLabel: t("security.roles.profileAccessSearch"),
    searchPlaceholder: t("security.roles.profileAccessSearchPlaceholder"),
    empty: t("security.roles.profileAccessEmpty"),
    noResults: t("security.roles.profileAccessNoResults"),
    loadWarning: t("security.roles.profileAccessLoadWarning"),
    grantsAllByPermission: t("security.roles.profileAccessManagedAll"),
    grantsAllSystemAdmin: t("security.roles.profileAccessSystemAdmin"),
  },
  // 候補はサーバー側で検索し、50 件ずつ読む。選択済みの名前は ids で読む（#608）。
  query: (query, { signal }) =>
    securityApi.profileAccessProfiles(query, { signal }).then((page) => ({
      items: page.items.map((profile) => ({
        id: profile.id,
        name: profile.name,
        secondary: profile.category,
        description: profile.description,
      })),
      total: page.total,
    })),
  selectedIds: (role) => role.allowed_profile_ids ?? [],
  grantsAll: (effectivePermissions) => effectivePermissions.has(PROFILE_MANAGE_PERMISSION),
};
const PERMISSION_TARGETS = [PROFILE_ACCESS_TARGET];

/**
 * 権限管理（NL2SQL 固有のメニュー。#206）。画面の実体は platform の共通 RolePermissionsPage（#220）。
 * ロールごとの機能権限と業務プロファイル利用権限を設定する。ロールの作成・名称変更・アーカイブはロール管理で行う。
 */
export function SecurityPermissionsPage() {
  const { hasPermission } = useAuth();
  return (
    <RolePermissionsPage<SecurityRole>
      api={PERMISSIONS_API}
      canManage={hasPermission(MENU_PERMISSIONS.securityPermissions)}
      targets={PERMISSION_TARGETS}
      listDetailStoragePrefix={LIST_DETAIL_STORAGE_PREFIX}
      messages={{
        title: t("nav.securityPermissions"),
        subtitle: t("security.permissions.subtitle"),
        listHint: t("security.permissions.listHint"),
        searchPlaceholder: t("security.permissions.searchPlaceholder"),
        formHint: t("security.permissions.formHint"),
        permissionsHint: t("security.roles.permissionsHint"),
      }}
    />
  );
}
