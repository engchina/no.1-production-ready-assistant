import { StatusBadge } from "@production-ready/ui";

import { type KnowledgeBaseStatus } from "@/lib/api";
import { t } from "@/lib/i18n";

/** ナレッジベース状態の日本語ラベル。 */
export function knowledgeBaseStatusLabel(status: KnowledgeBaseStatus) {
  return status === "ACTIVE"
    ? t("knowledgeBases.status.ACTIVE")
    : t("knowledgeBases.status.ARCHIVED");
}

/**
 * ナレッジベース状態のバッジ(一覧・詳細で共有)。
 * 状態は色だけで表さないため、共有の StatusBadge(アイコン + ラベル)で出す。検索・回答プロファイルの
 * 状態と同じ対応(有効 = success / アーカイブ済み = neutral)にする(#282)。
 */
export function KnowledgeBaseStatusPill({ status }: { status: KnowledgeBaseStatus }) {
  return (
    <StatusBadge
      variant={status === "ARCHIVED" ? "neutral" : "success"}
      label={knowledgeBaseStatusLabel(status)}
    />
  );
}
