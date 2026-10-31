import { ChatAttachmentSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";
import { t } from "@/lib/i18n";
import { draftGuardMessages } from "@/lib/leave-guard";

/**
 * Agent のチャットの添付の設定（設定は製品ごと。#2517）。画面の実体は platform の共有パッケージで、
 * どのチャットに効くかの説明は Agent の i18n から渡す。
 */
export function ChatAttachmentSettingsClient() {
  return (
    <ChatAttachmentSettingsPage
      api={{
        get: (options) => api.getChatAttachmentSettings(options),
        update: (payload) => api.updateChatAttachmentSettings(payload),
      }}
      messages={{ settingsDescription: t("settings.chatAttachments.description") }}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
