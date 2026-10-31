import { AnswerNoticeSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";
import { draftGuardMessages } from "@/lib/draft-guard-messages";
import { t } from "@/lib/i18n";

/**
 * NL2SQL の回答の注意書き（設定は製品ごと。#2449）。画面の実体は platform の共有パッケージ（#2405）で、
 * どの回答に出すかの説明は NL2SQL の i18n から渡す。
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
      }}
      draftGuardMessages={draftGuardMessages()}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
