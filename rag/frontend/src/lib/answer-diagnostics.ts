import { hasRunStepDetails, type RunStepDetailField, type RunStepDetails } from "@production-ready/ui";

import type { ExtractionFieldCondition } from "@/components/search/extraction-field-filters";
import { t } from "@/lib/i18n";
import { parseCragDiagnostics, type CragDiagnostics } from "@/lib/crag-diagnostics";
import { parseAnswerValidation } from "@/lib/answer-validation";

/** 回答フローの診断(backend の diagnostics.answer)。 */
/** 回答の対応（#1235。backend の diagnostics.answer.outcome）。 */
export type AnswerOutcome =
  | "answered"
  | "conditional"
  | "needs_clarification"
  | "needs_environment_data"
  | "needs_human"
  | "insufficient_evidence";

const ANSWER_OUTCOMES: readonly AnswerOutcome[] = [
  "answered",
  "conditional",
  "needs_clarification",
  "needs_environment_data",
  "needs_human",
  "insufficient_evidence",
];

/** 業務ガイドの条件の、分からないときの扱い（backend の unknown_handling）。 */
export type GuideConditionHandling = "ask" | "branch" | "handoff";

/** 業務ガイドの適用範囲の項目（値のある項目だけ backend が出す。#1278）。 */
export type GuideApplicabilityKey = "business_domains" | "object_types" | "versions";

const GUIDE_APPLICABILITY_KEYS: readonly GuideApplicabilityKey[] = [
  "business_domains",
  "object_types",
  "versions",
];

/** 回答に使った業務ガイドと、条件の状態（#1238 / #1278。backend の diagnostics.answer.guide）。 */
export type AnswerGuide = {
  guideId: string;
  title: string;
  /** 公開の版。 */
  revision: number;
  /**
   * 分かっていた条件（値と出所）。source は user=確認の質問への答え / question=質問の文から読んだ /
   * document=回答の根拠（画面の図の項目の種類。#2404）で決めた。
   */
  known: { id: string; label: string; value: string; source: "user" | "question" | "document" | null }[];
  /**
   * 分からなかった条件。state=conflicting は質問から複数の値（candidates）が読み取れて決められなかった
   * 条件。handling は分からないときの扱い（古い記録など、無ければ null）。
   */
  unresolved: {
    id: string;
    label: string;
    state: "unknown" | "conflicting";
    candidates: string[];
    handling: GuideConditionHandling | null;
  }[];
  /**
   * 適用範囲の確かめ方（matched=質問・絞り込みの手がかりと合った / unverified=手がかりが無く
   * 確かめていない）。制限の無い項目は出ない。
   */
  applicability: { key: GuideApplicabilityKey; state: "matched" | "unverified" }[];
};

