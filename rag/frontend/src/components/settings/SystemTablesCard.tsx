import {
  SYSTEM_TABLES_MESSAGES,
  SystemTablesCard as SharedSystemTablesCard,
  type SystemTablesMessages,
} from "@production-ready/system-settings";

import { useAuth } from "@/components/security/AuthProvider";
import { DATABASE_GATE_ROUTES, databaseGateMessages } from "@/components/system/DatabaseGate";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api, ApiError } from "@/lib/api";
import { ja, t, type I18nKey } from "@/lib/i18n";
import { CAPABILITY_PERMISSIONS } from "@/lib/permissions";

/** 全再作成の確認語（backend の `RECREATE_CONFIRMATION` と同じ）。 */
const RECREATE_RAG_SYSTEM_TABLES_CONFIRMATION = "RECREATE_RAG_SYSTEM_TABLES";

/** 共通のカードの文言を、RAG の辞書にある値で上書きする（製品名が入る文言など）。 */
function systemTablesMessages(): Partial<SystemTablesMessages> {
  return Object.fromEntries(
    Object.keys(SYSTEM_TABLES_MESSAGES)
      .filter((key): key is I18nKey => key in ja)
      .map((key) => [key, t(key)])
  );
}

/** RAG のシステムテーブルの状態と明示の DDL 操作（3 製品共通のカード。#325）。 */
export function SystemTablesCard() {
  // 初期化・全再作成はシステムテーブル管理の権限がある利用者だけ。状態の確認は誰でもできる（#214）。
  const canManage = useAuth().hasPermission(CAPABILITY_PERMISSIONS.systemTablesManage);
  const confirm = useConfirm();
  return (
    <SharedSystemTablesCard
      api={api}
      canManage={canManage}
      recreateConfirmation={RECREATE_RAG_SYSTEM_TABLES_CONFIRMATION}
      databaseRoutes={DATABASE_GATE_ROUTES}
      databaseMessages={databaseGateMessages()}
      messages={systemTablesMessages()}
      describeOperationError={(cause) => (cause instanceof ApiError ? cause.message : null)}
      // RAG は文書・chunk・会話など業務データを消すため、確認語に加えて確認ダイアログでも承認させる。
      confirmRecreate={() =>
        confirm({
          title: t("settings.database.systemTables.confirm.title"),
          description: t("settings.database.systemTables.confirm.description"),
          confirmLabel: t("settings.database.systemTables.action.recreate"),
          tone: "danger",
          dismissOnOverlay: false,
        })
      }
      // 参照先のない行の削除（#511）は取り消せないため、表・外部キー・件数を示す確認ダイアログで承認させる。
      confirmDeleteOrphans={(request) =>
        confirm({ ...request, tone: "danger", dismissOnOverlay: false })
      }
      // データを消す未適用の migration（#619）は、削除される内容を示す確認ダイアログで承認させてから
      // 作成・更新する（承認したときだけ allow_destructive を送る）。
      confirmDestructiveMigrations={(request) =>
        confirm({ ...request, tone: "danger", dismissOnOverlay: false })
      }
    />
  );
}
