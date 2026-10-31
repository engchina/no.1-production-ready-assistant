// 「SQL の要件（AI の理解）」の見せ方の決まりごと（#1718）。SQL 生成の画面とチャットの回答前の確認が同じ
// 部品（components/RequirementClarification.tsx）で使う。値のある部分だけを 1 行ずつの一覧にし、値の無い
// 部分は「指定なし」の 1 行にまとめる。状態は 確定 / AI の推測 / 要確認 の 3 つだけにする。
import { t, type I18nKey } from "../../lib/i18n";

import type { RequirementItem, RequirementSection } from "./ontology/types";

/** 一覧の行の状態。`unspecified` は一覧に出さず「指定なし」の 1 行にまとめる。 */
export type RequirementItemKind = "confirmed" | "inferred" | "needs_answer" | "unspecified";

/** 「条件を追加」の項目の ID（backend の `ADD_ITEM_PREFIX`）。`add:<section>` で回答を送る。 */
export const ADD_ITEM_PREFIX = "add:";

/** SQL の部分の短い名前（一覧の項目名・指定なしの 1 行・要約）。 */
const SECTION_PART_KEY: Record<RequirementSection, I18nKey> = {
  select: "nl2sql.clarification.part.select",
  from: "nl2sql.clarification.part.from",
  join: "nl2sql.clarification.part.join",
  period: "nl2sql.clarification.part.period",
  where: "nl2sql.clarification.part.where",
  group_by: "nl2sql.clarification.part.groupBy",
  order_by: "nl2sql.clarification.part.orderBy",
  limit: "nl2sql.clarification.part.limit",
  subquery: "nl2sql.clarification.part.subquery",
  other: "nl2sql.clarification.part.other",
};

/** 一覧の並び（使用テーブル → 表示列 → 結合 → 期間 → 絞り込み → …）。 */
const SECTION_ORDER: RequirementSection[] = [
  "from",
  "select",
  "join",
  "period",
  "where",
  "group_by",
  "order_by",
  "limit",
  "subquery",
  "other",
];

/** 「指定なし」の 1 行に並べる部分。件数の上限は質問が求めるときだけ要件にするため、ここには並べない。 */
const UNSPECIFIED_SECTIONS: RequirementSection[] = [
  "join",
  "period",
  "where",
  "group_by",
  "order_by",
  "subquery",
  "other",
];

/** 「条件を追加」で足せる部分（SELECT・FROM は必ず値があり、「直す」で直す）。 */
export const ADDABLE_SECTIONS: RequirementSection[] = [
  "join",
  "period",
  "where",
  "group_by",
  "order_by",
  "limit",
  "subquery",
  "other",
];

const KIND_LABEL_KEY: Record<Exclude<RequirementItemKind, "unspecified">, I18nKey> = {
  confirmed: "nl2sql.clarification.kind.confirmed",
  inferred: "nl2sql.clarification.kind.inferred",
  needs_answer: "nl2sql.clarification.kind.needsAnswer",
};

export const KIND_BADGE_VARIANT: Record<Exclude<RequirementItemKind, "unspecified">, "success" | "info" | "warning"> = {
  confirmed: "success",
  inferred: "info",
  needs_answer: "warning",
};

export function sectionPartLabel(section: RequirementSection): string {
  return t(SECTION_PART_KEY[section]);
}

export function requirementKindLabel(kind: Exclude<RequirementItemKind, "unspecified">): string {
  return t(KIND_LABEL_KEY[kind]);
}

/**
 * 項目の状態。答えた項目は「確定」、不足・あいまいは「要確認」。値の無い確定・推測はその句を使わない
 * 「指定なし」で、「AI の推測」とは示さない（値の無い推測は直す対象が無い）。
 */
export function requirementItemKind(item: RequirementItem): RequirementItemKind {
  if (item.answered) return "confirmed";
  if (item.status === "missing" || item.status === "ambiguous") return "needs_answer";
  if (!(item.value_ja ?? "").trim() && !(item.sql_hint ?? "").trim()) return "unspecified";
  return item.status === "confirmed" ? "confirmed" : "inferred";
}

export interface RequirementOverview {
  /** 一覧に 1 行ずつ出す項目（値のある項目と要確認の項目）。 */
  listed: { item: RequirementItem; kind: Exclude<RequirementItemKind, "unspecified"> }[];
  /** 値も確認も無い部分（「指定なし: …」の 1 行）。 */
  unspecifiedSections: RequirementSection[];
  /** 「条件を追加」で足せる部分（値も確認も無い部分）。 */
  addableSections: RequirementSection[];
  needsAnswerCount: number;
  /** 畳んだときの要約の 1 行（値のある項目の「項目名 値」を ／ でつなぐ）。 */
  summary: string;
}

