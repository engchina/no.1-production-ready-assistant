import { evidenceCountLabel } from "@production-ready/ui";

import type { RetrievedChunk } from "./api";

export type CitationMetadataChipId =
  | "page"
  | "content_kind"
  | "section_title"
  | "section_path"
  | "chunk_profile"
  | "context_header"
  | "context_header_enabled";

export interface CitationMetadataChip {
  id: CitationMetadataChipId;
  value: string;
  /**
   * page のときだけ: 資料に印刷されたページ番号（PDF のページラベル。#1244）。物理頁と違うときだけ
   * 入る（backend の `page_label_start` / `page_label_end`。無ければ省く）。
   */
  printed?: string;
}

/** 引用カードで表示する低 cardinality metadata を抽出する。 */
export function citationMetadataChips(
  metadata: RetrievedChunk["metadata"]
): CitationMetadataChip[] {
  const chips: CitationMetadataChip[] = [];
  const page = pageRange(metadata);
  if (page) {
    const printed = printedPageRange(metadata, page);
    chips.push(printed ? { id: "page", value: page, printed } : { id: "page", value: page });
  }
  for (const id of ["content_kind", "section_title", "section_path", "chunk_profile"] as const) {
    const value = stringMetadata(metadata, id);
    if (value) chips.push({ id, value });
  }
  const header = stringMetadata(metadata, "context_header");
  if (header) chips.push({ id: "context_header", value: header });
  const enabled = metadata.context_header_enabled;
  chips.push({ id: "context_header_enabled", value: typeof enabled === "boolean" ? String(enabled) : header ? "true" : "unknown" });
  return chips;
}

function pageRange(metadata: RetrievedChunk["metadata"]): string {
  const start = integerMetadata(metadata, "page_start");
  const end = integerMetadata(metadata, "page_end") ?? start;
  if (start == null) return "";
  return end != null && end > start ? `${start}-${end}` : String(start);
}

/**
 * 資料に印刷されたページ番号の範囲（#1244）。backend は物理頁と違うラベルがあるときだけ
 * `page_label_start` / `page_label_end` を入れる。印刷の番号は「3-4」のようにハイフンを含むことが
 * あるため、範囲は「〜」でつなぐ。物理頁と同じ表記になるときは出さない（空文字）。
 */
export function printedPageRange(metadata: RetrievedChunk["metadata"], physical: string): string {
  const start = stringMetadata(metadata, "page_label_start");
  if (!start) return "";
  const end = stringMetadata(metadata, "page_label_end") || start;
  const printed = end !== start ? `${start}〜${end}` : start;
  return printed === physical ? "" : printed;
}

function integerMetadata(metadata: RetrievedChunk["metadata"], key: string): number | null {
  const value = metadata[key];
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.trunc(value);
}

function stringMetadata(metadata: RetrievedChunk["metadata"], key: string): string {
  const value = metadata[key];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * 回答で引用した箇所か（#1208）。回答エンジンは AI に渡した箇所（chunk）を全件 citations にし、
 * 回答で引用したものを `evidence_model_used` で示す。回答を作らない検索の結果は持たないので null。
 */
export function citationModelUsed(chunk: Pick<RetrievedChunk, "metadata">): boolean | null {
  const value = chunk.metadata.evidence_model_used;
  return typeof value === "boolean" ? value : null;
}

/**
 * 回答がこの箇所を含むまとまり（親）だけを参照したか（`evidence_parent_used`。#2710）。箇所そのものを引用した
 * ものは「回答で引用」なので false。
 */
export function citationParentUsed(chunk: Pick<RetrievedChunk, "metadata">): boolean {
  return citationModelUsed(chunk) !== true && chunk.metadata.evidence_parent_used === true;
}

/** 回答での使われ方（#2040）。confirmed は質問の条件への該当確認済みの記載、reference は参考の記載。 */
export type CitationAnswerUse = "confirmed" | "reference";

/**
 * 回答で引用した箇所の使われ方（`evidence_answer_use`。#2040）。回答で引用していない箇所と、
 * 使われ方を記録していない回答は null。
 */
export function citationAnswerUse(chunk: Pick<RetrievedChunk, "metadata">): CitationAnswerUse | null {
  if (citationModelUsed(chunk) !== true) return null;
  const value = chunk.metadata.evidence_answer_use;
  const support = chunk.metadata.evidence_claim_support;
  if (value === "confirmed" && support != null && typeof support === "object" && !Array.isArray(support) && (support as Record<string, unknown>).status !== "supported") return null;
  return value === "confirmed" || value === "reference" ? value : null;
}

/**
 * AI に渡した件数と内訳（#1208・#2040）。「AI に渡した 45 件 ／ 回答で引用 13 件 ／ うち該当確認済み
 * 10 件 ／ 参考 3 件」。書き方は 3 製品で共通の `evidenceCountLabel`（#2328・#2388）。回答で引用したかが分からない
 * （検索だけ）なら件数だけ、0 件の項目は出さない。
 */
export function citationCountLabel(citations: readonly Pick<RetrievedChunk, "metadata">[]): string {
  return evidenceCountLabel(
    citations.map((chunk) => ({
      usedInAnswer: citationModelUsed(chunk),
      answerUse: citationAnswerUse(chunk),
      usedAsContext: citationParentUsed(chunk),
    })),
  );
}
