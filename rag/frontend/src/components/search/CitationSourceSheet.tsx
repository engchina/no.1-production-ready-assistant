import type { ReactNode } from "react";
import { EvidenceSourceSheet, evidencePreviewFocus } from "@production-ready/ui";

import { DocumentPreview } from "@/components/documents/DocumentPreview";
import type { CitationPosition } from "@/lib/answer-text";
import type { RetrievedChunk } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useDocument, useDocumentRecipes } from "@/lib/queries";
import { firstMetadataToken } from "@/lib/table-cell-focus";

import { citationEvidenceItem, EVIDENCE_LABELS } from "./citation-evidence";

/**
 * 根拠 1 件を右のサイドパネルで開く（#2360。Agent のチャットと同じ共通の `EvidenceSourceSheet`）。上部に根拠の
 * 要約（番号・文書名・引用の印・経路・スコア・抜粋・頁と章節）、その下に原文のプレビュー（該当箇所を強調する。
 * #349 / #442）を出す。根拠のカードの「根拠を表示」・回答本文の根拠の行（#657）から開く。
 * `chunk` は閉じる間も前の根拠を渡す（`useEvidenceSource` の `shown`）。
 *
 * 本文の根拠の行が指す箇所を確定できないとき（`position` が `unresolved`。#2710）は、要約の上に「位置は確定して
 * いない」ことと候補の数を出し、原文のプレビューは頁へ移るだけで箇所を強調しない（別の箇所を正しい位置に見せない）。
 */
export function CitationSourceSheet({
  chunk,
  index,
  open,
  onClose,
  kind = "answer",
  position = { kind: "exact" },
  empty,
  scores,
}: {
  chunk: RetrievedChunk | null;
  /** 根拠の位置を取得中、または現在は閲覧できない場合の状態。 */
  empty?: ReactNode;
  /** 位置だけを ID で読んだ場合は、保存時のスコアだけを表示する。 */
  scores?: { score: number | null; rerankScore: number | null };
  /** 根拠の番号（0 始まり）。 */
  index: number;
  open: boolean;
  onClose: () => void;
  /** 回答の根拠（既定）か、回答を作らない検索の結果の箇所か（見出しが変わる）。 */
  kind?: "answer" | "search";
  /** 本文の根拠の行から開いたときの、箇所の位置の決まり方（#2710）。既定は確定（根拠の一覧から開く）。 */
  position?: CitationPosition;
}) {
  const unresolved = position.kind === "unresolved" ? position : null;
  return (
    <EvidenceSourceSheet
      open={open}
      onClose={onClose}
      item={chunk ? { ...citationEvidenceItem(chunk), ...scores } : null}
      index={index}
      labels={EVIDENCE_LABELS}
      kind={kind}
      notice={
        unresolved
          ? {
              title: t("search.citationPosition.unresolvedTitle"),
              body: t(
                unresolved.reason === "legacy"
                  ? "search.citationPosition.legacyBody"
                  : "search.citationPosition.candidatesBody",
                { count: unresolved.candidates.length },
              ),
            }
          : undefined
      }
    >
      {chunk ? <CitationSourcePreview chunk={chunk} open={open} highlight={!unresolved} /> : empty}
    </EvidenceSourceSheet>
  );
}

/**
 * 原文のプレビュー（Office 原本は変換済みの PDF。要素の表示領域を要素ごとに強調する。#349。テキストの原本は抜粋の
 * 箇所を強調する。#2512）。強調の計算は Agent の根拠のパネルと同じ共通の `evidencePreviewFocus`（#2389）。
 */
function CitationSourcePreview({
  chunk,
  open,
  highlight,
}: {
  chunk: RetrievedChunk;
  open: boolean;
  /** 箇所を強調するか。位置の決まらない根拠の行から開いたときは頁へ移るだけにする（#2710）。 */
  highlight: boolean;
}) {
  const previewFileName = chunk.file_name ?? chunk.document_id;
  const recipeId = firstMetadataToken(chunk.metadata.recipe_id);
  // パネルを開いたときだけ文書詳細を取得し、Office 原本でも変換済 PDF を表示する。
  const previewDoc = useDocument(open ? chunk.document_id : null);
  const previewRecipes = useDocumentRecipes(open ? chunk.document_id : null);
  const previewRecipe = previewRecipes.data?.find((recipe) => recipe.recipe_id === recipeId);
  const focus = evidencePreviewFocus(chunk.metadata);
  return (
    <DocumentPreview
      documentId={chunk.document_id}
      recipeId={recipeId}
      fileName={previewFileName}
      preparedArtifact={
        recipeId
          ? (previewRecipe?.preprocess_artifact ?? null)
          : (previewDoc.data?.preprocess_artifact ?? null)
      }
      focusPage={focus.focusPage}
      focusBbox={highlight ? focus.focusBbox : null}
      focusBboxMode={focus.focusBboxMode}
      focusBboxUnit={focus.focusBboxUnit}
      focusPageSize={focus.focusPageSize}
      highlights={highlight ? focus.highlights : []}
      // テキストの原本は、根拠の抜粋の箇所へ移って強調する（#2512）。
      focusText={highlight ? chunk.text : null}
      className="min-h-0 flex-1"
    />
  );
}
