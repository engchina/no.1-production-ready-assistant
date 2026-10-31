import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { setDisplayTimeZone } from "@production-ready/ui";

import { bindListPagingOwner } from "../lists/listPaging";
import { authBroadcastType, openAuthBroadcastChannel, postAuthBroadcast } from "./broadcast";
import { AUTH_UNAUTHORIZED_EVENT } from "./events";
import { createPermissionCheck as defaultPermissionCheck } from "./permissions";
import type { AuthApi, AuthStatus, BaseCurrentUser, HasPermission } from "./types";

export interface AuthContextValue<U extends BaseCurrentUser = BaseCurrentUser> {
  status: AuthStatus;
  user: U | null;
  /** 直近の `me` の 401 以外の失敗（`status` が `error` のときの理由。成功すると null）。 */
  error: unknown;
  hasPermission: HasPermission;
  /** ログイン直後など、context にまだ反映されていないユーザーの権限を判定する。 */
  permissionCheckFor: (user: U | null) => HasPermission;
  login: (loginUserId: string, password: string) => Promise<U>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<unknown>;
}

// 型引数ごとに context を分けられないため、内部では基底型で持ち useAuth<U>() で絞る。
const AuthContext = createContext<AuthContextValue<BaseCurrentUser> | null>(null);

/** 利用者と権限が同じかを判断する既定の key（user_uuid + 並べ替えた permissions）。 */
export function defaultIdentityKey(user: BaseCurrentUser): string {
  return JSON.stringify([user.user_uuid, [...user.permissions].sort()]);
}

function isAbortError(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "AbortError";
}

/** 製品の ApiError（`status` を持つ）の 401。未認証と判断してよいのはこのときだけ（#1061）。 */
function isUnauthorizedError(cause: unknown): boolean {
  return (
    typeof cause === "object" &&
    cause !== null &&
    (cause as { status?: unknown }).status === 401
  );
}

export interface AuthProviderProps<U extends BaseCurrentUser> {
  api: AuthApi<U>;
  /**
   * 利用者や権限が変わったと判断する key。変わったら React Query の cache を cancel / clear する。
   * 既定は user_uuid + permissions。製品固有の認可項目（例: 業務プロファイルの利用権限）があれば足す。
   */
  identityKey?: (user: U) => string;
  /**
   * 認証状態を反映するたびに呼ぶ（ログイン中は user、未認証は null）。
   * 製品の下書きの持ち主の記録・消去などに使う。同じ値で繰り返し呼ばれるので冪等にする。
   */
  onIdentityChange?: (user: U | null) => void;
  /**
   * 権限の判定を作る。既定は SYSTEM_ADMIN なら true、それ以外は `permissions` に含まれるか。
   * 旧コードの読み替えなど、製品固有の展開が必要な場合だけ差し替える。
   */
  createPermissionCheck?: (user: U) => HasPermission;
  children: ReactNode;
}

/**
 * Cookie セッションの認証状態を持つ（NL2SQL から移設。#220）。
 * 起動時に `me` で確認し、401 のイベント（`notifyAuthStatus`）で未認証に戻す。
 * ログインの Cookie は 3 製品で共有するため、ログイン・ログアウト・パスワード変更を
 * `BroadcastChannel` でほかの製品・タブへ知らせ、受けた側は利用者を確かめ直すか未認証に戻す（#1565）。
 */
