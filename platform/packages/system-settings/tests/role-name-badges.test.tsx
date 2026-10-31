// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  RoleManagementPage,
  RoleNameBadges,
  UserManagementPage,
  roleDisplayName,
  type RoleManagementApi,
  type SecurityRole,
  type SecurityUser,
  type UserManagementApi,
} from "../src";

// #1682: 画面にロールの ID（UUID）もロールコードも出さず、ロール名（表示名）を出す。
// 表示名が空ならコード、参照できないロールは「（削除済みのロール）」。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SYSTEM_ADMIN_ID = "00000000-0000-0000-0000-000000000001";
const DELETED_ID = "6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5";
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/iu;

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

async function render(node: ReactNode, entry = "/") {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[entry]}>
        <ConfirmProvider>{node}</ConfirmProvider>
      </MemoryRouter>,
    );
  });
  await act(async () => undefined);
}

/** 画面の文字と属性（title・aria-label など）のどこにも UUID が無い。 */
function expectNoUuidShown(scope: HTMLElement) {
  expect(scope.textContent ?? "").not.toMatch(UUID_PATTERN);
  for (const element of Array.from(scope.querySelectorAll<HTMLElement>("*"))) {
    for (const name of ["title", "aria-label", "aria-description", "placeholder"]) {
      expect(element.getAttribute(name) ?? "").not.toMatch(UUID_PATTERN);
    }
  }
}

describe("roleDisplayName（#1682）", () => {
  it("表示名を返し、空ならコード、どちらも無い・削除済みは ID を出さない文言にする", () => {
    expect(roleDisplayName({ display_name: "システム管理者", role_code: "SYSTEM_ADMIN" })).toBe("システム管理者");
    expect(roleDisplayName({ display_name: "  人事利用者 ", role_code: "HR_USER" })).toBe("人事利用者");
    expect(roleDisplayName({ display_name: "", role_code: "HR_USER" })).toBe("HR_USER");
    expect(roleDisplayName({ display_name: "", role_code: "", deleted: true })).toBe("（削除済みのロール）");
    expect(roleDisplayName({ display_name: "人事利用者", role_code: "HR_USER", deleted: true })).toBe("（削除済みのロール）");
    expect(roleDisplayName({ display_name: null, role_code: null })).toBe("（削除済みのロール）");
    expect(roleDisplayName(undefined, "削除されたロール")).toBe("削除されたロール");
  });
});

describe("RoleNameBadges（#1682）", () => {
  it("ロールをロール名のバッジで並べ、ID もコードも出さない", async () => {
    await render(
      <RoleNameBadges
        ariaLabel="利用可能ロール"
        roles={[
          { role_id: SYSTEM_ADMIN_ID, role_code: "SYSTEM_ADMIN", display_name: "システム管理者" },
          { role_id: "r-hr", role_code: "HR_USER", display_name: "人事利用者" },
          { role_id: DELETED_ID, role_code: "", display_name: "", deleted: true },
        ]}
      />,
    );
    const list = host.querySelector<HTMLUListElement>('[data-testid="role-name-badges"]');
    expect(list?.getAttribute("aria-label")).toBe("利用可能ロール");
    const labels = Array.from(list?.querySelectorAll("li") ?? []).map((item) => item.textContent?.trim());
    expect(labels).toEqual(["システム管理者", "人事利用者", "（削除済みのロール）"]);
    expect(host.textContent).not.toContain("SYSTEM_ADMIN");
    expect(host.textContent).not.toContain("HR_USER");
    expectNoUuidShown(host);
  });
});

function securityRole(id: string, code: string, name: string): SecurityRole {
  return { role_id: id, role_code: code, display_name: name, description: "", is_built_in: false, archived: false, version: 1 };
}

const never = () => new Promise<never>(() => undefined);

function userApi(users: SecurityUser[]): UserManagementApi {
  return {
    users: () => Promise.resolve(users),
    createUser: never,
    updateUser: never,
    deleteUser: never,
    resetPassword: never,
    unlockUser: never,
    setUserEnabled: never,
    roles: () =>
      Promise.resolve([
        { ...securityRole(SYSTEM_ADMIN_ID, "SYSTEM_ADMIN", "システム管理者"), is_built_in: true },
        securityRole("r-hr", "HR_USER", "人事利用者"),
      ]),
  };
}

function baseUser(overrides: Partial<SecurityUser>): SecurityUser {
  return {
    user_uuid: "user-1",
    login_user_id: "alice",
    display_name: "Alice",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [],
    is_bootstrap_admin: false,
    ...overrides,
  };
}

describe("ユーザー管理の削除済みのロール（#1682）", () => {
  it("backend が参照できないと返したロールは「（削除済みのロール）」と出し、UUID を出さない", async () => {
    const user = baseUser({
      role_ids: ["r-hr", DELETED_ID],
      assigned_roles: [
        { role_id: "r-hr", role_code: "HR_USER", display_name: "人事利用者", is_built_in: false, archived: false },
        // backend の AssignedRoleData.unresolved() の形（コード・表示名は空）。
        { role_id: DELETED_ID, role_code: "", display_name: "", is_built_in: false, archived: true, deleted: true },
      ],
    });
    await render(<UserManagementPage api={userApi([user])} canManage />);

    expect(host.textContent).toContain("（削除済みのロール）");
    expect(host.textContent).toContain("人事利用者");
    expectNoUuidShown(host);
  });

  it("assigned_roles が無いときも、ロール一覧に無い ID を名前に使わない", async () => {
    const user = baseUser({ role_ids: [SYSTEM_ADMIN_ID, DELETED_ID] });
    await render(<UserManagementPage api={userApi([user])} canManage />);

    expect(host.textContent).toContain("システム管理者");
    expect(host.textContent).toContain("（削除済みのロール）");
    expectNoUuidShown(host);
  });
});

describe("ロール管理の一覧と詳細（#1682）", () => {
  it("一覧の行・詳細の見出し・行の読み上げはロール名で、ID を出さない", async () => {
    const api: RoleManagementApi = {
      roles: () =>
        Promise.resolve([
          { ...securityRole(SYSTEM_ADMIN_ID, "SYSTEM_ADMIN", "システム管理者"), is_built_in: true },
          securityRole("r-hr", "HR_USER", "人事利用者"),
        ]),
      createRole: never,
      updateRole: never,
      archiveRole: never,
      restoreRole: never,
      deleteRole: never,
    };
    // 選択は URL の ?role= が持つ（先頭の行を自動で選ばない。#2282）。
    await render(<RoleManagementPage api={api} canManage />, `/?role=${SYSTEM_ADMIN_ID}`);

    const grid = host.querySelector<HTMLElement>('[data-testid="security-roles-grid"]');
    expect(grid?.textContent).toContain("システム管理者");
    expect(grid?.textContent).toContain("人事利用者");
    expect(grid?.textContent).not.toContain("SYSTEM_ADMIN");
    expect(grid?.textContent).not.toContain("HR_USER");
    expect(host.querySelector("#security-roles-detail-heading")?.textContent?.trim()).toBe("システム管理者");
    expectNoUuidShown(host);
  });
});
