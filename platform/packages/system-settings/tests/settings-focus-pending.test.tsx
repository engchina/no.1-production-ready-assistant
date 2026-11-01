// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DATABASE_SETTINGS_QUERY_KEY,
  DatabaseSettingsPage,
  RoleManagementPage,
  UserManagementPage,
  type DatabaseSettingsApi,
  type DatabaseSettingsData,
  type RoleManagementApi,
  type SecurityRole,
  type SecurityUser,
  type UserManagementApi,
} from "../src";

// #835: 処理中に押したボタンはネイティブの disabled にせず（loading の aria-disabled）、フォーカスを保つ（#355）。
// ページ全体を <fieldset disabled> で包んだり、押したボタンに disabled={busy} を重ねたりすると、押したボタンまで
// :disabled になり、Chromium ではフォーカスが body へ外れる。他の操作は部品ごとに disabled にする。

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

async function render(node: ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfirmProvider>{node}</ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
}

/** キーボードで押したのと同じく、フォーカスしてから押す。 */
async function press(button: HTMLButtonElement) {
  await act(async () => {
    button.focus();
    button.click();
  });
}

function buttonByText(text: string, scope: ParentNode = host) {
  const button = Array.from(scope.querySelectorAll<HTMLButtonElement>("button")).find(
    (element) => element.textContent?.trim() === text,
  );
  if (!button) throw new Error(`ボタン「${text}」が無い`);
  return button;
}

/** 押したボタンの期待: loading（aria-busy / aria-disabled）で、ネイティブの disabled ではなく、フォーカスがある。 */
function expectPressedKeepsFocus(button: HTMLButtonElement) {
  expect(button.getAttribute("aria-busy")).toBe("true");
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.matches(":disabled")).toBe(false);
  expect(button.closest("fieldset:disabled")).toBeNull();
  expect(document.activeElement).toBe(button);
}

/** 押していない操作の期待: ネイティブの disabled で、スピナーを出さない。 */
function expectDisabledOnly(element: HTMLElement) {
  expect(element.matches(":disabled")).toBe(true);
  expect(element.getAttribute("aria-busy")).not.toBe("true");
}

const DB_SETTINGS: DatabaseSettingsData = {
  user: "app",
  dsn: "db_high",
  driver_mode: "thin",
  connection_security: "wallet_mtls",
  client_lib_dir: "",
  wallet_dir: "/wallet",
  wallet_uploaded: true,
  available_services: ["db_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1024,
  vector_column: "EMBEDDING",
  config_source: "runtime",
};

function databaseApi(overrides: Partial<DatabaseSettingsApi> = {}): DatabaseSettingsApi {
  return {
    getDatabaseSettings: () => Promise.resolve(DB_SETTINGS),
    updateDatabaseSettings: never,
    uploadDatabaseWallet: never,
    testDatabaseSettings: never,
    ...overrides,
  };
}

async function renderDatabasePage(api: DatabaseSettingsApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, DB_SETTINGS);
  await render(<DatabaseSettingsPage api={api} />, client);
}

function databaseFormButton(id: "save" | "test") {
  const button = host.querySelector<HTMLButtonElement>(`form button[data-form-action-id="${id}"]`);
  if (!button) throw new Error(`DB 接続の操作 ${id} が無い`);
  return button;
}

describe("データベース設定: 押したボタンがフォーカスを保つ（#835）", () => {
  it("「DB設定を保存」の処理中は保存のボタンにフォーカスが残り、他の操作と入力欄は無効になる", async () => {
    await renderDatabasePage(databaseApi());
    const save = databaseFormButton("save");
    expect(save.textContent?.trim()).toBe("DB設定を保存");

    await press(save);

    expectPressedKeepsFocus(databaseFormButton("save"));
    expectDisabledOnly(databaseFormButton("test"));
    expect(host.querySelector<HTMLInputElement>("#oracle-user")?.matches(":disabled")).toBe(true);
    expect(host.querySelector<HTMLInputElement>("#oracle-password")?.matches(":disabled")).toBe(true);
    expect(host.querySelectorAll('button[aria-busy="true"]')).toHaveLength(1);
  });

  it("「DB接続テスト」の処理中は接続テストのボタンにフォーカスが残り、保存は無効になる", async () => {
    await renderDatabasePage(databaseApi());

    await press(databaseFormButton("test"));

    expectPressedKeepsFocus(databaseFormButton("test"));
    expectDisabledOnly(databaseFormButton("save"));
    expect(host.querySelector<HTMLInputElement>("#oracle-user")?.matches(":disabled")).toBe(true);
  });

  it("保存済みのパスワードの取得中は、表示の切り替えボタンにフォーカスが残る", async () => {
    await renderDatabasePage(databaseApi({ revealDatabasePassword: never }));
    const toggle = host.querySelector<HTMLButtonElement>("#oracle-password + button");
    if (!toggle) throw new Error("パスワードの表示の切り替えが無い");

    await press(toggle);

    const pending = host.querySelector<HTMLButtonElement>("#oracle-password + button");
    if (!pending) throw new Error("パスワードの表示の切り替えが無い");
    expectPressedKeepsFocus(pending);
    expect(host.querySelector<HTMLInputElement>("#oracle-password")?.matches(":disabled")).toBe(true);
    expectDisabledOnly(databaseFormButton("save"));
  });
});

const USER: SecurityUser = {
  user_uuid: "user-1",
  login_user_id: "alice",
  display_name: "Alice",
  status: "ACTIVE",
  force_password_change: false,
  locked_until: null,
  version: 1,
  role_ids: [],
  is_bootstrap_admin: false,
};

const ROLE: SecurityRole = {
  role_id: "role-1",
  role_code: "ANALYST",
  display_name: "分析者",
  description: "",
  is_built_in: false,
  archived: false,
  version: 1,
};

describe("ユーザー管理・ロール管理: 「最新情報を取得」がフォーカスを保つ（#835）", () => {
  it("ユーザー管理の再読込の間、「最新情報を取得」にフォーカスが残り、「新規作成」は無効になる", async () => {
    let calls = 0;
    const api: UserManagementApi = {
      users: () => (calls++ === 0 ? Promise.resolve([USER]) : never()),
      createUser: never,
      updateUser: never,
      deleteUser: never,
      resetPassword: never,
      unlockUser: never,
      setUserEnabled: never,
      roles: () => Promise.resolve([ROLE]),
    };
    await render(<UserManagementPage api={api} canManage />);
    await act(async () => undefined);

    await press(buttonByText("最新情報を取得"));

    expectPressedKeepsFocus(buttonByText("最新情報を取得"));
    expectDisabledOnly(buttonByText("新規作成"));
  });

  it("ロール管理の再読込の間、「最新情報を取得」にフォーカスが残り、「新規作成」は無効になる", async () => {
    let calls = 0;
    const api: RoleManagementApi = {
      roles: () => (calls++ === 0 ? Promise.resolve([ROLE]) : never()),
      createRole: never,
      updateRole: never,
      archiveRole: never,
      restoreRole: never,
      deleteRole: never,
    };
    await render(<RoleManagementPage api={api} canManage />);
    await act(async () => undefined);

    await press(buttonByText("最新情報を取得"));

    expectPressedKeepsFocus(buttonByText("最新情報を取得"));
    expectDisabledOnly(buttonByText("新規作成"));
  });
});
