/**
 * 回答の段落の印の記録（#2738。契約は platform/contracts/answer-passages/passage-marks.json）。
 *
 * 回答の検証は、根拠と矛盾する段落（`removed_passages`）だけを本文から外し、確かめられない段落（`unverified`）と
 * 規則の検査で注意の要る段落（`warning`）は本文に残して印（`passage_marks`）を付け、回答全体の注意
 * （`answer_warnings`）を返す。RAG（回答の診断）と Agent（成果物「資料との照合」）が同じ形で返し、画面は
 * ここで読んで共有の `AnswerSections` で描く。本文の文字列に印の記号は埋め込まれていない（段落の照合は `text`）。
 *
 * 3 つの項目がどれも無い記録（この形より前に保存した回答）は null を返し、呼び出し側は今までの表示のままにする
 * （保存済みの回答は書き換えない）。読めない項目は無いものとして扱う。
 */

export type AnswerPassageMarkState = "unverified" | "warning";

export interface AnswerPassageReason {
  /** 理由の種類（契約の `reason_codes`。画面の文言には使わない）。 */
  code: string;
  /** 利用者に見せる短い理由（backend の文をそのまま出す）。 */
  label: string;
  /** 監査・規則の理由（任意）。 */
  detail: string;
}

export interface AnswerPassageMark {
  /** 回答の本文の段落の原文（段落の照合に使う）。 */
  text: string;
  state: AnswerPassageMarkState;
  reasons: AnswerPassageReason[];
}

export interface AnswerRemovedPassage {
  text: string;
  reasons: AnswerPassageReason[];
}

export interface AnswerWarning {
  code: string;
  label: string;
  detail: string;
}

export interface AnswerPassageRecords {
  marks: AnswerPassageMark[];
  removed: AnswerRemovedPassage[];
  warnings: AnswerWarning[];
}

export interface AnswerPassageCounts {
  unverified: number;
  warning: number;
  removed: number;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record).filter((item): item is Record<string, unknown> => item !== null) : [];
}

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function reasons(value: unknown): AnswerPassageReason[] {
  return records(value).flatMap((item) => {
    const label = str(item.label);
    const code = str(item.code);
    if (!label && !code) return [];
    return [{ code, label: label || code, detail: str(item.detail) }];
  });
}

// 引用表示だけで除く書式。原文のtextや保存記録は変更しない。
const DISPLAY_LINE_MARK = /^\s*(?:#{1,6}\s+|[-*+]\s+|[・•]\s*|[0-9０-９]{1,3}[.)．）]\s+)/gm;
const DISPLAY_EVIDENCE_REF = /\s*[【〔［[]\s*(?:証拠|根拠|出典|evidence|source)[^】〕］\]\n]{0,200}[】〕］\]]/gi;