export type AnswerDiagnostics = {
  validation: ReturnType<typeof parseAnswerValidation>;
  crag: CragDiagnostics | null;
  /** 回答の対応（#1235）。古い記録など、無ければ null。 */
  outcome: AnswerOutcome | null;
  /** 回答に使った業務ガイド（#1238）。使っていなければ null。 */
  guide: AnswerGuide | null;
  /** 公開の業務ガイドを読み込めず、業務ガイドを使わずに回答したか（#1278）。 */
  guideLoadFailed: boolean;
  confidence: string;
  needsHumanReview: boolean | null;
  insufficientReason: string;
  /** 判断理由（引用照合の件数など。#651）。 */
  reasoningSummary: string;
  /** 資料では決まらず業務システムの実データで確かめる値（#651）。 */
  externalDataRequired: boolean;
  externalDataItems: string[];
  /** 問い合わせ型（回答モデルが付けた質問の種類の語。#651）。 */
  questionType: string[];
  /**
   * 質問から読み取って検索に足した抽出項目の条件（#652）。relaxed は、その条件で見つからず
   * 外して検索し直したか。読み取っていなければ null。
   */
  autoFieldFilter: { conditions: ExtractionFieldCondition[]; relaxed: boolean } | null;
  /**
   * 利用者が選んだ承認済み FAQ だけから回答したときの出典（回答した時点の原文。#737）。
   * answer は変更前に保存した回答では空。FAQ から回答していなければ null。
   */
  approvedFaq: { question: string; answer: string } | null;
  /** チャットで会話履歴から書き換えた質問(書き換えなしは空)。 */
  rewrittenQuestion: string;
  generatedQueries: string[];
  /**
   * 関係情報で根拠を加えた処理の記録（0 件も含む）。処理の記録がなければ null。
   */
  graphExpansion: { addedCount: number; status: string; graphAvailable: boolean | null; plannedCount: number; skippedExistingCount: number; entities: { name: string; addedCount: number }[] } | null;
  /**
   * 使ったモデル（#649）。LLM（選んだ回答モデル）は質問の理解・拡張・回答の生成・監査のすべてに使う（#3444）。
   * attachedImages はそのモデルへ添付した根拠の画像の件数（画像を読めないモデル・画像の無い根拠は 0）、rerank は
   * 無効なら空。
   */
  models: { llm: ModelRef | null; attachedImages: number; embedding: string; rerank: string };
  steps: {
    name: string;
    status: string;
    elapsedSeconds: number | null;
    llmCalls: number;
    /** API への実際の送信の回数（送り直しで呼び出しより多いときだけ。#2966）。 */
    llmSends?: number;
    /** この工程だけの詳細（見出しと値・行。選んだ条・検索文・件数など。#2294 / #2341）。 */
    details?: RunStepDetails;
  }[];
  tree: {
    parentId: string;
    source: string;
    page: number | null;
    children: {
      chunkId: string;
      role: string;
      modelUsed: boolean;
      /** 回答での使われ方（#2040）。回答で引用していない・記録の無い回答は null。 */
      answerUse: "confirmed" | "reference" | null;
      page: number | null;
    }[];
  }[];
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * 段の詳細（backend の共有の詳細の形 `{ fields, lines, truncated }`）を画面の `RunStepDetails` にする。旧い記録
 * （#2294）の行の配列は行として読む。中身が無ければ undefined（段に開閉の印を出さない）。
 */
export function parseRunStepDetails(value: unknown): RunStepDetails | undefined {
  const text = (item: unknown): item is string => typeof item === "string" && item.length > 0;
  if (Array.isArray(value)) {
    const lines = value.filter(text);
    return lines.length ? { lines } : undefined;
  }
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  const fields: RunStepDetailField[] = list(raw.fields).flatMap((field) => {
    const item = record(field);
    const label = item.label;
    const fieldValue = item.value;
    if (!text(label) || !text(fieldValue)) return [];
    return [{ label, value: fieldValue, ...(item.mono === true ? { mono: true } : {}) }];
  });
  const lines = list(raw.lines).filter(text);
  const details: RunStepDetails = {
    ...(fields.length ? { fields } : {}),
    ...(lines.length ? { lines } : {}),
    ...(raw.truncated === true ? { truncated: true } : {}),
  };
  return hasRunStepDetails(details) ? details : undefined;
}

/** diagnostics.answer を表示用に正規化する。回答フローの診断が無い回答では null。 */
export function parseAnswerDiagnostics(
  value: unknown,
): AnswerDiagnostics | null {
  if (!value || typeof value !== "object") return null;
  const raw = record(value);
  const outcome = String(raw.outcome ?? "");
  return {
    outcome: (ANSWER_OUTCOMES as readonly string[]).includes(outcome)
      ? (outcome as AnswerOutcome)
      : null,
    crag: parseCragDiagnostics(raw.crag_attempts),
    validation: parseAnswerValidation(raw.answer_validation),
    guide: parseGuide(raw.guide),
    guideLoadFailed: raw.guide_load_failed === true,
    confidence: String(raw.confidence ?? ""),
    needsHumanReview:
      typeof raw.needs_human_review === "boolean"
        ? raw.needs_human_review
        : null,
    insufficientReason: String(raw.insufficient_reason ?? ""),
    reasoningSummary: String(raw.reasoning_summary ?? ""),
    externalDataRequired: raw.external_data_required === true,
    externalDataItems: list(raw.external_data_items).map(String).filter(Boolean),
    questionType: list(raw.question_type).map(String).filter(Boolean),
    autoFieldFilter: parseAutoFieldFilter(raw.auto_field_filter),
    approvedFaq:
      raw.answer_source === "approved_faq"
        ? {
            question: String(raw.approved_faq_question ?? ""),
            answer: String(raw.approved_faq_answer ?? ""),
          }
        : null,
    rewrittenQuestion: String(raw.rewritten_question ?? ""),
    generatedQueries: list(raw.generated_queries).map(String).filter(Boolean),
    graphExpansion: parseGraphExpansion(raw.graph_expansion),
    models: parseModels(raw.models),
    steps: list(raw.execution_steps).map((step) => {
      const item = record(step);
      const details = parseRunStepDetails(item.details);
      return {
        name: String(item.name ?? ""),
        status: String(item.status ?? ""),
        elapsedSeconds: num(item.elapsed_seconds),
        llmCalls: num(item.llm_calls) ?? 0,
        ...(num(item.llm_sends) ? { llmSends: num(item.llm_sends) ?? undefined } : {}),
        // 工程の詳細（段を開いて見る。#2294 / #2341）。古い記録には無い。
        ...(details ? { details } : {}),
      };
    }),
    tree: list(raw.evidence_tree).map((parent) => {
      const item = record(parent);
      return {
        parentId: String(item.parent_id ?? ""),
        source: String(item.source ?? ""),
        page: num(item.page),
        children: list(item.children).map((child) => {
          const entry = record(child);
          return {
            chunkId: String(entry.chunk_id ?? ""),
            role: String(entry.role ?? ""),
            modelUsed: entry.is_model_used === true,
            answerUse:
              entry.is_model_used === true &&
              (entry.answer_use === "confirmed" || entry.answer_use === "reference")
                ? entry.answer_use
                : null,
            page: num(entry.page),
          };
        }),
      };
    }),
  };
}

function parseGraphExpansion(value: unknown): AnswerDiagnostics["graphExpansion"] {
  const item = record(value);
  const addedCount = num(item.added_count) ?? 0;
  if (Object.keys(item).length === 0) return null;
  return {
    status: typeof item.status === "string" ? item.status : "completed",
    graphAvailable: typeof item.graph_available === "boolean" ? item.graph_available : null,
    plannedCount: num(item.planned_count) ?? addedCount,
    skippedExistingCount: num(item.skipped_existing_count) ?? 0,
    addedCount,
    entities: list(item.entities)
      .map((entity) => {
        const entry = record(entity);
        return { name: String(entry.entity ?? ""), addedCount: num(entry.added_count) ?? 0 };
      })
      .filter((entity) => entity.name && entity.addedCount > 0),
  };
}

function oneOf<T extends string>(value: unknown, values: readonly T[]): T | null {
  return typeof value === "string" && (values as readonly string[]).includes(value)
    ? (value as T)
    : null;
}

function parseGuide(value: unknown): AnswerGuide | null {
  const raw = record(value);
  const revision = num(raw.revision);
  if (typeof raw.title !== "string" || !raw.title || revision === null) return null;
  const known = list(raw.known_conditions).flatMap((item): AnswerGuide["known"] => {
    const entry = record(item);
    const label = String(entry.label ?? "");
    if (!label) return [];
    const source = oneOf(entry.source, ["user", "question", "document"] as const);
    return [{ id: String(entry.id ?? label), label, value: String(entry.value ?? ""), source }];
  });
  const unresolved = list(raw.unknown_conditions).flatMap((item): AnswerGuide["unresolved"] => {
    const entry = record(item);
    const label = String(entry.label ?? "");
    if (!label) return [];
    const candidates = list(entry.candidates).map(String).filter(Boolean);
    // 矛盾は候補が 2 つ以上あるときだけ（候補が読めない古い記録などは「分からない」に寄せる）。
    const state: "unknown" | "conflicting" =
      entry.state === "conflicting" && candidates.length > 1 ? "conflicting" : "unknown";
    const handling = oneOf(entry.handling, ["ask", "branch", "handoff"] as const);
    return [
      {
        id: String(entry.id ?? label),
        label,
        state,
        candidates: state === "conflicting" ? candidates : [],
        handling,
      },
    ];
  });
  const scope = record(raw.applicability);
  const applicability = GUIDE_APPLICABILITY_KEYS.flatMap((key): AnswerGuide["applicability"] => {
    const state = oneOf(scope[key], ["matched", "unverified"] as const);
    return state ? [{ key, state }] : [];
  });
  return {
    guideId: String(raw.guide_id ?? ""),
    title: raw.title,
    revision,
    known,
    unresolved,
    applicability,
  };
}

function parseAutoFieldFilter(value: unknown): AnswerDiagnostics["autoFieldFilter"] {
  const raw = record(value);
  const conditions = list(raw.conditions).flatMap((item) => {
    const entry = record(item);
    const name = String(entry.name ?? "");
    const valueType = String(entry.value_type ?? "");
    const op = String(entry.op ?? "");
    if (!name || !["string", "number", "date", "bool"].includes(valueType)) return [];
    if (!["eq", "gte", "lte"].includes(op)) return [];
    return [
      {
        name,
        value_type: valueType as ExtractionFieldCondition["value_type"],
        op: op as ExtractionFieldCondition["op"],
        value: String(entry.value ?? ""),
      },
    ];
  });
  return conditions.length ? { conditions, relaxed: raw.relaxed === true } : null;
}

type ModelRef = { modelId: string; label: string };

function parseModelRef(value: unknown): ModelRef | null {
  const raw = record(value);
  const modelId = String(raw.model_id ?? "");
  return modelId ? { modelId, label: String(raw.label ?? "") || modelId } : null;
}

function imageCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/**
 * 回答モデルへ添付した根拠の画像の件数（#3444）。作成した直後の回答は配信の診断の `models.attached_images`、
 * 保存した回答は会話のメッセージの `answer_attached_images`（回答の記録の診断の同じ値）から読む。
 * 画像を添付しなかった回答・記録の無い回答は 0。
 */
export function answerAttachedImages(diagnostics: unknown, savedMessage: unknown): number {
  const live = parseAnswerDiagnostics(diagnostics)?.models.attachedImages ?? 0;
  return Math.max(live, imageCount(record(savedMessage).answer_attached_images));
}

/**
 * 回答の見出し（「回答モデル: {名前}」。根拠の画像を添付した回答は「（画像も参照）」を添える。#3439・#3444）。
 * チャットの列と RAG 検索の回答で同じ文言にする（#3488）。名前が無ければ null。
 */
export function answerModelHeading(label: string | null | undefined, attachedImages: number): string | null {
  if (!label) return null;
  return attachedImages > 0 ? t("chat.column.modelWithImages", { name: label }) : t("chat.column.model", { name: label });
}

function parseModels(value: unknown): AnswerDiagnostics["models"] {
  const raw = record(value);
  return {
    llm: parseModelRef(raw.llm),
    attachedImages: imageCount(raw.attached_images),
    embedding: String(raw.embedding ?? ""),
    rerank: String(raw.rerank ?? ""),
  };
}

/** 信頼度 → StatusBadge の variant。 */
export function confidenceVariant(
  confidence: string,
): "success" | "warning" | "danger" | "neutral" {
  if (confidence === "high") return "success";
  if (confidence === "medium") return "warning";
  if (confidence === "low") return "danger";
  return "neutral";
}

type BadgeVariant = "info" | "warning" | "danger";

/** 回答の対応のバッジ（「答えた」は出さない）。#1252。 */
export function outcomeBadge(
  outcome: AnswerOutcome | null
): { variant: BadgeVariant; key: `search.answerDetails.outcome.${Exclude<AnswerOutcome, "answered">}` } | null {
  switch (outcome) {
    case "conditional":
      return { variant: "warning", key: "search.answerDetails.outcome.conditional" };
    case "needs_clarification":
      return { variant: "info", key: "search.answerDetails.outcome.needs_clarification" };
    case "needs_environment_data":
      return { variant: "warning", key: "search.answerDetails.outcome.needs_environment_data" };
    case "needs_human":
      return { variant: "warning", key: "search.answerDetails.outcome.needs_human" };
    case "insufficient_evidence":
      return { variant: "danger", key: "search.answerDetails.outcome.insufficient_evidence" };
    default:
      return null;
  }
}


/**
 * 「回答に未反映の関連章節」の 1 行と、その章節の原文を開く先（#2040）。`line` は本文の行と同じ文
 * （rag_engine の `UnreadSection.gap_line`）、`label` は章節の名前、`page` は章節の始まりの頁。
 */
export type UnreadSectionLink = {
  line: string;
  label: string;
  documentId: string;
  page: number | null;
};

/** 回答の診断の `section_expansion.unread` から、本文の行ごとの章節を読む（#1436・#2040）。 */
export function unreadSectionLinks(diagnostics: unknown): UnreadSectionLink[] {
  const unread = list(record(record(diagnostics).section_expansion).unread);
  return unread.flatMap((item) => {
    const entry = record(item);
    const line = typeof entry.line === "string" ? entry.line.trim() : "";
    const documentId = typeof entry.document_id === "string" ? entry.document_id : "";
    if (!line || !documentId) return [];
    const label = typeof entry.label === "string" && entry.label ? entry.label : line;
    return [{ line, label, documentId, page: num(entry.page) }];
  });
}
