import { describe, expect, it } from "vitest";

import { answerPromptError, groupStagesByPhase, isRetrievalPromptStage } from "./answer-prompt.logic";

// #1010: 編集できるプロンプトは backend の validate_prompt と同じ規則で、送る前に欄の下へ出す。
describe("編集できるプロンプトの欄の検証", () => {
  const placeholders = ["question", "answer_context"];

  it("必須の placeholder がすべてあればエラーにしない", () => {
    expect(answerPromptError("独自 {{question}} {{answer_context}}", "テンプレート", placeholders)).toBeNull();
  });

  it("空・空白だけは入力を求める", () => {
    expect(answerPromptError("  \n", "テンプレート", placeholders)).toBe("テンプレートを入力してください。");
  });

  it("欠けた placeholder を backend と同じ書き方で示す", () => {
    expect(answerPromptError("質問だけ {{question}}", "テンプレート", placeholders)).toBe(
      "必須の placeholder がありません: {{answer_context}}"
    );
    expect(answerPromptError("何もない", "テンプレート", placeholders)).toBe(
      "必須の placeholder がありません: {{question}}、{{answer_context}}"
    );
  });
});

// #2054: 各段は処理の順のまま、区分ごとに見出しを付けてまとめる。
describe("各段のプロンプトの区分ごとのまとめ", () => {
  it("続く同じ区分を 1 つにまとめ、順を変えない", () => {
    const stages = [
      { id: "a", phase: "ingestion" },
      { id: "b", phase: "ingestion" },
      { id: "c", phase: "question" },
      { id: "d", phase: "generation" },
    ];
    expect(groupStagesByPhase(stages)).toEqual([
      { phase: "ingestion", stages: [stages[0], stages[1]] },
      { phase: "question", stages: [stages[2]] },
      { phase: "generation", stages: [stages[3]] },
    ]);
  });

  it("飛び飛びの区分は並べ替えずに別のまとまりにする", () => {
    const stages = [
      { id: "a", phase: "audit" },
      { id: "b", phase: "evaluation" },
      { id: "c", phase: "audit" },
    ];
    expect(groupStagesByPhase(stages).map((group) => group.phase)).toEqual(["audit", "evaluation", "audit"]);
  });

  it("空なら空", () => {
    expect(groupStagesByPhase([])).toEqual([]);
  });
});


describe("プロンプトの表示先", () => {
  it("条件認識だけを回答プロンプトに置き、他の質問解釈は検索方法に残す", () => {
    expect(isRetrievalPromptStage({ id: "support_guide_conditions", phase: "question" })).toBe(false);
    for (const id of ["history_rewrite", "field_filter", "routing", "expansion"]) {
      expect(isRetrievalPromptStage({ id, phase: "question" })).toBe(true);
    }
    expect(isRetrievalPromptStage({ id: "audit", phase: "audit" })).toBe(false);
    expect(isRetrievalPromptStage({ id: "retrieval", phase: "retrieval" })).toBe(true);
  });
});


it("単一括弧の入力テンプレートの差し込み口も保存前に検査する", () => {
  expect(answerPromptError("## Step 1: {question}", "指示", [], ["{question}", "{context}"]))
    .toBe("必須の placeholder がありません: {context}");
  expect(answerPromptError("## Step 1: {question} {context}", "指示", [], ["{question}", "{context}"]))
    .toBeNull();
});
