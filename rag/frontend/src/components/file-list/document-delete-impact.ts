import type { DocumentDeleteImpact } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 確認ダイアログに名前を並べるナレッジベースの上限。残りは件数で示す。 */
const KNOWLEDGE_BASE_NAME_LIMIT = 3;

export interface DeleteImpactSummary {
  /** 重複文書から参照されている正本の件数。 */
  sourceCount: number;
  /** 正本を参照している重複文書の件数（合計）。 */
  duplicateCount: number;
  /** 検索対象から内容が消えるナレッジベースの名前（重複を除く）。 */
  knowledgeBaseNames: string[];
}

/** 削除する文書ごとの影響を、確認ダイアログに出す 1 つの要約にまとめる（#303）。 */
export function summarizeDeleteImpact(impacts: readonly DocumentDeleteImpact[]): DeleteImpactSummary {
  const names = new Map<string, string>();
  let sourceCount = 0;
  let duplicateCount = 0;
  for (const impact of impacts) {
    if (impact.duplicate_count <= 0) continue;
    sourceCount += 1;
    duplicateCount += impact.duplicate_count;
    for (const knowledgeBase of impact.knowledge_bases) {
      if (!names.has(knowledgeBase.id)) names.set(knowledgeBase.id, knowledgeBase.name);
    }
  }
  return { sourceCount, duplicateCount, knowledgeBaseNames: [...names.values()] };
}

function knowledgeBaseList(names: readonly string[]): string {
  if (names.length <= KNOWLEDGE_BASE_NAME_LIMIT) return names.join("、");
  return t("fileList.delete.impact.moreKnowledgeBases", {
    names: names.slice(0, KNOWLEDGE_BASE_NAME_LIMIT).join("、"),
    count: names.length - KNOWLEDGE_BASE_NAME_LIMIT,
  });
}

/**
 * 正本の削除で、重複文書の所属 KB の検索対象から内容が消えることを説明する文。
 * 影響が無ければ null（既存の確認文だけを出す）。
 */
export function deleteImpactDescription(
  summary: DeleteImpactSummary,
  { bulk }: { bulk: boolean }
): string | null {
  if (summary.duplicateCount <= 0) return null;
  const params = {
    count: summary.duplicateCount,
    sources: summary.sourceCount,
    knowledgeBases: knowledgeBaseList(summary.knowledgeBaseNames),
  };
  const hasNames = summary.knowledgeBaseNames.length > 0;
  const impact = bulk
    ? t(
        hasNames
          ? "fileList.bulkDelete.impact.duplicates"
          : "fileList.bulkDelete.impact.duplicatesWithoutKnowledgeBase",
        params
      )
    : t(
        hasNames
          ? "fileList.delete.impact.duplicates"
          : "fileList.delete.impact.duplicatesWithoutKnowledgeBase",
        params
      );
  return `${impact}${t("fileList.delete.impact.restoreHint")}`;
}

/** 確認ダイアログの本文。影響があれば既存の確認文の後ろに続ける。 */
export function deleteConfirmDescription(
  base: string,
  impacts: readonly DocumentDeleteImpact[],
  options: { bulk: boolean }
): string {
  const impact = deleteImpactDescription(summarizeDeleteImpact(impacts), options);
  return impact ? `${base}${impact}` : base;
}
