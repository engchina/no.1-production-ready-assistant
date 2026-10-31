import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { PermissionDefinition } from "@production-ready/system-settings";
import { describe, expect, it } from "vitest";

import { visibleNavSections } from "@/components/layout/nav-config";
import { t } from "./i18n";
import { CAPABILITY_PERMISSIONS } from "./permissions";
import { arrangeRagPermissions } from "./permission-targets";

// 権限管理の機能の一覧は、左のナビを正本にしてグループ・並び順・名前をそろえる（Issue 567）。
// backend のカタログ（app/security/permissions.py）の code を、ナビと違うグループ・名前・逆順で渡しても、
// 一覧はサイドナビと同じになることを確かめる。

const backendCodes = [
  ...readFileSync(resolve(__dirname, "../../../backend/app/security/permissions.py"), "utf8").matchAll(
    /^[A-Z_]+ = "((?:menu|rag)\.[a-z_.]+)"$/gm,
  ),
].map((match) => match[1]);

const scrambledCatalog: PermissionDefinition[] = backendCodes
  .map((code) => ({
    code,
    group: code.startsWith("menu.") ? "backend のグループ" : "管理権限",
    label: `backend の名前 ${code}`,
    description: `${code} の説明`,
    implies: [],
  }))
  .reverse();

/** サイドナビに出る見出しと項目の名前（Sidebar.tsx と同じ規則: sidebarLabelKey を優先）。 */
function sidebarGroups(): [string, string[]][] {
  return visibleNavSections(() => true).map((section) => [
    t(section.titleKey),
    section.items.map((item) => t(item.sidebarLabelKey ?? item.labelKey)),
  ]);
}

function permissionGroups(permissions: PermissionDefinition[]): [string, string[]][] {
  const groups = new Map<string, string[]>();
  for (const permission of permissions) {
    groups.set(permission.group, [...(groups.get(permission.group) ?? []), permission.label]);
  }
  return [...groups.entries()];
}

describe("権限管理の機能の一覧と左のナビ", () => {
  const arranged = arrangeRagPermissions(scrambledCatalog);

  it("メニュー権限は、サイドナビと同じグループ・並び順・名前になる", () => {
    const menuGroups = permissionGroups(arranged.filter((permission) => permission.code.startsWith("menu.")));
    expect(menuGroups).toEqual(sidebarGroups());
  });

  it("ナビに無い権限は capability だけで、ナビの後ろに backend のグループのまま置く", () => {
    const firstCapability = arranged.findIndex((permission) => !permission.code.startsWith("menu."));
    const rest = arranged.slice(firstCapability);
    expect(rest.every((permission) => permission.group === "管理権限")).toBe(true);
    expect(rest.map((permission) => permission.code).sort()).toEqual(Object.values(CAPABILITY_PERMISSIONS).sort());
  });

  it("カタログの権限を落とさず、code・説明は変えない", () => {
    expect(arranged.map((permission) => permission.code).sort()).toEqual([...backendCodes].sort());
    for (const permission of arranged) {
      expect(permission.description).toBe(`${permission.code} の説明`);
    }
  });
});
