/**
 * ユーザー管理・ロール管理の送信前の検証（#540）。
 * 規則は backend（`pr_system_settings.users_roles`）と同じにする。画面の検証は先回りで、正本は backend。
 */

/** backend の `RoleCreateRequest.normalize_role_code` と同じ規則（英大文字で始まる 2〜64 文字）。 */
const ROLE_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;

export type RoleCodeErrorKey =
  | "security.roles.codeRequired"
  | "security.roles.codeTooShort"
  | "security.roles.codeInvalid";

/** ロールコード（大文字化・前後の空白を除いた値）の検証。問題が無ければ null。 */
export function roleCodeValidationError(normalizedRoleCode: string): RoleCodeErrorKey | null {
  if (!normalizedRoleCode) return "security.roles.codeRequired";
  if (normalizedRoleCode.length < 2) return "security.roles.codeTooShort";
  if (!ROLE_CODE_PATTERN.test(normalizedRoleCode)) return "security.roles.codeInvalid";
  return null;
}
