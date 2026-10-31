import {
  DATABASE_STATUS_QUERY_KEY,
  DatabaseSettingsPage,
} from "@production-ready/system-settings";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/leave-guard";

/** データベース設定。画面の実体は platform の共有パッケージ（#108）。 */
export function DatabaseSettingsClient() {
  const queryClient = useQueryClient();
  return (
    <DatabaseSettingsPage
      api={api}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
      // 接続情報を変えたら DB ゲートの状態を確かめ直す（業務画面へ戻ったときに古い案内を出さない。#325）。
      onDatabaseChanged={async () => {
        await queryClient.invalidateQueries({ queryKey: DATABASE_STATUS_QUERY_KEY });
      }}
    />
  );
}
