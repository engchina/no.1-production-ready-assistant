import type { PermissionDefinition } from "./types";

/**
 * 権限管理の機能の一覧を、左のナビにそろえるための形（#567）。
 * 見出し（`title`）と項目の名前（`label`）は、ナビに表示している文言をそのまま使う。
 */
export interface PermissionNavSection {
  title: string;
  items: readonly { label: string; permission: string }[];
}

/** 製品の nav config のセクション（i18n の key と、画面を開くのに必要な権限）。 */
export interface NavConfigSectionLike<K extends string> {
  titleKey: K;
  items: readonly { labelKey: K; sidebarLabelKey?: K; permission: string }[];
}

/**
 * 製品の nav config から、権限管理の一覧の見出しと名前を作る。名前はサイドナビに表示する文言
 * （`sidebarLabelKey` があればそれ、なければ `labelKey`。共通の Sidebar と同じ規則）にする。
 */
export function permissionNavSections<K extends string>(
  sections: readonly NavConfigSectionLike<K>[],
  translate: (key: K) => string,
): PermissionNavSection[] {
  return sections.map((section) => ({
    title: translate(section.titleKey),
    items: section.items.map((item) => ({
      label: translate(item.sidebarLabelKey ?? item.labelKey),
      permission: item.permission,
    })),
  }));
}

/**
 * 権限カタログ（`GET /api/security/permissions`）を、左のナビのグループ・並び順・名前に並べ替える（#567）。
 *
 * - ナビにある権限は、ナビのセクションを `group`、ナビの項目の名前を `label` にして、ナビの順に並べる。
 * - ナビに無い権限（画面ではなく画面の中の操作を許可する capability など）は、ナビの後ろに
 *   backend のグループ・名前・順序のまま置く。
 * - ナビにあってもカタログに無い権限は出さない（ロールに付けられない）。権限の code・説明・implies は変えない。
 */
export function arrangePermissionsByNav(
  catalog: readonly PermissionDefinition[],
  sections: readonly PermissionNavSection[],
): PermissionDefinition[] {
  const byCode = new Map(catalog.map((permission) => [permission.code, permission]));
  const placed = new Set<string>();
  const arranged: PermissionDefinition[] = [];
  for (const section of sections) {
    for (const item of section.items) {
      const permission = byCode.get(item.permission);
      if (!permission || placed.has(permission.code)) continue;
      placed.add(permission.code);
      arranged.push({ ...permission, group: section.title, label: item.label });
    }
  }
  return [...arranged, ...catalog.filter((permission) => !placed.has(permission.code))];
}
