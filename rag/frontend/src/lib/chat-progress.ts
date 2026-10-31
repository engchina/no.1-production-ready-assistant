/**
 * 回答（チャット・RAG 検索・実行の詳細）の処理の経過の工程の定義（3 製品共通の段階のイベント。#1359 / #3226）。
 *
 * 処理の経過は回答の trace（span の木）の投影（#3226）。backend（`app/rag/chat_progress.py`）は span の開始・終了を
 * 段のイベントとして記録し、工程を測る span に表示用のデータ（種類と値。`app/rag/span_display.py`）を付ける。
 *
 * - 回答フローの工程: 種類 `answer_step`（値の `name` が工程名、終わったら LLM の回数 `llm_calls`・送信の回数
 *   `llm_sends`・結果の説明 `note`）。
 * - 回答フローの外の工程（`history_rewrite` / `field_filter` / `answer_guardrail` / `answer_record_save`）: 工程の名前が
 *   種類。
 *
 * 入れ子は span の親子で決まる（共通の `labelChatProgressSteps`）。種類の無い span（全体を包む段・モデルの呼び出し
 * など）は共通の規則（段の名前は `answerStageName`、技術の span は共通の名前）で名前を付ける。組み立て・配信の
 * 受け取りは共通の `@production-ready/ui`（`useChatProgressStream` / `chatProgressStepsFromEvents`）が持ち、RAG は
 * 工程の名前と補足（i18n）だけを持つ。工程の名前は状態で変えない（アイコンと読み上げの文言が状態を示す）。
 */

import {
  type ChatProgressParams,
  type ChatProgressStep,
  type ChatProgressStepDefinitions,
} from "@production-ready/ui";

import { ANSWER_STEP_KIND, PIPELINE_STEP_LABEL, answerStepDisplayName, answerStepMeta } from "./answer-process";
import { t } from "./i18n";

function count(params: ChatProgressParams, key: string): number | undefined {
  const value = params[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function text(params: ChatProgressParams, key: string): string {
  const value = params[key];
  return typeof value === "string" ? value : "";
}

/** 回答フローの外の工程の定義（名前は状態で変えない）。 */
const PIPELINE_DEFINITIONS: ChatProgressStepDefinitions = Object.fromEntries(
  Object.entries(PIPELINE_STEP_LABEL).map(([kind, key]) => [kind, { label: () => t(key) }])
);

/** RAG の工程の定義（工程の種類 → 名前・補足）。 */
export const CHAT_PROGRESS_DEFINITIONS: ChatProgressStepDefinitions = {
  [ANSWER_STEP_KIND]: {
    label: (_status, params) => answerStepDisplayName(text(params, "name")),
    meta: (_status, params) =>
      answerStepMeta({ llmCalls: count(params, "llm_calls"), llmSends: count(params, "llm_sends"), note: text(params, "note") }),
  },
  ...PIPELINE_DEFINITIONS,
};

/**
 * 送った質問（サーバーの `start` の前）の段階。送信が遅いとき、回答の作成ではなく送信で待っていることが分かる。
 * `start` の後、最初の段階のイベントが届くまでも同じ段階を出す。
 */
export function chatSubmitProgressSteps(sentAtMs: number): ChatProgressStep[] {
  return [
    {
      id: "submit",
      label: t("chat.progress.submit.running"),
      status: "running",
      startedAt: new Date(sentAtMs).toISOString(),
    },
  ];
}
