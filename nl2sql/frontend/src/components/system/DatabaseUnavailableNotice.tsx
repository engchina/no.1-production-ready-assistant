import {
  DATABASE_GATE_MESSAGES,
  DatabaseUnavailableNotice as SharedDatabaseUnavailableNotice,
  type DatabaseGateMessages,
  type DatabaseGateRoutes,
  type DatabaseUnavailableNoticeProps,
} from "@production-ready/system-settings";

import { useAuth } from "@/features/security/AuthProvider";
import { canOpenRoute } from "@/features/security/route-permissions";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

/**
 * DB ゲートの導線（接続情報の確認と ADB の起動はデータベース設定。ADB が停止中などは共通の案内が
 * ADB 管理のカードを付ける。初期化はシステムテーブル。#820）。
 */
export const DATABASE_GATE_ROUTES: DatabaseGateRoutes = {
  databaseSettings: APP_ROUTES.settingsDatabase,
  systemTables: APP_ROUTES.settingsSystemTables,
};

/** 利用者が DB の復旧の画面（データベース設定・システムテーブル）を開けるか（#820）。 */
export function useDatabaseGatePermissions(): { canManageDatabase: boolean; canManageSystemTables: boolean } {
  const { hasPermission } = useAuth();
  return {
    canManageDatabase: canOpenRoute(APP_ROUTES.settingsDatabase, hasPermission),
    canManageSystemTables: canOpenRoute(APP_ROUTES.settingsSystemTables, hasPermission),
  };
}

/** 共通の DB ゲートの文言を、NL2SQL の辞書にある値で上書きする（製品名の入る文言など）。 */
export function databaseGateMessages(): Partial<DatabaseGateMessages> {
  return Object.fromEntries(
    Object.keys(DATABASE_GATE_MESSAGES).flatMap((key) => {
      const value = t(key);
      return value === key ? [] : [[key, value]];
    })
  );
}

/** NL2SQL の導線と文言を束ねた、共通の DB の案内（全画面 / banner。#325）。 */
export function DatabaseUnavailableNotice(
  props: Omit<DatabaseUnavailableNoticeProps, "routes" | "messages">
) {
  const permissions = useDatabaseGatePermissions();
  return (
    <SharedDatabaseUnavailableNotice
      routes={DATABASE_GATE_ROUTES}
      messages={databaseGateMessages()}
      {...permissions}
      {...props}
    />
  );
}
