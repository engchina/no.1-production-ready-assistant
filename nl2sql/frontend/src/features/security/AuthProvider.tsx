import type { ReactNode } from "react";
import {
  AuthProvider as SharedAuthProvider,
  useAuth as useSharedAuth,
  type AuthContextValue,
  type HasPermission,
} from "@production-ready/system-settings";

import { bindWorkspaceOwner, clearWorkspaceDrafts } from "@/lib/workspace-drafts";
import { securityApi } from "./api";
import { currentUserHasPermission, normalizeMenuPermissions } from "./menu-permissions";
import type { CurrentUser } from "./types";

/**
 * 利用者と認可が変わったと判断する key。NL2SQL は権限に加えて Data Grant と業務プロファイル利用権限も含め、
 * どれかが変わったら React Query の cache を破棄する（#220 で共通の AuthProvider に渡す形にした）。
 */
export function nl2sqlIdentityKey(user: CurrentUser): string {
  return JSON.stringify([
    user.user_uuid,
    [...user.permissions].sort(),
    user.data_entitlements,
    user.allowed_profile_ids,
  ]);
}

/** 旧コードの読み替えと NL2SQL の権限展開表で判定する（backend の展開前のコードにも対応する）。 */
export function nl2sqlPermissionCheck(user: CurrentUser): HasPermission {
  const normalizedPermissions = normalizeMenuPermissions(user.permissions);
  return (permission) => currentUserHasPermission(user, permission, normalizedPermissions);
}

/** 作業中の下書きの持ち主を記録し、未認証になったら下書きを消す。 */
function syncWorkspaceOwner(user: CurrentUser | null) {
  if (user) bindWorkspaceOwner(window.sessionStorage, user.user_uuid);
  else clearWorkspaceDrafts(window.sessionStorage);
}

/** 認証状態。実体は platform の共通 AuthProvider（#220）。 */
export function AuthProvider({ children }: { children: ReactNode }) {
  return (
    <SharedAuthProvider<CurrentUser>
      api={securityApi}
      identityKey={nl2sqlIdentityKey}
      onIdentityChange={syncWorkspaceOwner}
      createPermissionCheck={nl2sqlPermissionCheck}
    >
      {children}
    </SharedAuthProvider>
  );
}

export function useAuth(): AuthContextValue<CurrentUser> {
  return useSharedAuth<CurrentUser>();
}
