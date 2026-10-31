import { apiErrorMessage, FeedbackControls, toast } from "@production-ready/ui";

import {
  agentApi,
  FEEDBACK_REASONS,
  type FeedbackReason,
  type RunFeedback,
  type RunFeedbackPayload,
  type RunState,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

export const FEEDBACK_COMMENT_MAX_CHARS = 1000;

/**
 * 回答への評価（#774）。見た目と操作は共有の `FeedbackControls`（#805。RAG の回答の評価と同じ部品）で、
 * ここは Run の評価の API と文言をつなぐだけ。
 * 👍 はすぐ保存、👎 は理由（必須）とコメント（任意）をその場で開いて保存する。付け直すと上書きする。
 *
 * - `owner`: 会話をした本人の評価（チャットの回答の下）。
 * - `admin`: 管理者の評価（Agent 管理の権限。フィードバック・Run の詳細。本人の評価とは別に残す）。
 */
export function AnswerFeedback({
  runId,
  current,
  mode = "owner",
  onSaved,
}: {
  runId: string;
  current: RunFeedback | null;
  mode?: "owner" | "admin";
  onSaved: (run: RunState) => void;
}) {
  const admin = mode === "admin";
  const prefix = admin ? "admin-review" : "chat-feedback";

  async function save(payload: RunFeedbackPayload) {
    const updated = admin
      ? await agentApi.putRunAdminReview(runId, payload)
      : await agentApi.putRunFeedback(runId, payload);
    onSaved(updated);
    toast.success(t("chat.feedback.saved"));
  }

  return (
    <FeedbackControls<FeedbackReason>
      data-testid={`${prefix}-${runId}`}
      commentId={`${prefix}-comment-${runId}`}
      value={current}
      reasons={FEEDBACK_REASONS.map((reason) => ({
        value: reason,
        label: t(`feedback.reason.${reason}` as I18nKey),
      }))}
      commentMaxLength={FEEDBACK_COMMENT_MAX_CHARS}
      labels={{
        question: admin ? t("chat.feedback.adminQuestion") : t("chat.feedback.question"),
        helpful: t("chat.feedback.helpful"),
        notHelpful: t("chat.feedback.notHelpful"),
        savedInline: t("chat.feedback.savedInline"),
        reasonLegend: t("chat.feedback.reasonLegend"),
        commentLabel: t("chat.feedback.commentLabel"),
        commentPlaceholder: t("chat.feedback.commentPlaceholder"),
        commentCount: (count, max) => t("chat.feedback.commentCount", { count, max }),
        save: t("chat.feedback.save"),
        cancel: t("common.cancel"),
        retry: t("common.retry"),
        saveError: t("chat.feedback.saveFailed"),
      }}
      getErrorMessage={(error) => apiErrorMessage(error, t("chat.feedback.saveFailed"))}
      onSubmit={(submission) =>
        save(
          submission.rating === "helpful"
            ? { rating: "helpful" }
            : {
                rating: "not_helpful",
                reason: submission.reason ?? undefined,
                comment: submission.comment ?? "",
              }
        )
      }
    />
  );
}
