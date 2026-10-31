import { UploadStorageSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/leave-guard";

/** アップロード保存先設定。画面の実体は platform の共有パッケージ（#97。保存先はローカルだけ。#11）。 */
export function UploadStorageSettingsClient() {
  return (
    <UploadStorageSettingsPage
      api={{
        get: (options) => api.getUploadStorageSettings(options),
        update: (payload) => api.updateUploadStorageSettings(payload),
      }}
      draftGuardMessages={draftGuardMessages()}
      placeholders={{ localStorageDir: "/u01/data/production-ready-rag" }}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
