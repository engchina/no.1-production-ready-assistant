import { evidenceLabels, type EvidenceListItem, type EvidenceTag } from "@production-ready/ui";
import { Layers, Paperclip } from "lucide-react";

import type { RetrievedChunk } from "@/lib/api";
import {
  citationAnswerUse,
  citationMetadataChips,
  citationModelUsed,
  citationParentUsed,
  citationSourceUrl,
  type CitationMetadataChip,
} from "@/lib/chunk-metadata";
import { t } from "@/lib/i18n";
import { integerMetadataValue } from "@/lib/table-cell-focus";

/** 根拠の文言（3 製品で共通。#2328）。 */
export const EVIDENCE_LABELS = evidenceLabels();

/** RAG の根拠 → 共通の根拠のデータの契約（#2328）。 */
export function citationEvidenceItem(chunk: RetrievedChunk): EvidenceListItem {
  const recipeSlot = integerMetadataValue(chunk.metadata.recipe_slot_no);
  const tags: EvidenceTag[] = [];
  const support = chunk.metadata.evidence_claim_support;
  if (citationModelUsed(chunk) === true && support != null && typeof support === "object" && !Array.isArray(support)) {
    const observation = support as Record<string, unknown>;
    if (observation.status !== "supported") {
      if (observation.policy === "diagnostic_only") tags.push({ id: "claim-support-policy", value: t("search.citation.supportDiagnostic"), tone: "neutral" });
      const key = observation.execution === "not_run" ? "search.citation.supportNotRun" : observation.execution === "error" ? "search.citation.supportError" : ["timeout", "timed_out"].includes(String(observation.execution)) ? "search.citation.supportTimeout" : observation.execution === "completed" ? "search.citation.supportCompletedUnknown" : observation.execution === "partial" ? "search.citation.supportPartial" : "search.citation.supportUnknown";
      tags.push({ id: "claim-support", value: t(key), tone: "neutral" });
    }
  }
  if (recipeSlot) tags.push({ id: "recipe", value: t("documents.recipes.name", { slot: recipeSlot }), icon: Layers });
  if (chunk.category_name) tags.push({ id: "category", value: chunk.category_name, tone: "info" });
  // チャットの添付の根拠（#2517）。文書の表に無いので、原文のプレビューは開かない。
  const attachment = isAttachmentCitation(chunk);
  if (attachment) tags.push({ id: "attachment", value: t("chat.attachments.evidenceSource"), icon: Paperclip });
  return {
    key: chunk.chunk_id,
    fileName: chunk.file_name ?? chunk.document_id,
    text: chunk.text,
    // AI に渡した箇所のうち、回答で引用したもの（#1208）。該当確認済みの記載と参考の記載を分ける（#2040）。
    usedInAnswer: citationModelUsed(chunk),
    answerUse: citationAnswerUse(chunk),
    // 回答がまとまり（親）だけを参照した箇所（「回答で引用」とは分ける。#2710）。
    usedAsContext: citationParentUsed(chunk),
    // 新しい版に置き換えた文書（旧版）の根拠（#1248。include_superseded で含めたときだけ出る）。
    superseded: chunk.metadata.document_superseded === true,
    routeBadges: citationRetrievalBadges(chunk),
    score: chunk.score,
    rerankScore: chunk.rerank_score,
    chips: citationMetadataChips(chunk.metadata).map((chip) => ({
      id: chip.id,
      label: chipLabel(chip),
      value: chipValue(chip),
    })),
    tags,
    // 原典の URL（XML ツリー JSON の文書。#2024）。根拠を原典で確かめる。
    sourceUrl: citationSourceUrl(chunk.metadata),
    sourceUnavailable: attachment,
  };
}

/** チャットの添付の根拠か（backend の `chat_attachment_id` の印。#2517）。 */
export function isAttachmentCitation(chunk: RetrievedChunk): boolean {
  return typeof chunk.metadata.chat_attachment_id === "string" && chunk.metadata.chat_attachment_id !== "";
}

function citationRetrievalBadges(chunk: RetrievedChunk): string[] {
  const vectorRank = integerMetadataValue(chunk.metadata.vector_rank);
  const keywordRank = integerMetadataValue(chunk.metadata.keyword_rank);
  const rerankRank = integerMetadataValue(chunk.metadata.rerank_rank);
  const badges: string[] = [];
  if (vectorRank != null && keywordRank != null) badges.push(t("search.citation.badge.both"));
  if (vectorRank != null) badges.push(t("search.citation.badge.vector", { rank: vectorRank }));
  if (keywordRank != null) badges.push(t("search.citation.badge.keyword", { rank: keywordRank }));
  if (rerankRank != null) badges.push(t("search.citation.badge.rerank", { rank: rerankRank }));
  if (chunk.metadata.context_role === "evidence" || badges.length > 0) {
    badges.push(EVIDENCE_LABELS.evidenceBadge);
  }
  return badges;
}
function chipValue(chip: CitationMetadataChip): string {
  switch (chip.id) {
    case "context_header":
      return chip.value;
    case "context_header_enabled":
      return t(chip.value === "true" ? "search.citation.contextHeaderOn" : chip.value === "false" ? "search.citation.contextHeaderOff" : "search.citation.contextHeaderUnknown");
    case "citation_label":
      return chip.value;
    case "page":
      // 印刷のページ番号があれば物理頁と並べる（「p.12（資料の表記: 3-4）」。#1287）。
      return chip.printed
        ? t("search.citation.pageWithLabel", { page: chip.value, label: chip.printed })
        : t("search.citation.page", { page: chip.value });
    case "content_kind":
      return t("search.citation.contentKindValue", {
        kind: contentKindLabel(chip.value),
      });
    case "section_title":
      return t("search.citation.sectionTitleValue", { title: chip.value });
    case "section_path":
      return t("search.citation.sectionPathValue", { path: chip.value });
    case "chunk_profile":
      return t("search.citation.profileValue", { profile: chip.value });
  }
}

function chipLabel(chip: CitationMetadataChip): string {
  switch (chip.id) {
    case "context_header":
      return t("search.citation.contextHeaderLabel");
    case "context_header_enabled":
      return t("search.citation.contextHeaderStatusLabel");
    case "citation_label":
      return t("search.citation.citationLabelLabel");
    case "page":
      return t("search.citation.pageLabel");
    case "content_kind":
      return t("search.citation.contentKindLabel");
    case "section_title":
      return t("search.citation.sectionTitleLabel");
    case "section_path":
      return t("search.citation.sectionPathLabel");
    case "chunk_profile":
      return t("search.citation.profileLabel");
  }
}

function contentKindLabel(kind: string): string {
  switch (kind) {
    case "text":
      return t("search.filters.contentKind.text");
    case "list":
      return t("search.filters.contentKind.list");
    case "table":
      return t("search.filters.contentKind.table");
    case "figure":
      return t("search.filters.contentKind.figure");
    case "equation":
      return t("search.filters.contentKind.equation");
    case "code":
      return t("search.filters.contentKind.code");
    case "email":
      return t("search.filters.contentKind.email");
    case "slide":
      return t("search.filters.contentKind.slide");
    case "sheet":
      return t("search.filters.contentKind.sheet");
    case "field":
      return t("search.filters.contentKind.field");
    case "section_summary":
      return t("search.filters.contentKind.section_summary");
    default:
      return kind;
  }
}
