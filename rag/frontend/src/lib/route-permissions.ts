import {
  firstAllowedRoute as sharedFirstAllowedRoute,
  routePermissionMap,
  type HasPermission,
} from "@production-ready/system-settings";

import { NAV_ITEMS, NAV_SECTIONS } from "@/components/layout/nav-config";
import { MENU_PERMISSIONS } from "./permissions";
import { APP_ROUTES } from "./routes";

/**
 * ルートの権限（#214）。ナビ項目の URL は項目の `permission`、ナビに無い詳細画面は開く導線の権限で守る。
 * 判定の実体は platform の共通関数（`routePermissionMap` / `firstAllowedRoute`）。
 */
export const ROUTE_PERMISSIONS: Readonly<Record<string, string>> = routePermissionMap(NAV_ITEMS);

/**
 * 文書の詳細は、文書ワークスペース（アップロード・文書インデックス）の権限で開ける。詳細の画面は
 * ワークスペース専用の API（backend の `_DOCUMENT_WORKSPACE`）を使うため、RAG 検索・チャット・
 * ナレッジベースだけの利用者には開かせず、そこから詳細へのリンクも出さない（#303）。
 * 引用カードのプレビュー（ドロワー）は backend の `_DOCUMENT_VIEW` の API だけで表示できる。
 */
const DOCUMENT_DETAIL_PERMISSIONS = [MENU_PERMISSIONS.upload, MENU_PERMISSIONS.fileList];

/** 文書の詳細を開けるか。検索の引用・KB の所属文書・フィードバックの根拠から詳細へのリンクを出す条件。 */
export function canOpenDocumentDetail(hasPermission: HasPermission): boolean {
  return DOCUMENT_DETAIL_PERMISSIONS.some((permission) => hasPermission(permission));
}

function isDetailPath(pathname: string, base: string): boolean {
  return pathname.startsWith(`${base}/`) && pathname.length > base.length + 1;
}

/**
 * URL を開くのに必要な権限（どれか 1 つを持てばよい）。undefined は権限を問わない URL
 * （`/` や未知の URL のように、既定の画面へ振り分けるだけのもの）。
 */
export function routeRequiredPermissions(pathname: string): readonly string[] | undefined {
  const exact = ROUTE_PERMISSIONS[pathname];
  if (exact) return [exact];
  if (isDetailPath(pathname, APP_ROUTES.documents)) return DOCUMENT_DETAIL_PERMISSIONS;
  if (isDetailPath(pathname, APP_ROUTES.knowledgeBases)) return [MENU_PERMISSIONS.knowledgeBases];
  if (isDetailPath(pathname, APP_ROUTES.history)) return [MENU_PERMISSIONS.history];
  return undefined;
}

/** 利用者が URL を開けるか。権限を問わない URL は true。 */
export function canOpenRoute(pathname: string, hasPermission: HasPermission): boolean {
  const required = routeRequiredPermissions(pathname);
  return !required || required.some((permission) => hasPermission(permission));
}

/** ナビの並び順で最初に開ける画面。どれも開けなければ権限なしの画面。 */
export function firstAllowedRoute(hasPermission: HasPermission): string {
  return sharedFirstAllowedRoute(NAV_ITEMS, hasPermission, APP_ROUTES.forbidden);
}

/**
 * ログイン後・未知の URL・廃止した旧 URL の移動先（NL2SQL と同じ方針。#261）。主画面のチャット
 * （ナビの先頭。#399 / #432）を開ければチャット、開けなければ `/` に戻し、`/` がナビの並び順で
 * 最初に開ける画面へ振り分ける。
 */
export function defaultEntryRoute(hasPermission: HasPermission): string {
  return hasPermission(ROUTE_PERMISSIONS[APP_ROUTES.chat]) ? APP_ROUTES.chat : APP_ROUTES.home;
}

/** `/settings` の移動先。共通のシステム設定のうち最初に開ける画面、無ければ既定の入口。 */
export function settingsEntryRoute(hasPermission: HasPermission): string {
  const settings = NAV_SECTIONS.find((section) => section.titleKey === "nav.section.settings");
  const route = sharedFirstAllowedRoute(settings?.items ?? [], hasPermission, "");
  return route || defaultEntryRoute(hasPermission);
}
