import { describe, expect, it } from "vitest";

import {
  defaultFeedbackSearch,
  feedbackListParams,
  isFeedbackFiltered,
  parseFeedbackUrl,
} from "./FeedbackClient.logic";

describe("feedback URL state", () => {
  it("restores filters, page size and selected feedback", () => {
    const state = parseFeedbackUrl(
      new URLSearchParams(
        "period=90&search_answer_profile=bv-1&target=answer&rating=not_helpful&reason=incorrect&q=規程&sort=oldest&size=100&page=3&feedback=fb-1"
      )
    );

    expect(state).toMatchObject({
      periodDays: 90,
      searchAnswerProfileId: "bv-1",
      targetType: "answer",
      rating: "not_helpful",
      reason: "incorrect",
      q: "規程",
      sortOrder: "oldest",
      pageSize: 100,
      page: 3,
      feedbackId: "fb-1",
    });
    expect(feedbackListParams(state).offset).toBe(200);
  });

  it("falls back to stable defaults for invalid parameters", () => {
    const state = parseFeedbackUrl(new URLSearchParams("period=3&size=12&page=-2"));
    expect(state.periodDays).toBe(30);
    // 表示件数は 3 製品の一覧と同じ既定の 25 件（#1891・#1952）。
    expect(state.pageSize).toBe(25);
    expect(state.page).toBe(1);
  });

  // #1891: 独自の 25 / 50 / 100 をやめ、3 製品の一覧と同じ段にした。
  it("表示件数は共通の段（10 / 25 / 50 / 100）だけを受け付ける", () => {
    expect(parseFeedbackUrl(new URLSearchParams("size=10")).pageSize).toBe(10);
    expect(parseFeedbackUrl(new URLSearchParams("size=25")).pageSize).toBe(25);
    expect(parseFeedbackUrl(new URLSearchParams("size=20")).pageSize).toBe(25);
    expect(parseFeedbackUrl(new URLSearchParams(defaultFeedbackSearch())).pageSize).toBe(25);
  });
});

describe("feedback list count", () => {
  it("期間・条件・検索語のどれかがあれば絞り込み中（「N 件（全 M 件）」）", () => {
    expect(isFeedbackFiltered(parseFeedbackUrl(new URLSearchParams("period=all")))).toBe(false);
    expect(isFeedbackFiltered(parseFeedbackUrl(new URLSearchParams("period=all&sort=oldest&size=50")))).toBe(false);
    expect(isFeedbackFiltered(parseFeedbackUrl(new URLSearchParams(defaultFeedbackSearch())))).toBe(true);
    expect(isFeedbackFiltered(parseFeedbackUrl(new URLSearchParams("period=all&rating=helpful")))).toBe(true);
    expect(isFeedbackFiltered(parseFeedbackUrl(new URLSearchParams("period=all&q=規程")))).toBe(true);
  });
});