export function AuthProvider<U extends BaseCurrentUser>({
  api,
  identityKey = defaultIdentityKey,
  onIdentityChange,
  createPermissionCheck,
  children,
}: AuthProviderProps<U>) {
  const queryClient = useQueryClient();
  const identity = useRef("");
  // props の関数は render ごとに変わりうるため、最新値を ref で読む（effect の再実行を避ける）。
  const optionsRef = useRef({ api, identityKey, onIdentityChange, createPermissionCheck });
  useEffect(() => {
    optionsRef.current = { api, identityKey, onIdentityChange, createPermissionCheck };
  });

  const applyIdentity = useCallback(
    (current: U | null) => {
      const { identityKey: keyOf, onIdentityChange: notify } = optionsRef.current;
      // 画面の日時はシステムの設定の時間帯で表示する（#3265）。利用者を state に入れる前に設定し、
      // 認証の後の最初の描画から設定の時間帯で書く。未認証に戻っても直前の時間帯のまま。
      if (current) setDisplayTimeZone(current.timezone);
      const next = current ? keyOf(current) : "";
      // 一覧のページ番号・表示件数の作業状態を今の利用者に結び付ける（ログアウト・別の利用者で消す。#1891）。
      bindListPagingOwner(next);
      if (identity.current !== next) {
        void queryClient.cancelQueries();
        queryClient.clear();
        identity.current = next;
      }
      try {
        notify?.(current);
      } catch {
        /* storage が無効などでも認証を妨げない */
      }
    },
    [queryClient],
  );
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<U | null>(null);
  const [error, setError] = useState<unknown>(null);

  // state は応答の callback の中だけで更新する（effect から同期的に setState しない）。
  const refresh = useCallback(
    (signal?: AbortSignal): Promise<void> =>
      optionsRef.current.api.me({ signal }).then(
        (current) => {
          if (signal?.aborted) return;
          applyIdentity(current);
          setUser(current);
          setError(null);
          setStatus("authenticated");
        },
        (cause: unknown) => {
          if (isAbortError(cause)) return;
          if (isUnauthorizedError(cause)) {
            applyIdentity(null);
            setUser(null);
            setError(null);
            setStatus("unauthenticated");
            return;
          }
          // 5xx・通信断・timeout はログイン済みかどうかが分からない。未認証と扱わず（ログイン画面へ移さず、
          // 作業状態を消さず）、ログイン済みと分かっていればそのまま、分からなければ error にする（#1061）。
          setError(cause);
          setStatus((previous) => (previous === "authenticated" ? previous : "error"));
        },
      ),
    [applyIdentity],
  );

  // 確認の失敗（error）からの再試行は、確認している間を読み込み中の表示に戻す。
  const recheck = useCallback((): Promise<void> => {
    setStatus((previous) => (previous === "error" ? "loading" : previous));
    return refresh();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  const markUnauthenticated = useCallback(() => {
    applyIdentity(null);
    setUser(null);
    setError(null);
    setStatus("unauthenticated");
  }, [applyIdentity]);

  useEffect(() => {
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, markUnauthenticated);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, markUnauthenticated);
  }, [markUnauthenticated]);

  // ほかの製品・タブのログイン状態の変化を受ける（#1565）。送った自分の channel には届かない。
  const channel = useRef<BroadcastChannel | null>(null);
  useEffect(() => {
    const opened = openAuthBroadcastChannel();
    channel.current = opened;
    if (!opened) return undefined;
    const handleMessage = (event: MessageEvent) => {
      const type = authBroadcastType(event.data);
      // ログアウト（共有のセッションの失効）: この画面も未認証に戻し、ログイン画面へ移す。
      if (type === "logout") markUnauthenticated();
      // ログイン（別の利用者かもしれない）: 共有の Cookie で利用者を確かめ直す。
      if (type === "login") void refresh();
    };
    opened.addEventListener("message", handleMessage);
    return () => {
      opened.removeEventListener("message", handleMessage);
      opened.close();
      if (channel.current === opened) channel.current = null;
    };
  }, [markUnauthenticated, refresh]);

  const login = useCallback(
    async (loginUserId: string, password: string) => {
      const current = await optionsRef.current.api.login(loginUserId, password);
      applyIdentity(current);
      setUser(current);
      setError(null);
      setStatus("authenticated");
      postAuthBroadcast(channel.current, "login");
      return current;
    },
    [applyIdentity],
  );

  const logout = useCallback(async () => {
    try {
      await optionsRef.current.api.logout();
      // 共有のセッションを失効させた。ほかの製品・タブもログイン画面へ移す。
      postAuthBroadcast(channel.current, "logout");
    } finally {
      markUnauthenticated();
    }
  }, [markUnauthenticated]);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    const result = await optionsRef.current.api.changePassword(currentPassword, newPassword);
    // パスワードを変えるとセッションは失効し、Cookie も消える。ほかの製品・タブもログインし直す。
    postAuthBroadcast(channel.current, "logout");
    return result;
  }, []);

  const permissionCheckFor = useCallback(
    (target: U | null): HasPermission =>
      target
        ? (optionsRef.current.createPermissionCheck ?? defaultPermissionCheck)(target)
        : () => false,
    [],
  );

  const value = useMemo<AuthContextValue<U>>(
    () => ({
      status,
      user,
      error,
      login,
      logout,
      refresh: recheck,
      changePassword,
      permissionCheckFor,
      hasPermission: permissionCheckFor(user),
    }),
    [changePassword, error, login, logout, permissionCheckFor, recheck, status, user],
  );

  return (
    <AuthContext.Provider value={value as unknown as AuthContextValue<BaseCurrentUser>}>
      {children}
    </AuthContext.Provider>
  );
}

/** `AuthProvider` の状態と操作。U は製品の CurrentUser 型。 */
export function useAuth<U extends BaseCurrentUser = BaseCurrentUser>(): AuthContextValue<U> {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider が設定されていません。");
  return value as unknown as AuthContextValue<U>;
}
