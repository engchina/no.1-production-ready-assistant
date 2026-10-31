import { EvidenceCard, EvidenceSourceButton, useEvidenceSource } from "@production-ready/ui";

import { FeedbackControls } from "@/components/feedback/FeedbackControls";
import type {
  FeedbackContentSnapshot,
  FeedbackSourceSurface,
  RetrievedChunk,
} from "@/lib/api";
import { citationModelUsed } from "@/lib/chunk-metadata";

import { citationEvidenceItem, EVIDENCE_LABELS, isAttachmentCitation } from "./citation-evidence";
import { CitationSourceSheet } from "./CitationSourceSheet";

export { citationEvidenceItem, EVIDENCE_LABELS } from "./citation-evidence";

/** 根拠のカードの操作の設定（チャット・RAG 検索・回答の記録・実行履歴で渡す）。 */
export interface CitationCardOptions {
  traceId?: string | null;
  searchAnswerProfileId?: string | null;
  sourceSurface?: FeedbackSourceSurface;
  messageId?: string | null;
  contentSnapshot?: FeedbackContentSnapshot | null;
  /**
   * 引用箇所のプレビューを出すか。文書を読む権限（backend の `_DOCUMENT_VIEW`）が無い利用者の画面
   * （実行履歴だけの利用者。#1890）では false にする（押すと 403 で権限なしの画面へ移るため）。
   */
  preview?: boolean;
  /**
   * 引用箇所のパネルの見出し（#2360）。`answer` は「回答の根拠」、`search` は回答を作らない検索の「検索で見つかった
   * 箇所」。省略時は回答で引用したかが分かる箇所を回答の根拠、分からない箇所を検索の結果として開く。
   */
  sourceKind?: "answer" | "search";
}

/**
 * 引用チャンク1件の表示。retrieval 由来の score/metadata を併記する。見た目は 3 製品で共通の `EvidenceCard`
 * （#2328）で、ここは RAG の根拠（`RetrievedChunk`）をデータの契約に写し、引用箇所のプレビュー（右のサイドパネル。
 * #2360）と根拠の評価を足す。
 */
export function CitationCard({ chunk, index, ...options }: CitationCardOptions & { chunk: RetrievedChunk; index: number }) {
  const source = useEvidenceSource<RetrievedChunk>();
  const preview = options.preview ?? true;
  const item = citationEvidenceItem(chunk);
  return (
    <EvidenceCard
      item={item}
      index={index}
      labels={EVIDENCE_LABELS}
      // 「根拠を表示」は文書を読む権限がある画面（preview）だけ。
      leadingActions={
        preview && !item.sourceUnavailable ? (
          <EvidenceSourceButton fileName={item.fileName} onOpen={() => source.open(chunk)} labels={EVIDENCE_LABELS} />
        ) : undefined
      }
      actions={<CitationActions chunk={chunk} {...options} />}
    >
      {preview && source.mounted ? (
        <CitationSourceSheet
          chunk={source.shown}
          index={index}
          open={source.selected !== null}
          onClose={source.close}
          // 指定が無ければ、回答で引用したかが分からない箇所（比較の検索結果など）は検索の結果として開く。
          kind={options.sourceKind ?? (citationModelUsed(chunk) === null ? "search" : "answer")}
        />
      ) : null}
    </EvidenceCard>
  );
}

/**
 * 根拠のカードの操作の行の右の組（根拠の評価）。左の組の「根拠を表示」は共通の部品が並べる（#2388）。根拠は画面を
 * 移動せず右のサイドパネルで見せる（#2360）。
 */
export function CitationActions({
  chunk,
  traceId,
  searchAnswerProfileId,
  sourceSurface = "search",
  messageId = null,
  contentSnapshot = null,
}: CitationCardOptions & { chunk: RetrievedChunk }) {
  // チャットの添付の根拠（#2517）は文書の根拠のフィードバックの対象にしない（文書の表に無い）。
  if (!traceId || !searchAnswerProfileId || isAttachmentCitation(chunk)) return null;
  return (
    <FeedbackControls
      traceId={traceId}
      searchAnswerProfileId={searchAnswerProfileId}
      targetType="citation"
      sourceSurface={sourceSurface}
      documentId={chunk.document_id}
      chunkId={chunk.chunk_id}
      messageId={messageId}
      contentSnapshot={contentSnapshot}
      compact
    />
  );
}

/** chunk_id(document:chunk_set:index)から chunk_set(variant)id を取り出す。無ければ null。 */
export function variantIdFromChunkId(chunkId: string): string | null {
  const parts = chunkId.split(":");
  return parts.length === 3 ? parts[1] : null;
}
