import assert from "node:assert/strict";
import test from "node:test";

import {
  CAPABILITY_PERMISSIONS,
  MENU_PERMISSIONS,
  currentUserHasPermission,
} from "../src/features/security/menu-permissions.ts";
import type { CurrentUser } from "../src/features/security/types.ts";

function currentUser(overrides: Partial<CurrentUser>): CurrentUser {
  return {
    user_uuid: "user-1",
    login_user_id: "operations.admin",
    display_name: "運用管理者",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: [],
    role_display_names: [],
    is_system_admin: false,
    permissions: [],
    data_entitlements: [],
    allowed_profile_ids: [],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
  };
}

test("SYSTEM_ADMIN 能力は login_user_id ではなく is_system_admin で判定する", () => {
  const user = currentUser({
    login_user_id: "operations.admin",
    role_codes: ["OPERATIONS_ADMIN"],
    is_system_admin: true,
  });

  assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.securityUsers), true);
  assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.adminSql), true);
});

test("通常ユーザーは付与された権限だけ利用できる", () => {
  const user = currentUser({
    login_user_id: "sales.user",
    role_codes: ["QUERY_USER"],
    permissions: [MENU_PERMISSIONS.query],
  });

  assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.query), true);
  assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.securityUsers), false);
});

test("コメント・アノテーション・ドメインの管理はスキーマの参照・更新を含む（#972）", () => {
  for (const menu of [
    MENU_PERMISSIONS.commentManagement,
    MENU_PERMISSIONS.annotationManagement,
    MENU_PERMISSIONS.domainManagement,
  ]) {
    const user = currentUser({ permissions: [menu] });
    assert.equal(currentUserHasPermission(user, CAPABILITY_PERMISSIONS.schemaRead), true, menu);
    assert.equal(currentUserHasPermission(user, CAPABILITY_PERMISSIONS.schemaRefresh), true, menu);
    assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.tableManagement), false, menu);
  }
});

test("用語・同義語と共通ルールの権限は業務プロファイル管理と学習素材管理を含まない（#1006）", () => {
  for (const menu of [MENU_PERMISSIONS.glossaryRules, MENU_PERMISSIONS.globalRules]) {
    const user = currentUser({ permissions: [menu] });

    assert.equal(currentUserHasPermission(user, menu), true);
    assert.equal(currentUserHasPermission(user, CAPABILITY_PERMISSIONS.schemaRead), true);
    assert.equal(currentUserHasPermission(user, CAPABILITY_PERMISSIONS.profilesManage), false);
    assert.equal(currentUserHasPermission(user, CAPABILITY_PERMISSIONS.learningMaterialManage), false);
    assert.equal(currentUserHasPermission(user, MENU_PERMISSIONS.profiles), false);
  }
});
