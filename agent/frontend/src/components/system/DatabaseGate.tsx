import type { ReactNode } from "react";
import {
  DATABASE_GATE_MESSAGES,
  DatabaseGate as SharedDatabaseGate,
  type DatabaseGateMessages,
  type DatabaseGateRoutes,
} from "@production-ready/system-settings";

import { useAuth } from "@/components/security/AuthProvider";
import { api } from "@/lib/api";
import { isI18nKey, t } from "@/lib/i18n";
import { canOpenRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";

/**
 * DB ゲートの導線。接続情報の確認と ADB の起動はデータベース設定（ADB が停止中などは共通の案内が
 * ADB 管理のカードを付ける）、システムテーブルの作成・更新は運用設定のシステムテーブル（状態 API が
 * `setup_required` を返す。RAG / NL2SQL と同じ。#751 / #820）。
 */
export const DATABASE_GATE_ROUTES: DatabaseGateRoutes = {
  databaseSettings: APP_ROUTES.settingsDatabase,
  systemTables: APP_ROUTES.settingsSystemTables,
};

/** 共通の DB ゲートの文言を、Agent の辞書にある値で上書きする（製品名の入る文言など）。 */
export function databaseGateMessages(): Partial<DatabaseGateMessages> {
  return Object.fromEntries(
    Object.keys(DATABASE_GATE_MESSAGES)
      .filter(isI18nKey)
      .map((key) => [key, t(key)])
  );
}

/**
 * 業務画面を開く前にデータベースの利用可否を確認する（3製品共通の部品。#325）。
 * ゲートを通さない画面は共通の既定（システム設定の 5 画面とシステムテーブル）。ローカル認証でも
 * ユーザー・ロールは共通 DB にあるため、DB を確かめる（#750 / #751）。
 */
export function DatabaseGate({ children }: { children: ReactNode }) {
  const { hasPermission } = useAuth();
  // 導線は開ける画面だけに出す。開けない利用者にはシステム管理者への連絡を案内する（#820）。
  return (
    <SharedDatabaseGate
      api={api}
      routes={DATABASE_GATE_ROUTES}
      messages={databaseGateMessages()}
      canManageDatabase={canOpenRoute(APP_ROUTES.settingsDatabase, hasPermission)}
      canManageSystemTables={canOpenRoute(APP_ROUTES.settingsSystemTables, hasPermission)}
    >
      {children}
    </SharedDatabaseGate>
  );
}
