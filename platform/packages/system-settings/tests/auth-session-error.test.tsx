// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { displayTimeZone, formatRunDateTime, setDisplayTimeZone } from "@production-ready/ui";

import { AuthProvider, RequireAuth, type AuthApi, type BaseCurrentUser } from "../src";

// #1061: 起動時の /api/auth/me の 401 以外の失敗（5xx・通信断）を未認証と扱わない。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const routes = { login: "/login", passwordChange: "/password/change", forbidden: "/forbidden" };

const USER: BaseCurrentUser = {
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
};

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const never = () => new Promise<never>(() => undefined);

async function render(api: AuthApi, onIdentityChange: (user: BaseCurrentUser | null) => void) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/work"]}>
          <AuthProvider api={api} onIdentityChange={onIdentityChange}>
            <Routes>
              <Route path="/login" element={<p>ログイン画面</p>} />
              <Route
                path="/work"
                element={
                  <RequireAuth routes={routes}>
                    <p>作業の画面</p>
                  </RequireAuth>
                }
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  });
  await act(async () => undefined);
}

describe("起動時の認証の確認の失敗（#1061）", () => {
  it("503 ではログイン画面へ移さず、作業状態を消さず、失敗と再試行を出す。再試行で成功すれば画面を出す", async () => {
    let calls = 0;
    const api: AuthApi = {
      me: () =>
        calls++ === 0
          ? Promise.reject(new HttpError(503, "一時的に利用できません。"))
          : Promise.resolve(USER),
      login: never,
      logout: never,
      changePassword: never,
    };
    const onIdentityChange = vi.fn();
    await render(api, onIdentityChange);

    expect(host.textContent).not.toContain("ログイン画面");
    expect(host.querySelector('[data-testid="auth-session-error"]')?.textContent).toContain(
      "ログインの状態を確認できませんでした。",
    );
    expect(host.textContent).toContain("一時的に利用できません。");
    expect(onIdentityChange).not.toHaveBeenCalledWith(null);

    const retry = Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("再試行"));
    await act(async () => {
      retry?.click();
    });
    await act(async () => undefined);

    expect(host.textContent).toContain("作業の画面");
    expect(onIdentityChange).toHaveBeenLastCalledWith(USER);
  });

  it("通信断（status の無い失敗）でもログイン画面へ移さない", async () => {
    const api: AuthApi = {
      me: () => Promise.reject(new TypeError("Failed to fetch")),
      login: never,
      logout: never,
      changePassword: never,
    };
    const onIdentityChange = vi.fn();
    await render(api, onIdentityChange);

    expect(host.textContent).not.toContain("ログイン画面");
    expect(host.querySelector('[data-testid="auth-session-error"]')).not.toBeNull();
    expect(onIdentityChange).not.toHaveBeenCalled();
  });

  it("401 は今までどおり未認証にしてログイン画面へ移す", async () => {
    const api: AuthApi = {
      me: () => Promise.reject(new HttpError(401, "ログインしてください。")),
      login: never,
      logout: never,
      changePassword: never,
    };
    const onIdentityChange = vi.fn();
    await render(api, onIdentityChange);

    expect(host.textContent).toContain("ログイン画面");
    expect(onIdentityChange).toHaveBeenCalledWith(null);
  });
});

describe("画面の日時の時間帯（#3265）", () => {
  afterEach(() => setDisplayTimeZone(null));

  it("認証の確認の応答の timezone を、最初の描画の前に表示の時間帯にする", async () => {
    const seen: string[] = [];
    function Stamp() {
      seen.push(displayTimeZone());
      return <p>{formatRunDateTime("2026-10-24T15:30:00Z")}</p>;
    }
    const api: AuthApi = {
      me: () => Promise.resolve({ ...USER, timezone: "UTC" }),
      login: never,
      logout: never,
      changePassword: never,
    };
    await act(async () => {
      root.render(
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={["/work"]}>
            <AuthProvider api={api}>
              <Routes>
                <Route
                  path="/work"
                  element={
                    <RequireAuth routes={routes}>
                      <Stamp />
                    </RequireAuth>
                  }
                />
              </Routes>
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
    });
    await act(async () => undefined);

    expect(displayTimeZone()).toBe("UTC");
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((zone) => zone === "UTC")).toBe(true);
    expect(host.textContent).toContain("2026/10/24 15:30");
  });

  it("timezone の無い応答は既定（UTC。#3274）", async () => {
    setDisplayTimeZone("Asia/Tokyo");
    await render(
      { me: () => Promise.resolve(USER), login: never, logout: never, changePassword: never },
      () => undefined,
    );
    expect(displayTimeZone()).toBe("UTC");
  });
});
