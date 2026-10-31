import { ConfirmProvider } from "@production-ready/ui";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  RolePermissionsPage,
  effectivePermissionCodes,
  normalizeCustomTargetId,
  permissionInheritanceSources,
  targetItemLabel,
  targetItemsWithCustomIds,
  resolveTargetItems,
  rolePermissionTargetSearchParams,
  type PermissionDefinition,
  type PermissionRole,
  type RolePermissionTargetQuery,
  type RolePermissionTargetSection,
  type RolePermissionsApi,
} from "../src";

const pending = () => new Promise<never>(() => undefined);

interface RagRole extends PermissionRole {
  search_answer_profile_ids: string[];
  knowledge_base_ids: string[];
}

const api: RolePermissionsApi<RagRole> = { roles: pending, permissions: pending, save: pending };

function targetMessages(title: string) {
  return {
    title,
    all: `すべての${title}`,
    searchLabel: `${title}を検索`,
    searchPlaceholder: "名前・説明で絞り込み",
    empty: `${title}がありません。`,
    noResults: `条件に一致する${title}がありません。`,
    loadWarning: `${title}の候補を読み込めませんでした。詳細: {message}`,
    grantsAllByPermission: `管理権限により、すべての${title}を利用できます。`,
    grantsAllSystemAdmin: `SYSTEM_ADMIN はすべての${title}を利用できます。`,
  };
}

// RAG の検索・回答プロファイル・ナレッジベースを targets として渡す例。
const ragTargets: RolePermissionTargetSection<RagRole>[] = [
  {
    key: "search-answer-profiles",
    messages: targetMessages("検索・回答プロファイル"),
    query: pending,
    selectedIds: (role) => role.search_answer_profile_ids,
    grantsAll: (codes) => codes.has("rag.search_answer_profiles.manage"),
  },
  {
    key: "knowledge-bases",
    messages: targetMessages("ナレッジベース"),
    query: pending,
    selectedIds: (role) => role.knowledge_base_ids,
  },
];

function render(node: ReactNode) {
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={["/settings/security/permissions?role=role-1"]}>
      <ConfirmProvider>{node}</ConfirmProvider>
    </MemoryRouter>,
  );
}

const catalog: PermissionDefinition[] = [
  { code: "menu.search", group: "メニュー", label: "検索", description: "", implies: ["rag.read"] },
  { code: "rag.read", group: "参照", label: "参照", description: "", implies: ["rag.meta.read"] },
  { code: "rag.meta.read", group: "参照", label: "メタ参照", description: "", implies: [] },
];

describe("RolePermissionsPage", () => {
  it("読み込み中は状態表示を出し、NL2SQL と同じテスト ID の一覧 + 詳細を持つ", () => {
    const html = render(<RolePermissionsPage api={api} canManage targets={ragTargets} />);
    expect(html).toContain("権限管理");
    expect(html).toContain('data-testid="security-permissions-loading"');
    expect(html).toContain("権限を読込中");
    expect(html).toContain('data-testid="security-permissions-actions"');
    expect(html).toContain('data-testid="security-permissions-search"');
    // 一覧の列は狭いので、利用できる対象の件数は列にせず詳細に出す（#2282）。
    expect(html).toContain('data-testid="list-detail-security-permissions"');
  });

  it("文言は messages で上書きできる", () => {
    const html = render(
      <RolePermissionsPage api={api} canManage messages={{ subtitle: "RAG のロール権限を設定します。" }} />,
    );
    expect(html).toContain("RAG のロール権限を設定します。");
  });
});

describe("権限の継承", () => {
  const byCode = new Map(catalog.map((permission) => [permission.code, permission]));

  it("implies を推移的にたどり、継承元の権限名を記録する", () => {
    const sources = permissionInheritanceSources(["menu.search"], byCode);
    expect(sources.get("rag.read")).toEqual(["検索"]);
    expect(sources.get("rag.meta.read")).toEqual(["検索"]);
    expect([...effectivePermissionCodes(["menu.search"], byCode)].sort()).toEqual([
      "menu.search",
      "rag.meta.read",
      "rag.read",
    ]);
  });

  it("対象の表示名は補足を括弧で添える", () => {
    expect(targetItemLabel({ id: "p1", name: "財務", secondary: "会計" })).toBe("財務 (会計)");
    expect(targetItemLabel({ id: "p1", name: "財務" })).toBe("財務");
  });
});

