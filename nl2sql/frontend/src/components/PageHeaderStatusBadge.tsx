import { StatusBadge, type StatusVariant } from "@production-ready/ui";

/**
 * ヘッダー横の短いページ状態。頻繁に変わる件数などを見た目に出しつつ、
 * screen reader には安定した状態文言だけを live region で通知する。
 */
export function PageHeaderStatusBadge({
  variant,
  label,
  announcementLabel = label,
  testId,
}: {
  variant: StatusVariant;
  label: string;
  /** 頻繁に変わる件数を除外し、screen reader へ通知する安定した状態文言。 */
  announcementLabel?: string;
  testId?: string;
}) {
  return (
    <span
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-page-header-status="true"
      data-testid={testId}
    >
      <span aria-hidden="true">
        <StatusBadge variant={variant} label={label} />
      </span>
      <span className="sr-only">{announcementLabel}</span>
    </span>
  );
}
