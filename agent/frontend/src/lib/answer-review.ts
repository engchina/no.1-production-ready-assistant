import {
  hasAnswerPassageRecords,
  parseAnswerPassageRecords,
  type AnswerPassageRecords,
} from "@production-ready/ui";

import type { Artifact, ClarificationKind, RunState } from "@/lib/api";

/**
 * 資料との照合（#1286・#2361）。Run の成果物の「支援タスクの状態」（kind=`support_task`。#1243）と
 * 「資料との照合」（kind=`answer_validation`。#1246 / #1277）を、業務の利用者が読める形にする。
 *
 * 成果物の内容（JSON）の項目名・内部の語（`budget_exceeded`・`no_rag_evidence`・条件の出所の値など）は
 * 画面に出さない（handoff §13）。ここは値を取り出して分類するだけで、文言は画面が i18n で決める。
 * 内容は backend の `support_task.build_support_task` と `answer_validation.validation_content` /
 * `combine_validations` が作る形。読めない項目は無いものとして扱う（古い版・壊れた内容で画面を止めない）。
 */

export const SUPPORT_TASK_KIND = "support_task";
export const ANSWER_VALIDATION_KIND = "answer_validation";
export const ANSWER_KIND = "answer";

/**
 * 回答の対応（#1305 / #1314）。成果物 `answer` の `outcome.value`（RAG の AnswerEnvelope の `outcome` と同じ語彙）の
 * うち、画面にバッジで出すもの。「答えた（answered）」は RAG の回答の詳細（#1252）と同じく出さない。
 */
export type ReviewOutcome =
  | "conditional"
  | "needs_clarification"
  | "needs_environment_data"
  | "needs_human"
  | "insufficient_evidence";

const REVIEW_OUTCOMES = new Set<string>([
  "conditional",
  "needs_clarification",
  "needs_environment_data",
  "needs_human",
  "insufficient_evidence",
]);

/** 条件の出所（`user_answer` = 利用者の答え、`rag_guide` = RAG が質問の文から読んだ）。 */
export type ConditionSource = "user" | "question" | null;

export interface ReviewCondition {
  key: string;
  /** 条件の名前（業務ガイドのラベル。無ければ条件の id）。 */
  label: string;
  value: string;
  source: ConditionSource;
  /** 上書きする前の値（最も新しいもの）。無ければ null。 */
  previousValue: string | null;
}

export interface ReviewClarification {
  key: string;
  /** 確認の種類（NL2SQL の SQL の生成前の確認・RAG の確認。種類の無い古い問いは RAG。#1441）。 */
  kind: ClarificationKind;
  question: string;
  options: string[];
}

export interface ReviewGuide {
  title: string;
  /** 業務ガイドの版。分からなければ null。 */
  revision: number | null;
}

/**
 * 資料との照合の結果。
 * - verified: 資料で確かめた（回答はそのまま。印・外した段落・回答の注意が無い）
 * - marked: 印の付いた段落・根拠と矛盾して外した段落・回答全体の注意がある（段落の印の記録のある Run。#2738）
 * - withheld: 確かめられない点がある（段落の印の記録の無い過去の Run。段落を外した・本文を載せていない・不足を示した）
 * - unvalidated: 確かめられなかった（照合の失敗・資料を使わなかった回答）
 * - skipped: 照合の対象外（資料を使わない業務 Agent・空の回答）
 */
export type ValidationState = "verified" | "marked" | "withheld" | "unvalidated" | "skipped";

/** 確かめられなかった理由（画面の文言を選ぶため。内部の reason の値そのものは出さない）。 */
export type ValidationCause =
  | "noEvidence"
  | "emptyAnswer"
  | "clarification"
  | "absenceOnly"
  | "ragDeclined"
  | "noToolGrounding"
  | "failure"
  | null;

export interface ReviewValidation {
  state: ValidationState;
  cause: ValidationCause;
  /** 回答から外した段落の数。 */
  withheldClaims: number;
  /** 回答の本文をすべて載せていないか。 */
  withheldAll: boolean;
  /**
   * 回答の本文の「資料で確認できず除いた内容」の節（画面の「資料から確認できなかった事項」）の項目の数（backend の
   * `notice.items`。#2361）。除いた内容の一覧は節の 1 か所だけに出し、件数のバッジはこの数を使う（節と一致させる）。
   */
  listedItems: number;
  /**
   * 段落の印・外した段落・回答全体の注意（#2738）。この記録の無い過去の Run は null（今までの表示のまま。本文の
   * 「資料から確認できなかった事項」の節と件数のバッジ）。
   */
  passages: AnswerPassageRecords | null;
}

export interface AnswerReview {
  conditions: ReviewCondition[];
  clarifications: ReviewClarification[];
  guide: ReviewGuide | null;
  gaps: string[];
  /** この回答で、資料を調べる回数の上限に達して調べなかった呼び出しがあったか。 */
  limitReached: boolean;
  validation: ReviewValidation | null;
  /** 回答の対応（出すものだけ。対応の無い古い Run・「答えた」は null）。 */
  outcome: ReviewOutcome | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record).filter((item): item is Record<string, unknown> => item !== null) : [];
}

