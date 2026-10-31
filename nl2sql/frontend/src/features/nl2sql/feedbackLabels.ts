import { t } from "@/lib/i18n";

import type { FeedbackRating } from "./types";

export function feedbackRatingLabel(rating?: FeedbackRating | null) {
  if (rating === "good") return t("nl2sql.feedback.good");
  if (rating === "bad") return t("nl2sql.feedback.bad");
  return t("nl2sql.feedback.unrated");
}

export function userFeedbackRatingBadgeLabel(rating?: FeedbackRating | null) {
  return t("nl2sql.feedback.userRatingBadge", { rating: feedbackRatingLabel(rating) });
}

export function adminFeedbackReviewBadgeLabel(rating?: FeedbackRating | null) {
  return t("nl2sql.feedback.adminReviewBadge", { rating: feedbackRatingLabel(rating) });
}
