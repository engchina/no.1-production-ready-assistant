import type { KnowledgeBaseStatus } from "./api";

/** 選択済み KB の解決に使う最小の形（一覧の要約・検索・回答プロファイルの参照のどちらでもよい）。 */
export interface KnowledgeBaseRefLike {
  id: string;
  name: string;
  status: KnowledgeBaseStatus;
  document_count?: number;
}

export interface ResolvedKnowledgeBaseRef {
  id: string;
  name: string;
  status: KnowledgeBaseStatus;
  document_count: number;
  /** ID が見つからない（存在しない・利用者の範囲外）。 */
  missing: boolean;
}

export interface KnowledgeBaseSelectionHealth {
  /** 選択順の解決結果。まだ判定できない ID は含めない。 */
  items: ResolvedKnowledgeBaseRef[];
  archived: ResolvedKnowledgeBaseRef[];
  missing: ResolvedKnowledgeBaseRef[];
}

/**
 * 選択済みの KB ID を、ID で引いた結果（`found`。利用者の範囲内の最新）と、画面が既に持つ参照
 * （`known`。例: 検索・回答プロファイル詳細の tenant 内の参照 KB）から解決する（#302）。
 *
 * - `found` → `known` の順に探す。
 * - どちらにもなく、ID の検索が終わっている（`lookupSettled`）か `knownMissingIds` に含まれる
 *   ID は「見つからない」とする。検索中・失敗時は判定しない（誤って警告しない）。
 */
export function resolveKnowledgeBaseSelection({
  ids,
  found = [],
  known = [],
  knownMissingIds = [],
  lookupSettled,
  missingName,
}: {
  ids: readonly string[];
  found?: readonly KnowledgeBaseRefLike[];
  known?: readonly KnowledgeBaseRefLike[];
  knownMissingIds?: readonly string[];
  lookupSettled: boolean;
  /** 見つからない KB のチップに出す名前。 */
  missingName: (id: string) => string;
}): KnowledgeBaseSelectionHealth {
  const foundById = new Map(found.map((item) => [item.id, item]));
  const knownById = new Map(known.map((item) => [item.id, item]));
  const knownMissing = new Set(knownMissingIds);
  const items: ResolvedKnowledgeBaseRef[] = [];
  for (const id of new Set(ids)) {
    const ref = foundById.get(id) ?? knownById.get(id);
    if (ref) {
      items.push({
        id,
        name: ref.name,
        status: ref.status,
        document_count: ref.document_count ?? 0,
        missing: false,
      });
    } else if (lookupSettled || knownMissing.has(id)) {
      items.push({ id, name: missingName(id), status: "ACTIVE", document_count: 0, missing: true });
    }
  }
  return {
    items,
    archived: items.filter((item) => !item.missing && item.status === "ARCHIVED"),
    missing: items.filter((item) => item.missing),
  };
}
