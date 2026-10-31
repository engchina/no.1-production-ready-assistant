/**
 * 回答（チャット・RAG 検索・実行の詳細）の「処理の経過」の工程の名前と計測値（#2096 / #3226）。
 *
 * 処理の経過は回答の trace（span の木）の投影で、行は共通の `labelChatProgressSteps` が span のイベントから作る。
 * ここは工程の名前（backend の工程名 → 利用者向けの名前）・段の名前（`pr.stage`）・計測値（LLM の回数・結果の
 * 説明）を持ち、`lib/chat-progress.ts` の工程の定義と root の `TraceDetailsLabelsProvider` が使う。
 */
import { createTraceDetailsLabels, type TraceDetailsLabels } from "@production-ready/ui";

import { formatNumber } from "./format";
import { t, type I18nKey } from "./i18n";

/**
 * 回答フローの中の各工程（質問の理解・文書検索など）の工程名の接頭辞（#593）。後ろは backend の工程名（日本語）で、
 * 画面は `answerStepDisplayName` で利用者向けの名前にする。backend の `app/rag/answer_engine.py` の
 * `ANSWER_STEP_STAGE_PREFIX` と同じ。
 */
export const ANSWER_STEP_STAGE_PREFIX = "answer_step:";

/** 回答フローの外の工程のうち、行にするもの（工程の名前 → 名前の i18n）。 */
export const PIPELINE_STEP_LABEL: Readonly<Record<string, I18nKey>> = {
  history_rewrite: "search.stage.historyRewrite",
  field_filter: "search.stage.fieldFilter",
  answer_guardrail: "search.stage.answerGuardrail",
  answer_record_save: "search.stage.answerRecordSave",
};

/** 回答フローの工程の段階の種類（backend の `ANSWER_STEP_KIND`）。 */
export const ANSWER_STEP_KIND = "answer_step";

/** 回答の記録の工程の状態のうち、結果の説明ではないもの（完了・例外で中断・中止。失敗は状態で示す）。 */
const PLAIN_STEP_STATUSES = new Set(["", "complete", "completed", "done", "ok", "エラーで中断", "中止"]);

/**
 * backend の工程名（`rag_engine.generation.answering` の `_execution_step`）→ 利用者向けの名前（i18n）。
 * backend の工程名はログ・記録の識別に使うので変えず、画面の名前だけをここで言い換える（#2096）。
 * 回数つきの工程（「文書検索（2回目）」）は回数を除いた名前で引き、「〈名前〉（2 回目）」にする。
 */
const ANSWER_STEP_NAME: Readonly<Record<string, I18nKey>> = {
  "質問の理解": "answer.step.understandQuestion",
  "用語・同義語の確認": "answer.step.terms",
  "回答ルールの確認": "answer.step.rules",
  "質問拡張戦略": "answer.step.expandQuestion",
  "検索文と検索語の確定": "answer.step.decideQueries",
  "回答生成フロー": "answer.step.answerFlow",
  "根拠検索フロー": "answer.step.evidenceFlow",
  "検索語の再確定（書き換え後）": "answer.step.redecideKeywords",
  "文書検索": "answer.step.searchDocuments",
  "Rerank": "answer.step.rerank",
  "rerank.request": "answer.step.rerankRequest",
  "文書の選択": "answer.step.selectDocuments",
  "文書の選択の解除": "answer.step.unselectDocuments",
  "根拠確認": "answer.step.checkEvidence",
  "根拠確認の結論": "answer.step.evidenceConclusion",
  "補正検索の準備": "answer.step.prepareResearch",
  "文脈の追加": "answer.step.addContext",
  "画面の選択": "answer.step.selectScreens",
  "章節の拡張": "answer.step.expandSections",
  "要求ごとの根拠の確認": "answer.step.coverRequests",
  "回答に添付する画像": "answer.step.attachImages",
  "回答文の生成と根拠確認": "answer.step.generateAnswer",
  "是正": "answer.step.revise",
  "是正の打ち切り": "answer.step.stopRevising",
  "文書ごとの起点の上限": "answer.step.deferDocumentCandidates",
  "条の選択": "answer.step.selectArticles",
  "未回答の要求の探し直し": "answer.step.retrieveUnanswered",
  "ベクトル検索": "answer.step.vectorSearch",
  "全文検索": "answer.step.textSearch",
  "検索対象のチャンクの読み込み": "answer.step.loadChunkPool",
  "要約・抽出値の原文の読み込み": "answer.step.loadDerivedSource",
  "前後の文脈の読み込み": "answer.step.loadSiblings",
  "参照先の根拠の追加": "answer.step.expandReferences",
  "実体・別名による検索拡張": "answer.step.expandEntities",
  "文書構造による検索拡張": "answer.step.expandStructure",
  "根拠の図の切り出し": "answer.step.prepareFigures",
  "文書の分類と背景の読み込み": "answer.step.loadDocumentInfo",
  "元の質問の検索": "answer.step.searchOriginalQuestion",
};

