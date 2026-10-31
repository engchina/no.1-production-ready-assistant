// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RoleManagementPage, type RoleManagementApi, type SecurityRole } from "../src";

// #1038: ロール管理の読み込みの失敗を空の一覧と誤表示しない。編集の画面の操作の失敗を 2 回出さない。

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

const ROLE: SecurityRole = {
  role_id: "role-1",
  role_code: "ANALYST",
  display_name: "分析者",
  description: "",
  is_built_in: false,
  archived: false,
  version: 1,
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

function buttons(text: string, scope: ParentNode = document.body) {
  return Array.from(scope.querySelectorAll<HTMLElement>("button, [role='menuitem']")).filter(
    (element) => element.textContent?.trim() === text,
  );
}

async function click(element: HTMLElement | undefined) {
  if (!element) throw new Error("押す対象が無い");
  await act(async () => {
    element.click();
  });
  await act(async () => undefined);
}

function occurrences(text: string) {
  return (document.body.textContent ?? "").split(text).length - 1;
}

describe("ロール管理の読み込みと操作の失敗（#1038）", () => {
  it("初回の読み込みに失敗したら、空の一覧と新規作成を出さず、再試行で読み込み直せる", async () => {
    let calls = 0;
    const api: RoleManagementApi = {
      roles: () => (calls++ === 0 ? Promise.reject(new Error("ロールを読み込めませんでした。")) : Promise.resolve([ROLE])),
      createRole: never,
      updateRole: never,
      archiveRole: never,
      restoreRole: never,
      deleteRole: never,
    };
    await render(<RoleManagementPage api={api} canManage />);

    expect(occurrences("ロールを読み込めませんでした。")).toBe(1);
    expect(host.textContent).not.toContain("対象データはありません");
    expect(host.querySelector('[data-testid="security-roles-grid"]')).toBeNull();
    expect(buttons("新規作成")).toHaveLength(0);

    await click(buttons("再試行")[0]);

    expect(host.textContent).not.toContain("ロールを読み込めませんでした。");
    expect(host.querySelector('[data-testid="security-roles-grid"]')?.textContent).toContain("分析者");
    expect(buttons("新規作成")).toHaveLength(1);
  });

  it("最新情報を取得したときの失敗は、前の一覧を残して失敗を出す", async () => {
    let calls = 0;
    const api: RoleManagementApi = {
      roles: () => (calls++ === 0 ? Promise.resolve([ROLE]) : Promise.reject(new Error("更新に失敗しました。"))),
      createRole: never,
      updateRole: never,
      archiveRole: never,
      restoreRole: never,
      deleteRole: never,
    };
    await render(<RoleManagementPage api={api} canManage />);
    await click(buttons("最新情報を取得")[0]);

    expect(occurrences("更新に失敗しました。")).toBe(1);
    expect(host.querySelector('[data-testid="security-roles-grid"]')?.textContent).toContain("分析者");
    expect(buttons("再試行")).toHaveLength(0);
  });

  it("編集の画面でアーカイブに失敗したら、失敗の文を 1 回だけ出す", async () => {
    const api: RoleManagementApi = {
      roles: () => Promise.resolve([ROLE]),
      createRole: never,
      updateRole: never,
      archiveRole: () => Promise.reject(new Error("アーカイブできません。")),
      restoreRole: never,
      deleteRole: never,
    };
    await render(<RoleManagementPage api={api} canManage />);
    // 先頭の行は自動で選ばれない（#2282）。一覧の行を選んでから詳細の「編集」を押す。
    await click(host.querySelector<HTMLElement>('[data-testid="security-roles-grid"] [data-row-title-button]') ?? undefined);

    const detailActions = host.querySelector<HTMLElement>('[data-testid="security-roles-detail-actions"]');
    await click(buttons("編集", detailActions ?? undefined)[0]);
    const objectActions = host.querySelector<HTMLElement>('[data-testid="security-roles-object-actions"]');
    expect(objectActions).not.toBeNull();
    const archive = buttons("アーカイブ", objectActions ?? undefined)[0];
    if (archive) {
      await click(archive);
    } else {
      await click(buttons("その他の操作", objectActions ?? undefined)[0]);
      await click(buttons("アーカイブ")[0]);
    }
    await click(buttons("実行")[0]);

    expect(occurrences("アーカイブできません。")).toBe(1);
  });
});
