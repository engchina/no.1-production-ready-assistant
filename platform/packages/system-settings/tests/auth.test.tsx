import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AUTH_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  AuthProvider,
  CSRF_COOKIE_NAME,
  CSRF_HEADER_NAME,
  ForbiddenPage,
  LoginPage,
  requestedPathFrom,
  PasswordChangePage,
  RequireAuth,
  createPermissionCheck,
  csrfHeader,
  defaultIdentityKey,
  expandPermissions,
  firstAllowedRoute,
  isRouteForbidden,
  notifyAuthResponse,
  notifyAuthStatus,
  responseErrorCode,
  routePermissionMap,
  type AuthApi,
  type BaseCurrentUser,
} from "../src";

const pending = () => new Promise<never>(() => undefined);

const authApi: AuthApi = {
  me: pending,
  login: pending,
  logout: pending,
  changePassword: pending,
};

const routes = { login: "/login", passwordChange: "/password/change", forbidden: "/forbidden" };
const brand = { line1: "Production Ready", line2: "RAG" };

function user(overrides: Partial<BaseCurrentUser> = {}): BaseCurrentUser {
  return {
    user_uuid: "user-1",
    login_user_id: "sales.user",
    display_name: "営業ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["SALES"],
    is_system_admin: false,
    permissions: ["menu.search"],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
  };
}

function render(node: ReactNode, initialEntries: Array<string | { pathname: string; state?: unknown }> = ["/"]) {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={initialEntries}>
        <ConfirmProvider>
          <AuthProvider api={authApi}>{node}</AuthProvider>
        </ConfirmProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("権限の判定とルート", () => {
  it("SYSTEM_ADMIN はすべて許可し、それ以外は permissions に含まれるものだけを許可する", () => {
    expect(createPermissionCheck(user({ is_system_admin: true, permissions: [] }))("menu.users")).toBe(true);
    const check = createPermissionCheck(user());
    expect(check("menu.search")).toBe(true);
    expect(check("menu.users")).toBe(false);
    expect(createPermissionCheck(null)("menu.search")).toBe(false);
  });

  it("implies を推移的に展開する", () => {
    expect(
      [...expandPermissions(["menu.query"], { "menu.query": ["a.read"], "a.read": ["b.read"] })].sort(),
    ).toEqual(["a.read", "b.read", "menu.query"]);
  });

  it("ナビの並び順で最初に開ける画面を返し、無ければ fallback", () => {
    const items = [
      { href: "/search", permission: "menu.search" },
      { href: "/users", permission: "menu.users" },
      { href: "/help" },
    ];
    expect(routePermissionMap(items)).toEqual({ "/search": "menu.search", "/users": "menu.users" });
    expect(firstAllowedRoute(items, (code) => code === "menu.users", "/forbidden")).toBe("/users");
    expect(firstAllowedRoute(items, () => false, "/forbidden")).toBe("/forbidden");
  });

  it("既定の identity key は permissions の並び順に依存しない", () => {
    expect(defaultIdentityKey(user({ permissions: ["b", "a"] }))).toBe(
      defaultIdentityKey(user({ permissions: ["a", "b"] })),
    );
    expect(defaultIdentityKey(user({ user_uuid: "user-2" }))).not.toBe(defaultIdentityKey(user()));
  });
});

describe("CSRF と認証イベント", () => {
  it("状態を変える method だけ Cookie の値を X-CSRF-Token で返す", () => {
    // Cookie 名は 3 製品で共有の 1 つ（#1565）。製品ごとの旧名は読まない。
    expect(CSRF_COOKIE_NAME).toBe("pr_csrf");
    vi.stubGlobal("document", { cookie: "other=1; pr_csrf=token%2Fvalue" });
    expect(csrfHeader("post")).toEqual({ [CSRF_HEADER_NAME]: "token/value" });
    expect(csrfHeader("DELETE")).toEqual({ "X-CSRF-Token": "token/value" });
    expect(csrfHeader("GET")).toEqual({});
    expect(csrfHeader()).toEqual({});
    vi.stubGlobal("document", { cookie: "rag_csrf=old-token" });
    expect(csrfHeader("PUT")).toEqual({});
  });

  it("401 / 403 で対応する window イベントを発火し、403 には request ID を載せる", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    const received: Array<{ type: string; detail: unknown }> = [];
    for (const type of [AUTH_UNAUTHORIZED_EVENT, AUTH_FORBIDDEN_EVENT]) {
      target.addEventListener(type, (event) =>
        received.push({ type, detail: (event as CustomEvent).detail }),
      );
    }
    notifyAuthStatus(200);
    notifyAuthStatus(401);
    notifyAuthStatus(403, "req-1");
    expect(received).toEqual([
      { type: "app-auth-unauthorized", detail: null },
      { type: "app-auth-forbidden", detail: { requestId: "req-1" } },
    ]);
  });

  it("403 は経路の権限拒否（または error_code なし）のときだけ権限なしの画面へ移す", async () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    const received: string[] = [];
    target.addEventListener(AUTH_FORBIDDEN_EVENT, (event) =>
      received.push(String((event as CustomEvent).detail?.requestId)),
    );
    const response = (status: number, body: unknown, requestId: string) =>
      new Response(JSON.stringify(body), { status, headers: { "X-Request-ID": requestId } });

    await notifyAuthResponse(response(403, { error_code: "SECURITY_ROUTE_FORBIDDEN" }, "route"));
    await notifyAuthResponse(response(403, { problem: { code: "SECURITY_ROUTE_UNCLASSIFIED" } }, "unclassified"));
    await notifyAuthResponse(response(403, { error_messages: ["従来の 403"] }, "legacy"));
    await notifyAuthResponse(response(403, { error_code: "RAG_SCOPE_FORBIDDEN" }, "scope"));
    await notifyAuthResponse(response(403, { error_code: "SECURITY_PERMISSION_DENIED" }, "grant"));
    await notifyAuthResponse(response(403, { error_code: "SECURITY_CSRF_INVALID" }, "csrf"));
    await notifyAuthResponse(response(200, { error_code: "SECURITY_ROUTE_FORBIDDEN" }, "ok"));

    expect(received).toEqual(["route", "unclassified", "legacy"]);
    expect(isRouteForbidden(undefined)).toBe(true);
    expect(isRouteForbidden("RAG_SCOPE_FORBIDDEN")).toBe(false);
    // 本文は消費しない（呼び出し側が続けて読める）。
    const body = response(403, { error_code: "RAG_SCOPE_FORBIDDEN" }, "keep");
    await notifyAuthResponse(body);
    expect(await responseErrorCode(body)).toBe("RAG_SCOPE_FORBIDDEN");
    expect(await body.json()).toEqual({ error_code: "RAG_SCOPE_FORBIDDEN" });
  });
});

