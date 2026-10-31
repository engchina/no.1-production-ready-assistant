import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { BaseCurrentUser } from "../src";

const auth = vi.hoisted(() => ({ user: null as BaseCurrentUser | null }));
vi.mock("../src/auth/AuthProvider", () => ({
  useAuth: () => ({ user: auth.user, status: "authenticated", hasPermission: () => true }),
}));

const { AccountPage } = await import("../src/account/AccountPage");
const { API_KEYS_QUERY_KEY, mcpEndpoints } = await import("../src");

function user(overrides: Partial<BaseCurrentUser> = {}): BaseCurrentUser {
  return {
    user_uuid: "user-1",
    login_user_id: "sales.user",
    display_name: "営業ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["SALES"],
    role_display_names: ["営業"],
    is_system_admin: false,
    permissions: [],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
  };
}

const pending = () => new Promise<never>(() => undefined);
const myApi = { list: pending, create: pending, revoke: pending };

function render(node: ReactNode, path = "/account"): string {
  const client = new QueryClient();
  client.setQueryData(API_KEYS_QUERY_KEY, { keys: [], public_base_url: "" });
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <ConfirmProvider>{node}</ConfirmProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
}

describe("アカウントの画面（#1873）", () => {
  it("見出しは「アカウント」で名前とロールを添え、既定は「API キー」のタブ", () => {
    auth.user = user();
    const html = render(
      <AccountPage loginRoute="/login" apiKeys={{ api: myApi, endpoints: mcpEndpoints("rag", "/rag/", "https://suite.example.com") }} />
    );
    expect(html).toContain(">アカウント</h1>");
    expect(html).toContain("sales.user（ロール: 営業）");
    expect(html).toContain('role="tablist"');
    expect(html).toContain('aria-label="アカウントの設定"');
    expect(html).toMatch(/id="account-tab-api-keys"[^>]*aria-selected="true"/);
    expect(html).toContain("パスワード変更");
    // API キーのタブの中身（接続先・発行・一覧）。
    expect(html).toContain("接続先");
    expect(html).toContain("API キーを発行");
    expect(html).not.toContain('id="auth-password-current"');
  });

  it("?tab=password ではパスワードの変更の欄を出し、API キーの中身は出さない", () => {
    auth.user = user();
    const html = render(<AccountPage loginRoute="/login" apiKeys={{ api: myApi, endpoints: [] }} />, "/account?tab=password");
    expect(html).toMatch(/id="account-tab-password"[^>]*aria-selected="true"/);
    for (const id of ["auth-password-current", "auth-password-new", "auth-password-confirm"]) {
      expect(html).toContain(`id="${id}"`);
    }
    expect(html).toContain('data-testid="account-password-submit"');
    expect(html).not.toContain("API キーを発行");
  });

  it("ローカルの認証ではパスワードを使わない説明、変えられないアカウントはその説明を出す", () => {
    auth.user = user({ debug_mode: true });
    const local = render(<AccountPage loginRoute="/login" apiKeys={{ api: myApi, endpoints: [] }} />, "/account?tab=password");
    expect(local).toContain("ローカルの認証（ログインの省略）ではパスワードを使いません。");
    expect(local).not.toContain('id="auth-password-current"');
    auth.user = user({ password_change_allowed: false });
    const notAllowed = render(<AccountPage loginRoute="/login" apiKeys={{ api: myApi, endpoints: [] }} />, "/account?tab=password");
    expect(notAllowed).toContain('data-testid="account-password-not-allowed"');
    expect(notAllowed).not.toContain('id="auth-password-current"');
  });
});
