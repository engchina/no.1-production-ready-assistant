import { StatusBadge as UiStatusBadge, type StatusVariant } from "@production-ready/ui";
import { Clock3, Hourglass, type LucideIcon } from "lucide-react";

import { t } from "@/lib/i18n";
import type { FileStatus, IngestionJobPhase } from "@/lib/api";
import { queueWaitLabel } from "@/lib/ingestion-queue";

export type { FileStatus };

/**
 * RAG のファイル処理状態 → 共有バッジの variant / アイコンの対応表。
 * 処理中（機械が進めている）は info + 砂時計、人の確認待ち（REVIEW）は warning にする。
 * 非推奨の pending（warning の別名）は使わない。
 */
const STATUS_BADGE: Record<FileStatus, { variant: StatusVariant; icon?: LucideIcon }> = {
  UPLOADED: { variant: "neutral" },
  PREPROCESSING: { variant: "info", icon: Hourglass },
  PREPROCESSED: { variant: "info" },
  INGESTING: { variant: "info", icon: Hourglass },
  REVIEW: { variant: "warning" },
  CHUNKING: { variant: "info", icon: Hourglass },
  CHUNKED: { variant: "info" },
  INDEXING: { variant: "info", icon: Hourglass },
  INDEXED: { variant: "success" },
  ERROR: { variant: "danger" },
};

/**
 * ステータスバッジ（ファイル処理状態をアイコン + 日本語で表示）。
 *
 * `queuedPhase` は、取込の job が worker の空きを待っている工程（#2026）。待っている間はレシピの状態
 * （例: 抽出の後の REVIEW）を「確認待ち」と読まず、処理中の側の「〈工程〉の順番待ち」（info + 時計）にする。
 */
export function StatusBadge({
  status,
  queuedPhase,
}: {
  status: FileStatus;
  queuedPhase?: IngestionJobPhase | null;
}) {
  if (queuedPhase) {
    return <UiStatusBadge variant="info" icon={Clock3} label={queueWaitLabel(queuedPhase)} />;
  }
  const { variant, icon } = STATUS_BADGE[status];
  return <UiStatusBadge variant={variant} icon={icon ?? true} label={t(`status.${status}`)} />;
}
