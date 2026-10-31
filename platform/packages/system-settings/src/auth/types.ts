/**
 * ログイン系の API 契約（backend は `pr_system_settings.auth.router`。3製品で同じ。#220）。
 * 通信（Cookie セッション・CSRF・エラー形式）は製品の API client が持ち、この型を満たす `api` を渡す。
 */

/** `GET /api/auth/me` などが返す現在のユーザーの共通項目。製品は項目を足した型をそのまま使ってよい。 */
export interface BaseCurrentUser {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: string;
  force_password_change: boolean;
  role_codes: string[];
  /** ロールの表示名（`role_codes` と同じ順）。サイドナビのアカウント欄に出す（#1644）。 */
  role_display_names: string[];
  is_system_admin: boolean;
  /** backend が `implies` を展開済みの権限コード。 */
  permissions: string[];
  debug_mode: boolean;
  password_change_allowed: boolean;
  /**
   * システムの時間帯（共通の設定 `PLATFORM_TIMEZONE`。IANA の地域名）。画面は日時をこの時間帯で表示する
   * （`AuthProvider` が `@production-ready/ui` の `setDisplayTimeZone` に渡す。#3265）。
   */
  timezone?: string;
}

export interface AuthRequestOptions {
  signal?: AbortSignal;
}

export interface AuthApi<U extends BaseCurrentUser = BaseCurrentUser> {
  /** `GET /api/auth/me` */
  me: (options?: AuthRequestOptions) => Promise<U>;
  /** `POST /api/auth/login {login_user_id, password}` */
  login: (loginUserId: string, password: string) => Promise<U>;
  /** `POST /api/auth/logout` */
  logout: () => Promise<unknown>;
  /** `POST /api/auth/password/change {current_password, new_password}` */
  changePassword: (currentPassword: string, newPassword: string) => Promise<unknown>;
}

/**
 * 認証の状態。`error` は起動時の確認（`me`）が 401 以外（5xx・通信断・timeout）で失敗し、ログイン済みかどうかが
 * 分からない状態（未認証と扱ってログイン画面へ移したり、作業状態を消したりしない。#1061）。
 */
export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

export type HasPermission = (permission: string) => boolean;

/** 認証画面とルートの保護が使う URL。 */
export interface AuthRoutes {
  login: string;
  passwordChange: string;
  forbidden: string;
}
