import { describe, expect, it } from "vitest";

import { answerPassageLabels, answerPassages, withoutPassageRecordSections } from "./answer-passages";

// #2738: 回答の記録の診断の段落の印・外した段落・回答全体の注意を読む（Agent と同じ共有の部品で描く）。内容は合成。

describe("answerPassages", () => {
  it("診断の 3 つの項目を読み、記録の無い過去の回答は null", () => {
    const records = answerPassages({
      outcome: "conditional",
      passage_marks: [{ text: "合成の段落。", state: "unverified", reasons: [{ code: "unsupported", label: "根拠で確かめられない" }] }],
      removed_passages: [],
      answer_warnings: [{ code: "off_goal", label: "質問からずれている可能性", detail: "" }],
    });
    expect(records?.marks).toEqual([
      { text: "合成の段落。", state: "unverified", reasons: [{ code: "unsupported", label: "根拠で確かめられない", detail: "" }] },
    ]);
    expect(records?.warnings).toHaveLength(1);
    expect(answerPassages({ outcome: "answered", confidence: "high" })).toBeNull();
    expect(answerPassages(null)).toBeNull();
    expect(answerPassages([])).toBeNull();
  });

  it("回答の構造（envelope）に置かれた記録も読む", () => {
    const records = answerPassages({ envelope: { removed_passages: [{ text: "合成の段落。", reasons: [] }] } });
    expect(records?.removed).toHaveLength(1);
  });

  it("文言は Agent と同じ（未確認・要注意・件数のバッジ）", () => {
    const labels = answerPassageLabels();
    expect(labels.state).toEqual({ unverified: "未確認", warning: "要注意" });
    expect(labels.badge.removed(2)).toBe("矛盾で除外 2 件");
    expect(labels.jumpLabel("「合成」")).toBe("「合成」（本文の段落へ移動）");
  });
});

describe("withoutPassageRecordSections", () => {
  // #3488: 回答エンジンが本文の末尾に足した 2 つの節は、記録から共有の部品が作るので本文から除く（2 回出さない）。
  const answer = [
    "合成の回答の段落です。",
    "資料により確認できた事項",
    "・合成の確かめた説明です。\n根拠：合成の資料.pdf p.2",
    "根拠と矛盾し除いた内容",
    "次の内容は根拠と矛盾するため、回答から除きました。\n・根拠と矛盾: 「合成の外した段落。」",
    "確認が必要な点",
    "次の点は根拠で確かめきれていないか、注意が必要です。\n・要約を根拠で確かめていない: 「合成の回答の段落です。」",
    "（対象: 範囲 A）",
  ].join("\n\n");

  it("保存した表示形式の見出しの節（見出しと項目の段落）だけを除き、ほかの節と後ろの文は残す", () => {
    const body = withoutPassageRecordSections(answer, {
      removed_section: "根拠と矛盾し除いた内容",
      marked_section: "確認が必要な点",
    });
    expect(body).not.toContain("根拠と矛盾し除いた内容");
    expect(body).not.toContain("確認が必要な点");
    expect(body).not.toContain("合成の外した段落");
    expect(body).toContain("資料により確認できた事項");
    expect(body).toContain("根拠：合成の資料.pdf p.2");
    expect(body.endsWith("（対象: 範囲 A）")).toBe(true);
  });

  it("表示形式の無い回答は画面の文言の見出しで探す", () => {
    expect(withoutPassageRecordSections(answer)).not.toContain("根拠と矛盾し除いた内容");
    expect(withoutPassageRecordSections("合成の段落です。")).toBe("合成の段落です。");
  });
});
