import { TraceRecordingSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/draft-guard-messages";

/** 画面と設定の契約は platform が持つ。 */
export function TraceRecordingSettingsClient() {
  return (
    <TraceRecordingSettingsPage
      api={{
        get: (options) => api.getTraceRecordingSettings(options),
        update: (payload) => api.updateTraceRecordingSettings(payload),
      }}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
