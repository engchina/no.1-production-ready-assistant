import { describe, expect, it } from "vitest";

import { chatHistoryWindowLabels } from "../src/components/chat/chat-history-window";

describe("chatHistoryWindowLabels（#2340）", () => {
  it("期間の日数から見出しの補足と 0 件の文を作る", () => {
    expect(chatHistoryWindowLabels(90)).toEqual({
      caption: "最近 90 日",
      empty: "最近 90 日の会話はありません",
    });
  });

  it("日数が分からない・0（絞っていない）なら出さない", () => {
    expect(chatHistoryWindowLabels(undefined)).toBeNull();
    expect(chatHistoryWindowLabels(null)).toBeNull();
    expect(chatHistoryWindowLabels(0)).toBeNull();
    expect(chatHistoryWindowLabels(Number.NaN)).toBeNull();
  });
});
