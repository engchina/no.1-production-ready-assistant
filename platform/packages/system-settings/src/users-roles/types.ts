/**
 * ユーザー管理・ロール管理の API 契約（backend は `pr_system_settings.users_roles`。#206）。
 * 通信（認証 cookie・CSRF・エラー形式）は製品の API client が持ち、この型を満たす `api` を画面へ渡す。
 */

export const SYSTEM_ADMIN_ROLE_CODE = "SYSTEM_ADMIN";

export interface AssignedRole {
  role_id: string;
  role_code: string;
  display_name: string;
  is_built_in: boolean;
  archived: boolean;
  /** 削除済み等で参照できないロール。`role_code` / `display_name` は空で、画面は ID を出さない（#1682）。 */
  deleted?: boolean;
}

export interface SecurityUser {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: "ACTIVE" | "DISABLED";
  force_password_change: boolean;
  locked_until: string | null;
  version: number;
  role_ids: string[];
  assigned_roles?: AssignedRole[];
  is_bootstrap_admin: boolean;
}

/** ロールの共通部分。製品は権限などの項目を足した型をそのまま渡してよい。 */
export interface SecurityRole {
  role_id: string;
  role_code: string;
  display_name: string;
  description: string;
  is_built_in: boolean;
  archived: boolean;
  version: number;
}

export interface UserDraft {
  login_user_id: string;
  display_name: string;
  role_ids: string[];
  temporary_password?: string;
}

/** ロール管理画面の新規作成。権限は含めない（製品の権限管理で付ける）。 */
export interface RoleDraft {
  role_code: string;
  display_name: string;
  description: string;
}

export interface UserWithTemporaryPassword {
  user: SecurityUser;
  temporary_password: string;
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export interface UserManagementApi<R extends SecurityRole = SecurityRole> {
  users: (options?: RequestOptions) => Promise<SecurityUser[]>;
  createUser: (draft: UserDraft) => Promise<UserWithTemporaryPassword>;
  updateUser: (user: SecurityUser) => Promise<SecurityUser>;
  deleteUser: (user: SecurityUser) => Promise<unknown>;
  resetPassword: (userUuid: string, temporaryPassword?: string) => Promise<UserWithTemporaryPassword>;
  unlockUser: (userUuid: string) => Promise<SecurityUser>;
  setUserEnabled: (user: SecurityUser, enabled: boolean) => Promise<SecurityUser>;
  roles: (includeArchived?: boolean, options?: RequestOptions) => Promise<R[]>;
}

export interface RoleManagementApi<R extends SecurityRole = SecurityRole> {
  roles: (includeArchived?: boolean, options?: RequestOptions) => Promise<R[]>;
  createRole: (draft: RoleDraft) => Promise<R>;
  /** 基本情報（version / display_name / description）だけを送る。 */
  updateRole: (role: R) => Promise<R>;
  archiveRole: (role: R) => Promise<R>;
  restoreRole: (role: R) => Promise<R>;
  deleteRole: (role: R) => Promise<unknown>;
}

/** 入力項目に結び付く API の問題（JSON Pointer と表示文言）。 */
export interface ApiFieldProblem {
  pointer: string;
  message: string;
}

/** 製品の API エラーから、画面が使う情報だけを取り出したもの。 */
export interface ApiErrorDetails {
  message?: string;
  code?: string;
  fieldErrors?: ApiFieldProblem[];
}

export type DescribeApiError = (error: unknown) => ApiErrorDetails | undefined;
