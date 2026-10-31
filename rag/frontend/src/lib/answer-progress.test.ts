import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  ANSWER_STAGE_LABEL,
  ANSWER_STEP_STAGE_PREFIX,
  answerProgressLabel,
  answerStageLabel,
  currentAnswerStage,
  type AnswerStageEvent,
} from "./answer-progress";
import { t } from "./i18n";

describe("answer-progress（Issue 375）", () => {
  it("今の工程は、終わっていない最後の工程（入れ子の内側が終われば外側）", () => {
    const events: AnswerStageEvent[] = [
      { stage: "history_rewrite", outcome: "started" },
      { stage: "history_rewrite", outcome: "success" },
      { stage: "answer", outcome: "started" },
    ];
    expect(currentAnswerStage([])).toBeNull();
    expect(currentAnswerStage(events.slice(0, 1))).toBe("history_rewrite");
    expect(currentAnswerStage(events)).toBe("answer");
    const nested: AnswerStageEvent[] = [
      ...events,
      { stage: "answer_step:文書検索", outcome: "started" },
      { stage: "answer_step:文書検索", outcome: "success" },
    ];
    expect(currentAnswerStage(nested)).toBe("answer");
    // どれも実行中でなければ最後に通知された工程。
    expect(currentAnswerStage([...nested, { stage: "answer", outcome: "success" }])).toBe(
      "answer"
    );
  });

  it("処理中の文言に今の工程を入れる。未開始は検索の準備", () => {
    expect(answerProgressLabel([])).toBe("回答を生成しています（検索の準備）");
    expect(answerProgressLabel([], { generateAnswer: false })).toBe("検索しています（検索の準備）");
    expect(answerProgressLabel([{ stage: "history_rewrite", outcome: "started" }])).toBe(
      "回答を生成しています（前の会話を踏まえて質問を補う）"
    );
    expect(answerProgressLabel([{ stage: "retrieval", outcome: "started" }])).toBe(
      "回答を生成しています（文書から根拠の候補を検索する）"
    );
    // 回答を作らない RAG 検索の全体（#3506）。
    expect(answerProgressLabel([{ stage: "evidence", outcome: "started" }], { generateAnswer: false })).toBe(
      "検索しています（質問の条件を整理して根拠を探す）"
    );
    expect(answerStageLabel("unknown_stage")).toBe(t("search.stage.processing"));
  });

  it("工程の名前は backend の時間切れの文言（ANSWER_STAGE_LABELS）と同じ", () => {
    const source = readFileSync(
      resolve(__dirname, "../../../backend/app/rag/span_display.py"),
      "utf-8"
    );
    const block = source.slice(
      source.indexOf("ANSWER_STAGE_LABELS: dict[str, str] = {"),
      source.indexOf("}", source.indexOf("ANSWER_STAGE_LABELS: dict[str, str] = {"))
    );
    const backendLabels = Object.fromEntries(
      [...block.matchAll(/"([a-z_]+)": "([^"]+)"/g)].map((match) => [match[1], match[2]])
    );
    // backend が送る工程と画面が名前を持つ工程は同じ集合（片方だけに残る工程を作らない。#595）。
    expect(Object.keys(ANSWER_STAGE_LABEL).sort()).toEqual(Object.keys(backendLabels).sort());
    for (const [stage, label] of Object.entries(backendLabels)) {
      expect(answerStageLabel(stage), stage).toBe(label);
    }
    const preparing = /ANSWER_STAGE_BEFORE_START_LABEL = "([^"]+)"/.exec(source)?.[1];
    expect(t("answer.progress.preparing")).toBe(preparing);
  });

  it("回答フローの各工程（Issue 593）は利用者向けの名前（Issue 2096）で出し、入れ子の内側を今の工程にする", () => {
    const engineSource = readFileSync(
      resolve(__dirname, "../../../backend/app/rag/answer_engine.py"),
      "utf-8"
    );
    expect(engineSource).toContain(`ANSWER_STEP_STAGE_PREFIX = "${ANSWER_STEP_STAGE_PREFIX}"`);
    expect(answerStageLabel("answer_step:文書検索（1回目）")).toBe("検索文から根拠の候補を集める（1 回目）");
    expect(answerStageLabel("answer_step:")).toBe(t("search.stage.processing"));
    const events: AnswerStageEvent[] = [
      { stage: "answer", outcome: "started" },
      { stage: "answer_step:質問の理解", outcome: "started" },
      { stage: "answer_step:質問の理解", outcome: "success" },
      { stage: "answer_step:文書検索", outcome: "started" },
    ];
    expect(answerProgressLabel(events)).toBe("回答を生成しています（検索文から根拠の候補を集める）");
    expect(answerProgressLabel(events.slice(0, 3))).toBe(
      "回答を生成しています（質問の条件を整理して回答を作る）"
    );
  });
});
