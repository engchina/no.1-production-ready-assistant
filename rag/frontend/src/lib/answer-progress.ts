/**
 * 回答生成（RAG 検索・チャット）の進捗の工程の名前と、今の工程の判定（#375）。
 *
 * backend は SSE の `stage` で、会話を踏まえた質問の書き換え（`history_rewrite`）・根拠の検索と
 * 回答の生成（`answer`。中の各工程は `answer_step:` で入れ子に送る）・回答側の安全チェック（`answer_guardrail`）・
 * 回答を作らない RAG 検索の全体（`evidence`。チャットと同じ流れを回答の生成の手前で止める。#3506）・KB の検索テストの
 * 検索だけ（`retrieval`）を送る。
 * 工程の名前は backend の時間切れの文言（`app/rag/span_display.py` の `ANSWER_STAGE_LABELS`。span に書く工程の名前にも使う）と揃える。
 */

import { ANSWER_STEP_STAGE_PREFIX, answerStepDisplayName } from "./answer-process";
import { t, type I18nKey } from "./i18n";

export interface AnswerStageEvent {
  stage: string;
  outcome: "started" | "success" | "error" | "cancelled" | "skipped";
}

export const ANSWER_STAGE_LABEL: Record<string, I18nKey> = {
  retrieval: "search.stage.retrieval",
  evidence: "search.stage.evidence",
  history_rewrite: "search.stage.historyRewrite",
  // 会話の添付からの根拠の選択（#3475）
  attachment_evidence: "search.stage.attachmentEvidence",
  field_filter: "search.stage.fieldFilter",
  answer: "search.stage.answer",
  // 回答側の安全チェック（#1146）
  answer_guardrail: "search.stage.answerGuardrail",
  // 回答の記録・会話の回答の保存（#2342）
  answer_record_save: "search.stage.answerRecordSave",
  chat_message_save: "search.stage.chatMessageSave",
};

/** 回答フローの中の各工程の工程名の接頭辞（#593。定義は `lib/answer-process.ts`）。 */
export { ANSWER_STEP_STAGE_PREFIX };

/** 工程の表示名（回答フローの工程は利用者向けの名前。#2096）。未知の工程は「処理」。 */
export function answerStageLabel(stage: string): string {
  if (stage.startsWith(ANSWER_STEP_STAGE_PREFIX)) {
    const name = stage.slice(ANSWER_STEP_STAGE_PREFIX.length).trim();
    if (name) return answerStepDisplayName(name);
  }
  return t(ANSWER_STAGE_LABEL[stage] ?? "search.stage.processing");
}

/**
 * 今の工程（最後に始まった、まだ終わっていない工程）。工程は入れ子になる（追加の検索の中の根拠の
 * 整理など）ため、終わった工程を外して残りの最後を返す。どれも実行中でなければ最後に通知された工程。
 * backend の `StageTracker` と同じ判定。
 */
export function currentAnswerStage(events: readonly AnswerStageEvent[]): string | null {
  const active: string[] = [];
  let last: string | null = null;
  for (const event of events) {
    last = event.stage;
    if (event.outcome === "started") {
      active.push(event.stage);
      continue;
    }
    const index = active.lastIndexOf(event.stage);
    if (index >= 0) active.splice(index, 1);
  }
  return active.length ? active[active.length - 1] : last;
}

/**
 * 回答生成中の表示の文言（「回答を生成しています（今の工程）」）。回答を作らない RAG 検索は
 * 「検索しています（今の工程）」（#649）。
 */
export function answerProgressLabel(
  events: readonly AnswerStageEvent[],
  { generateAnswer = true }: { generateAnswer?: boolean } = {}
): string {
  const stage = currentAnswerStage(events);
  return t(generateAnswer ? "answer.progress.label" : "answer.progress.searchLabel", {
    stage: stage ? answerStageLabel(stage) : t("answer.progress.preparing"),
  });
}
