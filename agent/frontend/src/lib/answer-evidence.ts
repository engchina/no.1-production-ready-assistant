import { evidenceLabels, type EvidenceChip, type EvidenceListItem } from "@production-ready/ui";
import { Paperclip } from "lucide-react";

import type { Artifact } from "@/lib/api";
import { evidenceLocation } from "@/lib/rag-evidence";
import { t } from "@/lib/i18n";

/**
 * 回答の根拠（成果物 kind=`answer_evidence`。#2328）。backend の `answer_evidence.answer_evidence` が回答の確定の
 * ときに作る（AI に渡した根拠・回答で引用した根拠・根拠の ID の印・段落ごとの根拠）。画面は RAG のチャットと同じ
 * 共有の部品（`AnswerEvidence`・`AnswerSections`）で出す。読めない項目は無いものとして扱う。
 */

export const ANSWER_EVIDENCE_KIND = "answer_evidence";

/** 根拠の文言（3 製品で共通）。 */
export const EVIDENCE_LABELS = evidenceLabels();

export interface AnswerEvidenceEntry {
  item: EvidenceListItem;
  documentId: string;
  chunkId: string;
  /** 根拠の行の場所（頁があれば頁、無ければ節）。 */
  citationLocation: string;
  /** 会話の添付の根拠か（#2554）。RAG の文書ではないので、原文のプレビューを開かない。 */
  attachment: boolean;
}

export interface AnswerEvidenceMarker {
  text: string;
  chunkIds: string[];
}

export interface AnswerEvidencePassage {
  text: string;
  chunkIds: string[];
}

/** 「参考情報」の根拠（根拠の原文と chunk。#2707）。 */
export interface AnswerEvidenceReference {
  quote: string;
  chunkIds: string[];
}

/**
 * 結び付けの方法（#2707）。`validation` は最終の検証の判定（裏付けた段落は「資料により確認できた事項」）、
 * `text` は検証が判定を出さなかったときの決定的な照合（照合で確かめていないので「参考情報」）、`partial` は
 * 検証が時間の上限までに一部の段落だけを確かめたとき（確かめ終わった段落は判定、確かめ終わらなかった段落の
 * 根拠は決定的な照合で「参考情報」。#2700）。
 */
export type AnswerEvidenceBinding = "validation" | "partial" | "text";

export interface AnswerEvidenceData {
  entries: AnswerEvidenceEntry[];
  markers: AnswerEvidenceMarker[];
  passages: AnswerEvidencePassage[];
  binding: AnswerEvidenceBinding;
  references: AnswerEvidenceReference[];
  /** 根拠に結び付いた出典の行（`text` のとき。本文には出さず「参考情報」の根拠の行で示す）。 */
  citationLines: string[];
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record).filter((item): item is Record<string, unknown> => item !== null) : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item !== "") : [];
}

/** 頁の表示（「p.3」「p.3–5」。印刷の頁番号があれば添える）。無ければ空。 */
function pageLabel(locator: Record<string, unknown> | null): string {
  const start = positiveInteger(locator?.page_start);
  if (start == null) return "";
  const end = positiveInteger(locator?.page_end);
  const page = end != null && end !== start ? t("evidence.pageRange", { start, end }) : t("evidence.page", { page: start });
  const printed = text(locator?.page_label_start).trim();
  return printed && printed !== String(start) ? t("evidence.pageWithLabel", { page, label: printed }) : page;
}

function chips(item: Record<string, unknown>): EvidenceChip[] {
  const locator = record(item.locator);
  const result: EvidenceChip[] = [];
  const sheet = locator && text(locator.sheet_name).trim() ? evidenceLocation(locator) : null;
  if (sheet) result.push({ id: "sheet", label: t("evidence.chip.sheet"), value: sheet });
  const page = pageLabel(locator);
  if (page) result.push({ id: "page", label: t("evidence.chip.page"), value: page });
  const section = text(item.section_title).trim();
  if (section) result.push({ id: "section_title", label: t("evidence.chip.section"), value: section });
  const path = strings(locator?.section_path);
  if (path.length) result.push({ id: "section_path", label: t("evidence.chip.sectionPath"), value: path.join(" > ") });
  return result;
}

