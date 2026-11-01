import { apiDelete, apiGet, apiPatch, apiPost, apiPut, type ApiRequestOptions } from "@/lib/api";
import {
  rolePermissionTargetSearchParams,
  type ApiKeyCreated,
  type ApiKeyCreateRequest,
  type ApiKeysAdminApi,
  type ApiKeysList,
  type MyApiKeysApi,
  type MyApiKeysList,
  type RoleDraft,
  type RolePermissionTargetQuery,
  type UserDraft,
} from "@production-ready/system-settings";

import type {
  CurrentUser,
  PermissionDefinition,
  ProfileAccessProfilePage,
  SecurityRole,
  SecurityRoleDeleteResult,
  SecurityUser,
  SecurityUserDeleteResult,
} from "./types";

export type { RoleDraft, UserDraft };

/** 権限管理画面が送る、ロールの権限と業務プロファイル利用権限（#206）。 */
export type RolePermissionsDraft = Pick<
  SecurityRole,
  "role_id" | "version" | "permissions" | "allowed_profile_ids"
>;

export const securityApi = {
  login: (loginUserId: string, password: string) =>
    apiPost<CurrentUser>("/api/auth/login", { login_user_id: loginUserId, password }),
  me: (options: ApiRequestOptions = {}) => apiGet<CurrentUser>("/api/auth/me", options),
  logout: () => apiPost<{ logged_out: boolean }>("/api/auth/logout"),
  changePassword: (currentPassword: string, newPassword: string) =>
    apiPost<{ changed: boolean }>("/api/auth/password/change", {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  users: (options: ApiRequestOptions = {}) =>
    apiGet<SecurityUser[]>("/api/security/users", options),
  createUser: (draft: UserDraft) =>
    apiPost<{ user: SecurityUser; temporary_password: string }>("/api/security/users", draft),
  updateUser: (user: SecurityUser) =>
    apiPatch<SecurityUser>(`/api/security/users/${user.user_uuid}`, {
      version: user.version,
      display_name: user.display_name,
      status: user.status,
      role_ids: user.role_ids,
    }),
  deleteUser: (user: SecurityUser) =>
    apiDelete<SecurityUserDeleteResult>(`/api/security/users/${user.user_uuid}`, {
      "If-Match": `"${user.version}"`,
    }),
  resetPassword: (userUuid: string, temporaryPassword?: string) =>
    apiPost<{ user: SecurityUser; temporary_password: string }>(
      `/api/security/users/${userUuid}/reset-password`,
      { temporary_password: temporaryPassword || null }
    ),
  unlockUser: (userUuid: string) =>
    apiPost<SecurityUser>(`/api/security/users/${userUuid}/unlock`),
  setUserEnabled: (user: SecurityUser, enabled: boolean) =>
    apiPost<SecurityUser>(
      `/api/security/users/${user.user_uuid}/${enabled ? "enable" : "disable"}`,
      { version: user.version }
    ),
  roles: (includeArchived = false, options: ApiRequestOptions = {}) =>
    apiGet<SecurityRole[]>(
      `/api/security/roles?include_archived=${String(includeArchived)}`,
      options
    ),
  createRole: (draft: RoleDraft) => apiPost<SecurityRole>("/api/security/roles", draft),
  // ロール管理（共通画面）は基本情報だけを送る。権限は updateRolePermissions（権限管理）で送る（#206）。
  updateRole: (role: SecurityRole) =>
    apiPatch<SecurityRole>(`/api/security/roles/${role.role_id}`, {
      version: role.version,
      display_name: role.display_name,
      description: role.description,
    }),
  updateRolePermissions: (role: RolePermissionsDraft) =>
    apiPut<SecurityRole>(`/api/security/roles/${role.role_id}/permissions`, {
      version: role.version,
      permissions: role.permissions,
      allowed_profile_ids: role.allowed_profile_ids,
    }),
  archiveRole: (role: SecurityRole) =>
    apiPost<SecurityRole>(`/api/security/roles/${role.role_id}/archive`, {
      version: role.version,
    }),
  restoreRole: (role: SecurityRole) =>
    apiPost<SecurityRole>(`/api/security/roles/${role.role_id}/restore`, {
      version: role.version,
    }),
  deleteRole: (role: SecurityRole) =>
    apiDelete<SecurityRoleDeleteResult>(`/api/security/roles/${role.role_id}`, {
      "If-Match": `"${role.version}"`,
    }),
  permissions: (options: ApiRequestOptions = {}) =>
    apiGet<PermissionDefinition[]>("/api/security/permissions", options),
  /** 権限管理で選べる業務プロファイルの候補（サーバー側の検索とページング。#608）。 */
  profileAccessProfiles: (query: RolePermissionTargetQuery, options: ApiRequestOptions = {}) =>
    apiGet<ProfileAccessProfilePage>(
      `/api/security/profile-access/profiles?${rolePermissionTargetSearchParams(query).toString()}`,
      options,
    ),
};

/** 自分の API キー（`/api/account/api-keys*`。ログインだけで使える。#1805）。 */
export const myApiKeysApi: MyApiKeysApi = {
  list: () => apiGet<MyApiKeysList>("/api/account/api-keys"),
  create: (payload: ApiKeyCreateRequest) => apiPost<ApiKeyCreated>("/api/account/api-keys", payload),
  revoke: (keyId: string) => apiDelete<unknown>(`/api/account/api-keys/${encodeURIComponent(keyId)}`),
};

/** 全員の API キー（`/api/security/api-keys*`。管理のメニュー権限。#1805）。 */
export const apiKeysAdminApi: ApiKeysAdminApi = {
  list: () => apiGet<ApiKeysList>("/api/security/api-keys"),
  revoke: (keyId: string) => apiDelete<unknown>(`/api/security/api-keys/${encodeURIComponent(keyId)}`),
};
