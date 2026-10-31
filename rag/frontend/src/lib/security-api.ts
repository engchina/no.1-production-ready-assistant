/**
 * 認証・ユーザー管理・ロール管理・権限管理の API（#214）。
 *
 * - `/api/auth/*`・`/api/security/users*`・`/api/security/roles*` は platform の共通 router（3製品で同じ）。
 * - `/api/security/permissions`・`/api/security/access-targets/{kind}`・`PUT /api/security/roles/{id}/access` は RAG 固有。
 * 通信（Cookie セッション・CSRF・401 / 403 の通知・エラー形式）は `request`（lib/api.ts）が持つ。
 */

import { apiErrorMessage } from "@production-ready/ui";
import {
  rolePermissionTargetSearchParams,
  type ApiKeyCreated,
  type ApiKeyCreateRequest,
  type ApiKeysAdminApi,
  type ApiKeysList,
  type MyApiKeysApi,
  type MyApiKeysList,
  type AuthApi,
  type DescribeApiError,
  type PermissionDefinition,
  type RoleDraft,
  type RoleManagementApi,
  type RolePermissionTargetQuery,
  type SecurityUser,
  type UserDraft,
  type UserManagementApi,
  type UserWithTemporaryPassword,
} from "@production-ready/system-settings";

import {
  ApiError,
  request,
  type AccessTargetKind,
  type AccessTargetPage,
  type CurrentUser,
  type RoleAccessUpdate,
  type SecurityRole,
} from "./api";

interface RequestOptions {
  signal?: AbortSignal;
}