/** 成果物「回答の根拠」の内容を、共有の部品に渡す形にする。 */
export function answerEvidenceData(content: Record<string, unknown> | null | undefined): AnswerEvidenceData {
  const entries = records(content?.items).flatMap((item): AnswerEvidenceEntry[] => {
    const documentId = text(item.document_id);
    const chunkId = text(item.chunk_id);
    if (!documentId || !chunkId) return [];
    const locator = record(item.locator);
    const cited = item.cited === true;
    const answerUse = item.answer_use === "confirmed" || item.answer_use === "reference" ? item.answer_use : null;
    const fileName = text(item.file_name) || documentId;
    const page = pageLabel(locator);
    // 会話の添付の根拠（backend の `chat_attachment_id` の印。#2554）。RAG のチャットの添付の根拠と同じ扱い。
    const attachment = text(item.chat_attachment_id) !== "";
    return [
      {
        documentId,
        chunkId,
        citationLocation:
          page ||
          text(item.section_title).trim() ||
          strings(locator?.section_path).join(" > ") ||
          (attachment ? (evidenceLocation(locator) ?? "") : ""),
        attachment,
        item: {
          key: chunkId,
          fileName,
          text: text(item.excerpt),
          usedInAnswer: cited,
          answerUse: cited ? answerUse : null,
          superseded: item.superseded === true,
          // AI に渡した箇所の印（RAG の「Evidence」と同じ）。検索の順位は MCP の根拠に無いので出さない。
          routeBadges: [EVIDENCE_LABELS.evidenceBadge],
          score: finite(item.score),
          rerankScore: finite(item.rerank_score),
          chips: chips(item),
          tags: attachment ? [{ id: "attachment", value: t("chat.attachments.evidenceSource"), icon: Paperclip }] : [],
          sourceUnavailable: attachment,
        },
      },
    ];
  });
  const pairs = (value: unknown) =>
    records(value).flatMap((item) => {
      const body = text(item.text);
      const chunkIds = strings(item.chunk_ids);
      return body && chunkIds.length ? [{ text: body, chunkIds }] : [];
    });
  const references = records(content?.references).flatMap((item) => {
    const quote = text(item.quote).trim();
    const chunkIds = strings(item.chunk_ids);
    return quote && chunkIds.length ? [{ quote, chunkIds }] : [];
  });
  return {
    entries,
    markers: pairs(content?.markers),
    passages: pairs(content?.passages),
    binding: content?.binding === "text" || content?.binding === "partial" ? content.binding : "validation",
    references,
    citationLines: strings(content?.citation_lines),
  };
}

/** Run の成果物から回答の根拠を取り出す（無ければ null）。 */
export function runAnswerEvidence(artifacts: readonly Artifact[]): AnswerEvidenceData | null {
  for (const artifact of [...artifacts].reverse()) {
    if (artifact.kind === ANSWER_EVIDENCE_KIND) {
      const data = answerEvidenceData(artifact.content);
      return data.entries.length ? data : null;
    }
  }
  return null;
}

/**
 * 成果物「検索の根拠」（`rag_search` の出力。kind=`rag_evidence`）の根拠を、共有の部品に渡す形にする（実行の詳細）。
 * 回答で引用 = RAG の回答が引用した根拠（`used_in_answer`）。
 */
export function ragArtifactEvidence(content: Record<string, unknown>): AnswerEvidenceData | null {
  const items = records(content.evidence).map((item) => ({
    ...item,
    cited: item.used_in_answer === true,
    section_title: text(record(item.section)?.title),
  }));
  const data = answerEvidenceData({ items });
  return data.entries.length ? data : null;
}

/** 根拠の行の文（「根拠：文書 A.pdf p.3」）。 */
export function citationLabel(entry: AnswerEvidenceEntry): string {
  return EVIDENCE_LABELS.citation(entry.item.fileName, entry.citationLocation);
}
