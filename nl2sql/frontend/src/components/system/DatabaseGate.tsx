import { useEffect, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Banner, PageBody } from "@production-ready/ui";
import {
  DatabaseGate as SharedDatabaseGate,
  type DatabaseSecondaryGateProps,
} from "@production-ready/system-settings";

import {
  DATABASE_GATE_ROUTES,
  databaseGateMessages,
  useDatabaseGatePermissions,
} from "@/components/system/DatabaseUnavailableNotice";
import {
  DATABASE_UNAVAILABLE_EVENT,
  supersedeDatabaseUnavailableProbe,
  type DatabaseOperationalFailure,
} from "@/lib/database-load-error";
import { api } from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  clearDatabaseContextQueries,
  queryKeys,
  usePersistenceStatus,
  useRecoverPersistence,
} from "@/lib/queries";

/**
 * DB と保存済みの業務データ（保存領域）の両方が使えるまで業務ページを描画しない。
 * DB の確認・全画面の案内・ゲートを通さない画面は3製品共通のゲート（#325）、
 * 保存領域の確認と自動復旧は NL2SQL 固有なので secondaryGate で差し込む。
 */
export function DatabaseGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  // 導線は開ける画面だけに出す。開けない利用者にはシステム管理者への連絡を案内する（#820）。
  const permissions = useDatabaseGatePermissions();
  return (
    <SharedDatabaseGate
      api={api}
      routes={DATABASE_GATE_ROUTES}
      messages={databaseGateMessages()}
      {...permissions}
      onContextChange={() => clearDatabaseContextQueries(queryClient)}
      onBeforeRetry={supersedeDatabaseUnavailableProbe}
      secondaryGate={PersistenceGate}
    >
      {children}
    </SharedDatabaseGate>
  );
}

/** 保存領域（persisted snapshot / incremental store）の確認と自動復旧。 */
function PersistenceGate({
  children,
  renderNotice,
  renderChecking,
}: DatabaseSecondaryGateProps) {
  const queryClient = useQueryClient();
  const persistence = usePersistenceStatus();
  const recover = useRecoverPersistence();

  // 業務 API の失敗から保存領域の不通を確かめた通知で、今の状態を置き換える。
  useEffect(() => {
    const handle = (event: Event) => {
      const failure = (event as CustomEvent<DatabaseOperationalFailure>).detail;
      if (failure?.kind !== "persistence") return;
      queryClient.setQueryData(queryKeys.persistenceStatus, (current: unknown) => ({
        ...(typeof current === "object" && current !== null ? current : {}),
        ...failure.persistence,
      }));
    };
    window.addEventListener(DATABASE_UNAVAILABLE_EVENT, handle);
    return () => window.removeEventListener(DATABASE_UNAVAILABLE_EVENT, handle);
  }, [queryClient]);

  useEffect(() => {
    if (!persistence.data || persistence.data.ready || recover.isPending || recover.isError) {
      return;
    }
    recover.mutate();
  }, [persistence.data, recover]);

  const retry = async () => {
    supersedeDatabaseUnavailableProbe();
    recover.reset();
    const result = await persistence.refetch();
    if (result.data?.ready) return;
    try {
      await recover.mutateAsync();
    } catch {
      return;
    }
    await persistence.refetch();
  };

  if (persistence.isPending || recover.isPending) {
    return renderChecking(t("dbGate.recovering"), "dbGate.recovering");
  }
  if (persistence.isError || recover.isError || !persistence.data?.ready) {
    return renderNotice({
      title: t("dbGate.persistenceFailed.title"),
      message: t("dbGate.persistenceFailed.message"),
      reasonCode: persistence.data?.reason_code,
      onRetry: () => void retry(),
      isRetrying: persistence.isFetching || recover.isPending,
    });
  }

  return (
    <>
      {persistence.data.mode === "memory" ? (
        // 全画面が wide のため、バナーも wide にして直下の PageHeader と左端をそろえる。
        <PageBody wide className="pb-0">
          <Banner severity="warning" title={t("persistence.memoryWarning.title")}>
            {t("persistence.memoryWarning.message")}
          </Banner>
        </PageBody>
      ) : null}
      {children}
    </>
  );
}
