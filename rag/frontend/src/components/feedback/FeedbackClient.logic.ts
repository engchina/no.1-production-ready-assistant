import { DEFAULT_PAGE_SIZE, offsetForPage, PAGE_SIZE_OPTIONS } from "@production-ready/ui";

import type {
  CitationFeedbackRating,
  CitationFeedbackReason,
  FeedbackListParams,
  FeedbackTargetType,
} from "@/lib/api";
import type { I18nKey } from "@/lib/i18n";

/** 表示件数は 3 製品の一覧と同じ段（10 / 25 / 50 / 100、既定 25（#1952）。#1891 で独自の 25 / 50 / 100 をやめた）。 */
export const FEEDBACK_PAGE_SIZES = PAGE_SIZE_OPTIONS;
export const FEEDBACK_PERIODS = [7, 30, 90, null] as const;

export interface FeedbackUrlState {
  periodDays: number | null;
  searchAnswerProfileId: string;
  targetType: FeedbackTargetType | "";
  rating: CitationFeedbackRating | "";
  reason: CitationFeedbackReason | "";
  q: string;
  sortOrder: "newest" | "oldest";
  pageSize: (typeof FEEDBACK_PAGE_SIZES)[number];
  page: number;
  feedbackId: string;
}

const TARGETS = new Set<FeedbackTargetType>(["answer", "citation"]);
const RATINGS = new Set<CitationFeedbackRating>(["helpful", "not_helpful"]);
/** 理由の表示順（絞り込み・入力・表示で共通）。回答だけの理由は rag_poc の分類を含む。 */
export const FEEDBACK_ANSWER_REASONS: CitationFeedbackReason[] = [
  "incorrect",
  "incomplete",
  "missing_knowledge",
  "outdated_source",
  "ambiguous_question",
  "not_relevant",
  "answer_untrusted",
];
export const FEEDBACK_CITATION_REASONS: CitationFeedbackReason[] = [
  "missing_evidence",
  "not_relevant",
  "answer_untrusted",
];
export const FEEDBACK_REASONS: CitationFeedbackReason[] = [
  ...new Set([...FEEDBACK_ANSWER_REASONS, ...FEEDBACK_CITATION_REASONS]),
];
export const FEEDBACK_REASON_LABEL_KEYS: Record<CitationFeedbackReason, I18nKey> = {
  incorrect: "feedback.reason.incorrect",
  incomplete: "feedback.reason.incomplete",
  missing_evidence: "feedback.reason.missing_evidence",
  not_relevant: "feedback.reason.not_relevant",
  answer_untrusted: "feedback.reason.answer_untrusted",
  missing_knowledge: "feedback.reason.missing_knowledge",
  outdated_source: "feedback.reason.outdated_source",
  ambiguous_question: "feedback.reason.ambiguous_question",
};
const REASONS = new Set<CitationFeedbackReason>(FEEDBACK_REASONS);

export function parseFeedbackUrl(params: URLSearchParams): FeedbackUrlState {
  const period = params.get("period");
  const periodDays = period === "all" ? null : validNumber(period, [7, 30, 90], 30);
  const pageSize = validNumber(
    params.get("size"),
    [...FEEDBACK_PAGE_SIZES],
    DEFAULT_PAGE_SIZE
  ) as FeedbackUrlState["pageSize"];
  const target = params.get("target") ?? "";
  const rating = params.get("rating") ?? "";
  const reason = params.get("reason") ?? "";
  return {
    periodDays,
    searchAnswerProfileId: params.get("search_answer_profile") ?? "",
    targetType: TARGETS.has(target as FeedbackTargetType) ? (target as FeedbackTargetType) : "",
    rating: RATINGS.has(rating as CitationFeedbackRating)
      ? (rating as CitationFeedbackRating)
      : "",
    reason: REASONS.has(reason as CitationFeedbackReason)
      ? (reason as CitationFeedbackReason)
      : "",
    q: (params.get("q") ?? "").slice(0, 200),
    sortOrder: params.get("sort") === "oldest" ? "oldest" : "newest",
    pageSize,
    page: Math.max(1, integer(params.get("page"), 1)),
    feedbackId: params.get("feedback") ?? "",
  };
}

export function feedbackListParams(state: FeedbackUrlState): FeedbackListParams {
  return {
    search_answer_profile_id: state.searchAnswerProfileId || undefined,
    target_type: state.targetType || undefined,
    rating: state.rating || undefined,
    reason: state.reason || undefined,
    period_days: state.periodDays,
    q: state.q || undefined,
    sort_order: state.sortOrder,
    limit: state.pageSize,
    offset: offsetForPage(state.page, state.pageSize),
  };
}

/** 期間・検索・回答プロファイル・対象・評価・理由・検索語のどれかで絞り込んでいるか（件数の「N 件（全 M 件）」。#1891）。 */
export function isFeedbackFiltered(state: FeedbackUrlState): boolean {
  return (
    state.periodDays !== null ||
    Boolean(state.searchAnswerProfileId) ||
    Boolean(state.targetType) ||
    Boolean(state.rating) ||
    Boolean(state.reason) ||
    Boolean(state.q)
  );
}

/** 絞り込む前の全件（「全 M 件」）を数える取得の条件。1 件だけ取る。 */
export const FEEDBACK_ALL_PARAMS: FeedbackListParams = {
  period_days: null,
  sort_order: "newest",
  limit: 1,
  offset: 0,
};

/** 条件を初期に戻した URL（期間 30 日・新しい順・既定の表示件数・1 ページ目）。 */
export function defaultFeedbackSearch(): string {
  return `period=30&sort=newest&size=${DEFAULT_PAGE_SIZE}&page=1`;
}

function validNumber(value: string | null, allowed: readonly number[], fallback: number): number {
  const parsed = integer(value, fallback);
  return allowed.includes(parsed) ? parsed : fallback;
}

function integer(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : fallback;
}