function text(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return typeof value === "string" ? value.split(/\s+/).filter(Boolean).join(" ") : "";
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

function conditionSource(value: unknown): ConditionSource {
  if (value === "user_answer") return "user";
  if (value === "rag_guide") return "question";
  return null;
}

/** 最も新しい成果物（承認の後の再開で上書きされる成果物は 1 つだが、念のため最後のものを使う）。 */
function latest(artifacts: Artifact[], kind: string): Record<string, unknown> | null {
  for (const artifact of [...artifacts].reverse()) {
    if (artifact.kind === kind) return record(artifact.content);
  }
  return null;
}

function conditions(state: Record<string, unknown>): ReviewCondition[] {
  const known = record(state.known_conditions);
  if (!known) return [];
  return Object.entries(known).flatMap(([id, raw]) => {
    const entry = record(raw);
    const value = text(entry?.value);
    if (!entry || !value) return [];
    const previous = records(entry.previous).map((item) => text(item.value)).find(Boolean) ?? null;
    return [
      {
        key: id,
        label: text(entry.label) || text(id),
        value,
        source: conditionSource(entry.source),
        previousValue: previous && previous !== value ? previous : null,
      },
    ];
  });
}

function clarifications(state: Record<string, unknown>): ReviewClarification[] {
  return records(state.pending_clarifications).flatMap((item, index) => {
    const question = text(item.question);
    if (!question) return [];
    const options = Array.isArray(item.options) ? item.options.map(text).filter(Boolean) : [];
    const kind: ClarificationKind = item.kind === "nl2sql" ? "nl2sql" : "rag";
    return [{ key: `${kind}:${text(item.condition_id)}:${index}`, kind, question, options }];
  });
}

function guide(state: Record<string, unknown>): ReviewGuide | null {
  const value = record(state.guide);
  if (!value || !text(value.guide_id)) return null;
  const revision = count(value.revision);
  return { title: text(value.title), revision: revision > 0 ? revision : null };
}

function limitReached(state: Record<string, unknown>): boolean {
  const budget = record(state.budget);
  return count(record(budget?.run)?.budget_exceeded) > 0;
}

function cause(reason: unknown): ValidationCause {
  if (reason === "no_rag_evidence") return "noEvidence";
  if (reason === "empty_answer") return "emptyAnswer";
  if (reason === "clarification_only") return "clarification";
  // 資料に無い・答えられないことだけを述べた回答（拒答。#1879）。
  if (reason === "absence_only") return "absenceOnly";
  if (reason === "rag_declined") return "ragDeclined";
  // 根拠・データのツールを呼ばずに作った回答を拒答の文に置き換えた（#1835）。
  if (reason === "no_tool_grounding") return "noToolGrounding";
  return reason ? "failure" : null;
}

/**
 * 段落の印の記録の無い過去の Run で、当時の backend（`publish_answer`）が回答から外した段落の判定（確かめられない段落）。
 * 今の方針（#2738）では本文から外すのは根拠と矛盾した段落だけで、確かめられない段落・規則の検査の段落は本文に残して
 * 印を付け、その記録（`passage_marks`・`removed_passages`・`answer_warnings`）をそのまま読む（`publishedAsIs`）。
 * 保存済みの過去の回答の表示は変えないため、記録の無い Run だけを当時の判定で読む。
 */
const LEGACY_BLOCKING_CLAIMS = new Set(["contradicted", "unsupported", "citation_error", "unassessed"]);

function errorFindings(result: Record<string, unknown>): Record<string, unknown>[] {
  return records(result.findings).filter((item) => item.severity === "error");
}

/**
 * 過去の Run で回答から外した段落（確かめられない段落）。主張ではない段落（見出し・出典の行・利用者への質問・
 * 資料に記載が無いことを述べる文。backend が `non_claim` を付ける。#1306）は除く。
 */
function legacyBlockingClaims(result: Record<string, unknown>): Record<string, unknown>[] {
  return records(result.claims).filter(
    (claim) => typeof claim.status === "string" && LEGACY_BLOCKING_CLAIMS.has(claim.status) && !claim.non_claim,
  );
}

/** 過去の Run で、確かめられない点が無く回答をそのまま載せたか（当時の backend の `publish_answer` と同じ判定）。 */
function legacyPublishedAsIs(result: Record<string, unknown>): boolean {
  if (result.valid === true) return true;
  if (errorFindings(result).length > 0) return false;
  if (result.status === "no_claims") return true;
  if (result.status !== "completed") return false;
  if (records(result.stale_evidence).length > 0 || records(result.missing_evidence).length > 0) return false;
  const claims = records(result.claims);
  if (legacyBlockingClaims(result).length > 0) return false;
  return claims.some((claim) => claim.status === "supported" || Boolean(claim.non_claim));
}

/**
 * 回答をそのまま載せたか（#2738）。印の付いた段落・根拠と矛盾して外した段落・回答全体の注意のどれも無いとき。
 * 規則の検査の結果（findings）・確かめられない段落は本文を外さないので、記録の 3 つだけで決める。
 */
function publishedAsIs(passages: AnswerPassageRecords): boolean {
  return !hasAnswerPassageRecords(passages);
}

const NO_COUNTS = { withheldClaims: 0, withheldAll: false, listedItems: 0, passages: null } as const;

/** 成果物「資料との照合」を、照合の結果にする。 */
export function validationReview(content: Record<string, unknown>): ReviewValidation | null {
  const status = content.status;
  const reasonCause = cause(content.reason);
  if (status === "skipped") return { state: "skipped", cause: reasonCause, ...NO_COUNTS };
  if (status === "unvalidated") return { state: "unvalidated", cause: reasonCause ?? "failure", ...NO_COUNTS };
  if (status !== "completed") return null;
  const passages = parseAnswerPassageRecords(content);
  if (passages) {
    return publishedAsIs(passages)
      ? { state: "verified", cause: null, ...NO_COUNTS }
      : { state: "marked", cause: null, withheldClaims: 0, withheldAll: false, listedItems: 0, passages };
  }
  const result = record(content.result) ?? {};
  if (legacyPublishedAsIs(result)) return { state: "verified", cause: null, ...NO_COUNTS };
  const withheld = record(content.withheld);
  return {
    state: "withheld",
    cause: null,
    withheldClaims: count(withheld?.claims),
    withheldAll: withheld?.all === true,
    listedItems: count(record(content.notice)?.items),
    passages: null,
  };
}

/** Run の最新の成果物「資料との照合」の、段落の印の記録（#2738）。記録の無い Run・照合の無い Run は null。 */
export function runAnswerPassages(artifacts: Artifact[]): AnswerPassageRecords | null {
  const content = latest(artifacts, ANSWER_VALIDATION_KIND);
  return content?.status === "completed" ? parseAnswerPassageRecords(content) : null;
}

/**
 * 成果物 `answer` の内容から、画面に出す回答の対応を取り出す。対応の無い古い Run・知らない値・「答えた」は null
 * （内部の値をそのまま画面に出さない）。
 */
export function answerOutcome(content: Record<string, unknown> | null): ReviewOutcome | null {
  const value = record(content?.outcome)?.value;
  return typeof value === "string" && REVIEW_OUTCOMES.has(value) ? (value as ReviewOutcome) : null;
}

const CLARIFICATION_KINDS: readonly ClarificationKind[] = ["nl2sql", "rag"];

/**
 * Run が利用者の答えを待っている確認の種類（#1441・#1445。支援タスクの状態の確かめ中の問いから）。
 * 取り消しの操作（「データベースの検索は不要」「資料の検索は不要」）を出す種類。
 */
export function pendingClarificationKinds(artifacts: Artifact[]): ClarificationKind[] {
  const state = latest(artifacts, SUPPORT_TASK_KIND);
  if (!state) return [];
  const kinds = new Set(clarifications(state).map((item) => item.kind));
  return CLARIFICATION_KINDS.filter((kind) => kinds.has(kind));
}

/** Run で利用者が取り消した確認の種類（Run の記録 `metadata.clarification_cancel`。#1441・#1445）。 */
export function cancelledClarificationKinds(run: Pick<RunState, "metadata">): ClarificationKind[] {
  const kinds = new Set(
    records(run.metadata?.clarification_cancel).map((item) => item.kind).filter((kind) => typeof kind === "string")
  );
  return CLARIFICATION_KINDS.filter((kind) => kinds.has(kind));
}

/** Run の成果物から、資料との照合の表示を作る。出すものが何も無ければ null。 */
export function answerReview(artifacts: Artifact[]): AnswerReview | null {
  const state = latest(artifacts, SUPPORT_TASK_KIND);
  const validationContent = latest(artifacts, ANSWER_VALIDATION_KIND);
  const outcome = answerOutcome(latest(artifacts, ANSWER_KIND));
  const review: AnswerReview = {
    conditions: state ? conditions(state) : [],
    clarifications: state ? clarifications(state) : [],
    guide: state ? guide(state) : null,
    gaps: state && Array.isArray(state.gaps) ? state.gaps.map(text).filter(Boolean) : [],
    limitReached: state ? limitReached(state) : false,
    validation: validationContent ? validationReview(validationContent) : null,
    outcome,
  };
  const empty =
    review.conditions.length === 0 &&
    review.clarifications.length === 0 &&
    review.guide === null &&
    review.gaps.length === 0 &&
    !review.limitReached &&
    review.validation === null &&
    review.outcome === null;
  return empty ? null : review;
}
