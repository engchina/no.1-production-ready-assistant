// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  UserManagementPage,
  deletedUserLabel,
  userDisplayName,
  type SecurityRole,
  type SecurityUser,
  type UserManagementApi,
} from "../src";

// #1696: サイドナビのアカウント欄の 1 行目だけがログイン ID。それ以外の画面は利用者の表示名を出し、
// ログイン ID・UUID を出さない（表示名が空ならログイン ID、引けない利用者は「（削除済みのユーザー）」）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const USER_UUID = "6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5";
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/iu;
const ATTRIBUTES = ["title", "aria-label", "aria-description", "placeholder"];

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

// 選択は URL の ?user= が持ち、先頭の行を自動で選ばない（#2282）。詳細を見るテストは URL で選んで開く。
async function render(node: ReactNode, entry = `/?user=${USER_UUID}`) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[entry]}>
        <ConfirmProvider>{node}</ConfirmProvider>
      </MemoryRouter>,
    );
  });
  await act(async () => undefined);
}

const never = () => new Promise<never>(() => undefined);

function role(id: string, code: string, name: string): SecurityRole {
  return { role_id: id, role_code: code, display_name: name, description: "", is_built_in: false, archived: false, version: 1 };
}

function user(overrides: Partial<SecurityUser>): SecurityUser {
  return {
    user_uuid: USER_UUID,
    login_user_id: "alice.login",
    display_name: "アリス",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: ["r-hr"],
    assigned_roles: [{ role_id: "r-hr", role_code: "HR_USER", display_name: "人事利用者", is_built_in: false, archived: false }],
    is_bootstrap_admin: false,
    ...overrides,
  };
}

function userApi(users: SecurityUser[]): UserManagementApi {
  return {
    users: () => Promise.resolve(users),
    createUser: never,
    updateUser: never,
    deleteUser: never,
    resetPassword: never,
    unlockUser: never,
    setUserEnabled: never,
    roles: () => Promise.resolve([role("r-hr", "HR_USER", "人事利用者")]),
  };
}

/** 文字と属性（title・aria-label など）を、ログイン ID の欄（`<code>`）を除いて集める。 */
function textsOutsideLoginIdField(scope: HTMLElement): string[] {
  const clone = scope.cloneNode(true) as HTMLElement;
  for (const code of Array.from(clone.querySelectorAll("code"))) code.remove();
  const texts = [clone.textContent ?? ""];
  for (const element of Array.from(clone.querySelectorAll<HTMLElement>("*"))) {
    for (const name of ATTRIBUTES) texts.push(element.getAttribute(name) ?? "");
  }
  return texts;
}

describe("userDisplayName（#1696）", () => {
  it("表示名を返し、空ならログイン ID、どちらも無ければ削除済みの文言", () => {
    expect(userDisplayName({ display_name: "システム管理者", login_user_id: "system_admin" })).toBe("システム管理者");
    expect(userDisplayName({ display_name: "  アリス ", login_user_id: "alice" })).toBe("アリス");
    expect(userDisplayName({ display_name: " ", login_user_id: "alice" })).toBe("alice");
    expect(userDisplayName({ display_name: "", login_user_id: "" })).toBe("（削除済みのユーザー）");
    expect(userDisplayName(null)).toBe("（削除済みのユーザー）");
    expect(userDisplayName(undefined, "記録なし")).toBe("記録なし");
    expect(deletedUserLabel()).toBe("（削除済みのユーザー）");
  });
});

describe("ユーザー管理の利用者の表示（#1696）", () => {
  it("一覧の行・詳細の見出し・読み上げは表示名で、ログイン ID は「ログインユーザーID」の欄にだけ、UUID はどこにも出さない", async () => {
    await render(<UserManagementPage api={userApi([user({})])} canManage />);

    const grid = host.querySelector<HTMLElement>('[data-testid="security-users-grid"]');
    expect(grid?.textContent).toContain("アリス");
    expect(grid?.textContent).not.toContain("alice.login");
    expect(host.querySelector("#security-users-detail-heading")?.textContent?.trim()).toBe("アリス");
    const loginIdFields = Array.from(host.querySelectorAll("code")).filter((node) => node.textContent === "alice.login");
    expect(loginIdFields).toHaveLength(1);
    for (const text of textsOutsideLoginIdField(host)) {
      expect(text).not.toContain("alice.login");
      expect(text).not.toMatch(UUID_PATTERN);
    }
  });

  it("表示名が空の利用者はログイン ID を名前にする", async () => {
    await render(<UserManagementPage api={userApi([user({ display_name: "" })])} canManage />);

    expect(host.querySelector("#security-users-detail-heading")?.textContent?.trim()).toBe("alice.login");
    expect(host.textContent).not.toMatch(UUID_PATTERN);
  });
});
