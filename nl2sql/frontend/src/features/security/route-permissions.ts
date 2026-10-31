import {
  firstAllowedRoute as sharedFirstAllowedRoute,
  routePermissionMap,
} from "@production-ready/system-settings";

import { NAV_SECTIONS } from "@/components/layout/nav-config";
import { APP_ROUTES } from "@/lib/routes";

// ナビの並び順と各画面の権限から、ルートの判定を作る。判定の実体は platform の共通関数（#220）。
const NAV_ITEMS = NAV_SECTIONS.flatMap((section) => section.items);

export const ROUTE_PERMISSIONS: Record<string, string> = routePermissionMap(NAV_ITEMS);

/** 利用者がその画面を開けるか（ナビの権限が無い画面は開ける。`RequireAuth` の判定と同じ）。 */
export function canOpenRoute(pathname: string, hasPermission: (permission: string) => boolean): boolean {
  const permission = ROUTE_PERMISSIONS[pathname];
  return !permission || hasPermission(permission);
}

export function firstAllowedRoute(hasPermission: (permission: string) => boolean): string {
  return sharedFirstAllowedRoute(NAV_ITEMS, hasPermission, APP_ROUTES.forbidden);
}

/**
 * ログイン後などの既定入口。SQL 生成を利用できない場合は root に戻し、
 * root route が firstAllowedRoute で利用可能な画面へ振り分ける。
 */
export function defaultEntryRoute(hasPermission: (permission: string) => boolean): string {
  return hasPermission(ROUTE_PERMISSIONS[APP_ROUTES.query])
    ? APP_ROUTES.query
    : APP_ROUTES.home;
}
