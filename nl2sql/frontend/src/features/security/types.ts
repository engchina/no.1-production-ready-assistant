import type {
  AssignedRole,
  SecurityRole as SharedSecurityRole,
  SecurityUser,
} from "@production-ready/system-settings";

export type { AssignedRole, SecurityUser };

export interface CurrentUser {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: string;
  force_password_change: boolean;
  role_codes: string[];
  /** ロールの表示名（`role_codes` と同じ順。#1644）。 */
  role_display_names: string[];
  is_system_admin: boolean;
  permissions: string[];
  allowed_profile_ids: string[];
  debug_mode: boolean;
  password_change_allowed: boolean;
}

export interface SecurityUserDeleteResult {
  deleted: boolean;
  user_uuid: string;
  login_user_id: string;
}

/** 共通のロール項目（platform）に、NL2SQL の権限・Data Grant・業務プロファイル利用権限を足す（#206）。 */
export interface SecurityRole extends SharedSecurityRole {
  permissions: string[];
  allowed_profile_ids: string[];
}

export interface SecurityRoleDeleteResult {
  deleted: boolean;
  role_id: string;
  role_code: string;
}

export interface ProfileAccessProfile {
  id: string;
  name: string;
  category: string;
  description: string;
  archived: boolean;
  /** 業務プロファイルを使えるロール（ロール名の順）。画面はロール名を出し、ID・コードは出さない（#1682）。 */
  allowed_roles: ProfileAccessRole[];
}

/** 業務プロファイルを使えるロール。`role_id` は key にだけ使う（#1682）。 */
export interface ProfileAccessRole {
  role_id: string;
  role_code: string;
  display_name: string;
}

/** 権限管理の業務プロファイルの候補の 1 ページ（検索とページング。#608）。 */
export interface ProfileAccessProfilePage {
  items: ProfileAccessProfile[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

export interface PermissionDefinition {
  code: string;
  group: string;
  label: string;
  description: string;
  implies: string[];
}
