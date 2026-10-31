import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { SidebarAccount } from "../src";

const account = vi.hoisted(() => ({ current: null as SidebarAccount | null }));
vi.mock("../src/auth/RequireAuth", () => ({ useSidebarAccount: () => account.current }));

const { SidebarAccountSection } = await import("../src/auth/SidebarAccountSection");
const { sidebarAccountName, sidebarRoleNames } = await vi.importActual<typeof import("../src/auth/RequireAuth")>("../src/auth/RequireAuth");

const routes = { login: "/login" };

function base(overrides: Partial<SidebarAccount>): SidebarAccount {
  return {
    name: "営業ユーザー",
    roles: "営業",
    rolesLabel: "ロール: 営業",
    account: { href: "/rag/account", label: "アカウント設定を開く（営業ユーザー）", active: false },
    onLogout: () => undefined,
    labels: { logout: "ログアウト", switchToLight: "", switchToDark: "" },
    debugMode: false,
    ...overrides,
  };
}

describe("サイドバーのアカウント欄（#307）", () => {
  it("未ログインなら何も出さない", () => {
    account.current = null;
    expect(renderToStaticMarkup(<SidebarAccountSection routes={routes} collapsed={false} />)).toBe("");
  });

  it("ログイン中は利用者名・ロール・ログアウトを出し、ログイン省略の表示は出さない", () => {
    account.current = base({});
    const html = renderToStaticMarkup(<SidebarAccountSection routes={routes} collapsed={false} />);
    expect(html).toContain("営業ユーザー");
    // 見える行はロールの表示名だけ、title と読み上げは「ロール: …」の全文（#1644）。
    expect(html).toContain('title="ロール: 営業"');
    expect(html).toContain('<span class="sr-only">ロール: 営業</span><span aria-hidden="true">営業</span>');
    expect(html).toContain("ログアウト");
    expect(html).not.toContain("ログイン省略");
    // 欄の行全体がアカウントの画面へのリンク。個人の操作（API キー・パスワード変更）の行は出さない（#1873）。
    expect(html).toContain('href="/rag/account"');
    expect(html).toContain('aria-label="アカウント設定を開く（営業ユーザー） 営業ユーザー 営業"');
    expect(html).not.toContain('aria-current="page"');
    expect(html).not.toContain("パスワード変更");
    expect(html).not.toContain("API キー");
  });

  it("アカウントの画面を開いているときは欄を現在地にする（#1873）", () => {
    account.current = base({ account: { href: "/rag/account", label: "アカウント設定を開く（営業ユーザー）", active: true } });
    const html = renderToStaticMarkup(<SidebarAccountSection routes={routes} collapsed={false} />);
    expect(html).toContain('aria-current="page"');
    // 折りたたみ時もアイコンの行のリンクとして残す（読み上げ名は同じ）。
    const collapsed = renderToStaticMarkup(<SidebarAccountSection routes={routes} collapsed />);
    expect(collapsed).toContain('href="/rag/account"');
    expect(collapsed).toContain('aria-label="アカウント設定を開く（営業ユーザー）"');
  });

  it("ログイン省略では利用者名とログイン省略の表示を出し、ログアウトは出さない", () => {
    account.current = base({ name: "ローカル利用者", roles: "システム管理者", rolesLabel: "ロール: システム管理者", onLogout: undefined, debugMode: true });
    const html = renderToStaticMarkup(<SidebarAccountSection routes={routes} collapsed={false} />);
    expect(html).toContain("ローカル利用者");
    expect(html).toContain("ログイン省略");
    expect(html).toContain('role="status"');
    expect(html).not.toContain("ログアウト");
  });
});

describe("アカウント欄のロール（#1644）", () => {
  it("code ではなく表示名を「、」でつなぎ、表示名の無いロールだけ code を出す", () => {
    expect(sidebarRoleNames({ role_codes: ["SYSTEM_ADMIN"], role_display_names: ["システム管理者"] }, "、")).toBe("システム管理者");
    expect(
      sidebarRoleNames({ role_codes: ["SALES", "HR", "OPS"], role_display_names: ["営業", " ", "運用"] }, "、")
    ).toBe("営業、HR、運用");
    expect(sidebarRoleNames({ role_codes: ["SALES"], role_display_names: [] }, "、")).toBe("SALES");
  });
});

describe("アカウント欄の 1 行目（#1696）", () => {
  it("ログイン ID を出し、ほかの画面の表示名は使わない。local（debug）は表示名", () => {
    expect(sidebarAccountName({ login_user_id: "system_admin", display_name: "システム管理者", debug_mode: false })).toBe(
      "system_admin"
    );
    expect(sidebarAccountName({ login_user_id: "sales.user", display_name: "営業ユーザー", debug_mode: false })).toBe("sales.user");
    expect(sidebarAccountName({ login_user_id: " ", display_name: "営業ユーザー", debug_mode: false })).toBe("営業ユーザー");
    expect(sidebarAccountName({ login_user_id: "local", display_name: "ローカル利用者", debug_mode: true })).toBe("ローカル利用者");
  });
});
