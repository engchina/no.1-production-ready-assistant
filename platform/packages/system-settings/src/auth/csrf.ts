/**
 * Cookie セッションの CSRF 対策（double submit。#220）。
 * ログインの Cookie は 3 製品で共有するため、CSRF の Cookie 名もここ 1 か所に置く（#1565）。
 */
export const CSRF_HEADER_NAME = "X-CSRF-Token";

/**
 * CSRF の Cookie 名。backend の共通の設定 `PLATFORM_AUTH_CSRF_COOKIE_NAME` の既定値
 * （`pr_system_settings.auth.domain.DEFAULT_CSRF_COOKIE_NAME`）と同じにする。
 */
export const CSRF_COOKIE_NAME = "pr_csrf";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** document.cookie から値を 1 つ読む。ブラウザ外や未設定なら null。 */
export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

/** 状態を変える method（POST / PUT / PATCH / DELETE）のときだけ、共有の CSRF の Cookie の値を `X-CSRF-Token` として返す。 */
export function csrfHeader(method = "GET"): Record<string, string> {
  if (!UNSAFE_METHODS.has(method.toUpperCase())) return {};
  const token = readCookie(CSRF_COOKIE_NAME);
  return token ? { [CSRF_HEADER_NAME]: token } : {};
}
