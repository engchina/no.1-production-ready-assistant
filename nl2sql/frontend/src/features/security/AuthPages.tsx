import {
  AUTH_MESSAGES,
  ForbiddenPage as SharedForbiddenPage,
  LoginPage as SharedLoginPage,
  PasswordChangePage as SharedPasswordChangePage,
} from "@production-ready/system-settings";

import { ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import { defaultEntryRoute } from "./route-permissions";

// ログイン・パスワード変更・権限なしの画面は platform の共通部品（#220）。NL2SQL は wordmark とルートだけを渡す。
const brand = { line1: t("app.sidebarTitle.line1"), line2: t("app.sidebarTitle.line2") };
const routes = { login: APP_ROUTES.login, passwordChange: APP_ROUTES.passwordChange };

/** 認証失敗は利用者の入力ミスなので、request ID を含まない本文だけを出す。 */
function describeLoginError(cause: unknown): string | undefined {
  return cause instanceof ApiError ? cause.baseMessages[0] || AUTH_MESSAGES.loginError : undefined;
}

export function LoginPage() {
  return (
    <SharedLoginPage
      brand={brand}
      routes={routes}
      entryRoute={defaultEntryRoute}
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
