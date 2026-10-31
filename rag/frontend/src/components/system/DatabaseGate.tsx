import type { ReactNode } from "react";
import {
  DATABASE_GATE_MESSAGES,
  DatabaseGate as SharedDatabaseGate,
  isDatabaseGateExemptPath,
  type DatabaseGateMessages,
  type DatabaseGateRoutes,
} from "@production-ready/system-settings";

import { useAuth } from "@/components/security/AuthProvider";
import { api } from "@/lib/api";
import { ja, t, type I18nKey } from "@/lib/i18n";
import { canOpenRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";

/**
 * DB ゲートの導線。接続情報の確認と ADB の起動はデータベース設定（ADB が停止中などは共通の案内が
 * ADB 管理のカードを付ける）、システムテーブルの作成・更新は運用設定のシステムテーブル（NL2SQL と
 * 同じ。#658 / #820）。システムテーブルの画面は未初期化でも開ける。
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

/**
 * ゲートを通さない画面（3製品共通。#325）。システム設定の 5 画面だけで、RAG 固有の設定
 * （取込・検索・回答の設定など）やユーザー・ロール・権限管理は DB を使うのでゲートを通す。
 */
export const isDatabaseGateExempt = isDatabaseGateExemptPath;

/** 共通の DB ゲートの文言を、RAG の辞書にある値で上書きする（製品名の入る文言など）。 */
export function databaseGateMessages(): Partial<DatabaseGateMessages> {
  return Object.fromEntries(
    Object.keys(DATABASE_GATE_MESSAGES)
      .filter((key): key is I18nKey => key in ja)
      .map((key) => [key, t(key)])
  );
}

/** 設定ページ以外を開く前にデータベースの利用可否を確認する（3製品共通の部品。#325）。 */
export function DatabaseGate({ children }: { children: ReactNode }) {
  // 導線は開ける画面だけに出す。開けない利用者にはシステム管理者への連絡を案内する（#820）。
  const permissions = useDatabaseGatePermissions();
  return (
    <SharedDatabaseGate
      api={api}
      routes={DATABASE_GATE_ROUTES}
      messages={databaseGateMessages()}
      {...permissions}
    >
      {children}
    </SharedDatabaseGate>
  );
}
