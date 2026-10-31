import type { SearchableSelectOption } from "@production-ready/ui";

import type { DocumentSummary } from "@/lib/api";

/**
 * 「この文書を置き換えた新しい版」の候補（#1248）。
 *
 * - 文書自身と、この文書に置き換えられた文書（選ぶと A→B→A の循環になる）は候補に出さない。
 *   それより長い循環は backend が拒む（422）。
 * - 候補自身が旧版のときは、名前の後ろに印を出す（色だけに頼らず文字で伝える）。
 * - 保存済みの新しい版が検索の結果に無いときも、ボタンに名前を出すため `selected` を返す。
 */
export function documentVersionOptions(
  documents: readonly DocumentSummary[],
  documentId: string,
  labels: { superseded: string },
): SearchableSelectOption[] {
  return documents
    .filter(
      (item) => item.id !== documentId && item.superseded_by_document_id !== documentId,
    )
    .map((item) => ({
      value: item.id,
      label: item.file_name,
      badge: item.superseded_by_document_id ? labels.superseded : undefined,
    }));
}

/** 保存済みの新しい版の候補（名前が見えない文書は ID で出す）。 */
export function savedVersionOption(
  supersededById: string | null | undefined,
  supersededByName: string | null | undefined,
  unknownLabel: (id: string) => string,
): SearchableSelectOption | null {
  if (!supersededById) return null;
  return { value: supersededById, label: supersededByName || unknownLabel(supersededById) };
}
