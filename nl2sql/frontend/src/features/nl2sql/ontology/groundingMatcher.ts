import type { OntologyGraph, OntologyNode } from "./types";

/**
 * 決定論の日本語質問→オントロジーノード照合エンジン(LLM なし・依存追加なし)。
 *
 * 2 段階でマッチする:
 * - Phase A(消費・高精度): ノード名/エイリアス/物理名が質問文の部分文字列として
 *   現れる場合。最長一致で質問文からスパンを消費する(「注文明細」を「注文」より優先)。
 * - Phase B(トークン展開・部分一致): 残り文を `Intl.Segmenter`(ja, word)で分かち書きし、
 *   トークンとノード名の前方一致/部分一致をスコア付きで拾う(「部署」→「部署情報」)。
 *   `Intl.Segmenter` 非対応環境は 2-gram へフォールバックする。
 */

export type GroundingMatchVia = "name" | "alias" | "technical" | "token";

export interface GroundingQuestionSpan {
  start: number;
  end: number;
}

export interface GroundingCandidate {
  node: OntologyNode;
  /** 1.0=完全一致 / 0.9=名称が質問に包含 / 0.8=トークン前方一致 / 0.65=トークン部分一致 */
  score: number;
  /** 質問(正規化後)側の一致文字列。UI の下線表示に使う。 */
  matchedText: string;
  /** 正規化質問文中のスパン。Phase B で位置を特定できない場合は null。 */
  span: GroundingQuestionSpan | null;
  via: GroundingMatchVia;
}

export const GROUNDING_ENTITY_KINDS = new Set([
  "business_entity",
  "object_type",
  "interface",
  "function",
  "action_type",
  "business_rule",
  "object_set",
  "business_event",
  "table",
  "view",
]);
export const GROUNDING_ATTRIBUTE_KINDS = new Set([
  "property",
  "shared_property",
  "value_type",
  "enumeration",
  "metric",
  "business_term",
  "column",
  "enum_value",
]);

/** スコアがこの値未満の候補は採用しない。 */
export const GROUNDING_SCORE_THRESHOLD = 0.5;

/** 集計質問(平均/合計/〜ごと 等)の検出。 */
const AGGREGATE_PATTERN =
  /平均|合計|件数|総数|最大|最小|中央値|割合|比率|ごと|別の|集計|(?:^|[^a-z])(?:avg|sum|count|max|min|group by)(?:[^a-z]|$)/;

/** ノード照合の対象にしない機能語・依頼語(集計/一覧検出は正規表現側で行う)。 */
const NOISE_TOKENS = new Set([
  "教え",
  "教えて",
  "見せ",
  "見せて",
  "表示",
  "確認",
  "質問",
  "一覧",
  "関係",
  "内容",
  "情報",
  "データ",
  "とは",
  "何",
  "です",
  "ます",
  "ください",
  "したい",
  "する",
  "して",
  "こと",
  "もの",
  "どの",
  "どれ",
  "どう",
  "すべて",
  "全て",
  "リスト",
]);

export function normalizeGroundingText(text: string): string {
  return text.normalize("NFKC").toLowerCase().trim();
}

export function detectAggregateQuestion(normalizedQuestion: string): boolean {
  return AGGREGATE_PATTERN.test(normalizedQuestion);
}

let cachedSegmenter: Intl.Segmenter | null | undefined;

function jaSegmenter(): Intl.Segmenter | null {
  if (cachedSegmenter !== undefined) return cachedSegmenter;
  try {
    cachedSegmenter =
      typeof Intl !== "undefined" && typeof Intl.Segmenter === "function"
        ? new Intl.Segmenter("ja", { granularity: "word" })
        : null;
  } catch {
    cachedSegmenter = null;
  }
  return cachedSegmenter;
}

/** 2 文字以上の語トークンを返す。Segmenter 非対応時は 2-gram フォールバック。 */
export function tokenizeGroundingQuestion(normalizedQuestion: string): string[] {
  const segmenter = jaSegmenter();
  const tokens: string[] = [];
  if (segmenter) {
    for (const segment of segmenter.segment(normalizedQuestion)) {
      const token = segment.segment.trim();
      if (token.length < 2) continue;
      if (segment.isWordLike === false) continue;
      tokens.push(token);
    }
  } else {
    // フォールバック: 空白/記号区切りの塊から 2-gram を作る
    for (const chunk of normalizedQuestion.split(/[\s、。,.!?・:;()「」『』]+/)) {
      if (chunk.length < 2) continue;
      if (chunk.length <= 3) {
        tokens.push(chunk);
        continue;
      }
      for (let index = 0; index + 2 <= chunk.length; index += 1) {
        tokens.push(chunk.slice(index, index + 2));
      }
    }
  }
  return [...new Set(tokens.filter((token) => !NOISE_TOKENS.has(token)))];
}

