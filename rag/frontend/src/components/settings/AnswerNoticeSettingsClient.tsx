import { AnswerNoticeSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";
import { t } from "@/lib/i18n";
import { draftGuardMessages } from "@/lib/leave-guard";

/**
 * RAG の回答の注意書き（設定は製品ごと。#2449）。画面の実体は platform の共有パッケージ（#2405）で、
 * どの回答に出すかの説明は RAG の i18n から渡す。
 */
export function AnswerNoticeSettingsClient() {
  return (
    <AnswerNoticeSettingsPage
      api={{
        get: (options) => api.getAnswerNoticeSettings(options),
        update: (payload) => api.updateAnswerNoticeSettings(payload),
      }}
      messages={{
        settingsDescription: t("settings.answerNotice.description"),
        helperContact: t("settings.answerNotice.helperContact"),
      }}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
