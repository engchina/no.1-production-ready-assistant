import { describe, expect, it } from "vitest";

import { arrangePermissionsByNav, permissionNavSections, type PermissionDefinition } from "../src";

function permission(code: string, group: string, label: string, implies: string[] = []): PermissionDefinition {
  return { code, group, label, description: `${label}の説明`, implies };
}

// backend のカタログは、ナビと違うグループ・順序・名前を持つことがある（#567）。
const CATALOG: PermissionDefinition[] = [
  permission("app.manage", "管理権限", "管理"),
  permission("menu.search", "業務", "検索"),
  permission("menu.chat", "業務", "チャット"),
  permission("menu.upload", "取り込み", "文書アップロード"),
  permission("menu.settings", "システム設定", "設定"),
  permission("app.read", "参照権限", "参照", ["menu.search"]),
];

describe("permissionNavSections", () => {
  it("見出しはセクション、名前はサイドナビの表示名（sidebarLabelKey を優先）にする", () => {
    const labels: Record<string, string> = {
      "section.use": "利用",
      "nav.upload": "文書アップロード",
      "nav.upload.sidebar": "アップロード",
      "nav.chat": "チャット",
    };
    expect(
      permissionNavSections(
        [
          {
            titleKey: "section.use",
            items: [
              { labelKey: "nav.chat", permission: "menu.chat" },
              { labelKey: "nav.upload", sidebarLabelKey: "nav.upload.sidebar", permission: "menu.upload" },
            ],
          },
        ],
        (key) => labels[key] ?? key,
      ),
    ).toEqual([
      {
        title: "利用",
        items: [
          { label: "チャット", permission: "menu.chat" },
          { label: "アップロード", permission: "menu.upload" },
        ],
      },
    ]);
  });
});

describe("arrangePermissionsByNav", () => {
  const arranged = arrangePermissionsByNav(CATALOG, [
    {
      title: "利用",
      items: [
        { label: "チャット", permission: "menu.chat" },
        { label: "検索", permission: "menu.search" },
      ],
    },
    { title: "準備", items: [{ label: "アップロード", permission: "menu.upload" }] },
    // カタログに無い権限はロールに付けられないので出さない。
    { title: "運用", items: [{ label: "未登録", permission: "menu.unknown" }] },
    { title: "システム設定", items: [{ label: "設定", permission: "menu.settings" }] },
  ]);

  it("ナビの権限はナビのグループ・並び順・名前で並べ、ナビに無い権限は backend のまま後ろに置く", () => {
    expect(arranged.map(({ code, group, label }) => [group, label, code])).toEqual([
      ["利用", "チャット", "menu.chat"],
      ["利用", "検索", "menu.search"],
      ["準備", "アップロード", "menu.upload"],
      ["システム設定", "設定", "menu.settings"],
      ["管理権限", "管理", "app.manage"],
      ["参照権限", "参照", "app.read"],
    ]);
  });

  it("権限の code・説明・implies は変えない", () => {
    const byCode = new Map(arranged.map((item) => [item.code, item]));
    for (const original of CATALOG) {
      const item = byCode.get(original.code);
      expect(item?.description).toBe(original.description);
      expect(item?.implies).toEqual(original.implies);
    }
    expect(arranged).toHaveLength(CATALOG.length);
  });

  it("同じ権限がナビに 2 回あっても一覧には 1 回だけ出す", () => {
    const twice = arrangePermissionsByNav(CATALOG, [
      { title: "A", items: [{ label: "検索", permission: "menu.search" }] },
      { title: "B", items: [{ label: "検索（別の入口）", permission: "menu.search" }] },
    ]);
    expect(twice.filter((item) => item.code === "menu.search")).toEqual([
      { ...CATALOG[1], group: "A", label: "検索" },
    ]);
  });
});
