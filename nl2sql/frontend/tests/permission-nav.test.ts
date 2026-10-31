import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// 権限管理の機能の一覧は、左のナビを正本にしてグループ・並び順・名前をそろえる（Issue 567）。
// backend のカタログ（app/security/permissions.py）の code を、ナビと違うグループ・名前・逆順で渡しても、
// 一覧はサイドナビと同じになることを確かめる。

type ResolveFilename = (request: string, parent: unknown, isMain: boolean, options?: unknown) => string;
type ModuleWithResolver = { _resolveFilename: ResolveFilename };

const require = createRequire(import.meta.url);
const testDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(testDir, "..");
const moduleResolver = require("node:module") as ModuleWithResolver;
const originalResolveFilename = moduleResolver._resolveFilename;

// src の `@/` alias を解決する（nav-config-icons.test.ts と同じ）。
moduleResolver._resolveFilename = function resolveTestAlias(
  this: unknown,
  request: string,
  parent: unknown,
  isMain: boolean,
  options?: unknown
) {
  if (!request.startsWith("@/")) {
    return originalResolveFilename.call(this, request, parent, isMain, options);
  }
  const basePath = resolve(frontendRoot, "src", request.slice(2));
  const resolvedRequest = [basePath, `${basePath}.ts`, `${basePath}.tsx`].find((candidate) =>
    existsSync(candidate)
  );
  return originalResolveFilename.call(this, resolvedRequest ?? basePath, parent, isMain, options);
};

const [{ NAV_SECTIONS }, { arrangeNl2SqlPermissions }, { MENU_PERMISSIONS }, { t }] = await Promise.all([
  import("../src/components/layout/nav-config.ts"),
  import("../src/features/security/permission-nav.ts"),
  import("../src/features/security/menu-permissions.ts"),
  import("../src/lib/i18n.ts"),
]);
moduleResolver._resolveFilename = originalResolveFilename;

const backendSource = readFileSync(resolve(frontendRoot, "../backend/app/security/permissions.py"), "utf8");
// カタログの code。`_permission(` / `_menu_permission(` の最初の引数に文字列で書いたものと、
// capability の定数（`*_PERMISSION = "nl2sql.…"`）を合わせる。
const backendCodes = [
  ...new Set([
    ...[...backendSource.matchAll(/_(?:menu_)?permission\(\s*"((?:menu|nl2sql)\.[a-z0-9_.]+)"/gu)].map(
      (match) => match[1]
    ),
    ...[...backendSource.matchAll(/^[A-Z_]+_PERMISSION = "(nl2sql\.[a-z0-9_.]+)"$/gmu)].map(
      (match) => match[1]
    ),
  ]),
];

const scrambledCatalog = backendCodes
  .map((code) => ({
    code,
    group: code.startsWith("menu.") ? "backend のグループ" : "backend の capability",
    label: `backend の名前 ${code}`,
    description: `${code} の説明`,
    implies: [] as string[],
  }))
  .reverse();

type Permission = (typeof scrambledCatalog)[number];

function groupsOf(permissions: Permission[]): [string, string[]][] {
  const groups = new Map<string, string[]>();
  for (const permission of permissions) {
    groups.set(permission.group, [...(groups.get(permission.group) ?? []), permission.label]);
  }
  return [...groups.entries()];
}

// サイドナビに出る見出しと項目の名前（共通の Sidebar と同じ規則: sidebarLabelKey を優先）。
const sidebarGroups = NAV_SECTIONS.map((section) => [
  t(section.titleKey),
  section.items.map((item) => t(item.sidebarLabelKey ?? item.labelKey)),
]);

test("メニュー権限はすべてナビの項目で、backend のカタログにある", () => {
  const navPermissions = NAV_SECTIONS.flatMap((section) => section.items.map((item) => item.permission));
  assert.equal(new Set(navPermissions).size, navPermissions.length);
  assert.deepEqual([...navPermissions].sort(), Object.values(MENU_PERMISSIONS).sort());
  assert.deepEqual(
    backendCodes.filter((code) => code.startsWith("menu.")).sort(),
    [...navPermissions].sort()
  );
});

test("権限管理のメニュー権限は、サイドナビと同じグループ・並び順・名前になる", () => {
  const arranged = arrangeNl2SqlPermissions(scrambledCatalog);
  assert.deepEqual(
    groupsOf(arranged.filter((permission) => permission.code.startsWith("menu."))),
    sidebarGroups
  );
});

test("ナビに無い権限は capability だけで、ナビの後ろに置き、カタログの権限を落とさない", () => {
  const arranged = arrangeNl2SqlPermissions(scrambledCatalog);
  const firstCapability = arranged.findIndex((permission) => !permission.code.startsWith("menu."));
  const rest = arranged.slice(firstCapability);
  assert.equal(rest.length, backendCodes.filter((code) => code.startsWith("nl2sql.")).length);
  assert.ok(rest.every((permission) => permission.code.startsWith("nl2sql.")));
  assert.deepEqual(arranged.map((permission) => permission.code).sort(), [...backendCodes].sort());
});
