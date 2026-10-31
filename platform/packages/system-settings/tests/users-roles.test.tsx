import { ConfirmProvider } from "@production-ready/ui";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  RoleManagementPage,
  USER_ROLE_NAV_ITEMS,
  USER_ROLE_PATHS,
  UserManagementPage,
  identityInlineLabel,
  identitySecondaryName,
  type RoleManagementApi,
  type UserManagementApi,
} from "../src";
import {
  mapFieldErrors,
  unmappedErrorMessage,
} from "../src/users-roles/shared";

const pending = () => new Promise<never>(() => undefined);

const userApi: UserManagementApi = {
  users: pending,
  createUser: pending,
  updateUser: pending,
  deleteUser: pending,
  resetPassword: pending,
  unlockUser: pending,
  setUserEnabled: pending,
  roles: pending,
};

const roleApi: RoleManagementApi = {
  roles: pending,
  createRole: pending,
  updateRole: pending,
  archiveRole: pending,
  restoreRole: pending,
  deleteRole: pending,
};

function render(node: ReactNode) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ConfirmProvider>{node}</ConfirmProvider>
    </MemoryRouter>,
  );
}

describe("ユーザー管理・ロール管理のナビ", () => {
  it("NL2SQL の既存 URL を共通パスとして使う", () => {
    expect(USER_ROLE_PATHS).toEqual({
      users: "/settings/security/users",
      roles: "/settings/security/roles",
    });
    expect(USER_ROLE_NAV_ITEMS.map((item) => item.labelKey)).toEqual([
      "nav.securityUsers",
      "nav.securityRoles",
    ]);
  });
});

describe("UserManagementPage", () => {
  it("読み込み中は状態表示を出し、管理権限があれば新規作成を出す", () => {
    const html = render(<UserManagementPage api={userApi} canManage />);
    expect(html).toContain("ユーザー管理");
    expect(html).toContain('data-testid="security-users-loading"');
    expect(html).toContain("ユーザーを読込中");
    expect(html).toContain("新規作成");
  });

  it("管理権限がなければ新規作成を出さない", () => {
    const html = render(<UserManagementPage api={userApi} canManage={false} />);
    expect(html).not.toContain("新規作成");
  });
});

describe("RoleManagementPage", () => {
  it("ロールの基本情報だけを扱い、権限の編集を持たない", () => {
    const html = render(<RoleManagementPage api={roleApi} canManage />);
    expect(html).toContain("ロール管理");
    expect(html).toContain('data-testid="security-roles-loading"');
    expect(html).toContain("ロールを読込中");
    expect(html).toContain("権限管理で設定します");
    expect(html).not.toContain("機能権限");
  });
});

describe("入力エラーの結び付け", () => {
  const pointers = { "/role_code": "roleCode" } as const;

  it("JSON Pointer を field へ結び付け、エラーコードで文言を差し替えられる", () => {
    const details = {
      message: "conflict",
      code: "SECURITY_ROLE_CODE_CONFLICT",
      fieldErrors: [{ pointer: "/role_code", message: "server" }],
    };
    expect(mapFieldErrors(details, pointers)).toEqual({ roleCode: "server" });
    expect(
      mapFieldErrors(details, pointers, (_problem, d) => (d.code === "SECURITY_ROLE_CODE_CONFLICT" ? "使用済み" : "")),
    ).toEqual({ roleCode: "使用済み" });
  });

  it("すべて field に結び付いたときはフォーム全体のエラーを出さない", () => {
    const mapped = { fieldErrors: [{ pointer: "/role_code", message: "x" }], message: "m" };
    expect(unmappedErrorMessage(new Error("m"), mapped, pointers, "fallback")).toBe("");
    const unmapped = { fieldErrors: [{ pointer: "/other", message: "x" }], message: "m" };
    expect(unmappedErrorMessage(new Error("m"), unmapped, pointers, "fallback")).toBe("m");
    expect(unmappedErrorMessage("oops", undefined, pointers, "fallback")).toBe("fallback");
  });
});

describe("選択と表示名", () => {
  it("表示名は ID と異なるときだけ補助表示する", () => {
    expect(identitySecondaryName("data_user", " データユーザー ")).toBe("データユーザー");
    expect(identitySecondaryName("data_user", "data_user")).toBe("");
    expect(identityInlineLabel("DATA_ADMIN", "データ管理者")).toBe("DATA_ADMIN（データ管理者）");
  });
});
