import { describe, expect, it } from "vitest";

import { answerNoticeText } from "./run-artifacts";

describe("answerNoticeText", () => {
  it("最終回答の成果物の notice を返し、資料との照合の notice は読まない", () => {
    expect(
      answerNoticeText([
        { kind: "answer_validation", content: { notice: { items: 1, heading: "除いた内容" } } },
        { kind: "answer", content: { text: "回答", notice: "注意書き" } },
      ]),
    ).toBe("注意書き");
  });

  it("注意書きの無い回答・古い Run・回答の無い Run は null", () => {
    expect(answerNoticeText([{ kind: "answer", content: { text: "回答" } }])).toBeNull();
    expect(answerNoticeText([{ kind: "answer", content: { text: "回答", notice: "  " } }])).toBeNull();
    expect(answerNoticeText([{ kind: "answer_validation", content: {} }])).toBeNull();
  });

  it("最も新しい回答の注意書きを使う", () => {
    expect(
      answerNoticeText([
        { kind: "answer", content: { text: "前の回答", notice: "前の文" } },
        { kind: "answer", content: { text: "新しい回答" } },
      ]),
    ).toBeNull();
  });
});
