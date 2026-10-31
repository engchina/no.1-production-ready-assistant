import type { ReactNode } from "react";
import { Navigate, useHref, useLocation, useNavigate } from "react-router-dom";
import { ApiErrorState, PageLoadingState, type SidebarAccountLink } from "@production-ready/ui";

import { ACCOUNT_PATH } from "../paths";

import { useAuth } from "./AuthProvider";
import { AUTH_MESSAGES, formatMessage, type AuthMessages } from "./messages";
import type { AuthRoutes, BaseCurrentUser } from "./types";

export interface RequireAuthProps {
  routes: AuthRoutes;
  /** 今の URL を開くのに必要な権限コード（無ければ認証だけを確認する）。 */
  requiredPermission?: string;
  messages?: Partial<Pick<AuthMessages, "loading" | "sessionCheckError" | "sessionCheckErrorFallback">>;
  children: ReactNode;
}

/**
 * 認証が必要な画面の入口（NL2SQL の App から移設。#220）。
 * 確認中は読み込み表示、確認の失敗（5xx・通信断）は失敗と再試行（#1061）、未認証はログインへ（元の URL を state.from に載せる）、
 * 強制パスワード変更中はパスワード変更へ、権限が無ければ権限なしの画面へ移す。
 */
export function RequireAuth({ routes, requiredPermission, messages, children }: RequireAuthProps) {
  const auth = useAuth();
  const location = useLocation();
  const m = { ...AUTH_MESSAGES, ...messages };

  if (auth.status === "loading") {
    return (
      // 画面の中央に浮かせず、本文の先頭に本文と同じ幅で出す（ほかの読み込み中と同じ。#2510）。
      <main className="min-h-screen bg-canvas">
        <PageLoadingState label={m.loading} operationKey="auth-session" testId="auth-session-loading" />
      </main>
    );
  }
  if (auth.status === "error") {
    // ログイン済みかどうかが分からないため、ログイン画面へ移さず、失敗と再試行を出す（#1061）。
    return (
      <main className="flex min-h-screen items-center justify-center bg-canvas p-4">
        <div className="grid w-full max-w-lg gap-3" data-testid="auth-session-error">
          <h1 className="text-base font-semibold text-fg">{m.sessionCheckError}</h1>
          <ApiErrorState
            error={auth.error}
            fallback={m.sessionCheckErrorFallback}
            onRetry={() => void auth.refresh()}
          />
        </div>
      </main>
    );
  }
  if (auth.status === "unauthenticated") {
    return <Navigate to={routes.login} state={{ from: location.pathname }} replace />;
  }
  if (auth.user?.force_password_change) {
    return <Navigate to={routes.passwordChange} replace />;
  }
  if (requiredPermission && !auth.hasPermission(requiredPermission)) {
    return <Navigate to={routes.forbidden} replace />;
  }
  return <>{children}</>;
}

/** アカウント欄が開く画面。`account` はアカウントの画面（既定 `/account`。#1873）。 */
export type SidebarAccountRoutes = Pick<AuthRoutes, "login"> & { account?: string };

export interface SidebarAccount {
  name: string;
  /** ロールの表示名（複数は区切りでつなぐ）。アカウント欄の 2 行目に出す。 */
  roles: string;
  /** ロールの行の全文（「ロール: …」）。title と読み上げに使う。 */
  rolesLabel: string;
  /** 欄の行全体のリンク（アカウントの画面。API キーとパスワード変更のタブ。#1873）。 */
  account: SidebarAccountLink;
  /** ローカル DEBUG（ログイン省略）では undefined。 */
  onLogout?: () => void;
  labels: { logout: string; switchToLight: string; switchToDark: string };
  /** ローカル DEBUG（ログイン省略）で入っているか。製品はその旨の notice を出す。 */
  debugMode: boolean;
}

/**
 * アカウント欄に出すロール。code ではなく表示名を区切りでつなぐ。表示名の無いロールだけ code（#1644）。
 */
export function sidebarRoleNames(
  user: Pick<BaseCurrentUser, "role_codes" | "role_display_names">,
  separator: string
): string {
  return user.role_codes
    .map((code, index) => user.role_display_names?.[index]?.trim() || code)
    .join(separator);
}

/**
 * アカウント欄の 1 行目。ログイン ID（code）を出す。ほかの画面は表示名を出す（#1696）。
 * ログインの無い local（debug）はログイン ID が固定の記号なので、表示名（「ローカル利用者」など）を出す。
 */
export function sidebarAccountName(
  user: Pick<BaseCurrentUser, "login_user_id" | "display_name" | "debug_mode">
): string {
  if (user.debug_mode) return user.display_name;
  return user.login_user_id.trim() || user.display_name;
}

/**
 * `SidebarAccountFooter` に渡す値（ログイン ID・ロール名・アカウントの画面へのリンク・ログアウト）を作る（#220・#1696・#1873）。
 * 未ログインなら null。ログアウト後はログイン画面へ移す。個人の操作（API キー・パスワード変更）は、欄から開く
 * アカウントの画面のタブにまとめる（サイドナビの行には出さない）。
 */
export function useSidebarAccount({
  routes,
  messages,
}: {
  routes: SidebarAccountRoutes;
  messages?: Partial<AuthMessages>;
}): SidebarAccount | null {
  const auth = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const accountPath = routes.account ?? ACCOUNT_PATH;
  const accountHref = useHref(accountPath);
  const m = { ...AUTH_MESSAGES, ...messages };
  const user = auth.user;
  if (!user) return null;
  const roles = sidebarRoleNames(user, m.sidebarRoleSeparator);
  const name = sidebarAccountName(user);
  return {
    name,
    roles,
    rolesLabel: formatMessage(m.sidebarRoles, { roles }),
    debugMode: user.debug_mode,
    labels: { logout: m.sidebarLogout, switchToLight: "", switchToDark: "" },
    account: {
      href: accountHref,
      label: formatMessage(m.sidebarOpenAccount, { name }),
      active: pathname === accountPath || pathname.startsWith(`${accountPath}/`),
      onNavigate: () => navigate(accountPath),
    },
    onLogout: user.debug_mode
      ? undefined
      : () => void auth.logout().finally(() => navigate(routes.login, { replace: true })),
  };
}
