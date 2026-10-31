import type { BaseCurrentUser, HasPermission } from "./types";

/**
 * 権限の判定とルートの振り分け（#220）。
 * backend は `implies` を展開済みの `permissions` を返すため、既定の判定は「SYSTEM_ADMIN なら true、
 * それ以外は一覧に含まれるか」だけにする。旧コードの読み替えなど製品固有の判定は、
 * `AuthProvider` の `createPermissionCheck` で差し替える。
 */
export function createPermissionCheck(
  user: Pick<BaseCurrentUser, "is_system_admin" | "permissions"> | null | undefined,
): HasPermission {
  if (!user) return () => false;
  if (user.is_system_admin) return () => true;
  const granted = new Set(user.permissions);
  return (permission) => granted.has(permission);
}

/** `implies` の対応表で権限コードを推移的に展開する（元のコードも含む）。 */
export function expandPermissions(
  codes: Iterable<string>,
  implies: Readonly<Record<string, readonly string[]>>,
): Set<string> {
  const expanded = new Set<string>();
  const pending = [...codes];
  while (pending.length > 0) {
    const code = pending.pop();
    if (!code || expanded.has(code)) continue;
    expanded.add(code);
    pending.push(...(implies[code] ?? []));
  }
  return expanded;
}

export interface PermissionRouteItem {
  href: string;
  /** 画面を開くのに必要な権限コード。無ければ誰でも開ける画面として扱う。 */
  permission?: string;
}

/** ナビ項目から「URL → 必要な権限」の対応表を作る。 */
export function routePermissionMap(items: readonly PermissionRouteItem[]): Record<string, string> {
  return Object.fromEntries(
    items.flatMap((item) => (item.permission ? [[item.href, item.permission] as const] : [])),
  );
}

/** ナビの並び順で、最初に開ける画面を返す。どれも開けなければ fallback（例: 権限なしの画面）。 */
export function firstAllowedRoute(
  items: readonly PermissionRouteItem[],
  hasPermission: HasPermission,
  fallback: string,
): string {
  return (
    items.find((item) => Boolean(item.permission && hasPermission(item.permission)))?.href ??
    fallback
  );
}
