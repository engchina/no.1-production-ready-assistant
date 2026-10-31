import type { ReactNode } from "react";
import {
  INFORMATION_TABLE_VISIBLE_ROWS,
  ListSkeleton,
  TimedLoadingState,
  type ProcessingActivityIcon,
} from "@production-ready/ui";

/**
 * 履歴の行の一覧の読み込み中（初回）。フィードバック管理のフィードバック履歴（一覧 + 詳細の一覧の列。#2283）が使う。
 * 行の部品（質問 → 日時・バッジの複数行）は、一覧の列の 1 列の表（`RowTitleButton`）に移した。
 */
export function HistoryRowListSkeleton({
  label,
  operationKey,
  testId,
  skeletonTestId,
  activityIcon,
  children,
}: {
  label: string;
  operationKey: string;
  testId: string;
  skeletonTestId: string;
  activityIcon?: ProcessingActivityIcon;
  /** Skeleton の前に置く要素（見出しの sr-only など）。 */
  children?: ReactNode;
}) {
  return (
    <TimedLoadingState
      label={label}
      operationKey={operationKey}
      placement="panel"
      className="content-start"
      testId={testId}
      framed={false}
      activityIcon={activityIcon}
    >
      {children}
      <ListSkeleton rows={INFORMATION_TABLE_VISIBLE_ROWS} rowClassName="h-20" testId={skeletonTestId} />
    </TimedLoadingState>
  );
}
