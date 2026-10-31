import {
  SYSTEM_TABLES_MESSAGES,
  SystemTablesCard as SharedSystemTablesCard,
  type SystemTablesMessages,
} from "@production-ready/system-settings";
import { useConfirm } from "@production-ready/ui";

import { useAuth } from "@/components/security/AuthProvider";
import { DATABASE_GATE_ROUTES, databaseGateMessages } from "@/components/system/DatabaseGate";
import { api, ApiError } from "@/lib/api";
import { isI18nKey, t } from "@/lib/i18n";
import { MENU_PERMISSIONS } from "@/lib/permissions";

/** 全再作成の確認語（backend の `RECREATE_CONFIRMATION` と同じ）。 */
const RECREATE_AGENT_SYSTEM_TABLES_CONFIRMATION = "RECREATE_AGENT_SYSTEM_TABLES";

/** 共通のカードの文言を、Agent の辞書にある値で上書きする（製品名が入る文言など）。 */
function systemTablesMessages(): Partial<SystemTablesMessages> {
  return Object.fromEntries(
    Object.keys(SYSTEM_TABLES_MESSAGES)
      .filter(isI18nKey)
      .map((key) => [key, t(key)]),
  );
}

/**
 * Agent のシステムテーブルの状態と明示の DDL 操作（3 製品共通のカード。#325 / #751）。
 * 作成・更新と全再作成は「システムテーブル」のメニュー権限（NL2SQL と同じ）。状態の確認は画面を開ければできる。
 */
export function SystemTablesCard() {
  const canManage = useAuth().hasPermission(MENU_PERMISSIONS.settingsSystemTables);
  const confirm = useConfirm();
  return (
    <SharedSystemTablesCard
      api={api}
      canManage={canManage}
      recreateConfirmation={RECREATE_AGENT_SYSTEM_TABLES_CONFIRMATION}
      databaseRoutes={DATABASE_GATE_ROUTES}
      databaseMessages={databaseGateMessages()}
      messages={systemTablesMessages()}
      describeOperationError={(cause) => (cause instanceof ApiError ? cause.message : null)}
      // ロールに付けた Agent の権限を消すため、確認語に加えて確認ダイアログでも承認させる（RAG と同じ）。
      confirmRecreate={() =>
        confirm({
          title: t("settings.database.systemTables.confirm.title"),
          description: t("settings.database.systemTables.confirm.description"),
          confirmLabel: t("settings.database.systemTables.action.recreate"),
          tone: "danger",
          dismissOnOverlay: false,
        })
      }
      // データを消す未適用の migration（#619。旧版の検索・回答プロファイルの割り当ての表の削除など）は、
      // 削除される内容を示す確認ダイアログで承認させてから作成・更新する。
      confirmDestructiveMigrations={(request) => confirm({ ...request, tone: "danger", dismissOnOverlay: false })}
    />
  );
}