interface NameVariant {
  text: string;
  via: GroundingMatchVia;
}

function nodeNameVariants(node: OntologyNode): NameVariant[] {
  const variants: NameVariant[] = [];
  const push = (raw: string | undefined | null, via: GroundingMatchVia) => {
    const normalized = normalizeGroundingText(raw ?? "");
    if (normalized.length >= 2) variants.push({ text: normalized, via });
  };
  push(node.business_name_ja, "name");
  for (const alias of node.aliases ?? []) push(alias, "alias");
  push(node.technical_name, "technical");
  if (node.metadata?.definition) {
    push(node.description_ja, "token");
    for (const value of Object.values(node.metadata.definition)) if (typeof value === "string") push(value, "token");
  }
  // 物理名の構成語(ADMIN.EMPLOYEE → employee / DEPARTMENT_ID → department)も候補にする
  const technical = normalizeGroundingText(node.technical_name ?? "");
  for (const part of technical.split(/[._]/)) {
    if (part.length >= 3) variants.push({ text: part, via: "technical" });
  }
  return variants;
}

function isEligibleNode(node: OntologyNode): boolean {
  return GROUNDING_ENTITY_KINDS.has(node.kind) || GROUNDING_ATTRIBUTE_KINDS.has(node.kind);
}

/**
 * 質問文をオントロジーノードへ照合し、スコア降順の候補を返す。
 * 同一ノードは最高スコアの 1 候補に集約する。
 */
export function matchQuestionToNodes(
  graph: OntologyGraph,
  question: string
): { candidates: GroundingCandidate[]; normalizedQuestion: string; aggregate: boolean } {
  const normalizedQuestion = normalizeGroundingText(question);
  if (!normalizedQuestion) {
    return { candidates: [], normalizedQuestion, aggregate: false };
  }

  const eligibleNodes = graph.nodes.filter(isEligibleNode);
  // variant 文字列 → 同名を持つノード群(業務概念と物理表が同じ物理名を持つケースを両方拾う)
  const variantOwners = new Map<string, { via: GroundingMatchVia; nodes: OntologyNode[] }>();
  for (const node of eligibleNodes) {
    for (const variant of nodeNameVariants(node)) {
      const owner = variantOwners.get(variant.text);
      if (owner) {
        if (!owner.nodes.includes(node)) owner.nodes.push(node);
      } else {
        variantOwners.set(variant.text, { via: variant.via, nodes: [node] });
      }
    }
  }

  const best = new Map<string, GroundingCandidate>();
  const adopt = (candidate: GroundingCandidate) => {
    const current = best.get(candidate.node.id);
    if (!current || candidate.score > current.score) best.set(candidate.node.id, candidate);
  };

  // --- Phase A: 名称の包含一致(最長一致でスパンを消費) --------------------------------
  const variantTexts = [...variantOwners.keys()].sort(
    (a, b) => b.length - a.length || a.localeCompare(b)
  );
  let remaining = normalizedQuestion;
  for (const text of variantTexts) {
    const start = remaining.indexOf(text);
    if (start < 0) continue;
    remaining = remaining.split(text).join(" ".repeat(text.length));
    const owner = variantOwners.get(text);
    if (!owner) continue;
    const score = text === normalizedQuestion ? 1 : 0.9;
    for (const node of owner.nodes) {
      adopt({
        node,
        score,
        matchedText: text,
        span: { start, end: start + text.length },
        via: owner.via,
      });
    }
  }

  // --- Phase B: トークンの前方一致/部分一致 ------------------------------------------
  for (const token of tokenizeGroundingQuestion(remaining)) {
    for (const [text, owner] of variantOwners) {
      let score = 0;
      if (text.startsWith(token)) {
        score = 0.8;
      } else if (token.length >= 2 && text.includes(token)) {
        score = 0.65;
      } else if (text.length >= 2 && token.includes(text)) {
        score = 0.6 * (text.length / token.length);
      }
      if (score < GROUNDING_SCORE_THRESHOLD) continue;
      const start = normalizedQuestion.indexOf(token);
      for (const node of owner.nodes) {
        adopt({
          node,
          score,
          matchedText: token,
          span: start >= 0 ? { start, end: start + token.length } : null,
          via: "token",
        });
      }
    }
  }

  const candidates = [...best.values()].sort(
    (a, b) =>
      b.score - a.score ||
      b.matchedText.length - a.matchedText.length ||
      a.node.id.localeCompare(b.node.id)
  );
  // 集計判定は名称消費後の残り文に対して行う(「売上合計」というノード名の
  // 「合計」を集計意図と誤検出しない)。
  const aggregate = detectAggregateQuestion(remaining);
  return { candidates, normalizedQuestion, aggregate };
}
