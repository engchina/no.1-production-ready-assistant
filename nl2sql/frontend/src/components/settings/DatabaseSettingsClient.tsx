import { Skeleton, TimedLoadingState } from "@production-ready/ui";
import { DatabaseSettingsPage } from "@production-ready/system-settings";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/draft-guard-messages";
import { t } from "@/lib/i18n";
import { queryKeys } from "@/lib/queries";

/**
 * データベース設定。画面の実体は platform の共有パッケージ（#108）。
 * Select AI Credential は運用設定の専用の画面（`/settings/select-ai-credential`。#658）。
 */
export function DatabaseSettingsClient() {
  const queryClient = useQueryClient();
  return (
    <DatabaseSettingsPage
      api={api}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) =>
        error instanceof ApiError ? error.message : undefined
      }
      connectionSecurity
      onDatabaseChanged={() =>
        queryClient.invalidateQueries({ queryKey: queryKeys.databaseStatus })
      }
      loadingFallback={
        <TimedLoadingState
          label={t("settings.database.loading")}
          operationKey="settings-database-load"
          placement="page"
          testId="settings-database-loading"
        >
          <Skeleton className="h-20 w-full rounded-lg" />
          <Skeleton className="h-[33rem] w-full rounded-lg" />
        </TimedLoadingState>
      }
    />
  );
}
