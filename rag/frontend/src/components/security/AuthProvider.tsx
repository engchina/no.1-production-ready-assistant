import type { ReactNode } from "react";
import {
  AuthProvider as SharedAuthProvider,
  useAuth as useSharedAuth,
  type AuthContextValue,
} from "@production-ready/system-settings";

import type { CurrentUser } from "@/lib/api";
import { securityApi } from "@/lib/security-api";
import { bindWorkspaceOwner, clearWorkspace } from "@/lib/workspace-state";

/**
 * 利用者と認可が変わったと判断する key（#214）。権限に加えて、利用できる検索・回答プロファイルとナレッジベースも含める。
 * どれかが変わったら共通の AuthProvider が React Query の cache を破棄し、一覧を取り直させる。
 */
export function ragIdentityKey(user: CurrentUser): string {
  const sorted = (values: string[] | null) => (values === null ? null : [...values].sort());
  return JSON.stringify([
    user.user_uuid,
    [...user.permissions].sort(),
    sorted(user.allowed_search_answer_profile_ids),
    sorted(user.allowed_knowledge_base_ids),
  ]);
}

/** 作業状態の一時保存を今の利用者に結び付け、未認証になったら消す（workspace-state.md）。 */
export function syncWorkspaceOwner(user: CurrentUser | null): void {
  if (user) bindWorkspaceOwner(user.user_uuid);
  else clearWorkspace();
}

/**
 * 認証状態。実体は platform の共通 AuthProvider（#220）。ローカル DEBUG（`debug_mode: true`）は
 * backend がログインなしで全権限の利用者を返すため、ログイン画面を出さずに全画面を使える。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  return (
    <SharedAuthProvider<CurrentUser>
      api={securityApi}
      identityKey={ragIdentityKey}
      onIdentityChange={syncWorkspaceOwner}
    >
      {children}
    </SharedAuthProvider>
  );
}

export function useAuth(): AuthContextValue<CurrentUser> {
  return useSharedAuth<CurrentUser>();
}