function sectionRank(section: RequirementSection): number {
  return SECTION_ORDER.indexOf(section);
}

export function requirementOverview(items: RequirementItem[]): RequirementOverview {
  const listed = items
    .map((item, index) => ({ item, index, kind: requirementItemKind(item) }))
    .filter(
      (entry): entry is { item: RequirementItem; index: number; kind: Exclude<RequirementItemKind, "unspecified"> } =>
        entry.kind !== "unspecified"
    )
    .sort((a, b) => sectionRank(a.item.section) - sectionRank(b.item.section) || a.index - b.index)
    .map(({ item, kind }) => ({ item, kind }));
  const used = new Set(listed.map((entry) => entry.item.section));
  const summary = listed
    .filter((entry) => entry.kind !== "needs_answer")
    .map((entry) => `${sectionPartLabel(entry.item.section)} ${(entry.item.value_ja ?? "").trim() || entry.item.sql_hint}`)
    .join(" ／ ");
  return {
    listed,
    unspecifiedSections: UNSPECIFIED_SECTIONS.filter((section) => !used.has(section)),
    addableSections: ADDABLE_SECTIONS.filter((section) => !used.has(section)),
    needsAnswerCount: listed.filter((entry) => entry.kind === "needs_answer").length,
    summary,
  };
}

/** 「条件を追加」の入力に使う仮の項目（回答は `add:<section>` で送り、backend が項目を作る）。 */
export function addConditionItem(section: RequirementSection): RequirementItem {
  const part = sectionPartLabel(section);
  return {
    id: `${ADD_ITEM_PREFIX}${section}`,
    key: `add_${section}`,
    section,
    label_ja: part,
    value_ja: "",
    status: "missing",
    question_ja: t("nl2sql.clarification.addQuestion", { part }),
    answer_kind: "free_text",
    candidates: [],
    source: "user",
    tables: [],
    columns: [],
    relationship_ids: [],
    answered: false,
  };
}

/** 一覧の行で、候補を押すだけで答えられるか（1 つを選ぶ質問で、候補がある）。 */
export function canAnswerWithCandidate(item: RequirementItem): boolean {
  return item.answer_kind === "single_select" && item.candidates.length > 0;
}

/**
 * 確認のカード（確認の質問が無い・全部に答えた後。#1761）で選び直せる対象の表。AI が決めた表（確定・推測で、
 * まだ答えていない FROM）に、backend が業務プロファイルのほかの表を候補として添えたときだけ返す。
 */
export function confirmTargetItem(items: RequirementItem[]): RequirementItem | null {
  return (
    items.find(
      (item) =>
        item.section === "from" &&
        !item.answered &&
        (item.status === "confirmed" || item.status === "inferred") &&
        item.candidates.length > 1
    ) ?? null
  );
}

/** 対象の表の候補のうち、今の要件の表（AI が決めた表）に当たる候補の ID。 */
export function currentCandidateId(item: RequirementItem): string {
  const tables = [...item.tables].sort().join(",");
  return item.candidates.find((candidate) => [...candidate.tables].sort().join(",") === tables)?.id ?? "";
}

/**
 * 確認のカードで送る直し（#1761）。対象の表を別の候補に選び直した → その FROM の項目への回答。
 * 「その他（自由入力）」に書いた → `add:other` への自由入力（AI の再検査が解釈を直す）。どちらも無ければ null
 * （今の要件のまま SQL を生成できる）。
 */
export function confirmCorrection(
  target: RequirementItem | null,
  selectedCandidateIds: string[],
  freeText: string
): { item_id: string; selected_candidate_ids: string[]; free_text: string } | null {
  const text = freeText.trim();
  if (text) return { item_id: `${ADD_ITEM_PREFIX}other`, selected_candidate_ids: [], free_text: text };
  const selected = selectedCandidateIds[0] ?? "";
  if (target && selected && selected !== currentCandidateId(target)) {
    return { item_id: target.id, selected_candidate_ids: [selected], free_text: "" };
  }
  return null;
}

/**
 * 確認のカードの直しの種類（#1776）。生成のボタンが押せない理由の文を選ぶのに使う。自由入力（`add:other`）は
 * "free_text"、対象の表の選び直しは "table"、直しが無ければ null。候補を選ぶと自由入力は空になり、自由入力を
 * 書くと候補の選択は外れるので、2 つが同時に未送信になることは無い（あれば自由入力を優先して送る）。
 */
export function confirmCorrectionKind(
  correction: ReturnType<typeof confirmCorrection>
): "free_text" | "table" | null {
  if (!correction) return null;
  return correction.free_text ? "free_text" : "table";
}
