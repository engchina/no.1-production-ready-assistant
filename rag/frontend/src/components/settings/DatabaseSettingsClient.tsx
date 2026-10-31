import { DatabaseSettingsPage } from "@production-ready/system-settings";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/leave-guard";
import { queryKeys } from "@/lib/queries";

/**
 * データベース設定。画面の実体は platform の共有パッケージ（#108）。
 * システムテーブルは運用設定の専用の画面（`/settings/system-tables`。#658）。
 */
export function DatabaseSettingsClient() {
  const queryClient = useQueryClient();
  return (
    <DatabaseSettingsPage
      api={api}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
      onDatabaseChanged={async () => {
        await queryClient.invalidateQueries({ queryKey: queryKeys.databaseStatus });
      }}
    />
  );
}