describe("ログイン後の戻り先", () => {
  it("アプリ内のパスだけを使い、別オリジンや不正な値は使わない", () => {
    expect(requestedPathFrom({ from: "/search?q=1" })).toBe("/search?q=1");
    expect(requestedPathFrom({ from: "//evil.example/" })).toBeNull();
    expect(requestedPathFrom({ from: "https://evil.example/" })).toBeNull();
    expect(requestedPathFrom({ from: "/\\evil.example" })).toBeNull();
    expect(requestedPathFrom({ from: 1 })).toBeNull();
    expect(requestedPathFrom(null)).toBeNull();
  });
});

describe("認証画面", () => {
  it("確認中は RequireAuth が読み込み表示を出し、子を描画しない", () => {
    const html = render(
      <RequireAuth routes={routes}>
        <p>業務画面</p>
      </RequireAuth>,
    );
    expect(html).toContain('data-testid="auth-session-loading"');
    expect(html).toContain("認証状態を確認しています。");
    expect(html).not.toContain("業務画面");
  });

  it("ログイン画面は wordmark と NL2SQL と同じ入力 ID を持つ", () => {
    const html = render(<LoginPage brand={brand} routes={routes} entryRoute={() => "/"} />);
    expect(html).toContain("Production Ready");
    expect(html).toContain("システムにログイン");
    expect(html).toContain('id="auth-login-user-id"');
    expect(html).toContain('id="auth-login-password"');
    expect(html).toContain('autoComplete="username"');
    expect(html).toContain("ログイン");
  });

  it("ログイン画面は「パスワードを忘れた場合」の案内を同じ画面に開ける（#1873。メールの送信は無い）", () => {
    const html = render(<LoginPage brand={brand} routes={routes} entryRoute={() => "/"} />);
    expect(html).toContain('data-testid="auth-login-forgot-password"');
    expect(html).toContain("パスワードを忘れた場合");
    expect(html).toContain("<details");
  });

  it("文言は messages で一部だけ上書きできる", () => {
    const html = render(
      <LoginPage
        brand={brand}
        routes={routes}
        entryRoute={() => "/"}
        messages={{ loginSubtitle: "RAG コンソールにサインインします。" }}
      />,
    );
    expect(html).toContain("RAG コンソールにサインインします。");
    expect(html).toContain("ログインユーザーID");
  });

  it("パスワード変更画面は 3 つの入力を共通の TextField で出す", () => {
    const html = render(<PasswordChangePage brand={brand} routes={routes} entryRoute={() => "/"} />);
    for (const id of ["auth-password-current", "auth-password-new", "auth-password-confirm"]) {
      expect(html).toContain(`id="${id}"`);
    }
    expect(html).toContain("新しいパスワード（確認）");
    expect(html).toContain("パスワードを変更");
  });

  it("権限なし画面は 403 の request ID を表示する", () => {
    const html = render(<ForbiddenPage brand={brand} entryRoute={() => "/"} />, [
      { pathname: "/forbidden", state: { requestId: "req-403" } },
    ]);
    expect(html).toContain("この機能を利用する権限がありません");
    expect(html).toContain("req-403");
    expect(html).toContain("利用可能な画面へ戻る");
  });
});
