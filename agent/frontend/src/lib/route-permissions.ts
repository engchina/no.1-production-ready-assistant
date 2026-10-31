import {
  firstAllowedRoute as sharedFirstAllowedRoute,
  routePermissionMap,
  type HasPermission,
} from "@production-ready/system-settings";

import { NAV_ITEMS } from "@/components/layout/nav-config";
import { CAPABILITY_PERMISSIONS } from "./permissions";
import { APP_ROUTES } from "./routes";

/**
 * ルートの権限（#215）。ナビ項目の URL は項目の `permission`、ナビに出さない画面は
 * backend の manifest と同じ権限で守る（どれか 1 つを持てばよい）。
 * 判定の実体は platform の共通関数（`routePermissionMap` / `firstAllowedRoute`）。
 */
export const ROUTE_PERMISSIONS: Readonly<Record<string, string>> = routePermissionMap(NAV_ITEMS);

/** ナビに出さない画面（URL を直接開く管理者向けの画面）。 */
const HIDDEN_ROUTE_PERMISSIONS: Readonly<Record<string, readonly string[]>> = {
  // ツール一覧・ツール権限はナビに出さない管理者だけの画面。
  [APP_ROUTES.tools]: [CAPABILITY_PERMISSIONS.admin],
  [APP_ROUTES.settingsToolPolicy]: [CAPABILITY_PERMISSIONS.admin],
};

/**
 * URL を開くのに必要な権限（どれか 1 つを持てばよい）。undefined は権限を問わない URL
 * （未知の URL のように、既定の画面へ振り分けるだけのもの）。
 */
export function routeRequiredPermissions(pathname: string): readonly string[] | undefined {
  const exact = ROUTE_PERMISSIONS[pathname];
  if (exact) return [exact];
  return HIDDEN_ROUTE_PERMISSIONS[pathname];
}

/** 利用者が URL を開けるか。権限を問わない URL は true。 */
export function canOpenRoute(pathname: string, hasPermission: HasPermission): boolean {
  const required = routeRequiredPermissions(pathname);
  return !required || required.some((permission) => hasPermission(permission));
}

/**
 * `/` の移動先。ナビの並び順で最初に開ける画面。どれも開けなければ権限なしの画面（NL2SQL と同じ。#262）。
 */
export function firstAllowedRoute(hasPermission: HasPermission): string {
  return sharedFirstAllowedRoute(NAV_ITEMS, hasPermission, APP_ROUTES.forbidden);
}

/**
 * ログイン後・未知の URL・権限なしの画面から戻るときの既定入口。ナビの先頭のチャットを開ければチャット
 * （RAG と同じ。#791）、開けなければ `/` に戻し、`/` のルートが `firstAllowedRoute` で開ける画面へ
 * 振り分ける（NL2SQL と同じ形。#262）。
 */
export function defaultEntryRoute(hasPermission: HasPermission): string {
  return hasPermission(ROUTE_PERMISSIONS[APP_ROUTES.chat]) ? APP_ROUTES.chat : APP_ROUTES.home;
}
