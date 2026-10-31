import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * API 境界から認証状態の変化を画面へ伝える window イベント（NL2SQL の名前をそのまま使う。#220）。
 * 製品の API client は応答ごとに `notifyAuthStatus` を呼ぶ。401 は `AuthProvider` が未認証に戻し、
 * 403 は `useForbiddenRedirect` を使う画面が権限なしの画面へ移す。
 */
export const AUTH_UNAUTHORIZED_EVENT = "app-auth-unauthorized";
export const AUTH_FORBIDDEN_EVENT = "app-auth-forbidden";

export interface AuthForbiddenDetail {
  requestId?: string;
}

/**
 * 経路（API そのもの）の権限拒否を表す error_code（backend の `ROUTE_FORBIDDEN_CODES`。#224）。
 * 権限なしの画面へ移すのはこれらのときと、error_code のない従来の 403 のときだけ。
 * ほかの 403（権限の付与の制限・範囲外・CSRF など）は、呼び出した画面がその場で理由を表示する。
 */
export const ROUTE_FORBIDDEN_ERROR_CODES: ReadonlySet<string> = new Set([
  "SECURITY_ROUTE_FORBIDDEN",
  "SECURITY_ROUTE_UNCLASSIFIED",
]);

/** 403 を権限なしの画面へ移すべきか（error_code がなければ従来どおり移す）。 */
export function isRouteForbidden(errorCode?: string | null): boolean {
  return !errorCode || ROUTE_FORBIDDEN_ERROR_CODES.has(errorCode);
}

/**
 * HTTP status が 401 / 403 のとき、対応する window イベントを発火する。
 * 403 は `errorCode` が経路の権限拒否（または未指定）のときだけ権限なしの画面へ移す。
 */
export function notifyAuthStatus(status: number, requestId?: string, errorCode?: string | null): void {
  if (typeof window === "undefined") return;
  if (status === 401) window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
  if (status === 403 && isRouteForbidden(errorCode)) {
    window.dispatchEvent(
      new CustomEvent<AuthForbiddenDetail>(AUTH_FORBIDDEN_EVENT, {
        detail: { requestId: requestId || undefined },
      }),
    );
  }
}

/** 403 のイベントを受けたら、request ID を state に載せて権限なしの画面へ移す（履歴は置き換える）。 */
export function useForbiddenRedirect(forbiddenPath: string): void {
  const navigate = useNavigate();
  useEffect(() => {
    const handleForbidden = (event: Event) => {
      const detail = (event as CustomEvent<AuthForbiddenDetail | undefined>).detail;
      navigate(forbiddenPath, {
        replace: true,
        state: { requestId: detail?.requestId },
      });
    };
    window.addEventListener(AUTH_FORBIDDEN_EVENT, handleForbidden);
    return () => window.removeEventListener(AUTH_FORBIDDEN_EVENT, handleForbidden);
  }, [forbiddenPath, navigate]);
}

/** 応答本文（ApiResponse の `error_code`、または problem の `code`）から error_code を読む。 */
export async function responseErrorCode(response: Response): Promise<string | undefined> {
  try {
    const body: unknown = await response.clone().json();
    if (!body || typeof body !== "object") return undefined;
    const record = body as { error_code?: unknown; problem?: { code?: unknown } | null };
    const code = record.error_code ?? record.problem?.code;
    return typeof code === "string" && code ? code : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 応答の 401 / 403 を通知する（`notifyAuthStatus` の応答版。#224）。
 * 403 は本文の error_code を読み、経路の権限拒否のときだけ権限なしの画面へ移す。本文は消費しない。
 */
export async function notifyAuthResponse(response: Response): Promise<void> {
  if (response.status !== 401 && response.status !== 403) return;
  const requestId = response.headers.get("X-Request-ID") || undefined;
  const errorCode = response.status === 403 ? await responseErrorCode(response) : undefined;
  notifyAuthStatus(response.status, requestId, errorCode);
}
