import { useMemo } from "react";
import { useChatComposerAttachments, type ChatComposerAttachmentTexts } from "@production-ready/ui";

import { ApiError, agentApi } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

/** 添付の警告の code を文言にする（backend の `pr_system_settings.chat_attachments.parsing`）。 */
function warningLabel(code: string): string | undefined {
  const key = `chat.attachments.warning.${code}` as I18nKey;
  const label = t(key);
  return label === key || !label ? undefined : label;
}

/**
 * チャットの入力欄の添付（#2517）。仕組みは共通の `useChatComposerAttachments`、ここは API と文言だけ。
 * 製品の「運用設定 › チャットの添付」で無効なら `composer` は undefined（入力欄に添付の操作を出さない）。
 */
export function useAgentChatAttachments() {
  const texts = useMemo<ChatComposerAttachmentTexts>(
    () => ({
      add: t("chat.attachments.add"),
      drop: t("chat.attachments.drop"),
      list: t("chat.attachments.list"),
      remove: (name) => t("chat.attachments.remove", { name }),
      uploading: t("chat.attachments.uploading"),
      hint: ({ size, count, hours }) => t("chat.attachments.hint", { size, count, hours }),
      tooLarge: (size) => t("chat.attachments.tooLarge", { size }),
      tooMany: (count) => t("chat.attachments.tooMany", { count }),
      unsupported: t("chat.attachments.unsupported"),
      uploadFailed: (error) => (error instanceof ApiError ? error.message : t("chat.attachments.failed")),
      warning: warningLabel,
    }),
    []
  );
  return useChatComposerAttachments(
    {
      list: () => agentApi.listChatAttachments(),
      upload: (file) => agentApi.uploadChatAttachment(file),
      remove: (attachmentId) => agentApi.deleteChatAttachment(attachmentId),
    },
    texts
  );
}
