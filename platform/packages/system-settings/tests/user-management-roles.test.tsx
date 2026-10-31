// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { UserManagementPage, type SecurityRole, type SecurityUser, type UserManagementApi } from "../src";
import { nextUserRoleIds } from "../src/users-roles/shared";

// #1050: ユーザーの編集の保存で、選択欄に出ないほかのロールを黙って外さない。

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

const never = () => new Promise<never>(() => undefined);

function role(id: string, code: string, name: string): SecurityRole {
  return { role_id: id, role_code: code, display_name: name, description: "", is_built_in: false, archived: false, version: 1 };
}

const ROLES = [role("r1", "HR_USER", "人事利用者"), role("r2", "FINANCE_USER", "経理利用者"), role("r3", "SALES_USER", "営業利用者")];

const USER: SecurityUser = {
  user_uuid: "user-1",
  login_user_id: "alice",
  display_name: "Alice",
  status: "ACTIVE",
  force_password_change: false,
  locked_until: null,
  version: 3,
  role_ids: ["r1", "r2"],
  assigned_roles: [
    { role_id: "r1", role_code: "HR_USER", display_name: "人事利用者", is_built_in: false, archived: false },
    { role_id: "r2", role_code: "FINANCE_USER", display_name: "経理利用者", is_built_in: false, archived: false },
  ],
  is_bootstrap_admin: false,
};

async function render(node: ReactNode) {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <ConfirmProvider>{node}</ConfirmProvider>
      </MemoryRouter>,
    );
  });
  await act(async () => undefined);
}

async function click(element: HTMLElement | null | undefined) {
  if (!element) throw new Error("押す対象が無い");
  await act(async () => {
    element.click();
  });
  await act(async () => undefined);
}

function buttonByText(text: string, scope: ParentNode = host) {
  return Array.from(scope.querySelectorAll<HTMLButtonElement>("button")).find(
    (element) => element.textContent?.trim() === text,
  );
}

async function typeInto(input: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function setup() {
  const updates: SecurityUser[] = [];
  const api: UserManagementApi = {
    users: () => Promise.resolve([USER]),
    createUser: never,
    updateUser: (user) => {
      updates.push(user);
      return Promise.resolve({ ...user, version: user.version + 1 });
    },
    deleteUser: never,
    resetPassword: never,
    unlockUser: never,
    setUserEnabled: never,
    roles: () => Promise.resolve(ROLES),
  };
  return { api, updates };
}

async function openEdit() {
  // 先頭の行は自動で選ばれない（#2282）。一覧の行を選んでから詳細の「編集」を押す。
  await click(host.querySelector<HTMLElement>('[data-testid="security-users-grid"] [data-row-title-button]'));
  const detail = host.querySelector<HTMLElement>('[data-testid="security-users-detail-actions"]');
  await click(buttonByText("編集", detail ?? host));
}

describe("ユーザーの編集でほかのロールを残す（#1050）", () => {
  it("nextUserRoleIds は選び直したロールだけを置き換える", () => {
    expect(nextUserRoleIds(["r1", "r2"], "r1", "r1")).toEqual(["r1", "r2"]);
    expect(nextUserRoleIds(["r1", "r2"], "r1", "r3")).toEqual(["r3", "r2"]);
    expect(nextUserRoleIds(["r1", "r2"], "r1", "r2")).toEqual(["r2"]);
    // 選択欄に出るロールが無かった（アーカイブ済みだけ）ユーザーは、選んだロールを足す。
    expect(nextUserRoleIds(["archived"], "", "r1")).toEqual(["archived", "r1"]);
  });

  it("表示名だけを変えて保存しても、2 つ目のロールを外さない", async () => {
    const { api, updates } = setup();
    await render(<UserManagementPage api={api} canManage />);
    await openEdit();

    expect(host.querySelector('[data-testid="security-users-other-roles"]')?.textContent).toContain("経理利用者");

    await typeInto(host.querySelector<HTMLInputElement>("#security-user-display-name")!, "Alice 2");
    await act(async () => {
      host.querySelector<HTMLFormElement>("form")?.requestSubmit();
    });
    await act(async () => undefined);

    expect(updates).toHaveLength(1);
    expect(updates[0].display_name).toBe("Alice 2");
    expect(updates[0].role_ids).toEqual(["r1", "r2"]);
  });

  it("ロールを選び直すと、選ばれていたロールだけを置き換える", async () => {
    const { api, updates } = setup();
    await render(<UserManagementPage api={api} canManage />);
    await openEdit();

    await click(host.querySelector<HTMLInputElement>('input[type="radio"][value="r3"]'));
    await act(async () => {
      host.querySelector<HTMLFormElement>("form")?.requestSubmit();
    });
    await act(async () => undefined);

    expect(updates).toHaveLength(1);
    expect(updates[0].role_ids).toEqual(["r3", "r2"]);
  });
});

describe("システム管理者のロールを選択肢に出さない（#1792）", () => {
  const SYSTEM_ADMIN = role("role-system", "SYSTEM_ADMIN", "システム管理者");

  function setupWithSystemAdmin(user: SecurityUser) {
    const api: UserManagementApi = {
      ...setup().api,
      users: () => Promise.resolve([user]),
      roles: () => Promise.resolve([SYSTEM_ADMIN, ...ROLES]),
    };
    return api;
  }

  function roleRadioValues() {
    return Array.from(host.querySelectorAll<HTMLInputElement>('input[type="radio"][name="security-users-role"]')).map(
      (input) => input.value,
    );
  }

  it("新規作成で出さず、理由を添える", async () => {
    await render(<UserManagementPage api={setupWithSystemAdmin(USER)} canManage />);
    await click(buttonByText("新規作成"));

    expect(roleRadioValues()).toEqual(["r1", "r2", "r3"]);
    expect(host.querySelector('[data-testid="security-users-role-system-admin-note"]')?.textContent).toContain(
      "構成管理者（system_admin）だけが持つ",
    );
  });

  it("既存の割り当てを持つユーザーの編集でも出さず、ほかのロールとして示す", async () => {
    const legacyAdmin: SecurityUser = {
      ...USER,
      role_ids: ["role-system"],
      assigned_roles: [
        { role_id: "role-system", role_code: "SYSTEM_ADMIN", display_name: "システム管理者", is_built_in: true, archived: false },
      ],
    };
    await render(<UserManagementPage api={setupWithSystemAdmin(legacyAdmin)} canManage />);
    await openEdit();

    expect(roleRadioValues()).toEqual(["r1", "r2", "r3"]);
    expect(host.querySelector<HTMLInputElement>('input[type="radio"]:checked')).toBeNull();
    expect(host.querySelector('[data-testid="security-users-other-roles"]')?.textContent).toContain("システム管理者");
  });
});