const STEP_ATTEMPT = /（(\d+)回目）$/;

/** 回答エンジンの工程の利用者向けの名前。知らない工程名は undefined。 */
function knownAnswerStepName(value: string): string | undefined {
  if (Object.prototype.hasOwnProperty.call(ANSWER_STEP_NAME, value)) return t(ANSWER_STEP_NAME[value]);
  const attempt = STEP_ATTEMPT.exec(value);
  const base = attempt ? value.slice(0, attempt.index) : value;
  if (!attempt || !Object.prototype.hasOwnProperty.call(ANSWER_STEP_NAME, base)) return undefined;
  return t("answer.step.attempt", { name: t(ANSWER_STEP_NAME[base]), count: Number(attempt[1]) });
}

/** 工程の利用者向けの名前（backend の工程名から。知らない工程名はそのまま）。 */
export function answerStepDisplayName(name: string): string {
  const value = name.trim();
  if (!value) return t("search.stage.processing");
  return knownAnswerStepName(value) ?? value;
}

/**
 * 段の利用者向けの名前（#3219 / #3226）。処理の経過の段の span（属性 `pr.stage`）の名前に使う。
 * パイプラインの段（全体を包む `answer` / `evidence` / `retrieval` を含む）と回答エンジンの工程名を引き、知らない段は undefined
 * （処理の経過も trace も段の名前をそのまま出す）。
 */
export function answerStageName(stage: string): string | undefined {
  const value = stage.trim();
  if (value === "answer") return t("search.stage.answer");
  if (value === "evidence") return t("search.stage.evidence");
  if (value === "retrieval") return t("search.stage.retrieval");
  if (Object.prototype.hasOwnProperty.call(PIPELINE_STEP_LABEL, value)) return t(PIPELINE_STEP_LABEL[value]);
  return knownAnswerStepName(value);
}

/** RAG の span の名前の文言（root の `TraceDetailsLabelsProvider` で全画面に渡す。段の名前は工程の行と同じ）。 */
export const RAG_TRACE_DETAILS_LABELS: TraceDetailsLabels = createTraceDetailsLabels(t, { stageName: answerStageName, product: "rag" });

/**
 * 右の列の計測値（結果の説明・LLM の回数）。無ければ undefined。送り直しで API への送信（`llmSends`）が呼び出しより
 * 多いときは送信の回数も示す（#2966。「LLM 1 回」の中で 2 回送ったことを分ける）。
 */
export function answerStepMeta({
  llmCalls,
  llmSends,
  note,
}: {
  llmCalls?: number | null;
  llmSends?: number | null;
  note?: string | null;
}): string | undefined {
  const parts = [
    note && !PLAIN_STEP_STATUSES.has(note.trim()) ? note.trim() : null,
    llmCalls && llmCalls > 0 ? llmCallsLabel("search.answerDetails", llmCalls, llmSends) : null,
  ].filter((part): part is string => Boolean(part));
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

/** LLM の回数の文言（送信が呼び出しより多いときは送信の回数も）。`prefix` は i18n の key の接頭辞。 */
export function llmCallsLabel(
  prefix: "search.answerDetails" | "history.process",
  calls: number,
  sends?: number | null,
): string {
  return sends && sends > calls
    ? t(`${prefix}.llmCallsWithSends`, { count: formatNumber(calls), sends: formatNumber(sends) })
    : t(`${prefix}.llmCalls`, { count: formatNumber(calls) });
}
