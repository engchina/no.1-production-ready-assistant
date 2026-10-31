import type { DraftGuardMessages } from "@production-ready/system-settings";

import { t } from "@/lib/i18n";

/**
 * 共通のシステム設定の画面（モデル・データベース・アップロード保存先）に渡す離脱の確認の文言。
 * 文言は製品の i18n で持つ（RAG / Agent と同じ。#1118）。
 */
export function draftGuardMessages(): DraftGuardMessages {
  return {
    discardTitle: t("settings.leaveGuard.title"),
    discardDescription: t("settings.leaveGuard.description"),
    discardConfirm: t("settings.leaveGuard.confirm"),
  };
}
