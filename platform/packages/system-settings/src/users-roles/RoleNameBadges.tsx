// ロールを画面に出す共通の部品（#1682）。
// ロールはロール名（表示名。例「システム管理者」）で出す。表示名が空ならロールコード。
// ID（UUID の `role_id`）は文字にも tooltip にも出さず、参照できない（削除済みの）ロールは
// 「（削除済みのロール）」と出す。3 製品で同じ関数・部品を使う。
import { compareNames, RowTitleButton, StatusBadge, cn, type RowTitleButtonProps } from "@production-ready/ui";

import { t } from "./messages";

/** 画面に出すロールの参照。`role_id` は key にだけ使い、表示には使わない。 */
export interface RoleNameReference {
  role_id: string;
  display_name?: string | null;
  role_code?: string | null;
  /** 削除済み等で参照できないロール（backend の `AssignedRoleData.deleted`）。 */
  deleted?: boolean;
}

/** 削除済み・参照できないロールの文言の既定。 */
export function deletedRoleLabel(): string {
  return t("security.roles.deletedRole");
}

/**
 * ロールを画面に出す名前。表示名を返し、表示名が空ならロールコード、どちらも無い・削除済みの
 * ロールは ID を出さずに「（削除済みのロール）」を返す。
 */
export function roleDisplayName(
  role: Pick<RoleNameReference, "display_name" | "role_code" | "deleted"> | null | undefined,
  deletedLabel: string = deletedRoleLabel(),
): string {
  if (!role || role.deleted) return deletedLabel;
  return role.display_name?.trim() || role.role_code?.trim() || deletedLabel;
}

/** ロール名で並べるための比較（共通の名前の順。#2753）。 */
export function compareRoleDisplayNames(
  left: Pick<RoleNameReference, "display_name" | "role_code" | "deleted">,
  right: Pick<RoleNameReference, "display_name" | "role_code" | "deleted">,
): number {
  return compareNames(roleDisplayName(left), roleDisplayName(right));
}

export interface RoleNameBadgesProps {
  roles: readonly RoleNameReference[];
  /** 削除済みのロールの文言（製品の i18n で差し替えるとき）。 */
  deletedLabel?: string;
  /** 一覧の名前（読み上げ用）。 */
  ariaLabel?: string;
  className?: string;
}

/** ロールをロール名のバッジで並べる（業務プロファイルの「利用可能ロール」など）。 */
export function RoleNameBadges({ roles, deletedLabel, ariaLabel, className }: RoleNameBadgesProps) {
  const fallback = deletedLabel ?? deletedRoleLabel();
  return (
    <ul
      className={cn("flex flex-wrap gap-1.5", className)}
      aria-label={ariaLabel}
      data-testid="role-name-badges"
    >
      {roles.map((role) => (
        <li key={role.role_id} className="min-w-0">
          <StatusBadge icon={false} variant="neutral" label={roleDisplayName(role, fallback)} />
        </li>
      ))}
    </ul>
  );
}

/** 一覧の行の題名のボタン（ロール名。補足 1 行は `subtitle` で渡す。#2282）。 */
export function RoleNameRowTitleButton({
  securityRole,
  ...props
}: { securityRole: RoleNameReference } & Omit<RowTitleButtonProps, "title">) {
  return <RowTitleButton {...props} title={roleDisplayName(securityRole)} />;
}
