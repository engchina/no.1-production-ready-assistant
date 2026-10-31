import {
  SYSTEM_TABLES_MESSAGES,
  SystemTablesCard as SharedSystemTablesCard,
  type SystemTablesMessages,
} from "@production-ready/system-settings";

import { DATABASE_GATE_ROUTES, databaseGateMessages } from "@/components/system/DatabaseUnavailableNotice";
import { DbObjectName } from "@/features/nl2sql/components/DbObjectName";
import { useAuth } from "@/features/security/AuthProvider";
import { MENU_PERMISSIONS } from "@/features/security/menu-permissions";
import { api, ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { queryKeys } from "@/lib/queries";

/** 全再作成の確認語（backend の `RECREATE_CONFIRMATION` と同じ）。 */
const RECREATE_CONFIRMATION = "RECREATE_NL2SQL_SYSTEM_TABLES";

/** 作成・更新の後に捨てる NL2SQL の cache（保存領域・スキーマ・NL2SQL の業務データ）。 */
const INVALIDATE_ON_SUCCESS = [
  queryKeys.persistenceStatus,
  queryKeys.schemaOwners,
  ["schema"],
  ["nl2sql"],
] as const;

/** 共通のカードの文言を、NL2SQL の辞書にある値で上書きする（製品名が入る文言など）。 */
function systemTablesMessages(): Partial<SystemTablesMessages> {
  return Object.fromEntries(
    Object.keys(SYSTEM_TABLES_MESSAGES).flatMap((key) => {
      const value = t(key);
      return value === key ? [] : [[key, value]];
    })
  );
}

/** NL2SQL のシステムテーブルの状態と明示の DDL 操作（3 製品共通のカード。#325）。 */
export function SystemTablesCard() {
  const { hasPermission } = useAuth();
  return (
    <SharedSystemTablesCard
      api={api}
      canManage={hasPermission(MENU_PERMISSIONS.settingsSystemTables)}
      recreateConfirmation={RECREATE_CONFIRMATION}
      databaseRoutes={DATABASE_GATE_ROUTES}
      databaseMessages={databaseGateMessages()}
      messages={systemTablesMessages()}
      invalidateQueryKeys={INVALIDATE_ON_SUCCESS}
      describeOperationError={(cause) => (cause instanceof ApiError ? cause.message : null)}
      renderObjectName={(object) => <DbObjectName object={object} size="xs" />}
    />
  );
}
