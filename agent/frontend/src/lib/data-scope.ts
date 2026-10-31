import type { ToolCallDataScope } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 実行の step に出す、データの範囲を当てた記録の文（#1378）。範囲が効かない呼び出しは空。
 * 使ったプロファイルと「補完 / 置き換え / 範囲内 / 拒否 / 絞り込み」、使わなかったナレッジベースを文字で出す。
 */
export function dataScopeNotes(scope: ToolCallDataScope | null | undefined): string[] {
  if (!scope) return [];
  const notes: string[] = [];
  const profile = scope.profile_id ?? "";
  const requested = scope.requested_profile_id ?? "";
  switch (scope.action) {
    case "filled":
      notes.push(t("run.dataScope.filled", { profile }));
      break;
    case "overridden":
      notes.push(t("run.dataScope.overridden", { profile, requested }));
      break;
    case "kept":
      notes.push(t("run.dataScope.kept", { profile }));
      break;
    case "rejected":
      notes.push(t("run.dataScope.rejected", { requested }));
      break;
    case "filtered":
      notes.push(t("run.dataScope.filtered", { profiles: (scope.allowed_profile_ids ?? []).join("、") }));
      break;
    default:
      break;
  }
  if (scope.ignored_knowledge_base_ids?.length) {
    notes.push(t("run.dataScope.ignoredKnowledgeBases", { ids: scope.ignored_knowledge_base_ids.join("、") }));
  }
  return notes;
}