describe("候補にない ID の直接入力（allowCustomIds。#215）", () => {
  it("前後の空白を除き、空・形式に合わない ID は受け付けない", () => {
    const pattern = /^[A-Za-z0-9._:-]{1,64}$/;
    expect(normalizeCustomTargetId("  sales-east  ", pattern)).toBe("sales-east");
    expect(normalizeCustomTargetId("   ", pattern)).toBeNull();
    expect(normalizeCustomTargetId("営業 東日本", pattern)).toBeNull();
    expect(normalizeCustomTargetId("x".repeat(65), pattern)).toBeNull();
    // 形式の指定が無ければ空でない文字列を受け付ける。
    expect(normalizeCustomTargetId(" 営業 ")).toBe("営業");
  });

  it("g フラグ付きの形式でも毎回先頭から判定する", () => {
    const pattern = /^[a-z-]+$/g;
    expect(normalizeCustomTargetId("sales", pattern)).toBe("sales");
    expect(normalizeCustomTargetId("sales", pattern)).toBe("sales");
  });

  it("選択済みで候補にない ID を名前 = ID の候補として末尾に足し、重複させない", () => {
    const items = [{ id: "sales", name: "sales" }];
    expect(targetItemsWithCustomIds(items, ["sales", "hr", "hr", "finance"], "直接入力")).toEqual([
      { id: "sales", name: "sales" },
      { id: "hr", name: "hr", status: "直接入力" },
      { id: "finance", name: "finance", status: "直接入力" },
    ]);
    // 状態の文言が無ければ status を付けない。元の配列は変えない。
    expect(targetItemsWithCustomIds(items, ["hr"])).toEqual([
      { id: "sales", name: "sales" },
      { id: "hr", name: "hr" },
    ]);
    expect(items).toEqual([{ id: "sales", name: "sales" }]);
  });

  it("直接入力を許可する対象を渡しても、NL2SQL / RAG と同じ一覧の構成で描画できる", () => {
    const agentTargets: RolePermissionTargetSection<RagRole>[] = [
      {
        ...ragTargets[0],
        key: "search-answer-profile-access",
        allowCustomIds: {
          label: "検索・回答プロファイル ID を直接入力",
          addLabel: "追加",
          pattern: /^[A-Za-z0-9._:-]{1,64}$/,
          invalidMessage: "検索・回答プロファイル ID の形式が正しくありません。",
          customStatus: "直接入力",
        },
      },
    ];
    const html = render(<RolePermissionsPage api={api} canManage targets={agentTargets} />);
    expect(html).toContain('data-testid="security-permissions-grid"');
    expect(html).toContain('data-testid="list-detail-security-permissions-list"');
  });
});

describe("選択済みの対象の名前の解決（#608）", () => {
  const signal = new AbortController().signal;

  it("ID を 100 件ずつに分けて ids で問い合わせ、重複を除いて読む", async () => {
    const calls: RolePermissionTargetQuery[] = [];
    const ids = Array.from({ length: 150 }, (_, index) => `bv-${index}`);
    const result = await resolveTargetItems(
      {
        query: async (query) => {
          calls.push(query);
          return { items: (query.ids ?? []).map((id) => ({ id, name: `名前 ${id}` })), total: query.ids?.length ?? 0 };
        },
      },
      [...ids, "bv-0", "bv-1"],
      signal,
    );
    expect(calls.map((call) => [call.q, call.offset, call.limit, call.ids?.length])).toEqual([
      ["", 0, 100, 100],
      ["", 0, 50, 50],
    ]);
    expect(result.items).toHaveLength(150);
    expect(result.items[0]).toEqual({ id: "bv-0", name: "名前 bv-0" });
    expect(result.warning).toBe("");
  });

  it("クエリ文字列は q（空なら付けない）・limit・offset と繰り返しの ids", () => {
    expect(rolePermissionTargetSearchParams({ q: "  人事 ", limit: 50, offset: 100 }).toString()).toBe(
      "limit=50&offset=100&q=%E4%BA%BA%E4%BA%8B",
    );
    expect(rolePermissionTargetSearchParams({ q: "", limit: 2, offset: 0, ids: ["a", "b"] }).toString()).toBe(
      "limit=2&offset=0&ids=a&ids=b",
    );
  });

  it("選択が無ければ問い合わせない。一部だけ読めた理由（#240）は前後の空白を除いて返す", async () => {
    let called = false;
    const none = await resolveTargetItems(
      {
        query: async () => {
          called = true;
          return { items: [], total: 0 };
        },
      },
      [],
      signal,
    );
    expect(called).toBe(false);
    expect(none).toEqual({ items: [], warning: "" });
    const partial = await resolveTargetItems(
      { query: async () => ({ items: [{ id: "sales", name: "営業" }], total: 1, warning: "  RAG の検索・回答プロファイルを読めませんでした。  " }) },
      ["sales"],
      signal,
    );
    expect(partial).toEqual({ items: [{ id: "sales", name: "営業" }], warning: "RAG の検索・回答プロファイルを読めませんでした。" });
  });
});