function send(method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown, headers?: HeadersInit): RequestInit {
  const merged = new Headers(headers);
  if (body !== undefined) merged.set("Content-Type", "application/json");
  return {
    method,
    headers: merged,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

/**
 * ユーザー / ロール / 権限の更新。「自分が持たない権限・対象は付けられない」などの 403 は
 * 経路の権限拒否ではないので、共通画面のフォームに理由を出す（error_code で判定。#224）。
 */
function mutate<T>(path: string, init: RequestInit): Promise<T> {
  return request<T>(path, init);
}

/** 楽観ロックの version を `If-Match` で送る（削除）。 */
function ifMatch(version: number): HeadersInit {
  return { "If-Match": `"${version}"` };
}

export const securityApi = {
  // ---- 認証（共通の AuthProvider が使う） ----
  me: (options: RequestOptions = {}) => request<CurrentUser>("/api/auth/me", { signal: options.signal }),
  login: (loginUserId: string, password: string) =>
    request<CurrentUser>(
      "/api/auth/login",
      send("POST", { login_user_id: loginUserId, password }),
    ),
  logout: () => request<{ logged_out: boolean }>("/api/auth/logout", send("POST")),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ changed: boolean }>(
      "/api/auth/password/change",
      send("POST", { current_password: currentPassword, new_password: newPassword }),
    ),

  // ---- ユーザー管理（共通） ----
  users: (options: RequestOptions = {}) =>
    request<SecurityUser[]>("/api/security/users", { signal: options.signal }),
  createUser: (draft: UserDraft) =>
    mutate<UserWithTemporaryPassword>("/api/security/users", send("POST", draft)),
  updateUser: (user: SecurityUser) =>
    mutate<SecurityUser>(
      `/api/security/users/${encodeURIComponent(user.user_uuid)}`,
      send("PATCH", {
        version: user.version,
        display_name: user.display_name,
        status: user.status,
        role_ids: user.role_ids,
      }),
    ),
  deleteUser: (user: SecurityUser) =>
    mutate<unknown>(
      `/api/security/users/${encodeURIComponent(user.user_uuid)}`,
      send("DELETE", undefined, ifMatch(user.version)),
    ),
  resetPassword: (userUuid: string, temporaryPassword?: string) =>
    mutate<UserWithTemporaryPassword>(
      `/api/security/users/${encodeURIComponent(userUuid)}/reset-password`,
      send("POST", { temporary_password: temporaryPassword || null }),
    ),
  unlockUser: (userUuid: string) =>
    mutate<SecurityUser>(
      `/api/security/users/${encodeURIComponent(userUuid)}/unlock`,
      send("POST"),
    ),
  setUserEnabled: (user: SecurityUser, enabled: boolean) =>
    mutate<SecurityUser>(
      `/api/security/users/${encodeURIComponent(user.user_uuid)}/${enabled ? "enable" : "disable"}`,
      send("POST", { version: user.version }),
    ),

  // ---- ロール管理（共通。基本情報だけを送る） ----
  roles: (includeArchived = false, options: RequestOptions = {}) =>
    request<SecurityRole[]>(`/api/security/roles?include_archived=${String(includeArchived)}`, {
      signal: options.signal,
    }).then((rows) => rows.map(normalizeRole)),
  createRole: (draft: RoleDraft) =>
    mutate<SecurityRole>("/api/security/roles", send("POST", draft)).then(normalizeRole),
  updateRole: (role: SecurityRole) =>
    mutate<SecurityRole>(
      `/api/security/roles/${encodeURIComponent(role.role_id)}`,
      send("PATCH", {
        version: role.version,
        display_name: role.display_name,
        description: role.description,
      }),
    ).then(normalizeRole),
  archiveRole: (role: SecurityRole) =>
    mutate<SecurityRole>(
      `/api/security/roles/${encodeURIComponent(role.role_id)}/archive`,
      send("POST", { version: role.version }),
    ).then(normalizeRole),
  restoreRole: (role: SecurityRole) =>
    mutate<SecurityRole>(
      `/api/security/roles/${encodeURIComponent(role.role_id)}/restore`,
      send("POST", { version: role.version }),
    ).then(normalizeRole),
  deleteRole: (role: SecurityRole) =>
    mutate<unknown>(
      `/api/security/roles/${encodeURIComponent(role.role_id)}`,
      send("DELETE", undefined, ifMatch(role.version)),
    ),

  // ---- 権限管理（RAG 固有） ----
  permissions: (options: RequestOptions = {}) =>
    request<PermissionDefinition[]>("/api/security/permissions", { signal: options.signal }),
  /** 権限管理で選べる検索・回答プロファイル / KB の候補（サーバー側の検索とページング。#608）。 */
  accessTargets: (kind: AccessTargetKind, query: RolePermissionTargetQuery, options: RequestOptions = {}) =>
    request<AccessTargetPage>(
      `/api/security/access-targets/${kind}?${rolePermissionTargetSearchParams(query).toString()}`,
      { signal: options.signal },
    ),
  /** ロールの RAG 権限と対象範囲（検索・回答プロファイル・KB）だけを置き換える。 */
  updateRoleAccess: (update: RoleAccessUpdate) =>
    mutate<SecurityRole>(
      `/api/security/roles/${encodeURIComponent(update.role_id)}/access`,
      send("PUT", {
        version: update.version,
        permissions: update.permissions,
        search_answer_profile_ids: update.search_answer_profile_ids,
        knowledge_base_ids: update.knowledge_base_ids,
      }),
    ).then(normalizeRole),
} satisfies AuthApi<CurrentUser> &
  UserManagementApi<SecurityRole> &
  RoleManagementApi<SecurityRole> &
  Record<string, unknown>;

/** 古い応答や部分応答でも一覧・件数の表示が崩れないよう、配列の項目を補う。 */
export function normalizeRole(role: SecurityRole): SecurityRole {
  return {
    ...role,
    permissions: role.permissions ?? [],
    search_answer_profile_ids: role.search_answer_profile_ids ?? [],
    knowledge_base_ids: role.knowledge_base_ids ?? [],
  };
}

/** 共通のユーザー管理・ロール管理画面へ、入力項目のエラーとエラーコードを渡す。 */
export const describeSecurityApiError: DescribeApiError = (error) => {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      code: error.errorCode,
      fieldErrors: error.fieldErrors.map(({ pointer, message }) => ({ pointer, message })),
    };
  }
  // timeout・通信断は利用者向けの文、組み込みの例外（英語の文）は共通画面の既定の文にする（#906）。
  const message = apiErrorMessage(error, "");
  return message ? { message } : undefined;
};

/** 自分の API キー（`/api/account/api-keys*`。ログインだけで使える。#1805）。 */
export const myApiKeysApi: MyApiKeysApi = {
  list: () => request<MyApiKeysList>("/api/account/api-keys"),
  create: (payload: ApiKeyCreateRequest) =>
    mutate<ApiKeyCreated>("/api/account/api-keys", send("POST", payload)),
  revoke: (keyId: string) =>
    mutate<unknown>(`/api/account/api-keys/${encodeURIComponent(keyId)}`, send("DELETE")),
};

/** 全員の API キー（`/api/security/api-keys*`。管理のメニュー権限。#1805）。 */
export const apiKeysAdminApi: ApiKeysAdminApi = {
  list: () => request<ApiKeysList>("/api/security/api-keys"),
  revoke: (keyId: string) =>
    mutate<unknown>(`/api/security/api-keys/${encodeURIComponent(keyId)}`, send("DELETE")),
};
