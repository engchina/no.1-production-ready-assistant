import { FeedbackControls as SharedFeedbackControls } from "@production-ready/ui";
import {
  ApiError,
  type CitationFeedbackReason,
  type FeedbackContentSnapshot,
  type FeedbackRequestBody,
  type FeedbackSourceSurface,
  type FeedbackTargetType,
  type RetrievedChunk,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { useCurrentFeedback, useSubmitFeedback } from "@/lib/queries";
import { toast } from "@/lib/toast";
import {
  FEEDBACK_ANSWER_REASONS,
  FEEDBACK_CITATION_REASONS,
  FEEDBACK_REASON_LABEL_KEYS,
} from "./FeedbackClient.logic";


interface FeedbackControlsProps {
  traceId: string | null | undefined;
  searchAnswerProfileId: string | null | undefined;
  targetType: FeedbackTargetType;
  sourceSurface: FeedbackSourceSurface;
  documentId?: string | null;
  chunkId?: string | null;
  messageId?: string | null;
  contentSnapshot?: FeedbackContentSnapshot | null;
  compact?: boolean;
}

/**
 * 回答・引用への評価。見た目と操作は共有の `FeedbackControls`（#805）で、ここは RAG の API（trace・検索・回答プロファイル・
 * 回答 / 引用・回答の記録）と文言をつなぐだけ。
 */
export function FeedbackControls({
  traceId,
  searchAnswerProfileId,
  targetType,
  sourceSurface,
  documentId = null,
  chunkId = null,
  messageId = null,
  contentSnapshot = null,
  compact = false,
}: FeedbackControlsProps) {
  const currentQuery = useCurrentFeedback(traceId ?? null);
  const mutation = useSubmitFeedback();
  const current = currentQuery.data?.find(
    (item) =>
      item.target_type === targetType &&
      (item.document_id ?? null) === documentId &&
      (item.chunk_id ?? null) === chunkId
  );
  const answer = targetType === "answer";
  const reasons = answer ? FEEDBACK_ANSWER_REASONS : FEEDBACK_CITATION_REASONS;

  if (!traceId || !searchAnswerProfileId) return null;

  return (
    <SharedFeedbackControls<CitationFeedbackReason>
      className={compact ? undefined : "mt-4"}
      compact={compact}
      disabled={currentQuery.isLoading}
      value={
        current
          ? {
              rating: current.rating,
              reason: current.reason ?? null,
              comment: current.comment ?? null,
              correctedAnswer: current.corrected_answer ?? null,
            }
          : null
      }
      reasons={reasons.map((reason) => ({ value: reason, label: t(FEEDBACK_REASON_LABEL_KEYS[reason]) }))}
      correctedAnswer={answer}
      commentId={`feedback-comment-${targetType}-${chunkId ?? "answer"}`}
      correctedAnswerId={`feedback-corrected-${chunkId ?? "answer"}`}
      labels={{
        question: answer ? t("feedback.controls.answerQuestion") : t("feedback.controls.citationQuestion"),
        helpful: answer ? t("feedback.controls.answerHelpful") : t("search.citation.feedback.helpful"),
        notHelpful: answer
          ? t("feedback.controls.answerNotHelpful")
          : t("search.citation.feedback.notHelpful"),
        savedInline: t("feedback.controls.savedInline"),
        reasonLegend: t("feedback.controls.reasonLegend"),
        commentLabel: t("feedback.controls.commentLabel"),
        commentPlaceholder: t("feedback.controls.commentPlaceholder"),
        commentCount: (count) => t("feedback.controls.commentCount", { count }),
        correctedAnswerLabel: t("feedback.controls.correctedAnswerLabel"),
        correctedAnswerPlaceholder: t("feedback.controls.correctedAnswerPlaceholder"),
        correctedAnswerHelp: t("feedback.controls.correctedAnswerHelp"),
        save: t("feedback.controls.save"),
        cancel: t("common.cancel"),
        retry: t("common.retry"),
        saveError: t("feedback.controls.saveError"),
      }}
      getErrorMessage={(error) => (error instanceof ApiError ? error.message : null)}
      onSubmit={async (submission) => {
        await mutation.mutateAsync(
          buildFeedbackPayload({
            trace_id: traceId,
            search_answer_profile_id: searchAnswerProfileId,
            target_type: targetType,
            source_surface: sourceSurface,
            document_id: answer ? null : documentId,
            chunk_id: answer ? null : chunkId,
            message_id: messageId,
            content_snapshot: feedbackSnapshotFor(targetType, messageId, contentSnapshot),
            rating: submission.rating,
            reason: submission.reason,
            comment: submission.rating === "not_helpful" ? submission.comment : null,
            corrected_answer:
              submission.rating === "not_helpful" && answer ? submission.correctedAnswer : null,
          })
        );
        toast.success(t("feedback.controls.savedToast"));
      }}
    />
  );
}

/**
 * 利用者が見た質問・回答・根拠の snapshot。回答を生成しない検索（検索結果だけ）でも、引用の評価が
 * どの質問への評価か分かるよう、質問があれば作る（回答は null。#978）。質問が無ければ null。
 */
export function buildFeedbackContentSnapshot(
  question: string,
  answer: string,
  citations: RetrievedChunk[]
): FeedbackContentSnapshot | null {
  const normalizedQuestion = question.trim();
  if (!normalizedQuestion) return null;
  return {
    question: normalizedQuestion,
    answer: answer.trim() || null,
    citations: citations.slice(0, 50).map((chunk) => ({
      document_id: chunk.document_id,
      chunk_id: chunk.chunk_id,
      file_name: chunk.file_name,
      section_title: metadataString(chunk.metadata.section_title),
      page_number: metadataInteger(chunk.metadata.page_number ?? chunk.metadata.page),
      content_preview: chunk.text.slice(0, 2000),
      rerank_score: chunk.rerank_score,
    })),
  };
}

/**
 * 送る snapshot。チャット（message id）はサーバーの記録を使うので送らない。回答の評価は回答の本文が
 * 必須（backend が 422 にする）なので、回答の無い snapshot は送らない。
 */
export function feedbackSnapshotFor(
  targetType: FeedbackTargetType,
  messageId: string | null,
  snapshot: FeedbackContentSnapshot | null
): FeedbackContentSnapshot | null {
  if (messageId || !snapshot) return null;
  if (targetType === "answer" && !snapshot.answer) return null;
  return snapshot;
}

export function buildFeedbackPayload(payload: FeedbackRequestBody): FeedbackRequestBody {
  if (payload.rating === "helpful") {
    return { ...payload, reason: null, comment: null, corrected_answer: null };
  }
  return {
    ...payload,
    comment: payload.comment?.trim() || null,
    corrected_answer: payload.corrected_answer?.trim() || null,
  };
}

function metadataString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim().slice(0, 1000) : null;
}

function metadataInteger(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : null;
}
