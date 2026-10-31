import { useLocation } from "react-router-dom";
import {
  AUTH_MESSAGES,
  ForbiddenPage as SharedForbiddenPage,
  LoginPage as SharedLoginPage,
  PasswordChangePage as SharedPasswordChangePage,
  type HasPermission,
} from "@production-ready/system-settings";

import { ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { canOpenRoute, defaultEntryRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";

// ログイン・パスワード変更・権限なしの画面は platform の共通部品（#220）。RAG は wordmark とルートだけを渡す（#214）。
const brand = { line1: t("app.sidebarTitle.line1"), line2: t("app.sidebarTitle.line2") };
const routes = { login: APP_ROUTES.login, passwordChange: APP_ROUTES.passwordChange };

/** 認証失敗は利用者の入力ミスなので、backend の本文（request ID を含まない）だけを出す。 */
function describeLoginError(cause: unknown): string | undefined {
  return cause instanceof ApiError ? cause.messages[0] || AUTH_MESSAGES.loginError : undefined;
}

/**
 * ログイン前に開こうとした URL（RequireAuth が state.from に載せる）へ、権限があれば戻す。
 * 共通の LoginPage はログイン済みの描画で entryRoute へ移すため、ここで state.from を入口に含める。
 */
export function loginEntryRoute(from: unknown) {
  return (hasPermission: HasPermission): string =>
    typeof from === "string" &&
    from.startsWith("/") &&
    !from.startsWith("//") &&
    from !== APP_ROUTES.login &&
    from !== APP_ROUTES.forbidden &&
    canOpenRoute(from, hasPermission)
      ? from
      : defaultEntryRoute(hasPermission);
}

export function LoginPage() {
  const from = (useLocation().state as { from?: unknown } | null)?.from;
  return (
    <SharedLoginPage
      brand={brand}
      routes={routes}
      entryRoute={loginEntryRoute(from)}
      describeLoginError={describeLoginError}
    />
  );
}

export function PasswordChangePage() {
  return <SharedPasswordChangePage brand={brand} routes={routes} entryRoute={defaultEntryRoute} />;
}

export function ForbiddenPage() {
  return <SharedForbiddenPage brand={brand} entryRoute={defaultEntryRoute} />;
}