/** 引用の見える本文。不可視文字や書式だけの記録を項目・件数へ変換しない。 */
export function answerPassageDisplayText(text: string): string {
  return text
    .replace(/\p{Default_Ignorable_Code_Point}/gu, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(DISPLAY_LINE_MARK, "")
    .replace(/^[ \t]*(?:[-*_]\s*){3,}$/gm, "")
    .replace(/^[ \t]*(?:`{3,}|#{1,6}|>)[ \t]*$/gm, "")
    .replace(/\*{2}|__|`/g, "")
    .replace(DISPLAY_EVIDENCE_REF, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** 表示・件数・移動先を、同じ可視項目の集合で組み立てる。 */
export function visibleAnswerPassageRecords(records: AnswerPassageRecords): AnswerPassageRecords {
  return {
    ...records,
    marks: records.marks.filter((item) => answerPassageDisplayText(item.text)),
    removed: records.removed.filter((item) => answerPassageDisplayText(item.text)),
  };
}

/**
 * 回答の記録（RAG の回答の診断・Agent の成果物「資料との照合」の内容）から、段落の印・外した段落・回答全体の注意を
 * 読む。3 つの項目がどれも配列でなければ null（新しい記録の無い過去の回答）。
 */
export function parseAnswerPassageRecords(source: unknown): AnswerPassageRecords | null {
  const raw = record(source);
  if (!raw) return null;
  if (![raw.passage_marks, raw.removed_passages, raw.answer_warnings].some(Array.isArray)) return null;
  const marks = records(raw.passage_marks).flatMap((item): AnswerPassageMark[] => {
    const text = str(item.text);
    if (!answerPassageDisplayText(text)) return [];
    const itemReasons = reasons(item.reasons);
    // 印の状態は unverified を優先する（契約）。知らない値は注意（warning）として出す。
    const state: AnswerPassageMarkState = item.state === "unverified" ? "unverified" : "warning";
    return [{ text, state, reasons: itemReasons }];
  });
  const removed = records(raw.removed_passages).flatMap((item): AnswerRemovedPassage[] => {
    const text = str(item.text);
    return answerPassageDisplayText(text) ? [{ text, reasons: reasons(item.reasons) }] : [];
  });
  const warnings = records(raw.answer_warnings).flatMap((item): AnswerWarning[] => {
    const label = str(item.label);
    const code = str(item.code);
    if (!label && !code) return [];
    return [{ code, label: label || code, detail: str(item.detail) }];
  });
  return { marks, removed, warnings };
}

/** 印の数（バッジ）。 */
export function answerPassageCounts(records: AnswerPassageRecords | null | undefined): AnswerPassageCounts {
  if (!records) return { unverified: 0, warning: 0, removed: 0 };
  const visible = visibleAnswerPassageRecords(records);
  return {
    unverified: visible.marks.filter((mark) => mark.state === "unverified").length,
    warning: visible.marks.filter((mark) => mark.state === "warning").length,
    removed: visible.removed.length,
  };
}

/** 記録に出すもの（印・外した段落・回答全体の注意）が 1 つでもあるか。 */
export function hasAnswerPassageRecords(records: AnswerPassageRecords | null | undefined): boolean {
  if (!records) return false;
  const visible = visibleAnswerPassageRecords(records);
  return Boolean(visible.marks.length || visible.removed.length || visible.warnings.length);
}

// 行の先頭の箇条書き・番号・見出しの印（「- 」「・」「1. 」「（2）」「## 」）。
const LINE_MARK = /^(?:#{1,6}\s+|[-*+・•]\s*|[0-9０-９]{1,3}[.)．）]\s*|[（(][0-9０-９]{1,3}[）)]\s*)/;
// 強調の印。
const EMPHASIS = /\*{1,2}|__|`/g;
// RAG の本文の根拠の行（「根拠：文書 p.3」）。段落の文ではない。
const CITATION_LINE = /^根拠[:：]/;

/**
 * 段落の照合に使う形（NFKC・行の先頭の印・強調の印・根拠の行・空白を除く）。本文の段落の原文と印の `text` の
 * 書き方の差（箇条書きの印の有無・改行・空白）で照合を外さないため。
 */
export function answerPassageKey(text: string): string {
  return text
    .normalize("NFKC")
    .split("\n")
    .map((line) => line.trim().replace(LINE_MARK, ""))
    .filter((line) => line && !CITATION_LINE.test(line))
    .join("")
    .replace(EMPHASIS, "")
    .replace(/\s+/g, "");
}

/** 段落の印をまとめたもの（1 つの段落に複数の印が当たれば、理由を並べ、状態は unverified を優先する）。 */
export interface AnswerPassageMarkGroup {
  state: AnswerPassageMarkState;
  reasons: AnswerPassageReason[];
  /** 当たった印の位置（`records.marks` の index）。 */
  marks: number[];
}

// 短すぎる文の包含では照合しない（「はい。」が別の段落に当たらないように）。
const MIN_CONTAINED_KEY = 4;

/**
 * 本文の段落（表示の順）に印を当てる。印ごとに、原文が同じ段落 → 原文を含む段落 → 原文に含まれる段落の順で
 * 最初の段落を選ぶ（印の `text` は文の単位のことがあり、本文の段落は複数の文を持つことがある）。
 * 戻り値の `groups` は段落ごとのまとめ（当たらなければ null）、`targets` は印ごとの段落の位置（当たらなければ -1）。
 */
export function matchAnswerPassageMarks(
  passages: readonly (string | null | undefined)[],
  marks: readonly AnswerPassageMark[],
): { groups: (AnswerPassageMarkGroup | null)[]; targets: number[] } {
  const keys = passages.map((text) => (text ? answerPassageKey(text) : ""));
  const groups: (AnswerPassageMarkGroup | null)[] = keys.map(() => null);
  const targets = marks.map((mark, markIndex) => {
    const key = answerPassageKey(mark.text);
    if (!key) return -1;
    let index = keys.findIndex((value) => value === key);
    if (index < 0 && key.length >= MIN_CONTAINED_KEY) index = keys.findIndex((value) => value.includes(key));
    if (index < 0) index = keys.findIndex((value) => value.length >= MIN_CONTAINED_KEY * 2 && key.includes(value));
    if (index < 0) return -1;
    const group = groups[index] ?? { state: mark.state, reasons: [], marks: [] };
    if (mark.state === "unverified") group.state = "unverified";
    for (const reason of mark.reasons) {
      if (!group.reasons.some((item) => item.code === reason.code && item.label === reason.label && item.detail === reason.detail)) {
        group.reasons.push(reason);
      }
    }
    group.marks.push(markIndex);
    groups[index] = group;
    return index;
  });
  return { groups, targets };
}
